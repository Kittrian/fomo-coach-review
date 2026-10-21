"""In-process demo sample loop — seeds recommend-only DEMOA/DEMOB buys when DEMO_MODE.

No FOMOAPI / Telegram / Jupiter live paths. Safe for blank-UI repair after restart
(in-memory state is wiped on every LaunchAgent bounce).
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.adapters.live_feed import adapt_batch
from app.config import settings
from app.state import state

logger = logging.getLogger("coach.demo_loop")

TOKEN_A = "REDACTED"
TOKEN_B = "REDACTED"

# Re-seed periodically so the UI keeps showing fresh activity after restarts
# and so timestamps stay inside the cluster window.
DEFAULT_INTERVAL_SEC = 90.0


class DemoLoopState:
    def __init__(self) -> None:
        self.running = False
        self.seed_count = 0
        self.last_seed_at: Optional[float] = None
        self.last_error: Optional[str] = None
        self.task: Optional[asyncio.Task] = None

    def snapshot(self) -> Dict[str, Any]:
        return {
            "enabled": settings.demo_mode,
            "running": self.running,
            "seed_count": self.seed_count,
            "last_seed_at": self.last_seed_at,
            "last_error": self.last_error,
            "interval_sec": DEFAULT_INTERVAL_SEC,
            "tokens": ["DEMOA", "DEMOB"],
            "note": (
                "DEMO_MODE sample loop — recommend-only synthetic buys; "
                "not live FOMOAPI/Telegram/Jupiter"
            ),
        }


demo_loop_state = DemoLoopState()


def _envelope(payload: dict) -> dict:
    return {
        "type": "data",
        "topicType": "trading_activity",
        "payload": payload,
    }


def _buy(
    *,
    user_id: str,
    handle: str,
    token: str,
    ticker: str,
    usd: float,
    mcap: float,
    network_id: int = 101,
) -> dict:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return _envelope(
        {
            "type": "swap_buy",
            "id": f"demo-{user_id}-{ticker}-{int(time.time() * 1000)}",
            "userId": user_id,
            "userHandle": handle,
            "ticker": ticker,
            "tokenAddress": token,
            "networkId": network_id,
            "createdAt": now,
            "usdAmount": usd,
            "marketCap": mcap,
            "price": 0.00012,
        }
    )


def build_demo_activities() -> List[dict]:
    return [
        _buy(user_id="trader-alpha", handle="alpha", token=TOKEN_A, ticker="DEMOA", usd=150, mcap=180_000),
        _buy(user_id="trader-beta", handle="beta", token=TOKEN_A, ticker="DEMOA", usd=220, mcap=185_000),
        _buy(user_id="trader-gamma", handle="gamma", token=TOKEN_A, ticker="DEMOA", usd=90, mcap=190_000),
        _buy(user_id="trader-delta", handle="delta", token=TOKEN_A, ticker="DEMOA", usd=310, mcap=195_000),
        _buy(user_id="trader-echo", handle="echo", token=TOKEN_B, ticker="DEMOB", usd=80, mcap=420_000),
        _buy(user_id="trader-foxtrot", handle="foxtrot", token=TOKEN_B, ticker="DEMOB", usd=95, mcap=430_000),
        _buy(user_id="trader-golf", handle="golf", token=TOKEN_B, ticker="DEMOB", usd=110, mcap=440_000),
    ]


def seed_once() -> Dict[str, Any]:
    """Inject synthetic Live Feed buys into CoachState (recommend-only)."""
    activities = build_demo_activities()
    result = adapt_batch({"events": activities})
    for item in result["ingested"]:
        ev = item.pop("_event")
        state.record_buy(ev, channel="demo_loop")
    demo_loop_state.seed_count += 1
    demo_loop_state.last_seed_at = time.time()
    demo_loop_state.last_error = None
    logger.info(
        "demo_loop seeded %s buys (seed #%s) — recommend-only DEMOA/DEMOB",
        len(result["ingested"]),
        demo_loop_state.seed_count,
    )
    return {
        "ingested_count": len(result["ingested"]),
        "seed_count": demo_loop_state.seed_count,
    }


async def _loop(interval_sec: float = DEFAULT_INTERVAL_SEC) -> None:
    demo_loop_state.running = True
    try:
        while True:
            await asyncio.sleep(interval_sec)
            try:
                seed_once()
            except Exception as exc:  # noqa: BLE001
                demo_loop_state.last_error = str(exc)[:200]
                logger.warning("demo_loop seed soft-fail: %s", exc)
    except asyncio.CancelledError:
        logger.info("demo_loop cancelled")
        raise
    finally:
        demo_loop_state.running = False


def start_demo_loop() -> Optional[asyncio.Task]:
    """Start background demo seed when DEMO_MODE is on. Returns task or None."""
    if not settings.demo_mode:
        logger.info("demo_loop skipped (DEMO_MODE=false)")
        return None
    if demo_loop_state.task and not demo_loop_state.task.done():
        return demo_loop_state.task
    # Immediate seed so /ui is not blank on first paint
    try:
        seed_once()
    except Exception as exc:  # noqa: BLE001
        demo_loop_state.last_error = str(exc)[:200]
        logger.warning("demo_loop initial seed soft-fail: %s", exc)
    demo_loop_state.task = asyncio.create_task(_loop(), name="coach-demo-loop")
    logger.info("demo_loop started (interval=%ss)", DEFAULT_INTERVAL_SEC)
    return demo_loop_state.task


async def stop_demo_loop() -> None:
    task = demo_loop_state.task
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    demo_loop_state.task = None
    demo_loop_state.running = False
