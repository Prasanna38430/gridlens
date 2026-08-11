from __future__ import annotations

import random
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

import httpx

from gridlens.ingest.ratelimit import TokenBucket

BASE_URL: Final = "https://web-api.tp.entsoe.eu/api"

# the platform allows 400 requests per minute and counts them against the
# token rather than the source address, so this ceiling is shared with anything
# else using the same token. 5/s sustained leaves room for that.
DEFAULT_RATE_PER_SECOND: Final = 5.0
DEFAULT_BURST: Final = 10

# going over the limit bans the token rather than refusing one request, and the
# ban runs about ten minutes.
RATE_LIMIT_COOLDOWN_SECONDS: Final = 600.0

_NO_DATA_PREFIX: Final = "No matching data found"


class EntsoeError(Exception):
    """Base for everything this client raises."""


class EntsoeRejected(EntsoeError):
    """The platform answered with an acknowledgement instead of data."""

    def __init__(self, code: str, text: str, status_code: int) -> None:
        super().__init__(
            f"entsoe rejected the request (http {status_code}): {code} {text}"
        )
        self.code = code
        self.text = text
        self.status_code = status_code


class EntsoeNoData(EntsoeRejected):
    """Nothing published for that window. A mistyped zone looks identical."""


class EntsoeAuthError(EntsoeRejected):
    """Token is wrong, or api access was never granted for it."""


class RateLimited(EntsoeError):
    def __init__(self, retry_after: float) -> None:
        super().__init__(f"rate limited, do not retry for {retry_after:.0f}s")
        self.retry_after = retry_after


class EntsoeUnavailable(EntsoeError):
    """Transient failures did not clear within the attempt budget."""


@dataclass(frozen=True)
class FetchResult:
    body: bytes
    fetched_at: datetime
    status_code: int


def format_period(moment: datetime) -> str:
    """Render an aware datetime as the UTC yyyyMMddHHmm the api expects."""
    if moment.tzinfo is None:
        raise ValueError(
            "naive datetime. pass an aware one so the utc conversion is explicit"
        )
    return moment.astimezone(UTC).strftime("%Y%m%d%H%M")


def _is_acknowledgement(body: bytes) -> bool:
    # cheap scan of the prologue rather than parsing 190 KB to learn the root
    return b"Acknowledgement_MarketDocument" in body[:500]


def _reason(body: bytes) -> tuple[str, str]:
    root = ET.fromstring(body)
    namespace = root.tag.partition("}")[0].lstrip("{") if "}" in root.tag else ""
    ns = {"n": namespace} if namespace else {}
    prefix = "n:" if namespace else ""
    code = root.findtext(f".//{prefix}Reason/{prefix}code", default="", namespaces=ns)
    text = root.findtext(f".//{prefix}Reason/{prefix}text", default="", namespaces=ns)
    return code.strip(), text.strip()


class EntsoeClient:
    def __init__(
        self,
        token: str,
        *,
        client: httpx.Client | None = None,
        bucket: TokenBucket | None = None,
        max_attempts: int = 4,
        backoff_base: float = 0.5,
        backoff_cap: float = 8.0,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        if not token:
            raise ValueError("token is required")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")

        self._token = token
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=10.0)
        )
        self._bucket = bucket or TokenBucket(DEFAULT_RATE_PER_SECOND, DEFAULT_BURST)
        self._max_attempts = max_attempts
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap
        self._sleep = sleep
        self._rng = rng or random.Random()

    def __repr__(self) -> str:
        # the token is a query parameter, so anything that prints this object
        # anywhere near a log is a leak
        return f"EntsoeClient(token=<redacted {len(self._token)} chars>)"

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> EntsoeClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def actual_generation_per_type(
        self, zone: str, start: datetime, end: datetime
    ) -> FetchResult:
        return self.fetch(
            {
                "documentType": "A75",
                "processType": "A16",
                "in_Domain": zone,
                "periodStart": format_period(start),
                "periodEnd": format_period(end),
            }
        )

    def fetch(self, params: Mapping[str, str]) -> FetchResult:
        query = {**params, "securityToken": self._token}
        last: Exception | None = None

        for attempt in range(1, self._max_attempts + 1):
            self._bucket.acquire()
            try:
                response = self._client.get(BASE_URL, params=query)
            except httpx.TransportError as exc:
                last = exc
            else:
                if response.status_code == 429:
                    # backing off and retrying here extends the ban rather than
                    # riding it out. stop, and let the scheduler come back.
                    raise RateLimited(_retry_after(response))
                if response.status_code < 500:
                    return self._interpret(response)
                last = EntsoeUnavailable(f"http {response.status_code}")

            if attempt < self._max_attempts:
                self._sleep(self._backoff(attempt))

        raise EntsoeUnavailable(
            f"giving up after {self._max_attempts} attempts"
        ) from last

    def _interpret(self, response: httpx.Response) -> FetchResult:
        body = response.content
        # our clock, not the createdDateTime in the payload. known_at means
        # when we learned it, and their clock is not ours to trust.
        fetched_at = datetime.now(UTC)

        if _is_acknowledgement(body):
            code, text = _reason(body)
            if response.status_code == 401:
                raise EntsoeAuthError(code, text, response.status_code)
            if text.startswith(_NO_DATA_PREFIX):
                raise EntsoeNoData(code, text, response.status_code)
            raise EntsoeRejected(code, text, response.status_code)

        if response.status_code >= 400:
            raise EntsoeRejected("", "unexpected error body", response.status_code)

        return FetchResult(
            body=body, fetched_at=fetched_at, status_code=response.status_code
        )

    def _backoff(self, attempt: int) -> float:
        ceiling = min(self._backoff_cap, self._backoff_base * 2 ** (attempt - 1))
        # full jitter. fixed backoff just moves the thundering herd along by a
        # constant and every retrying caller still wakes together.
        return self._rng.uniform(0.0, ceiling)


def _retry_after(response: httpx.Response) -> float:
    header = response.headers.get("Retry-After")
    if header:
        try:
            return float(header)
        except ValueError:
            pass
    return RATE_LIMIT_COOLDOWN_SECONDS
