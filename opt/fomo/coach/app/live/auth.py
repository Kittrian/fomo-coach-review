"""Local auth for /api/exec/swap and /api/live/*: loopback-only + token from a 0600 file (never printed)."""
from __future__ import annotations

import hmac
import os
import secrets
import stat
from pathlib import Path
from typing import Optional

from fastapi import Header, HTTPException, Request

from app.live.config import config

LOOPBACK = {"127.0.0.1", "::1", "localhost"}
_cache: dict = {}


def ensure_token() -> str:
    """Create the token file (dir 0700, file 0600) if missing; tighten perms if loose. Returns it in memory."""
    p = Path(config()["token_path"])
    if _cache.get("path") == str(p) and _cache.get("token"):
        return _cache["token"]
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(p.parent, 0o700)
    except OSError:
        pass
    if not p.exists():
        fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(secrets.token_urlsafe(32))
    if stat.S_IMODE(p.stat().st_mode) & 0o077:
        os.chmod(p, 0o600)
    tok = p.read_text(encoding="utf-8").strip()
    _cache.update(path=str(p), token=tok)
    return tok


def token_file_ok() -> bool:
    p = Path(config()["token_path"])
    return p.is_file() and not (stat.S_IMODE(p.stat().st_mode) & 0o077)


def require_localhost(request: Request) -> None:
    host = (request.client.host if request.client else "") or ""
    if host not in LOOPBACK:
        raise HTTPException(status_code=403, detail="live/exec endpoints are loopback-only")


def require_live_token(x_live_token: Optional[str] = Header(default=None)) -> None:
    want = ensure_token()
    if not x_live_token or not hmac.compare_digest(x_live_token.strip(), want):
        raise HTTPException(status_code=401, detail="missing/invalid X-Live-Token (see LIVE_API_TOKEN_PATH)")
