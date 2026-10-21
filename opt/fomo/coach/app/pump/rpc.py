"""Minimal async Solana JSON-RPC (read-only). Public RPC by default; SOLANA_RPC_URL overrides.

Never logs the URL (it may embed an API key).
"""
from __future__ import annotations

import base64
import itertools
import os
from typing import Any, Dict, List, Optional

import httpx

PUBLIC_RPC = "https://api.mainnet-beta.solana.com"
_ids = itertools.count(1)


_MAIN_HOST = "://mainnet.helius-rpc.com"
_GK_HOST = "://beta.helius-rpc.com"
_GK: Dict[str, Any] = {"fail": 0, "until": 0.0, "fallbacks": 0, "trips": 0}


def _gk_on() -> bool:
    """v1.41 Gatekeeper: RPC_GATEKEEPER=true routes Helius mainnet traffic via beta.helius-rpc.com (no Cloudflare).
    3 gateway failures in a row → 120 s back on mainnet, then retry Gatekeeper."""
    import time as _t
    return os.getenv("RPC_GATEKEEPER", "").strip().lower() in {"1", "true", "yes", "on"} and _t.time() >= _GK["until"]


def gk_url(u: str) -> str:
    return u.replace(_MAIN_HOST, _GK_HOST, 1) if (u and _MAIN_HOST in u and _gk_on()) else u


def gk_fallback(u: str) -> Optional[str]:
    return u.replace(_GK_HOST, _MAIN_HOST, 1) if (u and _GK_HOST in u) else None


def gk_failed() -> None:
    import time as _t
    _GK["fail"] += 1
    _GK["fallbacks"] += 1
    if _GK["fail"] >= 3:
        _GK["fail"] = 0
        _GK["trips"] += 1
        _GK["until"] = _t.time() + 120.0


def gk_ok() -> None:
    _GK["fail"] = 0


def gk_status() -> Dict[str, Any]:
    import time as _t
    return {"enabled": os.getenv("RPC_GATEKEEPER", "").strip().lower() in {"1", "true", "yes", "on"},
            "active": _gk_on(), "fallbacks": _GK["fallbacks"], "trips": _GK["trips"],
            "cooldown_s": max(0.0, round(_GK["until"] - _t.time(), 1))}


def rpc_url() -> str:
    return gk_url(os.getenv("SOLANA_RPC_URL", "").strip() or PUBLIC_RPC)


def ws_url() -> str:
    explicit = os.getenv("SOLANA_WS_URL", "").strip()
    if explicit:
        return explicit
    u = rpc_url()
    if u.startswith("https://"):
        return "wss://" + u[len("https://"):]
    if u.startswith("http://"):
        return "ws://" + u[len("http://"):]
    return u


def rpc_label() -> str:
    """Safe-to-show label (no query string / key)."""
    u = rpc_url()
    host = u.split("://", 1)[-1].split("/", 1)[0].split("?", 1)[0]
    return "public:" + host if u == PUBLIC_RPC else "custom:" + host


class RpcError(RuntimeError):
    pass


async def call(method: str, params: List[Any], *, client: Optional[httpx.AsyncClient] = None,
               timeout: float = 4.0, url: Optional[str] = None) -> Any:
    """`url` overrides the endpoint (used by low-priority sources to stay off the CA lane's RPC budget)."""
    body = {"jsonrpc": "2.0", "id": next(_ids), "method": method, "params": params}
    own = client is None
    c = client or httpx.AsyncClient(timeout=timeout)
    try:
        target = gk_url(url) if url else rpc_url()
        fb = gk_fallback(target)
        try:
            r = await c.post(target, json=body, timeout=timeout)
            if fb and (r.status_code >= 500 or r.status_code in (403, 404, 429)):
                raise httpx.HTTPError(f"gatekeeper http {r.status_code}")
            if fb:
                gk_ok()
        except httpx.HTTPError:
            if not fb:
                raise
            gk_failed()
            r = await c.post(fb, json=body, timeout=timeout)
        if r.status_code != 200:
            raise RpcError(f"rpc http {r.status_code}")
        j = r.json()
        if "error" in j:
            raise RpcError(f"rpc error {str(j['error'])[:160]}")
        return j.get("result")
    finally:
        if own:
            await c.aclose()


def _decode_account(v: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not v:
        return None
    data = v.get("data")
    raw = base64.b64decode(data[0]) if isinstance(data, list) and data else b""
    return {"owner": v.get("owner"), "lamports": v.get("lamports"), "data": raw}


async def get_account(pubkey: str, **kw: Any) -> Optional[Dict[str, Any]]:
    res = await call("getAccountInfo", [pubkey, {"encoding": "base64", "commitment": "processed"}], **kw)
    return _decode_account((res or {}).get("value"))


async def get_multiple_accounts(pubkeys: List[str], **kw: Any) -> List[Optional[Dict[str, Any]]]:
    res = await call("getMultipleAccounts", [pubkeys, {"encoding": "base64", "commitment": "processed"}], **kw)
    return [_decode_account(v) for v in ((res or {}).get("value") or [])]
