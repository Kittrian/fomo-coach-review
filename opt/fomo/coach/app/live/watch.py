"""v1.17 open-position account watcher: accountSubscribe on each open live position's bonding curve (or, after
migration, its PumpSwap pool vaults) so a gap-down / liquidity pull reaches the exit logic on the next account
write instead of the next poll. Free standard Solana websocket; read-only.

If the socket is down, stale (no message for LIVE_WATCH_STALE_S) or disabled, LiveEngine falls back to polling every
LIVE_MARK_FAST_S (~1 s). With LIVE_WATCH_ENABLED=false nothing here runs and the old 2 s poll is unchanged.
"""
from __future__ import annotations

import asyncio
import base64
import collections
import json
import logging
import os
import time
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

logger = logging.getLogger("coach.live.watch")

Callback = Callable[[str, str, bytes, Optional[int]], Awaitable[None]]


def _on(name: str, d: str) -> bool:
    return os.getenv(name, d).strip().lower() in {"1", "true", "yes", "on"}


def enabled() -> bool:
    return _on("LIVE_WATCH_ENABLED", "true")


def ws_url() -> str:
    """LIVE_WATCH_WS_URL, else the paid LIVE_FAST_WS_URL only when LIVE_FAST_ENABLED=true, else the coach's free WS."""
    from app.pump import rpc
    explicit = os.getenv("LIVE_WATCH_WS_URL", "").strip()
    if explicit:
        return explicit
    if _on("LIVE_FAST_ENABLED", "false") and os.getenv("LIVE_FAST_WS_URL", "").strip():
        return os.getenv("LIVE_FAST_WS_URL", "").strip()
    return rpc.ws_url()


class AccountWatch:
    def __init__(self) -> None:
        self.wanted: Dict[str, Tuple[str, str]] = {}      # account -> (mint, kind)
        self.task: Optional[asyncio.Task] = None
        self.callback: Optional[Callback] = None
        self.connected = False
        self.last_msg_ms: Optional[int] = None
        self.connected_ms: Optional[int] = None
        self.stats: Dict[str, int] = collections.defaultdict(int)
        self._gen = 0
        self.unwatched = 0                                 # v1.37 open positions with no watchable account

    def healthy(self, stale_s: Optional[float] = None) -> bool:
        if not enabled() or not self.connected or not self.wanted:
            return False
        stale_s = stale_s if stale_s is not None else float(os.getenv("LIVE_WATCH_STALE_S", "20"))
        ref = self.last_msg_ms or self.connected_ms or 0
        return time.time() * 1000 - ref <= stale_s * 1000

    def sync(self, wanted: Dict[str, Tuple[str, str]], callback: Callback) -> None:
        """Set the accounts to watch; (re)starts the socket when the set changes. Empty set → stop."""
        if not enabled():
            return
        self.callback = callback
        if wanted == self.wanted and self.task and not self.task.done():
            return
        self.wanted = dict(wanted)
        self._gen += 1
        if self.task and not self.task.done():
            self.task.cancel()
        self.connected = False
        if not self.wanted:
            return
        try:
            self.task = asyncio.get_running_loop().create_task(self._run(self._gen))
        except RuntimeError:
            self.task = None

    async def _run(self, gen: int) -> None:
        import websockets
        backoff = 1.0
        while gen == self._gen and self.wanted:
            try:
                async with websockets.connect(ws_url(), max_size=8 * 2**20, open_timeout=8, ping_interval=20) as ws:
                    ids: Dict[int, str] = {}
                    for i, acc in enumerate(list(self.wanted), start=1):
                        ids[i] = acc
                        await ws.send(json.dumps({"jsonrpc": "2.0", "id": i, "method": "accountSubscribe",
                                                  "params": [acc, {"encoding": "base64", "commitment": "processed"}]}))
                    subs: Dict[int, str] = {}
                    self.connected, self.connected_ms = True, int(time.time() * 1000)
                    self.stats["connects"] += 1
                    backoff = 1.0
                    while gen == self._gen:
                        msg = json.loads(await ws.recv())
                        if "id" in msg and "result" in msg and msg["id"] in ids:
                            subs[int(msg["result"])] = ids[msg["id"]]
                            continue
                        if "id" in msg and "error" in msg and msg["id"] in ids:
                            self.stats["subscribe_errors"] += 1
                            continue
                        if msg.get("method") != "accountNotification":
                            continue
                        p = msg.get("params") or {}
                        acc = subs.get(p.get("subscription"))
                        if not acc or acc not in self.wanted:
                            continue
                        self.last_msg_ms = int(time.time() * 1000)
                        self.stats["notifications"] += 1
                        val = (p.get("result") or {}).get("value") or {}
                        slot = ((p.get("result") or {}).get("context") or {}).get("slot")
                        try:
                            data = base64.b64decode((val.get("data") or [""])[0])
                        except Exception:  # noqa: BLE001
                            continue
                        mint, kind = self.wanted[acc]
                        if self.callback:
                            try:
                                await self.callback(mint, kind, data, slot)
                            except Exception as exc:  # noqa: BLE001
                                self.stats["callback_errors"] += 1
                                logger.warning("watch callback %s: %s", kind, type(exc).__name__)
            except asyncio.CancelledError:
                self.connected = False
                raise
            except Exception as exc:  # noqa: BLE001
                self.connected = False
                self.stats["errors"] += 1
                logger.info("live watch socket: %s", type(exc).__name__)
                await asyncio.sleep(backoff)
                backoff = min(30.0, backoff * 2)
        self.connected = False

    def status(self) -> Dict[str, Any]:
        return {"enabled": enabled(), "connected": self.connected, "healthy": self.healthy(),
                "accounts": len(self.wanted), "unwatched_positions": self.unwatched,
                "last_msg_ms": self.last_msg_ms, "stats": dict(self.stats)}

    async def stop(self) -> None:
        self._gen += 1
        self.wanted = {}
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)


watch = AccountWatch()
