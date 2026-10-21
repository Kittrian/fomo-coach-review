"""v1.16.1 — live buys of NON-SOL-quote pump.fun coins in the scout arms (scout_solo / scout_confirmed).

Andrew approved (2026-10-07 8:32 PM CT): buy size = LIVE_SCOUT_NONSOL_FRACTION (0.10) × the triggering scout's buy
(its SOL spend as measured by the v1.16 tx parser), capped at LIVE_SCOUT_NONSOL_MAX_SOL (0.40) and
LIVE_MAX_SOL_PER_TRADE. SOL-quote coins are untouched (fixed per-arm size).

Fail closed everywhere: an unknown / non-whitelisted quote asset, no or stale quote-asset price, price sources that
disagree, no curve, a migrated curve, a curve quoted in a different mint than the signal, no buy route, no sell
route, or an absurd round trip → the buy is REFUSED with the reason logged. Buys and sells go through Jupiter with
SOL in / SOL out (Jupiter routes through the quote asset). Positions are valued in SOL.

Read-only here: nothing in this module signs or sends anything.
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

import httpx

WSOL = "So11111111111111111111111111111111111111112"
SOL_QUOTE_ALIASES = {WSOL, "11111111111111111111111111111111", "", None}
SCOUT_LIVE_ARMS = ("scout_solo", "scout_confirmed")

# Quote assets identified by MINT (never by symbol from untrusted data). decimals are cross-checked against the
# Jupiter price API answer at refresh time; a mismatch refuses the buy.
KNOWN_QUOTES: Dict[str, Dict[str, Any]] = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": {"symbol": "USDC", "decimals": 6, "stable": True},
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": {"symbol": "USDT", "decimals": 6, "stable": True},
    "pumpCmXqMfrsAkQ5r49WcJnRayYRqmXz6ae8H7H9Dfn": {"symbol": "PUMP", "decimals": 6, "stable": False},
    # Seen in today's scout buys (Fox, R4) but NOT allowed by default: Wormhole-bridged Worldcoin with only
    # ~$0.23M Solana liquidity and a live bridge mint authority. Add "WLD" to LIVE_SCOUT_NONSOL_QUOTES to allow.
    "WLDTE73PPdfFFjhpBz8Yuhvx5N45gvgEgyMXqwa3rFn": {"symbol": "WLD", "decimals": 8, "stable": False},
}
DEFAULT_QUOTES = "USDC,USDT,PUMP"
PRICE_URL = "https://lite-api.jup.ag/price/v3?ids="


def _f(name: str, d: float) -> float:
    try:
        return float(os.getenv(name, str(d)).strip())
    except ValueError:
        return d


def cfg() -> Dict[str, Any]:
    syms = {s.strip().upper() for s in os.getenv("LIVE_SCOUT_NONSOL_QUOTES", DEFAULT_QUOTES).split(",") if s.strip()}
    return {
        "enabled": os.getenv("LIVE_SCOUT_NONSOL_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"},
        "fraction": _f("LIVE_SCOUT_NONSOL_FRACTION", 0.10),
        "max_sol": _f("LIVE_SCOUT_NONSOL_MAX_SOL", 0.40),
        "allowed": {m: q for m, q in KNOWN_QUOTES.items() if q["symbol"] in syms},
        "price_max_age_s": _f("LIVE_SCOUT_NONSOL_PRICE_MAX_AGE_S", 60.0),
        "price_tol": _f("LIVE_SCOUT_NONSOL_PRICE_TOL", 0.05),          # price API vs Jupiter route disagreement
        "max_entry_premium": _f("LIVE_SCOUT_NONSOL_MAX_ENTRY_PREMIUM", 0.25),   # buy route vs curve spot
        "min_roundtrip": _f("LIVE_SCOUT_NONSOL_MIN_ROUNDTRIP", 0.70),  # sell-route value / size
    }


def is_nonsol(quote_mint: Optional[str]) -> bool:
    return quote_mint not in SOL_QUOTE_ALIASES


def quote_symbol(quote_mint: Optional[str]) -> str:
    q = KNOWN_QUOTES.get(quote_mint or "")
    return q["symbol"] if q else f"{str(quote_mint or '?')[:6]}…"


def allowed(quote_mint: Optional[str]) -> bool:
    return bool(quote_mint) and quote_mint in cfg()["allowed"]


def size_for(scout_sol: Optional[float], *, max_sol_per_trade: float, min_scout_sol: float = 0.5
             ) -> Tuple[Optional[float], str]:
    """1/10th of the scout's SOL-equivalent buy, capped at 0.40 and LIVE_MAX_SOL_PER_TRADE. None → refuse."""
    c = cfg()
    try:
        s = float(scout_sol or 0)
    except (TypeError, ValueError):
        s = 0.0
    if s <= 0:
        return None, "non-SOL size: scout buy size unknown — no buy"
    if s < min_scout_sol:
        return None, f"non-SOL size: scout buy {s:.3f} SOL < {min_scout_sol}"
    if not (0 < c["fraction"] <= 1):
        return None, f"non-SOL size: bad LIVE_SCOUT_NONSOL_FRACTION {c['fraction']}"
    size = min(s * c["fraction"], c["max_sol"], max_sol_per_trade)
    size = int(round(size * 1e4, 6)) / 1e4             # round DOWN to 0.0001 SOL (float-noise safe)
    if size <= 0:
        return None, "non-SOL size 0"
    return size, f"non-SOL size {size:.4f} = min({c['fraction']:g} × scout {s:.3f}, {c['max_sol']:g}, {max_sol_per_trade:g})"


# ---------------- quote-asset price (USD + SOL per quote), two independent sources ----------------
_cache: Dict[str, Dict[str, Any]] = {}
_locks: Dict[str, asyncio.Lock] = {}
stats: Dict[str, int] = {}


def _bump(k: str) -> None:
    stats[k] = stats.get(k, 0) + 1


async def _fetch_prices(mints: List[str]) -> Dict[str, Dict[str, Any]]:
    """Jupiter lite price v3 (free, keyless): {mint: {usdPrice, decimals, ...}}."""
    async with httpx.AsyncClient(timeout=6.0, headers={"User-Agent": "fomo-signal-coach"}) as c:
        r = await c.get(PRICE_URL + ",".join(mints))
        r.raise_for_status()
        return r.json() or {}


async def _route_rate(quote_mint: str, lamports: int) -> Optional[int]:
    """Jupiter quote (no taker, never a transaction): raw quote-asset units out for `lamports` SOL in."""
    from app.pump.paper import _jupiter_quote
    q = await asyncio.wait_for(asyncio.to_thread(_jupiter_quote, WSOL, quote_mint, lamports), timeout=10)
    return int(q["out_amount"]) if q.get("ok") and q.get("out_amount") else None


async def refresh(quote_mint: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """Fetch + verify a quote asset's price. Returns (info, "ok") or (None, refusal reason)."""
    q = KNOWN_QUOTES.get(quote_mint)
    if not q:
        return None, f"quote asset {str(quote_mint)[:6]}… unknown"
    lock = _locks.setdefault(quote_mint, asyncio.Lock())
    async with lock:
        hit = _cache.get(quote_mint)
        if hit and time.time() - hit["ts"] < 10:            # coalesce bursts
            return hit, "ok"
        _bump("refresh")
        try:
            px = await _fetch_prices([quote_mint, WSOL])
        except Exception as exc:  # noqa: BLE001
            _bump("price_errors")
            return None, f"quote price unavailable ({quote_symbol(quote_mint)}: {type(exc).__name__})"
        qp, sp = px.get(quote_mint) or {}, px.get(WSOL) or {}
        try:
            usd, sol_usd = float(qp.get("usdPrice") or 0), float(sp.get("usdPrice") or 0)
        except (TypeError, ValueError):
            usd, sol_usd = 0.0, 0.0
        if usd <= 0 or not (1 < sol_usd < 100_000):
            return None, f"quote price unavailable ({quote_symbol(quote_mint)} usd={usd or None}, SOL usd={sol_usd or None})"
        if qp.get("decimals") is not None and int(qp["decimals"]) != q["decimals"]:
            return None, f"quote decimals mismatch for {q['symbol']} ({qp.get('decimals')} != {q['decimals']})"
        if q["stable"] and not (0.97 <= usd <= 1.03):
            return None, f"{q['symbol']} off peg (${usd:.4f}) — no buy"
        from app.fomo_onchain import market
        m_sol = market.sol_usd()
        if m_sol and abs(sol_usd / m_sol - 1) > 0.05:
            return None, f"SOL/USD sources disagree ({sol_usd:.2f} vs {m_sol:.2f})"
        lam = 100_000_000                                      # 0.1 SOL probe
        try:
            out = await _route_rate(quote_mint, lam)
        except Exception as exc:  # noqa: BLE001
            out = None
            _bump("route_errors")
        if not out:
            return None, f"no Jupiter SOL→{q['symbol']} route — quote price unverified"
        route_spq = (lam / 1e9) / (out / 10 ** q["decimals"])     # SOL per 1 quote unit via the route
        price_spq = usd / sol_usd                                 # SOL per 1 quote unit via USD prices
        dev = route_spq / price_spq - 1
        if abs(dev) > cfg()["price_tol"]:
            return None, f"{q['symbol']} price sources disagree ({dev * 100:+.1f}% route vs price API)"
        info = {"quote_mint": quote_mint, "symbol": q["symbol"], "decimals": q["decimals"], "usd": usd,
                "sol_usd": sol_usd, "sol_per_quote": price_spq, "route_sol_per_quote": route_spq,
                "route_dev_pct": round(dev * 100, 3), "ts": time.time()}
        _cache[quote_mint] = info
        return info, "ok"


def cached(quote_mint: str, max_age_s: Optional[float] = None) -> Optional[Dict[str, Any]]:
    hit = _cache.get(quote_mint)
    lim = cfg()["price_max_age_s"] if max_age_s is None else max_age_s
    return hit if hit and time.time() - hit["ts"] <= lim else None


async def price(quote_mint: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """A verified price no older than LIVE_SCOUT_NONSOL_PRICE_MAX_AGE_S (refreshes when needed)."""
    hit = cached(quote_mint, min(30.0, cfg()["price_max_age_s"]))
    if hit:
        return hit, "ok"
    info, why = await refresh(quote_mint)
    if info and time.time() - info["ts"] > cfg()["price_max_age_s"]:
        return None, f"{quote_symbol(quote_mint)} price stale"
    return info, why


# ---------------- curve math in quote units → SOL / USD ----------------
def curve_metrics(curve: Dict[str, Any], info: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Spot price + mcap for a non-SOL-quote pump.fun curve. Reserves are in raw QUOTE units (quote decimals)."""
    vtr, vqr = int(curve.get("virtual_token_reserves") or 0), int(curve.get("virtual_quote_reserves") or 0)
    if vtr <= 0 or vqr <= 0:
        return None
    spot_quote = (vqr / 10 ** info["decimals"]) / (vtr / 1e6)
    supply = (int(curve.get("token_total_supply") or 0) / 1e6) or 1e9
    return {"spot_quote": spot_quote, "spot_sol": spot_quote * info["sol_per_quote"],
            "mcap_usd": spot_quote * info["usd"] * supply, "supply": supply}


def curve_reasons(curve: Optional[Dict[str, Any]], quote_mint: str) -> List[str]:
    if curve is None:
        return ["non-SOL: no pump.fun curve (or curve read failed) — no buy"]
    why = []
    if curve.get("complete"):
        why.append("migrated: curve complete")
    if curve.get("quote_mint") != quote_mint:
        why.append(f"non-SOL: curve quote {quote_symbol(curve.get('quote_mint'))} ≠ signal quote {quote_symbol(quote_mint)}")
    return why


async def decision_gates(curve: Optional[Dict[str, Any]], quote_mint: Optional[str], *, max_mcap_usd: float
                         ) -> Tuple[List[str], Dict[str, Any]]:
    """Scout-engine (decision-time) gates for a non-SOL coin. Returns (reasons, facts). Empty reasons = pass."""
    c = cfg()
    facts: Dict[str, Any] = {"quote_mint": quote_mint, "quote_symbol": quote_symbol(quote_mint)}
    if not c["enabled"]:
        return [f"non-SOL quote coin ({quote_symbol(quote_mint)}) — LIVE_SCOUT_NONSOL_ENABLED=false"], facts
    if not allowed(quote_mint):
        return [f"non-SOL quote {quote_symbol(quote_mint)} not whitelisted"], facts
    why = curve_reasons(curve, quote_mint)
    if why:
        return why, facts
    info, pr = await price(quote_mint)
    if not info:
        return [pr], facts
    m = curve_metrics(curve, info)
    if not m:
        return ["non-SOL: curve reserves unreadable"], facts
    facts.update({k: info[k] for k in ("usd", "sol_usd", "sol_per_quote", "route_dev_pct")}, **m)
    if m["mcap_usd"] >= max_mcap_usd:
        why.append(f"mcap ${m['mcap_usd']:,.0f} ≥ ${max_mcap_usd:,.0f}")
    return why, facts


async def prebuy(mint: str, quote_mint: str, size_sol: float, *, max_mcap_usd: float,
                 read_curve: Callable[[str], Awaitable[Optional[Dict[str, Any]]]],
                 quote: Callable[[str, str, int], Awaitable[Optional[int]]]) -> Tuple[bool, str, Dict[str, Any]]:
    """Live-engine gate run right before signing (after the mesh check), on FRESH data:
    whitelist, curve (exists / not complete / same quote mint), verified quote price, mcap < limit, a Jupiter buy
    route whose implied price is near the curve spot, and a Jupiter SELL route back to SOL for the tokens we'd get
    (round trip ≥ LIVE_SCOUT_NONSOL_MIN_ROUNDTRIP). simulateTransaction / reserve lock / pending_buy / corrupt state
    / kill run in the engine as for every buy."""
    c = cfg()
    facts: Dict[str, Any] = {"quote_mint": quote_mint, "quote_symbol": quote_symbol(quote_mint)}
    if not c["enabled"]:
        return False, "LIVE_SCOUT_NONSOL_ENABLED=false", facts
    if not allowed(quote_mint):
        return False, f"non-SOL quote {quote_symbol(quote_mint)} not whitelisted", facts
    try:
        curve = await read_curve(mint)
    except Exception as exc:  # noqa: BLE001
        return False, f"non-SOL pre-buy: curve read error ({type(exc).__name__})", facts
    why = curve_reasons(curve, quote_mint)
    if why:
        return False, "non-SOL pre-buy: " + "; ".join(why), facts
    info, pr = await price(quote_mint)
    if not info:
        return False, "non-SOL pre-buy: " + pr, facts
    m = curve_metrics(curve, info)
    if not m:
        return False, "non-SOL pre-buy: curve reserves unreadable", facts
    facts.update(mcap_usd=round(m["mcap_usd"], 2), spot_sol=m["spot_sol"], quote_usd=info["usd"],
                 sol_per_quote=info["sol_per_quote"])
    if m["mcap_usd"] >= max_mcap_usd:
        return False, f"non-SOL pre-buy: mcap ${m['mcap_usd']:,.0f} ≥ ${max_mcap_usd:,.0f}", facts
    lam = int(size_sol * 1e9)
    try:
        toks = await quote(WSOL, mint, lam)
    except Exception as exc:  # noqa: BLE001
        toks = None
    if not toks:
        return False, "non-SOL pre-buy: no Jupiter SOL→token route", facts
    implied = (lam / 1e9) / (toks / 1e6)
    prem = implied / m["spot_sol"] - 1 if m["spot_sol"] else None
    facts.update(route_tokens=toks, route_price_sol=implied, entry_premium_pct=round(prem * 100, 2) if prem is not None else None)
    if prem is None or prem > c["max_entry_premium"]:
        return False, (f"non-SOL pre-buy: buy route price {implied:.3e} SOL is "
                       f"{(prem or 0) * 100:+.0f}% vs curve spot (max +{c['max_entry_premium'] * 100:.0f}%)"), facts
    try:
        back = await quote(mint, WSOL, toks)
    except Exception as exc:  # noqa: BLE001
        back = None
    if not back:
        return False, "non-SOL pre-buy: no Jupiter token→SOL SELL route — would be stranded", facts
    rt = back / lam
    facts["roundtrip"] = round(rt, 4)
    if rt < c["min_roundtrip"]:
        return False, f"non-SOL pre-buy: round trip {rt * 100:.0f}% < {c['min_roundtrip'] * 100:.0f}% of size", facts
    return True, "non-SOL pre-buy ok", facts


def mark_price_sol(curve: Dict[str, Any], quote_mint: str) -> Optional[float]:
    """SOL per token for a held non-SOL position from the curve + a FRESH verified quote price (else None →
    the engine falls back to a Jupiter sell quote, then the stale-mark rules)."""
    info = cached(quote_mint)
    if not info or curve.get("complete") or curve.get("quote_mint") != quote_mint:
        return None
    m = curve_metrics(curve, info)
    return m["spot_sol"] if m else None


def summary() -> Dict[str, Any]:
    c = cfg()
    return {"enabled": c["enabled"], "fraction": c["fraction"], "max_sol": c["max_sol"],
            "quotes_allowed": {q["symbol"]: m for m, q in c["allowed"].items()},
            "prices": {v["symbol"]: {"usd": v["usd"], "age_s": round(time.time() - v["ts"], 1)} for v in _cache.values()},
            "stats": dict(stats)}
