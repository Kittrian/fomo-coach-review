"""Free per-mint pump.fun trade stream over the standard Solana websocket (logsSubscribe).

Subscribes `logsSubscribe {"mentions":[<mint>]}` (free on public/most RPCs; one key per filter) and
decodes pump.fun TradeEvents from `Program data:` lines. A light curve poll (getAccountInfo) runs
alongside to catch graduation and refresh reserves. No PumpPortal, no paid streams, read-only.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Deque, Dict, List, Optional

from app.pump import rpc
from app.pump.curve import read_curve
from app.pump.events import event_price_sol, logs_show_migration, parse_logs

logger = logging.getLogger("coach.pump.stream")

MAX_STREAMS = int(os.getenv("PUMP_STREAM_MAX", "8"))
STREAM_TTL_S = float(os.getenv("PUMP_STREAM_TTL_S", "1800"))
CURVE_POLL_S = float(os.getenv("PUMP_CURVE_POLL_S", "5"))
STREAM_ENABLED = os.getenv("PUMP_STREAM_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}

Listener = Callable[["MintStream"], Awaitable[None]]


@dataclass
class MintStream:
    mint: str
    started_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    status: str = "starting"
    creator: Optional[str] = None
    trades: Deque[Dict[str, Any]] = field(default_factory=lambda: deque(maxlen=3000))
    last_price_sol: Optional[float] = None
    high_price_sol: Optional[float] = None
    real_sol: Optional[int] = None
    peak_real_sol: Optional[int] = None
    curve: Optional[Dict[str, Any]] = None
    migrated: bool = False
    migrated_at_ms: Optional[int] = None
    messages: int = 0
    reconnects: int = 0
    error: Optional[str] = None
    last_event_ms: Optional[int] = None
    price_ms: Optional[int] = None      # v1.15: source time of last_price_sol (trade recv / curve read); live
                                        # marks only accept a price whose source time advanced

    def apply_trade(self, ev: Dict[str, Any], *, recv_ms: Optional[int] = None) -> None:
        ev = dict(ev)
        ev["recv_ms"] = recv_ms or int(time.time() * 1000)
        if self.creator and ev.get("user") == self.creator:
            ev["by_creator"] = True
        if ev.get("creator") and not self.creator:
            self.creator = ev["creator"]
        self.trades.append(ev)
        self.last_event_ms = ev["recv_ms"]
        p = event_price_sol(ev)
        if p:
            self.last_price_sol = p
            self.high_price_sol = max(self.high_price_sol or p, p)
            self.price_ms = max(self.price_ms or 0, ev["recv_ms"])
        rs = ev.get("real_sol_reserves")
        if rs is not None:
            self.real_sol = rs
            self.peak_real_sol = max(self.peak_real_sol or rs, rs)

    def apply_curve(self, c: Dict[str, Any]) -> None:
        if c.get("status") != "ok":
            return
        self.curve = c
        self.creator = self.creator or c.get("creator")
        if c.get("complete") and not self.migrated:
            self.mark_migrated()
        p = c.get("price_sol")
        src = int(c.get("read_at_ms") or 0)
        if p and src and src <= (self.price_ms or 0):
            p = None                                   # cached / older curve read: don't overwrite a newer price
        if p:
            self.last_price_sol = p
            self.high_price_sol = max(self.high_price_sol or p, p)
            self.price_ms = src or int(time.time() * 1000)
        if not c.get("complete"):
            rs = c.get("real_quote_reserves")
            if rs is not None:
                self.real_sol = rs
                self.peak_real_sol = max(self.peak_real_sol or rs, rs)

    def mark_migrated(self) -> None:
        self.migrated = True
        self.migrated_at_ms = self.migrated_at_ms or int(time.time() * 1000)

    def summary(self, window_s: float = 30.0) -> Dict[str, Any]:
        now = int(time.time() * 1000)
        recent = [t for t in self.trades if now - t["recv_ms"] <= window_s * 1000]
        buys = sum(t["sol_amount"] for t in recent if t["is_buy"]) / 1e9
        sells = sum(t["sol_amount"] for t in recent if not t["is_buy"]) / 1e9
        return {
            "mint": self.mint, "status": self.status, "started_ms": self.started_ms,
            "creator": self.creator, "trades_seen": len(self.trades), "messages": self.messages,
            "last_price_sol": self.last_price_sol, "high_price_sol": self.high_price_sol,
            "real_sol": (self.real_sol / 1e9) if self.real_sol is not None else None,
            "peak_real_sol": (self.peak_real_sol / 1e9) if self.peak_real_sol is not None else None,
            "migrated": self.migrated, "migrated_at_ms": self.migrated_at_ms,
            "window_s": window_s, "window_buy_sol": round(buys, 4), "window_sell_sol": round(sells, 4),
            "window_buys": sum(1 for t in recent if t["is_buy"]),
            "window_sells": sum(1 for t in recent if not t["is_buy"]),
            "creator_sells": sum(1 for t in self.trades if t.get("by_creator") and not t["is_buy"]),
            "last_event_ms": self.last_event_ms, "reconnects": self.reconnects, "error": self.error,
            "transport": "logsSubscribe(mentions mint) " + rpc.rpc_label(),
        }


class StreamManager:
    def __init__(self) -> None:
        self.streams: Dict[str, MintStream] = {}
        self._tasks: Dict[str, List[asyncio.Task]] = {}
        self.listeners: List[Listener] = []
        self.pinned: set = set()      # v1.14.1: mints held by live positions — exempt from TTL expiry + eviction

    def get(self, mint: str) -> Optional[MintStream]:
        return self.streams.get(mint)

    async def _notify(self, s: MintStream) -> None:
        for fn in list(self.listeners):
            try:
                await fn(s)
            except Exception as exc:  # noqa: BLE001
                logger.warning("stream listener error: %s", exc)

    def start(self, mint: str, *, curve: Optional[Dict[str, Any]] = None) -> Optional[MintStream]:
        if not STREAM_ENABLED:
            return None
        if mint in self.streams and self.streams[mint].status not in ("stopped", "expired"):
            return self.streams[mint]
        active = [m for m, s in self.streams.items() if s.status not in ("stopped", "expired")]
        if len(active) >= MAX_STREAMS:
            evictable = [m for m in active if m not in self.pinned]
            if evictable:       # never evict a live position's stream (if all are pinned, exceed the cap)
                oldest = min(evictable, key=lambda m: self.streams[m].started_ms)
                self.stop(oldest, reason="evicted")
        s = MintStream(mint=mint)
        if curve:
            s.apply_curve(curve)
        self.streams[mint] = s
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            s.status = "no_loop"
            return s
        self._tasks[mint] = [loop.create_task(self._ws_loop(s)), loop.create_task(self._poll_loop(s))]
        return s

    def stop(self, mint: str, *, reason: str = "stopped") -> None:
        s = self.streams.get(mint)
        if s:
            s.status = "expired" if reason == "expired" else "stopped"
        for t in self._tasks.pop(mint, []):
            t.cancel()

    async def stop_all(self) -> None:
        for m in list(self._tasks):
            self.stop(m)

    def _alive(self, s: MintStream) -> bool:
        if s.mint not in self.pinned and time.time() * 1000 - s.started_ms > STREAM_TTL_S * 1000:
            self.stop(s.mint, reason="expired")
            return False
        return s.status not in ("stopped", "expired")

    async def _poll_loop(self, s: MintStream) -> None:
        while self._alive(s):
            try:
                c = await read_curve(s.mint, use_cache=False)
                was = s.migrated
                s.apply_curve(c)
                if s.migrated != was or c.get("status") == "ok":
                    await self._notify(s)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                s.error = f"poll: {str(exc)[:120]}"
            await asyncio.sleep(CURVE_POLL_S)

    async def _ws_loop(self, s: MintStream) -> None:
        import websockets

        backoff = 1.0
        while self._alive(s):
            try:
                async with websockets.connect(rpc.ws_url(), max_size=8 * 2**20, open_timeout=8,
                                              ping_interval=20) as ws:
                    await ws.send(json.dumps({
                        "jsonrpc": "2.0", "id": 1, "method": "logsSubscribe",
                        "params": [{"mentions": [s.mint]}, {"commitment": "processed"}],
                    }))
                    s.status = "subscribed"
                    s.error = None
                    backoff = 1.0
                    while self._alive(s):
                        raw = await ws.recv()
                        msg = json.loads(raw)
                        val = ((msg.get("params") or {}).get("result") or {}).get("value")
                        if not val:
                            continue
                        s.messages += 1
                        if val.get("err"):
                            continue
                        logs = val.get("logs") or []
                        now = int(time.time() * 1000)
                        evs = parse_logs(logs, s.mint)
                        for ev in evs:
                            s.apply_trade(ev, recv_ms=now)
                        mig = logs_show_migration(logs)
                        if mig:
                            s.mark_migrated()
                        if evs or mig:
                            await self._notify(s)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                s.status = "reconnecting"
                s.error = f"ws: {type(exc).__name__}: {str(exc)[:100]}"
                s.reconnects += 1
                await asyncio.sleep(backoff)
                backoff = min(30.0, backoff * 2)

    def status(self) -> Dict[str, Any]:
        return {
            "enabled": STREAM_ENABLED, "max_streams": MAX_STREAMS, "ttl_s": STREAM_TTL_S,
            "transport": "solana websocket logsSubscribe (free)", "rpc": rpc.rpc_label(),
            "active": [s.summary() for s in self.streams.values() if s.status not in ("stopped", "expired")],
        }


streams = StreamManager()
