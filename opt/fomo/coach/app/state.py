from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional

from app.config import settings


@dataclass
class BuyEvent:
    trader_id: str
    trader_handle: str
    token_address: str
    token_symbol: str
    chain: str
    usd_amount: Optional[float]
    market_cap: Optional[float]
    price: Optional[float]
    occurred_at_ms: int
    source: str
    source_event_id: Optional[str] = None
    meta: Dict[str, Any] = field(default_factory=dict)


class CoachState:
    """In-memory buy clusters (rehydrate scaffold — replace with durable store later)."""

    def __init__(self, window_minutes: int = 180) -> None:
        self.window_ms = window_minutes * 60_000
        self._lock = threading.RLock()
        self.buys: Deque[BuyEvent] = deque(maxlen=50_000)
        self.ingest_counts: Dict[str, int] = defaultdict(int)
        self.last_ingest_at: Dict[str, float] = {}
        self.started_at = time.time()
        self.auto_trade_enabled = False  # product lock: recommend-only

    def record_buy(self, event: BuyEvent, *, channel: str) -> None:
        with self._lock:
            self.buys.append(event)
            self.ingest_counts[channel] += 1
            self.last_ingest_at[channel] = time.time()

    def recent_buys(self, token_address: Optional[str] = None) -> List[BuyEvent]:
        now = int(time.time() * 1000)
        cutoff = now - self.window_ms
        with self._lock:
            out = [b for b in self.buys if b.occurred_at_ms >= cutoff]
        if token_address:
            ta = token_address.lower()
            out = [b for b in out if b.token_address.lower() == ta]
        return out

    def cluster_summary(self, token_address: str) -> Dict[str, Any]:
        buys = self.recent_buys(token_address)
        traders = {b.trader_id for b in buys}
        return {
            "token_address": token_address,
            "buy_count": len(buys),
            "unique_traders": len(traders),
            "trader_handles": sorted({b.trader_handle for b in buys if b.trader_handle})[:20],
        }

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "buy_events_buffered": len(self.buys),
                "ingest_counts": dict(self.ingest_counts),
                "last_ingest_at": dict(self.last_ingest_at),
                "uptime_sec": round(time.time() - self.started_at, 1),
                "auto_trade_enabled": self.auto_trade_enabled,
            }


state = CoachState(window_minutes=settings.cluster_window_minutes)
