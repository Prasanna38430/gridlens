from __future__ import annotations

import logging

PLACEHOLDER = "<redacted>"


class RedactingFilter(logging.Filter):
    """Strip known secrets out of log records before anything writes them.

    Muting a chatty logger is the first line and it only holds until someone
    adds another entry point. This one does not care which logger produced the
    record, which is the property that makes it worth having.
    """

    def __init__(self, *secrets: str) -> None:
        super().__init__()
        self._secrets = tuple(s for s in secrets if s)

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True

        message = record.getMessage()
        if not any(secret in message for secret in self._secrets):
            return True

        for secret in self._secrets:
            message = message.replace(secret, PLACEHOLDER)

        # collapse msg and args into the redacted text, otherwise a handler
        # formats the original arguments again and undoes this
        record.msg = message
        record.args = ()
        return True


def install(*secrets: str) -> RedactingFilter:
    """Attach a redacting filter to every handler on the root logger."""
    redactor = RedactingFilter(*secrets)
    root = logging.getLogger()
    for handler in root.handlers:
        handler.addFilter(redactor)
    if not root.handlers:
        root.addFilter(redactor)
    return redactor
