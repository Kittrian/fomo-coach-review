from __future__ import annotations

import json
import logging
import os
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.auth import require_ingest_secret
from app.mesh import schedule_enrichment
from app.state import BuyEvent, state
from app.pump import fast_lane
from app.fomo_onchain import LOW_PRIORITY_SOURCES, SMUG_SOURCE
from app.scout.engine import engine as scout_engine
from app.scout.filters import filters as scout_filters
from app.fomo_onchain import strategy as fomo_strategy

router = APIRouter(tags=["ingest"])

# v1.12.1 dedupe: the CA fast lane (curve/mesh/JEV/quote/paper) runs once per (source, mint).
# Repeats still record the buy (cluster counter / notes) but never re-run the pipeline or touch paper.
_DEDUPE_MAX = 5000
_seen_pipeline: "OrderedDict[Tuple[str, str], int]" = OrderedDict()
# v1.15: the dedupe survives restarts (a restart used to make every repeat call look "first seen").
from app.paths import data_path  # noqa: E402
SEEN_PATH = data_path("INGEST_SEEN_PATH", "ingest_seen.json")
_log = logging.getLogger("coach.ingest")


def _load_seen() -> None:
    try:
        rows = json.loads(SEEN_PATH.read_text()) if SEEN_PATH.exists() else []
        for src, mint, n in rows[-_DEDUPE_MAX:]:
            _seen_pipeline[(str(src), str(mint))] = int(n)
    except Exception as exc:  # noqa: BLE001
        _log.warning("ingest dedupe: could not load %s (%s) — starting empty", SEEN_PATH.name, type(exc).__name__)


def _save_seen() -> None:
    try:
        SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = SEEN_PATH.with_name(SEEN_PATH.name + ".tmp")
        tmp.write_text(json.dumps([[k[0], k[1], v] for k, v in _seen_pipeline.items()]))
        tmp.replace(SEEN_PATH)
    except Exception as exc:  # noqa: BLE001
        _log.warning("ingest dedupe: could not save (%s)", type(exc).__name__)


_load_seen()


def _dedupe_enabled() -> bool:
    return os.getenv("COACH_INGEST_DEDUPE", "true").strip().lower() in {"1", "true", "yes", "on"}


def _note_seen(source: str, mint: str) -> int:
    """Return how many times (source, mint) was seen BEFORE this call, and record this one."""
    key = ((source or "").strip().lower(), mint)
    prev = _seen_pipeline.get(key, 0)
    _seen_pipeline[key] = prev + 1
    _seen_pipeline.move_to_end(key)
    while len(_seen_pipeline) > _DEDUPE_MAX:
        _seen_pipeline.popitem(last=False)
    if prev == 0:
        _save_seen()                    # persist only when a NEW (source, mint) key is added
    return prev


class BuyIngestBody(BaseModel):
    trader_id: str = Field(..., min_length=1)
    trader_handle: Optional[str] = None
    token_address: str = Field(..., min_length=1)
    token_symbol: Optional[str] = None
    chain: str = "solana"
    usd_amount: Optional[float] = None
    market_cap: Optional[float] = None
    price: Optional[float] = None
    occurred_at_ms: Optional[int] = None
    source: str = "manual"
    source_event_id: Optional[str] = None
    meta: Dict[str, Any] = Field(default_factory=dict)


@router.post("/api/ingest/buy")
async def ingest_buy(body: BuyIngestBody, _: None = Depends(require_ingest_secret)) -> Dict[str, Any]:
    """Canonical buy ingest used by watchlist bridges and Live Feed adapter."""
    ev = BuyEvent(
        trader_id=body.trader_id.strip(),
        trader_handle=(body.trader_handle or body.trader_id).strip(),
        token_address=body.token_address.strip(),
        token_symbol=(body.token_symbol or "?").strip(),
        chain=(body.chain or "unknown").strip().lower(),
        usd_amount=body.usd_amount,
        market_cap=body.market_cap,
        price=body.price,
        occurred_at_ms=body.occurred_at_ms or int(time.time() * 1000),
        source=body.source,
        source_event_id=body.source_event_id,
        meta=body.meta,
    )
    ingest_ms = int(time.time() * 1000)
    state.record_buy(ev, channel="buy")
    if ev.source == SMUG_SOURCE:
        # v1.13 arm C (smugcalls_control): record + scout paper engine only. No curve fast lane, mesh, JEV,
        # Jupiter or Fluffy paper — ~95 calls/day must never compete with the Fluffy/Zyrox CA lane.
        recv = ev.meta.get("tg_recv_ms") if isinstance(ev.meta.get("tg_recv_ms"), (int, float)) else ingest_ms
        msg_ms = ev.meta.get("tg_message_ms") if isinstance(ev.meta.get("tg_message_ms"), (int, float)) else None
        from app.ingest.recap import recap_reason
        recap = recap_reason(str(ev.meta.get("snippet") or ev.meta.get("text") or ""))
        if recap:
            # v1.16 data hygiene: a recap/brag post about an old call is not a new call — never reaches arm C
            scout_engine.note_recap(ev.token_address, recap, meta=ev.meta)
            return {"ok": True, "recommend_only": True, "auto_trade": False, "low_priority": True,
                    "event": {"trader_id": ev.trader_id, "token_address": ev.token_address, "source": ev.source},
                    "scout_arm_c": {"queued": False, "reason": recap}, "filtered": "recap_post",
                    "fast_lane_scheduled": False, "mesh_enrichment_scheduled": False}
        handled = scout_engine.on_smugcall(ev.token_address, recv_ms=int(recv), message_ms=msg_ms, meta=ev.meta)
        return {"ok": True, "recommend_only": True, "auto_trade": False, "low_priority": True,
                "event": {"trader_id": ev.trader_id, "token_address": ev.token_address, "source": ev.source},
                "scout_arm_c": handled, "fast_lane_scheduled": False, "mesh_enrichment_scheduled": False}
    if ev.source in ("telegram", "fluffy/telegram") or ev.source.endswith("/telegram"):
        scout_filters.note_telegram(ev.source, ev.token_address, ev.meta)   # scheduled-launch skip filter
    if ev.source in LOW_PRIORITY_SOURCES:
        # v1.12 low-priority lane: record + hand to the fomo_top_trader paper profile only.
        # No curve fast lane, mesh, JEV or Jupiter — those budgets stay reserved for the Telegram CA lane.
        handled = fomo_strategy.on_ingested_buy(ev)
        return {"ok": True, "recommend_only": True, "auto_trade": False, "low_priority": True,
                "event": {"trader_id": ev.trader_id, "token_address": ev.token_address, "source": ev.source},
                "fomo_top_trader": handled, "fast_lane_scheduled": False, "mesh_enrichment_scheduled": False}
    if _dedupe_enabled():
        prev = _note_seen(ev.source, ev.token_address)
        if prev > 0:
            return {"ok": True, "recommend_only": True, "auto_trade": False, "duplicate": True,
                    "first_seen": False, "repeat_count": prev + 1,
                    "event": {"trader_id": ev.trader_id, "token_address": ev.token_address,
                              "token_symbol": ev.token_symbol, "chain": ev.chain, "source": ev.source},
                    "cluster": state.cluster_summary(ev.token_address),
                    "fast_lane_scheduled": False, "mesh_enrichment_scheduled": False,
                    "latency": f"/api/latency/{ev.token_address}"}
    # CA fast lane: curve read first (≈100 ms), then mesh / JEV / quote — JEV advisory on curve coins
    fast_scheduled = fast_lane.schedule(ev.token_address, source=ev.source, meta=ev.meta,
                                        event_id=ev.source_event_id, ingest_ms=ingest_ms, chain=ev.chain)
    # CA announce → warm QP Data Mesh enrichment in the background (throttled; never blocks ingest)
    pool = next((ev.meta.get(k) for k in ("pool", "pool_address", "pair_address") if isinstance(ev.meta.get(k), str)), None)
    mesh_scheduled = schedule_enrichment(ev.token_address, ev.chain, pool=pool)
    cluster = state.cluster_summary(ev.token_address)
    return {
        "ok": True,
        "recommend_only": True,
        "auto_trade": False,
        "duplicate": False,
        "first_seen": True,
        "event": {
            "trader_id": ev.trader_id,
            "token_address": ev.token_address,
            "token_symbol": ev.token_symbol,
            "chain": ev.chain,
            "source": ev.source,
        },
        "cluster": cluster,
        "mesh_enrichment_scheduled": mesh_scheduled,
        "fast_lane_scheduled": fast_scheduled,
        "latency": f"/api/latency/{ev.token_address}",
    }
