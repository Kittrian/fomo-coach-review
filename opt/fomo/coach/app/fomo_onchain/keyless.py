"""fomoapi_keyless adapter — wss://api.fomoapi.io/ws/alerts (public, keyless, ~60 s delayed).

ANNOTATION ONLY: maps FOMO handle → wallet (confirming/contradicting the top100 seed by joining alerts to
our on-chain fills) and attaches the alert text as a thesis on the mint. NEVER an entry trigger: alerts are
not posted to /api/ingest/buy and never open paper positions. Dedup on eventId. Never sends FOMOAPI_KEY or
any token; no key/IP rotation. Backs off 5 s → 300 s and reports `unavailable` if the feed disappears.
fomoapi.io terms: no redistribution — theses stay local to this coach.
"""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import time
from typing import Any, Deque, Dict, List, Optional

logger = logging.getLogger("coach.fomo.keyless")

JOIN_WINDOW_S = 90
JOIN_USD_TOL = 0.20


def enabled() -> bool:
    return os.getenv("FOMOAPI_KEYLESS_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}


class Keyless:
    def __init__(self) -> None:
        self.url = os.getenv("FOMOAPI_KEYLESS_WS_URL", "wss://api.fomoapi.io/ws/alerts?chain=solana")
        self.seen: "collections.OrderedDict[str, None]" = collections.OrderedDict()
        self.alerts: Deque[Dict[str, Any]] = collections.deque(maxlen=300)
        self.handle_map: Dict[str, Dict[str, Any]] = {}
        self.task: Optional[asyncio.Task] = None
        self.state = "idle"
        self.delay_s: Optional[float] = None
        self.stats: Dict[str, int] = collections.defaultdict(int)
        self.consecutive_failures = 0
        self.last_error: Optional[str] = None

    def start(self) -> bool:
        if not enabled() or (self.task and not self.task.done()):
            return False
        self.task = asyncio.get_running_loop().create_task(self._run())
        return True

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        self.state = "stopped"

    async def _run(self) -> None:
        import websockets
        backoff = 5.0
        while True:
            try:
                self.state = "connecting"
                # plain connect: no auth header, no key, no cookies
                async with websockets.connect(self.url, open_timeout=10, ping_interval=20, ping_timeout=20,
                                              max_size=2 * 2**20) as ws:
                    self.state = "connected"
                    self.stats["connects"] += 1
                    t_ok = time.time()
                    async for raw in ws:
                        self.stats["msgs"] += 1
                        try:
                            self.on_message(json.loads(raw))
                        except Exception as exc:  # noqa: BLE001
                            self.stats["parse_errors"] += 1
                            self.last_error = f"parse: {type(exc).__name__}"
                        if time.time() - t_ok > 60:
                            backoff, self.consecutive_failures = 5.0, 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.stats["errors"] += 1
                self.last_error = f"{type(exc).__name__}: {str(exc)[:100]}"
            self.consecutive_failures += 1
            self.state = "unavailable" if self.consecutive_failures >= 3 else "reconnecting"
            await asyncio.sleep(backoff)
            backoff = min(300.0, backoff * 2)

    # ---------------- message handling (pure-ish; unit-tested) ----------------
    def on_message(self, m: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not isinstance(m, dict):
            return None
        if m.get("delaySeconds") is not None:
            self.delay_s = m.get("delaySeconds")
        if not m.get("alertType") and not m.get("eventId"):
            self.stats["control"] += 1
            return None
        eid = str(m.get("eventId") or m.get("tradeId") or "")
        if not eid:
            self.stats["no_event_id"] += 1
            return None
        if eid in self.seen:
            self.stats["dups"] += 1
            return None
        self.seen[eid] = None
        while len(self.seen) > 10_000:
            self.seen.popitem(last=False)
        if (m.get("chain") or "solana").lower() not in ("solana", "sol"):
            self.stats["other_chain"] += 1
            return None
        a = {"event_id": eid, "type": m.get("alertType"), "handle": m.get("trader"), "mint": m.get("tokenAddress"),
             "token": m.get("token"), "usd": m.get("usdValue"), "realized_pnl_usd": m.get("realizedPnlUsd"),
             "text": (m.get("text") or "")[:240], "ts": m.get("ts"), "replay": bool(m.get("replay")),
             "recv_ms": int(time.time() * 1000)}
        self.stats["alerts"] += 1
        a["join"] = self.join(a)
        self.alerts.appendleft(a)
        if a["mint"]:
            from app.fomo_onchain.strategy import strategy
            strategy.annotate(a["mint"], {"source": "fomoapi-keyless (60s delayed)", "handle": a["handle"],
                                          "type": a["type"], "usd": a["usd"], "text": a["text"], "ts": a["ts"]})
        return a

    @staticmethod
    def _ts_ms(ts: Any) -> Optional[int]:
        if ts is None:
            return None
        try:
            v = float(ts)
            return int(v if v > 1e12 else v * 1000)
        except (TypeError, ValueError):
            pass
        try:
            from datetime import datetime
            return int(datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp() * 1000)
        except ValueError:
            return None

    def join(self, a: Dict[str, Any], trades: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        """Match alert ↔ on-chain fill (same mint+side, |Δt| ≤ 90 s, buy USD within 20%) → handle→wallet map.

        Copy-trade clusters make several handles buy the same mint at the same size within seconds (seen live:
        3 handles ≈$5k on one mint), so a size/time match alone is NOT identity. Confirm only when the alert's
        handle owns the matching fill; otherwise record a weak candidate, and flag a seed mismatch only after
        ≥3 independent matches of the same handle to the same wallet."""
        if trades is None:
            from app.fomo_onchain.source import manager
            trades = list(manager.trades)
        typ = str(a.get("type") or "").lower()
        side = "buy" if "buy" in typ else "sell" if "sell" in typ else None
        ts = self._ts_ms(a.get("ts"))
        if not a.get("mint") or not side or ts is None:
            return {"status": "n/a"}
        from app.fomo_onchain.wallets import watchlist
        handle = (a.get("handle") or "").lstrip("@")
        matches = []
        for t in trades:
            if t.get("mint") != a["mint"] or t.get("kind") != side or abs(t["block_ms"] - ts) > JOIN_WINDOW_S * 1000:
                continue
            # buys: usdValue = trade size → must agree within 20%. Sells: usdValue is REALIZED PnL (verified live
            # 2026-10-07, e.g. "sold $TITS (+$1K realized)", negative on losses) → not comparable to fill size.
            if side == "buy" and a.get("usd") and t.get("usd") \
                    and abs(t["usd"] - float(a["usd"])) > JOIN_USD_TOL * abs(float(a["usd"])):
                continue
            matches.append(t)
        if not matches:
            self.stats["joins_unmatched"] += 1
            return {"status": "unmatched"}
        entry = self.handle_map.setdefault(handle.lower(), {"handle": handle, "wallets": {}, "confirmed": 0, "candidate": 0})
        own = next((t for t in matches if (watchlist.get(t["wallet"]) or None) and
                    watchlist.get(t["wallet"]).handle.lower() == handle.lower()), None)
        if own:
            entry["confirmed"] += 1
            entry["wallets"][own["wallet"]] = entry["wallets"].get(own["wallet"], 0) + 1
            watchlist.get(own["wallet"]).handle_confirmed = True
            self.stats["joins_confirmed"] += 1
            return {"status": "confirmed", "wallet": own["wallet"], "signature": own.get("signature")}
        if len(matches) == 1:
            t = matches[0]
            entry["candidate"] += 1
            n = entry["wallets"][t["wallet"]] = entry["wallets"].get(t["wallet"], 0) + 1
            w = watchlist.get(t["wallet"])
            if w and n >= 3 and not w.handle_confirmed:
                w.handle_confirmed, w.alt_handle = False, handle
            self.stats["joins_candidate"] += 1
            return {"status": "candidate", "wallet": t["wallet"], "matches_for_pair": n}
        self.stats["joins_ambiguous"] += 1
        return {"status": "ambiguous", "n": len(matches)}

    def status(self) -> Dict[str, Any]:
        return {"enabled": enabled(), "state": self.state, "delay_s": self.delay_s, "stats": dict(self.stats),
                "last_error": self.last_error, "entry_trigger": False, "use": "handle→wallet map + thesis annotation only",
                "handle_map": list(self.handle_map.values())[:50]}


keyless = Keyless()
