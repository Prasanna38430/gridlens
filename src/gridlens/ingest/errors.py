from __future__ import annotations


class IngestError(Exception):
    """Base for anything an ingestion client raises."""


class RateLimited(IngestError):
    """Stop calling. retry_after is how long, in seconds."""

    def __init__(self, retry_after: float) -> None:
        super().__init__(f"rate limited, do not retry for {retry_after:.0f}s")
        self.retry_after = retry_after


class SourceUnavailable(IngestError):
    """Transient failures did not clear within the attempt budget."""
