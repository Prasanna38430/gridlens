from __future__ import annotations

import base64
import random
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Final

import httpx

from gridlens.ingest.entsoe import FetchResult
from gridlens.ingest.errors import IngestError, RateLimited, SourceUnavailable
from gridlens.ingest.ratelimit import TokenBucket

TOKEN_URL: Final = "https://digital.iservices.rte-france.com/token/oauth/"
BASE_URL: Final = "https://digital.iservices.rte-france.com/open_api"
ACTUAL_GENERATION: Final = (
    "/actual_generation/v1/actual_generations_per_production_type"
)

# RTE asks for no more than one call an hour on this resource and caps the
# account at 50,000 a month. Neither is enforced per second, so the bucket here
# is only there to stop a runaway loop, not to implement their policy.
DEFAULT_RATE_PER_SECOND: Final = 2.0
DEFAULT_BURST: Final = 5

# refresh this long before the token actually dies, so a call never starts with
# a token that expires mid-flight
TOKEN_SKEW_SECONDS: Final = 60.0

RATE_LIMIT_COOLDOWN_SECONDS: Final = 300.0

# no more than 155 days per call, per their user guide
MAX_WINDOW = timedelta(days=155)


class RteError(IngestError):
    """Base for everything this client raises."""


class RteAuthError(RteError):
    """Credentials rejected."""


class RteNoApplication(RteError):
    """403. The credentials are valid but no application is subscribed."""


class RteUnavailable(RteError, SourceUnavailable):
    """Transient failures did not clear within the attempt budget."""


class RteClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        client: httpx.Client | None = None,
        bucket: TokenBucket | None = None,
        max_attempts: int = 4,
        backoff_base: float = 0.5,
        backoff_cap: float = 8.0,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        rng: random.Random | None = None,
    ) -> None:
        if not client_id or not client_secret:
            raise ValueError("client_id and client_secret are required")

        self._basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=10.0)
        )
        self._bucket = bucket or TokenBucket(DEFAULT_RATE_PER_SECOND, DEFAULT_BURST)
        self._max_attempts = max_attempts
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap
        self._sleep = sleep
        self._now = now
        self._rng = rng or random.Random()

        self._token: str | None = None
        self._expires_at = datetime.min.replace(tzinfo=UTC)
        self.token_fetches = 0

    def __repr__(self) -> str:
        return "RteClient(credentials=<redacted>)"

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> RteClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def actual_generation_per_type(self, start: datetime, end: datetime) -> FetchResult:
        """Hourly generation by production type over a window.

        Unlike entsoe this accepts any offset, so the local settlement day goes
        in as it stands. We still convert, so both sources send the same
        instants and a mismatch is a bug rather than a timezone opinion.
        """
        for label, moment in (("start", start), ("end", end)):
            if moment.tzinfo is None:
                raise ValueError(f"{label} is naive, pass an aware datetime")
        if end <= start:
            raise ValueError("end must be after start")
        if end - start > MAX_WINDOW:
            raise ValueError(f"window is longer than the {MAX_WINDOW.days} day limit")

        return self.fetch(
            ACTUAL_GENERATION,
            {
                "start_date": start.astimezone(UTC).isoformat(),
                "end_date": end.astimezone(UTC).isoformat(),
            },
        )

    def fetch(self, path: str, params: Mapping[str, str]) -> FetchResult:
        last: Exception | None = None
        refreshed = False

        for attempt in range(1, self._max_attempts + 1):
            self._bucket.acquire()
            try:
                response = self._client.get(
                    BASE_URL + path,
                    params=dict(params),
                    headers={"Authorization": f"Bearer {self._bearer()}"},
                )
            except httpx.TransportError as exc:
                last = exc
            else:
                if response.status_code == 401 and not refreshed:
                    # the token may have died between the expiry check and the
                    # call. one forced refresh, then believe the 401.
                    self._token = None
                    refreshed = True
                    continue
                if response.status_code == 401:
                    raise RteAuthError("credentials rejected")
                if response.status_code == 403:
                    raise RteNoApplication(
                        "no application subscribed to this api on the data portal"
                    )
                if response.status_code == 429:
                    raise RateLimited(_retry_after(response))
                if response.status_code < 500:
                    if response.status_code >= 400:
                        raise RteError(f"http {response.status_code}")
                    return FetchResult(
                        body=response.content,
                        fetched_at=self._now(),
                        status_code=response.status_code,
                    )
                last = RteUnavailable(f"http {response.status_code}")

            if attempt < self._max_attempts:
                self._sleep(self._backoff(attempt))

        raise RteUnavailable(f"giving up after {self._max_attempts} attempts") from last

    def _bearer(self) -> str:
        if self._token and self._now() < self._expires_at:
            return self._token

        response = self._client.post(
            TOKEN_URL,
            headers={
                "Authorization": f"Basic {self._basic}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            content="grant_type=client_credentials",
        )
        if response.status_code in (400, 401):
            raise RteAuthError("credentials rejected by the token endpoint")
        if response.status_code != 200:
            raise RteUnavailable(f"token endpoint returned http {response.status_code}")

        payload = response.json()
        self._token = str(payload["access_token"])
        lifetime = float(payload.get("expires_in", 3600))
        self._expires_at = self._now() + timedelta(
            seconds=max(0.0, lifetime - TOKEN_SKEW_SECONDS)
        )
        self.token_fetches += 1
        return self._token

    def _backoff(self, attempt: int) -> float:
        ceiling = min(self._backoff_cap, self._backoff_base * 2 ** (attempt - 1))
        return self._rng.uniform(0.0, ceiling)


def _retry_after(response: httpx.Response) -> float:
    # RTE documents this header. entsoe does not send one, which is why the two
    # clients disagree about the fallback.
    header = response.headers.get("Retry-After")
    if header:
        try:
            return float(header)
        except ValueError:
            pass
    return RATE_LIMIT_COOLDOWN_SECONDS
