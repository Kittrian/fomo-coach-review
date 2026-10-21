"""Redact API keys from log lines (v1.17.1).

httpx logs every request at INFO as 'HTTP Request: POST <full url> ...'. Keyed RPC providers (e.g. Helius) put the
key in the query string (?api-key=...), so without this the key would be written to logs/coach-launchd.err.log on
every call. The filter rewrites `api-key=<v>` / `api_key=<v>` / `apikey=<v>` / `token=<v>` query values to <redacted>.
"""
from __future__ import annotations

import logging
import re

_PAT = re.compile(r"((?:api[-_]?key|token)=)[^&\s\"'<>]+", re.IGNORECASE)


def redact(text: str) -> str:
    return _PAT.sub(r"\1<redacted>", text)


class RedactKeysFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:  # always keeps the record, only rewrites it
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        red = redact(msg)
        if red != msg:
            record.msg, record.args = red, ()
        return True


_FILTER = RedactKeysFilter()


def install() -> None:
    """Attach to the httpx/websockets loggers and to every root handler (covers propagated records). Idempotent."""
    for name in ("httpx", "httpcore", "websockets", "websockets.client"):
        lg = logging.getLogger(name)
        if _FILTER not in lg.filters:
            lg.addFilter(_FILTER)
    for h in logging.getLogger().handlers:
        if _FILTER not in h.filters:
            h.addFilter(_FILTER)
