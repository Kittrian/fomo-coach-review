from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, Request

from app.adapters.live_feed import adapt_batch
from app.auth import require_ingest_secret
from app.mesh import schedule_enrichment
from app.state import state

router = APIRouter(tags=["ingest"])


@router.post("/api/ingest/fomo-activity")
async def ingest_fomo_activity(
    request: Request,
    _: None = Depends(require_ingest_secret),
) -> Dict[str, Any]:
    """
    Live Feed adapter: trading_activity envelope | raw payload | TradeEventV1
    → buy cluster path. Non-buy actions soft-ignored.
    """
    try:
        body = await request.json()
    except Exception:
        return {
            "ok": False,
            "error": "invalid_json",
            "adapter": "fomo_live_feed",
            "recommend_only": True,
        }

    result = adapt_batch(body)
    for item in result["ingested"]:
        ev = item.pop("_event")
        state.record_buy(ev, channel="fomo_activity")

    clusters = {}
    mesh_scheduled = []
    for item in result["ingested"]:
        ta = item["token_address"]
        if ta not in clusters:
            clusters[ta] = state.cluster_summary(ta)
            # CA announce → warm QP Data Mesh enrichment (throttled background task)
            if schedule_enrichment(ta, item.get("chain") or "solana"):
                mesh_scheduled.append(ta)

    return {
        "ok": True,
        "adapter": "fomo_live_feed",
        "recommend_only": True,
        "auto_trade": False,
        "received": result["received"],
        "ingested_count": len(result["ingested"]),
        "ignored_count": result["ignored_count"],
        "invalid_count": result["invalid_count"],
        "ingested": result["ingested"],
        "ignored": result["ignored"],
        "invalid": result["invalid"],
        "clusters": clusters,
        "mesh_enrichment_scheduled": mesh_scheduled,
    }
