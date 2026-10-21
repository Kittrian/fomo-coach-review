"""/api/live/* — loopback-only + X-Live-Token (token lives in a 0600 file; never returned or logged)."""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.live.auth import require_live_token, require_localhost
from app.live.engine import engine

router = APIRouter(prefix="/api/live", tags=["live"], dependencies=[Depends(require_localhost), Depends(require_live_token)])


class KillBody(BaseModel):
    sell_all: bool = False


@router.get("/status")
async def live_status() -> Dict[str, Any]:
    await engine.sol_balance(cached_s=30)
    return {"ok": True, **engine.status()}


@router.post("/kill")
async def live_kill(body: KillBody = KillBody()) -> Dict[str, Any]:
    # v1.15: async so sell-all tasks are spawned on the running event loop (a sync handler runs in a threadpool
    # with no loop, so the sells were silently dropped); api_kill_sell_all is persisted in live_state.
    return {"ok": True, "kill": engine.kill(sell_all=body.sell_all)}


@router.post("/resume")
async def live_resume() -> Dict[str, Any]:
    return {"ok": True, "kill": engine.resume()}


class UnbenchBody(BaseModel):
    wallet: str


@router.get("/bench")
def live_bench() -> Dict[str, Any]:
    """v1.21 scout bench: benched wallets, streaks, open paper copies."""
    from app.live.bench import bench
    return {"ok": True, **bench.summary()}


@router.post("/bench/unbench")
def live_unbench(body: UnbenchBody) -> Dict[str, Any]:
    """v1.21 manual unbench (localhost + live token only)."""
    from app.live.bench import bench
    return {"ok": True, "unbenched": bench.unbench(body.wallet.strip(), "manual (API)")}


@router.get("/alerts")
def live_alerts(since_ms: int = Query(default=0)) -> Dict[str, Any]:
    return {"ok": True, "alerts": [a for a in engine.alerts if a["ts_ms"] > since_ms][:20]}
