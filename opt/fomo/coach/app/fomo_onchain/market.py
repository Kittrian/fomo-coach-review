"""Free market marks for the fomo_top_trader profile (no keys, no paid services).

DexScreener public token endpoint (batched ≤30 mints/call, documented 300 req/min; we stay ≪ that) for
price, run-up (m5/h1/h6/h24), liquidity, mcap and venue (`pumpfun` = still on the bonding curve).
Mint/freeze authority via getAccountInfo(jsonParsed) on the source's own RPC endpoint.
Deliberately does NOT use Jupiter or the QP mesh, so this source can't eat the Fluffy lane's budgets.
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Dict, Iterable, List, Optional

import httpx

from app.fomo_onchain import WSOL_MINT
from app.pump import rpc

DEX_URL = os.getenv("FOMO_DEX_URL", "https://api.dexscreener.com/tokens/v1/solana/")
CACHE_S = float(os.getenv("FOMO_MARK_CACHE_S", "10"))
MIN_INTERVAL_S = float(os.getenv("FOMO_DEX_MIN_INTERVAL_S", "1.0"))

_cache: Dict[str, Dict[str, Any]] = {}
_auth_cache: Dict[str, Dict[str, Any]] = {}
_lock = asyncio.Lock()
_last_call = 0.0
stats = {"dex_calls": 0, "dex_errors": 0, "auth_calls": 0, "auth_errors": 0, "last_error": None}


def _best_pair(pairs: List[Dict[str, Any]], mint: str) -> Optional[Dict[str, Any]]:
    mine = [p for p in pairs if (p.get("baseToken") or {}).get("address") == mint]
    if not mine:
        return None
    return max(mine, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))


def _mark(p: Dict[str, Any]) -> Dict[str, Any]:
    ch = p.get("priceChange") or {}
    return {"price_usd": float(p["priceUsd"]) if p.get("priceUsd") else None,
            "m5": ch.get("m5"), "h1": ch.get("h1"), "h6": ch.get("h6"), "h24": ch.get("h24"),
            "liquidity_usd": (p.get("liquidity") or {}).get("usd"), "mcap_usd": p.get("marketCap") or p.get("fdv"),
            "dex": p.get("dexId"), "pair": p.get("pairAddress"), "pair_created_ms": p.get("pairCreatedAt"),
            "symbol": (p.get("baseToken") or {}).get("symbol"), "at_ms": int(time.time() * 1000)}


async def marks(mints: Iterable[str], *, client: Optional[httpx.AsyncClient] = None,
                force: bool = False) -> Dict[str, Dict[str, Any]]:
    """mint -> mark (cached CACHE_S). Always includes SOL for USD→SOL conversion."""
    global _last_call
    want = list(dict.fromkeys([WSOL_MINT, *mints]))
    now = time.time()
    need = [m for m in want if force or now - _cache.get(m, {}).get("_t", 0) > CACHE_S]
    if need:
        async with _lock:
            for i in range(0, len(need), 30):
                chunk = need[i:i + 30]
                wait = MIN_INTERVAL_S - (time.time() - _last_call)
                if wait > 0:
                    await asyncio.sleep(wait)
                _last_call = time.time()
                own = client is None
                c = client or httpx.AsyncClient(timeout=6.0)
                try:
                    stats["dex_calls"] += 1
                    r = await c.get(DEX_URL + ",".join(chunk), timeout=6.0)
                    r.raise_for_status()
                    pairs = r.json() or []
                    for m in chunk:
                        bp = _best_pair(pairs, m)
                        prev = _cache.get(m) or {}
                        if not bp and m == WSOL_MINT and prev.get("price_usd") and time.time() - prev.get("_t", 0) < 120:
                            continue   # DexScreener has no WSOL-base pair; keep the fallback spot price
                        _cache[m] = {**(_mark(bp) if bp else {"price_usd": None, "missing": True}), "_t": time.time()}
                except Exception as exc:  # noqa: BLE001
                    stats["dex_errors"] += 1
                    stats["last_error"] = f"dex: {type(exc).__name__}"
                finally:
                    if own:
                        await c.aclose()
    if not (_cache.get(WSOL_MINT) or {}).get("price_usd"):
        await _sol_fallback(client)
    return {m: {k: v for k, v in _cache[m].items() if k != "_t"} for m in want if m in _cache}


SOL_PRICE_URLS = [u for u in os.getenv("SOL_USD_URLS", "https://lite-api.jup.ag/price/v3?ids=" + WSOL_MINT +
                                       ",https://api.coinbase.com/v2/prices/SOL-USD/spot").split(",") if u]
_sol_fb = {"t": 0.0}


async def _sol_fallback(client: Optional[httpx.AsyncClient]) -> None:
    """DexScreener's token endpoint returns [] for WSOL, so get SOL/USD from free keyless spot endpoints
    (Jupiter lite price v3, then Coinbase). Throttled to one attempt every 20 s."""
    async with _sol_lock:
        if (_cache.get(WSOL_MINT) or {}).get("price_usd") or time.time() - _sol_fb["t"] < 20:
            return
        _sol_fb["t"] = time.time()
        await _sol_fetch(client)


_sol_lock = asyncio.Lock()


async def _sol_fetch(client: Optional[httpx.AsyncClient]) -> None:
    own = client is None
    c = client or httpx.AsyncClient(timeout=5.0)
    try:
        for url in SOL_PRICE_URLS:
            try:
                stats["sol_fallback_calls"] = stats.get("sol_fallback_calls", 0) + 1
                r = await c.get(url, timeout=5.0)
                r.raise_for_status()
                j = r.json() or {}
                px = (j.get(WSOL_MINT) or {}).get("usdPrice") or (j.get("data") or {}).get("amount")
                px = float(px) if px else None
                if px and 1 < px < 100000:
                    _cache[WSOL_MINT] = {"price_usd": px, "source": url.split("/")[2], "at_ms": int(time.time() * 1000),
                                         "_t": time.time()}
                    return
            except Exception as exc:  # noqa: BLE001
                stats["sol_fallback_errors"] = stats.get("sol_fallback_errors", 0) + 1
                stats["last_error"] = f"sol_usd: {type(exc).__name__}"
    finally:
        if own:
            await c.aclose()


def sol_usd() -> Optional[float]:
    return (_cache.get(WSOL_MINT) or {}).get("price_usd")


async def mint_authorities(mint: str, *, url: str, client: Optional[httpx.AsyncClient] = None) -> Dict[str, Any]:
    if mint in _auth_cache:
        return _auth_cache[mint]
    stats["auth_calls"] += 1
    try:
        res = await rpc.call("getAccountInfo", [mint, {"encoding": "jsonParsed"}], client=client, url=url, timeout=6.0)
        info = ((((res or {}).get("value") or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
        out = {"ok": bool(info), "mint_authority": info.get("mintAuthority"),
               "freeze_authority": info.get("freezeAuthority"), "decimals": info.get("decimals")}
    except Exception as exc:  # noqa: BLE001
        stats["auth_errors"] += 1
        out = {"ok": False, "error": f"{type(exc).__name__}"}
    if out.get("ok"):
        _auth_cache[mint] = out
    return out
