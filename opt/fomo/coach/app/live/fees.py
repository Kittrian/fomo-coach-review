"""v1.38 dynamic priority fee + Sender tip planner (live buys).

- Tip market: Jito's public landed-tip percentiles (https://bundles.jito.wtf/api/v1/bundles/tip_floor), cached
  LIVE_TIP_FEED_TTL_S (60 s). Refreshed in the background; the hot path never waits on the network (it uses the last
  value, or the floor/min defaults if the feed has never answered).
- tip      = clamp(p75 x LIVE_TIP_P75_MULT, LIVE_SENDER_TIP_MIN_LAMPORTS, min(LIVE_SENDER_TIP_MAX_LAMPORTS, signer cap))
- priority = clamp(max(LIVE_PRIORITY_FLOOR_LAMPORTS, p75 x LIVE_PRIORITY_P75_MULT), floor, LIVE_PRIORITY_DYN_MAX_LAMPORTS)
- budget   = trade size x LIVE_FEE_BUDGET_BPS / 10_000 per side (50 bps = 0.5%). If tip + priority > budget the tip is
  priority is cut first (not below the floor), then the tip (not below the min).
Every plan is appended to data/fee_plans.jsonl and returned so the caller can log it with the trade.
Off switch: LIVE_FEE_DYNAMIC=false (old fixed values everywhere).
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, Dict, Optional

import httpx

TIP_FLOOR_URL = "https://bundles.jito.wtf/api/v1/bundles/tip_floor"
LAMPORTS = 1_000_000_000
_feed: Dict[str, Any] = {"t": 0.0, "p75": None, "p50": None, "p95": None, "err": None, "task": None, "ok_t": 0.0}


def _f(name: str, d: float) -> float:
    try:
        return float(os.getenv(name, str(d)))
    except ValueError:
        return d


def enabled() -> bool:
    return os.getenv("LIVE_FEE_DYNAMIC", "false").strip().lower() in {"1", "true", "yes", "on"}


def cfg() -> Dict[str, Any]:
    return {"budget_bps": _f("LIVE_FEE_BUDGET_BPS", 50.0),
            "priority_floor": int(_f("LIVE_PRIORITY_FLOOR_LAMPORTS", 50_000)),
            "priority_mult": _f("LIVE_PRIORITY_P75_MULT", 5.0),
            "priority_max": int(_f("LIVE_PRIORITY_DYN_MAX_LAMPORTS", 1_000_000)),
            "tip_min": int(_f("LIVE_SENDER_TIP_MIN_LAMPORTS", 5_000)),
            "tip_max": int(_f("LIVE_SENDER_TIP_MAX_LAMPORTS", 250_000)),
            "tip_mult": _f("LIVE_TIP_P75_MULT", 1.0),
            "dual_min": int(_f("LIVE_SENDER_DUAL_MIN_LAMPORTS", 200_000)),
            "ttl_s": _f("LIVE_TIP_FEED_TTL_S", 60.0),
            "stale_s": _f("LIVE_TIP_FEED_STALE_S", 600.0),
            # v1.45: buys only — Sender tip floor/ceiling (0.001 SOL = Helius dual-route minimum). 0 = off.
            "buy_tip_min": int(_f("LIVE_SENDER_BUY_TIP_MIN_LAMPORTS", 0)),
            "buy_tip_max": int(_f("LIVE_SENDER_BUY_TIP_MAX_LAMPORTS", 0))}


def _sol_to_lamports(v: Any) -> Optional[int]:
    try:
        return int(round(float(v) * LAMPORTS))
    except (TypeError, ValueError):
        return None


async def _refresh() -> None:
    try:
        async with httpx.AsyncClient(timeout=3.0) as cl:
            r = await cl.get(TIP_FLOOR_URL)
            j = r.json()
        row = j[0] if isinstance(j, list) and j else j
        p75 = _sol_to_lamports(row.get("landed_tips_75th_percentile"))
        if p75 is None or p75 < 0:
            raise ValueError("no p75")
        _feed.update(p75=p75, p50=_sol_to_lamports(row.get("landed_tips_50th_percentile")),
                     p95=_sol_to_lamports(row.get("landed_tips_95th_percentile")), err=None, ok_t=time.time())
    except Exception as exc:  # noqa: BLE001
        _feed["err"] = type(exc).__name__
    finally:
        _feed["t"] = time.time()


def kick() -> None:
    """Start a background refresh if the cache is older than the TTL (never blocks)."""
    if time.time() - _feed["t"] < cfg()["ttl_s"]:
        return
    t = _feed.get("task")
    if t is not None and not t.done():
        return
    try:
        _feed["t"] = time.time()          # one refresh per TTL even if it fails
        _feed["task"] = asyncio.get_running_loop().create_task(_refresh())
    except RuntimeError:
        pass


def feed() -> Dict[str, Any]:
    c = cfg()
    fresh = _feed["p75"] is not None and time.time() - _feed["ok_t"] < c["stale_s"]
    return {"p75": _feed["p75"] if fresh else None, "p50": _feed["p50"], "p95": _feed["p95"],
            "age_s": round(time.time() - _feed["ok_t"], 1) if _feed["ok_t"] else None, "err": _feed["err"],
            "fresh": fresh}


def plan(size_lamports: int, *, side: str = "buy", route: str = "jupiter", signer_tip_cap: Optional[int] = None,
         want_tip: bool = False) -> Dict[str, Any]:
    """Pure (apart from kick/log). size_lamports = SOL in for a buy."""
    kick()
    c = cfg()
    fd = feed()
    p75 = fd["p75"]
    budget = int(max(0, size_lamports) * c["budget_bps"] / 10_000)
    floor = max(0, c["priority_floor"])
    prio = floor if p75 is None else int(max(floor, p75 * c["priority_mult"]))
    prio = max(floor, min(prio, c["priority_max"]))
    tip = 0
    tmax = max(c["tip_max"], c["buy_tip_max"]) if side == "buy" and c["buy_tip_max"] > 0 else c["tip_max"]
    tip_cap = tmax if signer_tip_cap is None else min(tmax, int(signer_tip_cap))
    tmin = max(c["tip_min"], c["buy_tip_min"]) if side == "buy" else c["tip_min"]
    tip_min = min(tmin, tip_cap)                      # never above the signer's cap
    if want_tip and tip_cap > 0:
        tip = tip_min if p75 is None else int(max(tip_min, p75 * c["tip_mult"]))
        tip = max(tip_min, min(tip, tip_cap))
    capped = []
    if tip + prio > budget:              # with a tip: trim the priority first (to the floor), then the tip (to the min)
        prio = max(floor, min(prio, budget - tip))
        capped.append("priority")
        if tip + prio > budget and want_tip:
            tip = max(tip_min, budget - prio)
            capped.append("tip")
    out = {"ts_ms": int(time.time() * 1000), "side": side, "route": route, "size_lamports": int(size_lamports),
           "budget_lamports": budget, "priority_lamports": int(prio), "tip_lamports": int(tip),
           "total_lamports": int(prio + tip),
           "pct_of_size": round(100.0 * (prio + tip) / size_lamports, 4) if size_lamports else None,
           "p75_tip_feed": p75, "feed_age_s": fd["age_s"], "feed_err": fd["err"], "capped": capped,
           "dual_route": bool(want_tip and tip >= c["dual_min"])}
    _log(out)
    return out


def dual_url(url: str) -> str:
    """Sender URL without swqos_only (Jito auction + SWQoS); only used when the tip >= the Sender dual minimum."""
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
    u = urlsplit(url)
    q = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True) if k != "swqos_only"]
    return urlunsplit((u.scheme, u.netloc, u.path, urlencode(q), u.fragment))


def _log(rec: Dict[str, Any]) -> None:
    try:
        from app.paths import data_dir
        with open(os.path.join(str(data_dir()), "fee_plans.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
    except Exception:  # noqa: BLE001
        pass


def health() -> Dict[str, Any]:
    c = cfg()
    return {"enabled": enabled(), **{k: c[k] for k in ("budget_bps", "priority_floor", "priority_mult", "priority_max",
                                                     "tip_min", "tip_max", "tip_mult", "dual_min")}, "feed": feed()}
