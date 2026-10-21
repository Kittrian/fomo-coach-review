"""QP Data Mesh client — free enrichment + risk layer for every signal.

Base: MESH_BASE_URL (default https://quantumpools.io/api/mesh). GET only.
Public limit is 2 req/s per IP (burst 30) → client-side token bucket at
MESH_RPS (default 1.5) + per-op TTL cache + single-flight per mint.

Ops used (params discovered from /api/mesh index + live probes 2026-10-06):
  price       ?chain=solana&token=<mint>
  token_meta  ?chain=solana&token=<mint>
  token_risk  ?chain=solana&token=<mint>     (rugcheck + jupiter + goplus + gmgn; stricter wins)
  pool        ?chain=solana&address=<pool>   (only when the signal carries a pool address)
  verify_onchain ?chain=solana&address=<pool>
  compat/dexpaprika/networks/solana/tokens/<mint>  (liquidity_usd, fdv, supply, 24h buys/sells)
  pump curve lane: auto-detected from the index `ops` list (pump_curve / pump / bonding_curve / curve);
                   degrades to {"status": "lane_unavailable"} until the mesh ships it.

Never sends keys; the mesh holds all provider keys server-side.
Recommend-only: nothing here can trade.
"""
from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

from app.config import settings

logger = logging.getLogger("coach.mesh")

SOLANA_MINT_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
PUMP_LANE_CANDIDATES = ("pump_curve", "pump", "bonding_curve", "curve", "pumpfun")
PUMP_SUPPLY = 1_000_000_000  # pump.fun fixed supply (used only as an estimate fallback)

# client-side TTLs (seconds); mesh caches longer server-side
_TTL: Dict[str, float] = {
    "price": 20.0,
    "token_meta": 3600.0,
    "token_risk": 600.0,
    "dp_token": 60.0,
    "pool": 60.0,
    "verify_onchain": 30.0,
    "curve": 15.0,
    "index": 600.0,
    "health": 30.0,
}
ENRICH_TTL = 45.0
NEG_TTL = 60.0  # remember "mesh had nothing / was down" per mint so UI polls don't re-hammer it
STALE_MAX_AGE = 6 * 3600.0  # serve client-side stale data up to 6h when mesh is down


class TokenBucket:
    """Async token bucket — keeps us under the mesh's 2 rps/IP public limit."""

    def __init__(self, rate: float, burst: float) -> None:
        self.rate = max(0.1, rate)
        self.burst = max(1.0, burst)
        self.tokens = self.burst
        self.updated = time.monotonic()
        self._lock: Optional[asyncio.Lock] = None
        self._loop: Any = None
        self.cooldown_until = 0.0  # set on 429

    def _get_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._lock is None or self._loop is not loop:
            self._lock = asyncio.Lock()
            self._loop = loop
        return self._lock

    async def acquire(self, *, shared_cooldown: Optional["TokenBucket"] = None) -> None:
        async with self._get_lock():
            while True:
                now = time.monotonic()
                until = max(self.cooldown_until, shared_cooldown.cooldown_until if shared_cooldown else 0.0)
                if now < until:                                   # a 429 on either budget pauses both (same IP)
                    await asyncio.sleep(until - now)
                    continue
                self.tokens = min(self.burst, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return
                await asyncio.sleep((1.0 - self.tokens) / self.rate)


_bucket = TokenBucket(settings.mesh_rps, settings.mesh_burst)
# v1.17.2: live pre-buy checks get their own budget so they never queue behind background/UI enrichment.
# Default 1.5 (background) + 0.5 (live) = the mesh's 2 req/s per-IP public limit; a 429 pauses both.
_live_bucket = TokenBucket(float(os.getenv("MESH_LIVE_RPS", "0.5")), float(os.getenv("MESH_LIVE_BURST", "6")))
_live_ctx: "contextvars.ContextVar[bool]" = contextvars.ContextVar("mesh_live_priority", default=False)
_shared: Dict[str, Any] = {"client": None, "loop": None}
_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_stats: Dict[str, Any] = {
    "requests": 0,
    "ok": 0,
    "errors": 0,
    "rate_limited": 0,
    "cache_hits": 0,
    "stale_serves": 0,
    "live_requests": 0,
    "client_reuses": 0,
    "last_ok_at": None,
    "last_error": None,
    "last_error_at": None,
}
_enrich_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_neg_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_inflight: Dict[str, "asyncio.Task[Dict[str, Any]]"] = {}


def is_solana_mint(addr: Optional[str]) -> bool:
    return bool(addr) and bool(SOLANA_MINT_RE.match(addr or ""))


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


async def _get(
    client: httpx.AsyncClient,
    path: str,
    params: Optional[Dict[str, str]] = None,
    *,
    ttl_key: str,
) -> Dict[str, Any]:
    """Throttled + cached GET. Returns {"ok":bool, "body":..., "cached":bool, "client_stale":bool, "error":str?}."""
    params = params or {}
    key = path + "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    ttl = _TTL.get(ttl_key, 30.0)
    hit = _cache.get(key)
    if hit and (time.time() - hit[0]) < ttl:
        _stats["cache_hits"] += 1
        return {"ok": True, "body": hit[1], "cached": True, "client_stale": False}

    url = settings.mesh_base_url.rstrip("/") + "/" + path.lstrip("/")
    err: Optional[str] = None
    status: Optional[int] = None
    try:
        if _live_ctx.get():
            await _live_bucket.acquire(shared_cooldown=_bucket)
            _stats["live_requests"] += 1
        else:
            await _bucket.acquire()
        _stats["requests"] += 1
        resp = await client.get(url, params=params)
        status = resp.status_code
        if status == 429:
            _stats["rate_limited"] += 1
            retry = resp.headers.get("Retry-After")
            try:
                wait = min(30.0, float(retry)) if retry else 5.0
            except ValueError:
                wait = 5.0
            _bucket.cooldown_until = time.monotonic() + wait
            err = f"rate_limited/429 (cooldown {wait:.0f}s)"
        else:
            body = resp.json()
            if status == 200 and isinstance(body, dict) and body.get("ok", True) is not False:
                _cache[key] = (time.time(), body)
                _stats["ok"] += 1
                _stats["last_ok_at"] = _now_iso()
                return {"ok": True, "body": body, "cached": False, "client_stale": False}
            err = f"http {status}: {str(body.get('error') if isinstance(body, dict) else body)[:160]}"
    except Exception as exc:  # noqa: BLE001
        err = f"{type(exc).__name__}: {str(exc)[:160]}"

    _stats["errors"] += 1
    _stats["last_error"] = err
    _stats["last_error_at"] = _now_iso()
    # serve client-side stale copy when the mesh is down (never for 4xx "not found / bad input")
    if hit and (time.time() - hit[0]) < STALE_MAX_AGE and (status is None or status >= 429):
        _stats["stale_serves"] += 1
        return {"ok": True, "body": hit[1], "cached": True, "client_stale": True, "error": err}
    return {"ok": False, "body": None, "cached": False, "client_stale": False, "error": err, "status": status}


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(settings.mesh_timeout_s, connect=min(3.0, settings.mesh_timeout_s)),
        headers={"User-Agent": "fomo-signal-coach/mesh (+recommend-only)", "Accept": "application/json"},
        follow_redirects=True,
    )


def _shared_client() -> httpx.AsyncClient:
    """v1.17.2: one keep-alive client per event loop for enrichment (was a new client + TLS handshake per token)."""
    loop = asyncio.get_running_loop()
    c = _shared.get("client")
    if c is None or c.is_closed or _shared.get("loop") is not loop:
        c = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.mesh_timeout_s, connect=min(3.0, settings.mesh_timeout_s)),
            headers={"User-Agent": "fomo-signal-coach/mesh (+recommend-only)", "Accept": "application/json"},
            follow_redirects=True,
            limits=httpx.Limits(max_connections=16, max_keepalive_connections=8, keepalive_expiry=60.0),
        )
        _shared.update(client=c, loop=loop)
    else:
        _stats["client_reuses"] += 1
    return c


class _borrow_client:
    """`async with _borrow_client() as c:` — the shared client, never closed on exit."""

    async def __aenter__(self) -> httpx.AsyncClient:
        return _shared_client()

    async def __aexit__(self, *exc: Any) -> bool:
        return False


def _envelope_meta(res: Dict[str, Any]) -> Dict[str, Any]:
    """source / fallbacks / cache / stale flags from a mesh envelope."""
    b = res.get("body") or {}
    if not isinstance(b, dict):
        b = {}
    return {
        "ok": bool(res.get("ok")),
        "source": b.get("source") or ("dexpaprika(compat)" if "summary" in b else None),
        "fallbacks_tried": [
            f"{f.get('provider')}:{f.get('result')}" for f in (b.get("fallbacks_tried") or []) if isinstance(f, dict)
        ][:6],
        "cached": bool(b.get("cached")) or bool(res.get("cached")),
        "stale": bool(b.get("stale")) or bool(res.get("client_stale")),
        "age_s": b.get("age_s"),
        "error": res.get("error"),
    }


# ---------------------------------------------------------------- per-op fetchers


async def get_price(client: httpx.AsyncClient, mint: str, chain: str = "solana") -> Dict[str, Any]:
    return await _get(client, "price", {"chain": chain, "token": mint}, ttl_key="price")


async def get_token_meta(client: httpx.AsyncClient, mint: str, chain: str = "solana") -> Dict[str, Any]:
    return await _get(client, "token_meta", {"chain": chain, "token": mint}, ttl_key="token_meta")


async def get_token_risk(client: httpx.AsyncClient, mint: str, chain: str = "solana") -> Dict[str, Any]:
    return await _get(client, "token_risk", {"chain": chain, "token": mint}, ttl_key="token_risk")


async def get_dp_token(client: httpx.AsyncClient, mint: str, chain: str = "solana") -> Dict[str, Any]:
    """DexPaprika token summary via mesh compat lane (liquidity, fdv, supply, flow)."""
    return await _get(client, f"compat/dexpaprika/networks/{chain}/tokens/{mint}", ttl_key="dp_token")


async def get_pool(client: httpx.AsyncClient, pool: str, chain: str = "solana") -> Dict[str, Any]:
    return await _get(client, "pool", {"chain": chain, "address": pool}, ttl_key="pool")


async def verify_onchain(client: httpx.AsyncClient, pool: str, chain: str = "solana") -> Dict[str, Any]:
    return await _get(client, "verify_onchain", {"chain": chain, "address": pool}, ttl_key="verify_onchain")


async def get_index(client: httpx.AsyncClient) -> Dict[str, Any]:
    return await _get(client, "", ttl_key="index")


async def pump_lane(client: httpx.AsyncClient) -> Optional[str]:
    """Return the mesh op name for the pump.fun bonding-curve lane if the mesh advertises one."""
    idx = await get_index(client)
    ops = ((idx.get("body") or {}).get("ops") or []) if idx.get("ok") else []
    for cand in PUMP_LANE_CANDIDATES:
        if cand in ops:
            return cand
    return None


async def get_pump_curve(client: httpx.AsyncClient, mint: str) -> Dict[str, Any]:
    lane = await pump_lane(client)
    if not lane:
        return {"status": "lane_unavailable", "note": "mesh has no pump.fun curve op yet — degraded"}
    res = await _get(client, lane, {"chain": "solana", "token": mint}, ttl_key="curve")
    if not res.get("ok"):
        return {"status": "error", "lane": lane, **_envelope_meta(res)}
    d = (res["body"] or {}).get("data") or {}

    def pick(*names: str) -> Any:
        for n in names:
            if d.get(n) is not None:
                return d.get(n)
        return None

    progress_pct: Optional[float] = None
    for name in ("graduation_progress_pct", "progress_pct", "bonding_curve_progress", "graduation_progress", "progress", "curve_progress"):
        v = d.get(name)
        if isinstance(v, (int, float)):
            progress_pct = round(float(v) * 100.0, 2) if (v <= 1.0 and not name.endswith("pct")) else round(float(v), 2)
            break
    migrated = pick("migrated", "complete", "graduated")
    return {
        "status": "ok",
        "lane": lane,
        "curve_price_usd": pick("curve_price_usd", "price_usd", "price"),
        "graduation_progress_pct": progress_pct,
        "migrated": bool(migrated) if migrated is not None else None,
        **_envelope_meta(res),
    }


# ---------------------------------------------------------------- enrichment


def _num(v: Any) -> Optional[float]:
    try:
        f = float(v)
        return f if f == f else None  # drop NaN
    except (TypeError, ValueError):
        return None


def _risk_summary(res: Dict[str, Any]) -> Dict[str, Any]:
    meta = _envelope_meta(res)
    if not res.get("ok"):
        return {"status": "unavailable", **meta}
    d = (res["body"] or {}).get("data") or {}
    det = d.get("details") or {}
    rc = det.get("rugcheck") or {}
    gm = det.get("gmgn") or {}
    jup = det.get("jupiter") or {}
    gp = det.get("goplus") or {}
    flags = [
        {"flag": f.get("flag"), "source": f.get("source"), "severity": f.get("severity")}
        for f in (d.get("flags") or [])
        if isinstance(f, dict)
    ]

    def tri(v: Any) -> Optional[bool]:
        return None if v is None else bool(v)

    renounced_mint = tri(gm.get("renounced_mint"))
    renounced_freeze = tri(gm.get("renounced_freeze_account"))
    flag_texts = [(f.get("flag") or "").lower() for f in flags]

    def authority_live(renounced: Optional[bool], word: str) -> Optional[bool]:
        # RugCheck words these "Mint Authority still enabled" / "Freeze Authority still enabled"
        if renounced is False:
            return True
        if any(word in t and any(w in t for w in ("enabled", "still", "active", "live")) for t in flag_texts):
            return True
        if renounced is True:
            return False
        return None

    mint_live = authority_live(renounced_mint, "mint authority")
    freeze_live = authority_live(renounced_freeze, "freeze authority")
    hp = gm.get("is_honeypot")
    if hp or any("honeypot" in t for t in flag_texts):
        honeypot: Optional[bool] = True
    elif hp is not None:
        honeypot = False
    else:
        honeypot = None
    return {
        "status": "ok",
        "verdict": d.get("verdict"),
        "coverage": d.get("coverage"),
        "sources_answered": d.get("sources_answered") or [],
        "flags": flags,
        "mint_authority_live": mint_live,
        "freeze_authority_live": freeze_live,
        "honeypot": honeypot,
        "buy_tax": _num(gm.get("buy_tax")),
        "sell_tax": _num(gm.get("sell_tax")),
        "top10_holder_pct": round(_num(gm.get("top_10_holder_rate")) * 100, 2)
        if _num(gm.get("top_10_holder_rate")) is not None
        else None,
        "rugcheck_score_normalised": rc.get("score_normalised"),
        "rugcheck_rugged": rc.get("rugged"),
        "rugcheck_holders": rc.get("total_holders"),
        "rugcheck_lp_locked_pct": rc.get("lp_locked_pct"),
        "jupiter_verified": jup.get("verified"),
        "jupiter_organic_score": jup.get("organic_score"),
        "goplus_trusted": gp.get("trusted"),
        **meta,
    }


async def _enrich_uncached(mint: str, chain: str, pool: Optional[str]) -> Dict[str, Any]:
    t0 = time.time()
    async with _borrow_client() as client:
        price_r, risk_r, meta_r, dp_r = await asyncio.gather(
            get_price(client, mint, chain),
            get_token_risk(client, mint, chain),
            get_token_meta(client, mint, chain),
            get_dp_token(client, mint, chain),
        )
        pool_r = verify_r = None
        if pool and is_solana_mint(pool):
            pool_r, verify_r = await asyncio.gather(get_pool(client, pool, chain), verify_onchain(client, pool, chain))
        pump_like = mint.endswith("pump")
        curve = await get_pump_curve(client, mint) if (pump_like or chain == "solana") else None

    price_d = ((price_r.get("body") or {}).get("data") or {}) if price_r.get("ok") else {}
    meta_d = ((meta_r.get("body") or {}).get("data") or {}) if meta_r.get("ok") else {}
    dp_b = (dp_r.get("body") or {}) if dp_r.get("ok") else {}
    dp_s = dp_b.get("summary") or {}
    dp_24 = dp_s.get("24h") or {}
    risk = _risk_summary(risk_r)

    price = _num(price_d.get("price_usd")) or _num(dp_s.get("price_usd")) or _num(meta_d.get("price_usd"))
    liquidity = _num(dp_s.get("liquidity_usd"))
    liq_src = "dexpaprika" if liquidity is not None else None
    if liquidity is None:
        rc_liq = _num(((((risk_r.get("body") or {}).get("data") or {}).get("details") or {}).get("rugcheck") or {}).get("total_market_liquidity"))
        if rc_liq is not None:
            liquidity, liq_src = rc_liq, "rugcheck"
    pool_info: Optional[Dict[str, Any]] = None
    if pool_r is not None:
        pd = ((pool_r.get("body") or {}).get("data") or {}) if pool_r.get("ok") else {}
        pool_info = {
            "address": pool,
            "dex": pd.get("dex"),
            "tvl_usd": _num(pd.get("tvl_usd")),
            "volume_24h_usd": _num(pd.get("volume_24h_usd")),
            "truth": (pool_r.get("body") or {}).get("truth") if pool_r.get("ok") else None,
            "verify_onchain": {
                "ok": bool(verify_r and verify_r.get("ok")),
                "venue": (((verify_r or {}).get("body") or {}).get("data") or {}).get("venue") if verify_r else None,
                "price_1_per_0": (((verify_r or {}).get("body") or {}).get("data") or {}).get("price_1_per_0") if verify_r else None,
            },
            **_envelope_meta(pool_r),
        }
        if liquidity is None and pool_info["tvl_usd"] is not None:
            liquidity, liq_src = pool_info["tvl_usd"], "pool_tvl"

    supply = _num(dp_b.get("total_supply"))
    mcap = _num(dp_s.get("fdv"))
    mcap_src = "dexpaprika_fdv" if mcap else None
    if mcap is None and price is not None and supply:
        mcap, mcap_src = price * supply, "price×supply"
    if mcap is None and price is not None and mint.endswith("pump"):
        mcap, mcap_src = price * PUMP_SUPPLY, "price×1B (pump.fun est.)"

    any_ok = any(r.get("ok") for r in (price_r, risk_r, meta_r, dp_r))
    sources = {
        "price": _envelope_meta(price_r),
        "token_risk": {k: risk.get(k) for k in ("ok", "source", "fallbacks_tried", "cached", "stale", "age_s", "error")},
        "token_meta": _envelope_meta(meta_r),
        "dexpaprika": _envelope_meta(dp_r),
    }
    stale = any(s.get("stale") for s in sources.values() if s.get("ok"))
    out: Dict[str, Any] = {
        "status": "ok" if any_ok else "unavailable",
        "mint": mint,
        "chain": chain,
        "symbol": meta_d.get("symbol") or dp_b.get("symbol"),
        "name": meta_d.get("name") or dp_b.get("name"),
        "price_usd": price,
        "price_source": (price_r.get("body") or {}).get("source") if price_r.get("ok") else ("dexpaprika" if dp_s.get("price_usd") else None),
        "change_24h_pct": _num(price_d.get("change_24h_pct")) or _num(dp_24.get("last_price_usd_change")),
        "liquidity_usd": liquidity,
        "liquidity_source": liq_src,
        "market_cap_usd": round(mcap, 2) if mcap is not None else None,
        "market_cap_source": mcap_src,
        "volume_24h_usd": _num(dp_24.get("volume_usd")),
        "buys_24h": dp_24.get("buys"),
        "sells_24h": dp_24.get("sells"),
        "pools": dp_s.get("pools"),
        "pool": pool_info,
        "risk": risk,
        "pump": {"pump_like": mint.endswith("pump"), "curve": curve},
        "token_program": meta_d.get("token_program"),
        "metadata_mutable": meta_d.get("mutable"),
        "sources": sources,
        "stale": stale,
        "fetched_at": _now_iso(),
        "elapsed_ms": int((time.time() - t0) * 1000),
    }
    if not any_ok:
        out["error"] = price_r.get("error") or risk_r.get("error") or "mesh unreachable"
    return out


def _skip(mint: str, chain: str, status: str, note: str) -> Dict[str, Any]:
    return {"status": status, "mint": mint, "chain": chain, "note": note, "fetched_at": _now_iso()}


async def enrich_token(mint: str, chain: str = "solana", *, pool: Optional[str] = None, force: bool = False) -> Dict[str, Any]:
    """Full mesh enrichment for one token. Single-flight per mint; ENRICH_TTL cache."""
    chain = (chain or "solana").lower()
    if not settings.mesh_enabled:
        return _skip(mint, chain, "disabled", "MESH_ENABLED=false")
    if chain != "solana":
        return _skip(mint, chain, "unsupported_chain", "coach mesh enrichment is Solana-only for now")
    if not is_solana_mint(mint):
        return _skip(mint, chain, "invalid_mint", "not a base58 Solana mint (demo placeholder?) — no mesh call")

    key = f"{chain}:{mint}"
    hit = _enrich_cache.get(key)
    if hit and not force and (time.time() - hit[0]) < ENRICH_TTL:
        return hit[1]
    neg = _neg_cache.get(key)
    if neg and not force and not hit and (time.time() - neg[0]) < NEG_TTL:
        return neg[1]
    task = _inflight.get(key)
    if task is None or task.done() or task.get_loop() is not asyncio.get_running_loop():
        task = asyncio.ensure_future(_enrich_uncached(mint, chain, pool))
        _inflight[key] = task
    try:
        out = await task
    except Exception as exc:  # noqa: BLE001
        logger.warning("mesh enrich soft-fail %s: %s", mint[:8], exc)
        out = _skip(mint, chain, "error", f"{type(exc).__name__}: {str(exc)[:160]}")
    finally:
        if _inflight.get(key) is task and task.done():
            _inflight.pop(key, None)
    if out.get("status") == "ok":
        _enrich_cache[key] = (time.time(), out)
        _neg_cache.pop(key, None)
    else:
        _neg_cache[key] = (time.time(), out)
    if out.get("status") != "ok" and hit:  # mesh down now → keep last good enrichment, flagged stale
        old = dict(hit[1])
        old["stale"] = True
        old["note"] = f"mesh unavailable now ({out.get('error') or out.get('status')}); showing last good enrichment"
        return old
    return out


async def enrich_with_timeout(mint: str, chain: str = "solana", *, pool: Optional[str] = None, timeout: float = 3.0,
                              live: bool = False) -> Dict[str, Any]:
    """Await enrichment up to `timeout`; keep it running in the background if slower (returns pending).
    live=True (live pre-buy check): its GETs use the separate live rate budget."""
    key = f"{(chain or 'solana').lower()}:{mint}"
    hit = _enrich_cache.get(key)
    if hit and (time.time() - hit[0]) < ENRICH_TTL:
        return hit[1]
    tok = _live_ctx.set(True) if live else None
    try:
        fut = asyncio.ensure_future(enrich_token(mint, chain, pool=pool))   # task copies the context (live flag)
    finally:
        if tok is not None:
            _live_ctx.reset(tok)
    try:
        return await asyncio.wait_for(asyncio.shield(fut), timeout=timeout)
    except asyncio.TimeoutError:
        if hit:
            old = dict(hit[1])
            old["stale"] = True
            old["timed_out"] = True          # v1.15: live buys refuse an old answer served on timeout
            return old
        return _skip(mint, (chain or "solana").lower(), "pending", "mesh enrichment in progress (throttled) — refresh shortly")


def schedule_enrichment(mint: str, chain: str = "solana", *, pool: Optional[str] = None, live: bool = False) -> bool:
    """Fire-and-forget enrichment on CA announce / buy ingest (needs a running loop). live=True → live budget."""
    if not settings.mesh_enabled or (chain or "solana").lower() != "solana" or not is_solana_mint(mint):
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    tok = _live_ctx.set(True) if live else None
    try:
        loop.create_task(enrich_token(mint, chain, pool=pool))
    finally:
        if tok is not None:
            _live_ctx.reset(tok)
    return True


def cached_enrichment(mint: str, chain: str = "solana") -> Optional[Dict[str, Any]]:
    hit = _enrich_cache.get(f"{(chain or 'solana').lower()}:{mint}")
    return hit[1] if hit else None


# ---------------------------------------------------------------- health


async def mesh_health_summary() -> Dict[str, Any]:
    base = {
        "enabled": settings.mesh_enabled,
        "base_url": settings.mesh_base_url,
        "client_rps": settings.mesh_rps,
        "client_burst": settings.mesh_burst,
        "live_rps": _live_bucket.rate,
        "live_burst": _live_bucket.burst,
        "timeout_s": settings.mesh_timeout_s,
        "role": "free enrichment + rule risk gate (fallback when Jev unavailable)",
        "client_stats": dict(_stats),
        "enriched_tokens_cached": len(_enrich_cache),
    }
    if not settings.mesh_enabled:
        return {**base, "status": "disabled"}
    async with _client() as client:
        res = await _get(client, "health", ttl_key="health")
        lane = await pump_lane(client) if res.get("ok") else None
    base["client_stats"] = dict(_stats)
    if not res.get("ok"):
        return {**base, "status": "down", "error": res.get("error"), "fail_safe": "escalate_human"}
    b = res["body"] or {}
    provs = b.get("providers") or []
    by_state: Dict[str, int] = {}
    for p in provs:
        by_state[p.get("state") or "?"] = by_state.get(p.get("state") or "?", 0) + 1
    risk_src = (b.get("risk_sources") or {}).get("solana")
    return {
        **base,
        "status": "ok" if b.get("ok") else "degraded",
        "service": b.get("service"),
        "mesh_uptime_s": b.get("uptime_s"),
        "providers_by_state": by_state,
        "solana_risk_sources": risk_src[0] if isinstance(risk_src, list) and risk_src else risk_src,
        "public_rate_limit": b.get("public_rate_limit"),
        "pump_curve_lane": lane or "not_available_yet",
        "cached": bool(res.get("cached")),
        "client_stale": bool(res.get("client_stale")),
    }


def clear_mesh_cache() -> None:
    _cache.clear()
    _neg_cache.clear()
    _enrich_cache.clear()
    _inflight.clear()
