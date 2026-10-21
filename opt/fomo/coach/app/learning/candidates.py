"""Candidate log (data/candidates.jsonl): one row per decision, entered or skipped, across all arms.

Row: {"ts_ms","cid","version","arm","source","mint","decision","entered","skip_reason","reasons",
      "ref_price_sol","detect_ms","decision_ms","latency_ms","features":{...},"excluded"?}
`skip_reason` is the first reason with numbers replaced by '#', so it groups ("mcap $# ≥ $#").
Every logged candidate gets shadow outcomes (app/learning/outcomes.py) unless excluded.
v1.17: rows with "stage" (e.g. "live_prebuy": the live buy was refused AFTER the arm decided to enter — rug check,
mesh, reserve, …) record the skip reason for that later stage; they get no outcomes of their own (the arm's
decision row already has them) and the scorecard keeps them out of the per-arm numbers.
Never raises into the caller.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from app.paths import data_path

logger = logging.getLogger("coach.learning")
_NUM = re.compile(r"\d[\d,]*(\.\d+)?")


def enabled() -> bool:
    return os.getenv("LEARNING_LOG_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}


def path() -> Path:
    return data_path("CANDIDATES_PATH", "candidates.jsonl")


def norm_reason(r: Optional[str]) -> Optional[str]:
    if not r:
        return None
    s = _NUM.sub("#", str(r))
    s = re.sub(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b", "<addr>", s)
    return s.strip()[:80]


def _clean(v: Any) -> Any:
    if isinstance(v, float):
        return round(v, 10)
    if isinstance(v, dict):
        return {k: _clean(x) for k, x in v.items() if x is not None}
    if isinstance(v, (list, tuple)):
        return [_clean(x) for x in v][:20]
    return v


def log_candidate(*, arm: str, source: str, mint: str, decision: str, reasons: List[str],
                  features: Optional[Dict[str, Any]] = None, ref_price_sol: Optional[float] = None,
                  detect_ms: Optional[int] = None, decision_ms: Optional[int] = None,
                  entered: Optional[bool] = None, excluded: Optional[str] = None,
                  track: bool = True, stage: Optional[str] = None) -> Optional[str]:
    if not enabled() or not mint:
        return None
    try:
        from app import __version__
        now = int(time.time() * 1000)
        dms = decision_ms or now
        ent = bool(entered) if entered is not None else decision in ("enter", "buy")
        row: Dict[str, Any] = {
            "ts_ms": now, "cid": uuid.uuid4().hex[:12], "version": __version__, "arm": arm, "source": source,
            "mint": mint, "decision": decision, "entered": ent,
            "skip_reason": None if ent else norm_reason((reasons or [None])[0]),
            "reasons": [str(r)[:160] for r in (reasons or [])][:6],
            "ref_price_sol": ref_price_sol, "detect_ms": detect_ms, "decision_ms": dms,
            "latency_ms": (dms - detect_ms) if detect_ms else None,
            "features": _clean(features or {}),
        }
        if excluded:
            row["excluded"] = excluded
        if stage:                     # v1.17: a later stage of an earlier decision (e.g. live_prebuy refusal)
            row["stage"] = stage
            track = False
        p = path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as fh:
            fh.write(json.dumps(row, default=str) + "\n")
        if track and not excluded:
            from app.learning.outcomes import tracker
            tracker.track(row["cid"], mint, t0_ms=dms, ref_price_sol=ref_price_sol,
                          quote_is_sol=(features or {}).get("quote_is_sol"))
        try:                          # v1.48 paper_fresh5 (PAPER ONLY) sees every decision row
            from app.paper_fresh5 import paper as _f5
            _f5.on_candidate(row)
        except Exception:  # noqa: BLE001
            pass
        return row["cid"]
    except Exception as exc:  # noqa: BLE001
        logger.warning("candidate log failed: %s", type(exc).__name__)
        return None


def rows(since_ms: int = 0, *, include_excluded: bool = False) -> Iterator[Dict[str, Any]]:
    p = path()
    if not p.exists():
        return
    with p.open() as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if int(r.get("ts_ms") or 0) < since_ms:
                continue
            if r.get("excluded") and not include_excluded:
                continue
            yield r


def curve_features(c: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Decision-time curve features from a decoded/read curve or a TradeEvent (whatever keys are present)."""
    if not c:
        return {}
    out: Dict[str, Any] = {}
    rtr = c.get("real_token_reserves")
    if isinstance(rtr, (int, float)):
        init = 793_100_000 * 10**6
        out["curve_progress_pct"] = round(max(0.0, min(100.0, (1 - rtr / init) * 100)), 2)
    rq = c.get("real_quote_reserves", c.get("real_sol_reserves"))
    if isinstance(rq, (int, float)) and c.get("quote_is_sol", True):
        out["real_sol"] = round(rq / 1e9, 4)
    for k in ("progress_pct", "price_sol", "mcap_sol", "complete", "quote_is_sol", "quote_mint", "is_mayhem_mode",
              "is_cashback_coin", "creator", "phase"):
        if k in c and c[k] is not None:
            out["curve_progress_pct" if k == "progress_pct" else k] = c[k]
    return out


def flow_features(mint: str) -> Dict[str, Any]:
    """Buy/sell flow + unique buyers from the free per-mint trade stream, when one is running for this mint."""
    try:
        from app.pump.trade_stream import streams
        st = streams.get(mint)
        if not st:
            return {}
        sm = st.summary(30.0)
        trades = list(st.trades)
        buyers = {t.get("user") for t in trades if t.get("is_buy") and t.get("user")}
        return {"stream_trades": sm.get("trades_seen"), "unique_buyers_seen": len(buyers),
                "flow_buy_sol_30s": sm.get("window_buy_sol"), "flow_sell_sol_30s": sm.get("window_sell_sol"),
                "flow_buys_30s": sm.get("window_buys"), "flow_sells_30s": sm.get("window_sells"),
                "creator_sells": sm.get("creator_sells")}
    except Exception:  # noqa: BLE001
        return {}


def sol_usd_features() -> Dict[str, Any]:
    try:
        from app.fomo_onchain import market
        c = market._cache.get(market.WSOL_MINT) or {}
        return {"sol_usd": c.get("price_usd"),
                "sol_usd_age_s": round(time.time() - c["_t"], 1) if c.get("_t") else None}
    except Exception:  # noqa: BLE001
        return {}
