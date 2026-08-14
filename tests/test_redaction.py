from __future__ import annotations

import logging

import httpx
import pytest

from gridlens.ingest import redaction
from gridlens.ingest.entsoe import EntsoeClient
from gridlens.ingest.ratelimit import TokenBucket

TOKEN = "ebc09f7f-0000-0000-0000-000000000000"


def client(records: list[logging.LogRecord]) -> EntsoeClient:
    def handler(request: httpx.Request) -> httpx.Response:
        # what httpx itself logs at INFO: the whole url, token included
        logging.getLogger("httpx").info(
            'HTTP Request: GET %s "HTTP/1.1 200 "', request.url
        )
        return httpx.Response(200, content=b"<GL_MarketDocument/>")

    return EntsoeClient(
        TOKEN,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        bucket=TokenBucket(1000.0, 10_000, clock=lambda: 0.0, sleep=lambda _: None),
        sleep=lambda _: None,
    )


class Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@pytest.fixture
def root_capture():
    root = logging.getLogger()
    capture = Capture()
    previous, root.handlers = root.handlers, [capture]
    level, root.level = root.level, logging.DEBUG
    logging.getLogger("httpx").setLevel(logging.DEBUG)
    try:
        yield capture
    finally:
        root.handlers = previous
        root.level = level


def test_without_the_filter_the_token_reaches_the_log(root_capture):
    # this is the leak as it actually happened on the first cloud deploy
    client(root_capture.messages).fetch({"documentType": "A75"})
    assert any(TOKEN in m for m in root_capture.messages)


def test_the_filter_strips_it(root_capture):
    redaction.install(TOKEN)
    client(root_capture.messages).fetch({"documentType": "A75"})

    assert root_capture.messages
    assert not any(TOKEN in m for m in root_capture.messages)
    assert any(redaction.PLACEHOLDER in m for m in root_capture.messages)


def test_records_without_the_secret_are_untouched(root_capture):
    redaction.install(TOKEN)
    logging.getLogger("gridlens").warning("fetching zone=%s", "FR")
    assert "fetching zone=FR" in root_capture.messages


def test_an_empty_secret_is_ignored():
    assert redaction.RedactingFilter("").filter(
        logging.LogRecord("x", logging.INFO, "f", 1, "hello", None, None)
    )
