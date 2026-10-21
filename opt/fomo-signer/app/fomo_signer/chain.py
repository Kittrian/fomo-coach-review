"""RPC reads the policy needs: lookup tables, pre-state, simulation post-state. Uses the signer's own RPC
(SIGNER_RPC_URL from its credential), never anything the coach sends. URLs embed keys: never logged."""
from __future__ import annotations

import asyncio
import base64
import os
import time
from typing import Any, Dict, List, Optional

import httpx

from . import policy

_alt_cache: Dict[str, tuple] = {}
_GK: Dict[str, float] = {"fail": 0, "until": 0.0}


def _gk_url(u: str) -> str:
    if os.getenv("SIGNER_RPC_GATEKEEPER", "").strip().lower() not in {"1", "true", "yes", "on"}:
        return ""
    if time.time() < _GK["until"] or "://mainnet.helius-rpc.com" not in u:
        return ""
    return u.replace("://mainnet.helius-rpc.com", "://beta.helius-rpc.com", 1)


class Chain:
    def __init__(self, url: str = "", client: Optional[httpx.AsyncClient] = None):
        self.url = url or os.getenv("SIGNER_RPC_URL", "")
        if not self.url:
            raise RuntimeError("SIGNER_RPC_URL not set")
        self.client = client or httpx.AsyncClient(   # v1.40f: keep the RPC TLS connection warm between signs
            timeout=10.0, limits=httpx.Limits(max_connections=16, max_keepalive_connections=8, keepalive_expiry=300))

    async def rpc(self, method: str, params: List[Any]) -> Any:
        # v1.25: up to SIGNER_RPC_429_RETRIES short retries on HTTP 429 only (rate limit = nothing evaluated yet);
        # still fail-closed after that.
        tries = 1 + max(0, int(os.getenv("SIGNER_RPC_429_RETRIES", "2")))
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        # v1.43: SIGNER_RPC_GATEKEEPER=true → same key via Helius Gatekeeper (beta host) first; any transport error
        # or 403/404/429/5xx → the original URL for this call, and 3 failures in a row → 120 s on the original.
        # Reads only (pre-state, simulation, ALTs); every policy check is unchanged and still fails closed.
        gk = _gk_url(self.url)
        if gk:
            try:
                r = await self.client.post(gk, json=body, timeout=3.0)
                if r.status_code == 200:
                    _GK["fail"] = 0
                    j = r.json()
                    if "error" not in j:
                        return j.get("result")
                raise RuntimeError("gk")
            except Exception:  # noqa: BLE001   never log the url (key)
                _GK["fail"] += 1
                if _GK["fail"] >= 3:
                    _GK["fail"], _GK["until"] = 0, time.time() + 120
        for i in range(tries):
            r = await self.client.post(self.url, json=body)
            if r.status_code != 429 or i == tries - 1:
                break
            await asyncio.sleep(0.06 * (i + 1))
        if r.status_code != 200:
            raise RuntimeError(f"{method} http {r.status_code}")
        j = r.json()
        if "error" in j:
            raise RuntimeError(f"{method} rpc error {str(j['error'].get('code'))}")
        return j.get("result")

    @staticmethod
    def _acct(v: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not v:
            return None
        data = v.get("data") or ["", "base64"]
        return {"lamports": int(v.get("lamports") or 0), "owner": v.get("owner"),
                "data": base64.b64decode(data[0]) if isinstance(data, list) else b""}

    async def accounts(self, addrs: List[str]) -> Dict[str, Optional[Dict[str, Any]]]:
        out: Dict[str, Optional[Dict[str, Any]]] = {}
        for i in range(0, len(addrs), 100):
            chunk = addrs[i:i + 100]
            res = await self.rpc("getMultipleAccounts", [chunk, {"encoding": "base64", "commitment": "processed"}])
            for a, v in zip(chunk, (res or {}).get("value") or []):
                out[a] = self._acct(v)
        return out

    async def alts(self, keys: List[str]) -> Dict[str, List[str]]:
        now = time.time()
        need = [k for k in keys if k not in _alt_cache or now - _alt_cache[k][0] > 600]
        if need:
            got = await self.accounts(need)
            for k in need:
                a = got.get(k)
                if a and a["data"]:
                    _alt_cache[k] = (now, policy.parse_alt(a["data"]))
        return {k: _alt_cache[k][1] for k in keys if k in _alt_cache}

    async def simulate(self, tx_b64: str, addrs: List[str]) -> Dict[str, Any]:
        res = await self.rpc("simulateTransaction", [tx_b64, {
            "encoding": "base64", "sigVerify": False, "replaceRecentBlockhash": True, "commitment": "processed",
            "accounts": {"encoding": "base64", "addresses": addrs}}])
        v = (res or {}).get("value") or {}
        post = {a: self._acct(x) for a, x in zip(addrs, v.get("accounts") or [])}
        return {"err": v.get("err"), "post": post, "units": v.get("unitsConsumed"),
                "logs_tail": (v.get("logs") or [])[-3:]}
