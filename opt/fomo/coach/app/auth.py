from __future__ import annotations

from fastapi import Header, HTTPException

from app.config import settings


def require_ingest_secret(x_ingest_secret: str | None = Header(default=None)) -> None:
    expected = settings.ingest_secret
    if not expected:
        return  # open when unset (local/demo)
    if not x_ingest_secret or x_ingest_secret != expected:
        raise HTTPException(status_code=401, detail="invalid_ingest_secret")
