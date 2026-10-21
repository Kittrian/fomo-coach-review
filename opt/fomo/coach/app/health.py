from __future__ import annotations

from typing import Any, Dict

from app import __version__
from app.config import settings
from app.demo_loop import demo_loop_state
from app.feeds.optional import soft_feed_status
from app.jev_gate import jev_status_summary
from app.jupiter_exec import jupiter_status_summary
from app.state import state


def build_health() -> Dict[str, Any]:
    feeds = soft_feed_status()
    return {
        "ok": True,
        "service": "fomo-signal-coach",
        "version": __version__,
        "demo_mode": settings.demo_mode,
        "recommend_only": True,
        "auto_trade": False,
        # Priority #1 handoff flag — adapter code present in this build
        "fomo_live_feed_adapter": True,
        "demo_loop": demo_loop_state.snapshot(),
        "ingest": {
            "buy": "/api/ingest/buy",
            "fomo_activity": "/api/ingest/fomo-activity",
            "secret_required": bool(settings.ingest_secret),
        },
        "feeds": feeds,
        "jev_gate": jev_status_summary(),
        "jupiter_exec": jupiter_status_summary(),
        "stats": state.stats(),
        "recovery": {
            "rehydrated": True,
            "note": "Original box tree missing 2026-10-01; scaffold from 2026-09-30 handoff + Live Feed contracts",
        },
    }
