"""fomo-onchain source: free public Solana websocket → verified FOMO top-trader buys/sells.

Design for the free tier and for the Fluffy lane's safety:
* logsSubscribe {mentions:[wallet]}. NOT the USDC token account: verified 2026-10-07 that a top wallet's USDC
  account is flooded by FOMO referral-fee inflows (every copier buy pays the trader 0.1% in USDC, e.g. ~270/h for
  #1), while the wallet itself only appears in txs it signs plus third-party dust. Dust is dropped from the
  notification's logs alone (no RPC): we only fetch txs that invoke a known FOMO route program (DFlow, Jupiter,
  Relay, …). ≤FOMO_ONCHAIN_SUBS_PER_CONN subs/connection, ≤FOMO_ONCHAIN_MAX_SUBS total, backoff + resubscribe.
* WS on its OWN endpoint (default public mainnet-beta; publicnode WS silently drops sub acks) and tx fetches
  on its OWN HTTPS endpoint (default publicnode) with its own client, so it never spends the CA lane's RPC.
* One bounded queue (drop-oldest) + ONE fetch worker behind a token bucket (FOMO_ONCHAIN_RPS); the worker and
  the verification sweep pause while a Telegram/Fluffy CA pipeline is running.
* A tx counts only if fee payer == FOMO co-signer AND the tracked wallet signed (parser.classify).
"""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import time
from typing import Any, Deque, Dict, List, Optional

import httpx

from app.fomo_onchain import SOURCE, parser
from app.fomo_onchain import market
from app.fomo_onchain.wallets import Wallet, watchlist
from app.pump import rpc

logger = logging.getLogger("coach.fomo.source")


def _env(name: str, d: str) -> str:
    return os.getenv(name, d).strip()


def enabled() -> bool:
    return _env("FOMO_ONCHAIN_ENABLED", "true").lower() in {"1", "true", "yes", "on"}


ROUTER_PREFIXES = tuple(p.strip() for p in _env(
    "FOMO_ONCHAIN_ROUTER_PREFIXES",
    "DF1ow4tspf,99vQwtBwYt,JUP6LkbZbj,DeJBGdMFa1,proVF4pM").split(",") if p.strip())


def route_in_logs(logs: List[str]) -> bool:
    """True if any `Program <id> invoke` line names a FOMO route program (DFlow/Relay/Jupiter/…)."""
    for line in logs:
        if line.startswith("Program ") and " invoke [" in line and line[8:].startswith(ROUTER_PREFIXES):
            return True
    return False


def _host(u: str) -> str:
    """Safe label — never expose a path/query that may embed an API key."""
    return u.split("://", 1)[-1].split("/", 1)[0].split("?", 1)[0]


class TokenBucket:
    def __init__(self, rps: float) -> None:
        self.interval = 1.0 / max(0.1, rps)
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def take(self) -> None:
        async with self._lock:
            now = time.monotonic()
            if self._next > now:
                await asyncio.sleep(self._next - now)
            self._next = max(now, self._next) + self.interval


class Manager:
    def __init__(self) -> None:
        self.ws_url = _env("FOMO_ONCHAIN_WS_URL", "wss://api.mainnet-beta.solana.com")
        self.rpc_url = _env("FOMO_ONCHAIN_RPC_URL", "https://solana-rpc.publicnode.com")
        self.per_conn = int(_env("FOMO_ONCHAIN_SUBS_PER_CONN", "40"))
        self.max_subs = int(_env("FOMO_ONCHAIN_MAX_SUBS", "100"))
        self.sweep_txs = int(_env("FOMO_ONCHAIN_SWEEP_TXS", "6"))
        self.sweep_ttl_s = float(_env("FOMO_ONCHAIN_SWEEP_TTL_H", "6")) * 3600
        # Solana v1 transactions are live (FOMO's relayer uses them); "Transaction version (1) is not supported"
        # otherwise. Falls back to 0 if an endpoint rejects the parameter value.
        self.max_tx_version = int(_env("FOMO_ONCHAIN_MAX_TX_VERSION", "1"))
        self.bucket = TokenBucket(float(_env("FOMO_ONCHAIN_RPS", "3")))
        self.http: Optional[httpx.AsyncClient] = None
        self.queue: Optional[asyncio.Queue] = None
        self.shards: List[Dict[str, Any]] = []
        self.tasks: List[asyncio.Task] = []
        self.seen: "collections.OrderedDict[str, None]" = collections.OrderedDict()
        self.trades: Deque[Dict[str, Any]] = collections.deque(maxlen=300)
        self.msg_times: Deque[float] = collections.deque(maxlen=5000)
        self.started_ms: Optional[int] = None
        self.sweep_state = {"running": False, "done": 0, "total": 0, "last_finished_ms": None}
        self.stats: Dict[str, Any] = collections.defaultdict(int)
        self.last_errors: Deque[str] = collections.deque(maxlen=10)

    # ---------------- lifecycle ----------------
    def start(self) -> bool:
        if not enabled() or self.tasks:
            return False
        watchlist.load_seed()
        self.http = httpx.AsyncClient(timeout=8.0, limits=httpx.Limits(max_connections=4))
        self.queue = asyncio.Queue(maxsize=int(_env("FOMO_ONCHAIN_QUEUE_MAX", "300")))
        self.started_ms = int(time.time() * 1000)
        loop = asyncio.get_running_loop()
        self.tasks = [loop.create_task(self._worker()), loop.create_task(self._sweep_loop()),
                      loop.create_task(self._sol_loop())]
        self._sync_subs()
        return True

    async def stop(self) -> None:
        for t in self.tasks:
            t.cancel()
        for sh in self.shards:
            if sh.get("task"):
                sh["task"].cancel()
        await asyncio.gather(*self.tasks, *[s["task"] for s in self.shards if s.get("task")], return_exceptions=True)
        self.tasks, self.shards = [], []
        if self.http:
            await self.http.aclose()
            self.http = None
        watchlist.save()

    def _err(self, where: str, exc: Any) -> None:
        self.stats[f"{where}_errors"] += 1
        self.last_errors.appendleft(f"{time.strftime('%H:%M:%S')} {where}: {type(exc).__name__} {str(exc)[:80]}")

    # ---------------- subscriptions ----------------
    def _sync_subs(self) -> None:
        """Assign kept wallets' USDC accounts to shards (≤per_conn each, ≤max_subs total)."""
        kept = sorted(watchlist.kept(), key=lambda w: (-w.quality, w.rank))[: self.max_subs]
        try:                                  # v1.19: the fomo_top_paper wallets stay subscribed even if a sweep drops them
            from app.paper_fomo_top import cfg as _ftp_cfg
            fc = _ftp_cfg()
            if fc["enabled"]:
                have = {w.wallet for w in kept}
                kept += [w for w in (watchlist.get(a) for a in fc["targets"]) if w and w.wallet not in have]
        except Exception:  # noqa: BLE001
            pass
        assigned = {a for sh in self.shards for a in sh["want"]}
        for w in kept:
            if w.wallet in assigned:
                continue
            sh = next((s for s in self.shards if len(s["want"]) < self.per_conn), None)
            if sh is None:
                sh = {"idx": len(self.shards), "want": [], "subs": {}, "connected": False, "reconnects": 0}
                self.shards.append(sh)
                try:
                    sh["task"] = asyncio.get_running_loop().create_task(self._ws_shard(sh))
                except RuntimeError:
                    pass
            sh["want"].append(w.wallet)
            assigned.add(w.wallet)
        self.stats["subs_capped"] = max(0, len(watchlist.kept()) - self.max_subs)

    async def _ws_shard(self, sh: Dict[str, Any]) -> None:
        import websockets
        backoff = 2.0
        while True:
            pending: Dict[int, str] = {}
            sh["subs"], sh["connected"] = {}, False
            t_conn = time.time()
            try:
                async with websockets.connect(self.ws_url, max_size=4 * 2**20, open_timeout=10,
                                              ping_interval=20, ping_timeout=20) as ws:
                    sh["connected"], t_conn = True, time.time()
                    self.stats["ws_connects"] += 1
                    next_id = 1
                    while True:
                        subscribed = set(sh["subs"].values()) | set(pending.values())
                        for key in [a for a in sh["want"] if a not in subscribed][:10]:
                            pending[next_id] = key
                            await ws.send(json.dumps({"jsonrpc": "2.0", "id": next_id, "method": "logsSubscribe",
                                                      "params": [{"mentions": [key]}, {"commitment": "confirmed"}]}))
                            next_id += 1
                            await asyncio.sleep(0.05)
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                        except asyncio.TimeoutError:
                            continue
                        self.stats["ws_msgs"] += 1
                        self.msg_times.append(time.time())
                        msg = json.loads(raw)
                        if "id" in msg and msg["id"] in pending:
                            key = pending.pop(msg["id"])
                            if "result" in msg:
                                sh["subs"][msg["result"]] = key
                                self.stats["sub_acks"] += 1
                            else:
                                self.stats["sub_errors"] += 1
                                self.last_errors.appendleft(f"sub error: {str(msg.get('error'))[:80]}")
                            continue
                        if msg.get("method") != "logsNotification":
                            continue
                        p = msg.get("params") or {}
                        key = sh["subs"].get(p.get("subscription"))
                        val = ((p.get("result") or {}).get("value") or {})
                        if not key or val.get("err") is not None or not val.get("signature"):
                            self.stats["notifications_skipped"] += 1
                            continue
                        if not route_in_logs(val.get("logs") or []):
                            self.stats["notifications_dust_filtered"] += 1
                            continue
                        self.stats["notifications"] += 1
                        self._enqueue(key, val["signature"])
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._err("ws", exc)
            sh["connected"] = False
            sh["subs"] = {}
            sh["reconnects"] += 1
            self.stats["ws_reconnects"] += 1
            # v1.13 fix: public mainnet-beta recycles sockets every few minutes (1002 "invalid status code").
            # A connection that was healthy for >60 s reconnects fast instead of compounding to 5-min gaps.
            if time.time() - t_conn > 60:
                backoff = 2.0
            await asyncio.sleep(backoff)
            backoff = min(120.0, backoff * 2)

    def _enqueue(self, key: str, sig: str) -> None:
        if sig in self.seen:
            self.stats["dup_sigs"] += 1
            return
        self.seen[sig] = None
        while len(self.seen) > 5000:
            self.seen.popitem(last=False)
        assert self.queue is not None
        if self.queue.full():
            try:
                self.queue.get_nowait()
                self.stats["queue_drops"] += 1
            except asyncio.QueueEmpty:
                pass
        self.queue.put_nowait((key, sig, int(time.time() * 1000)))

    # ---------------- RPC (own endpoint, token bucket, yields to Fluffy) ----------------
    async def _yield(self) -> None:
        from app.pump import fast_lane
        t0 = time.time()
        while fast_lane._tasks and time.time() - t0 < 15:
            self.stats["yield_waits"] += 1
            await asyncio.sleep(0.25)

    async def _rpc(self, method: str, params: List[Any]) -> Any:
        await self._yield()
        await self.bucket.take()
        self.stats["rpc_calls"] += 1
        try:
            return await rpc.call(method, params, client=self.http, url=self.rpc_url, timeout=8.0)
        except Exception as exc:  # noqa: BLE001
            self._err("rpc", exc)
            if method == "getTransaction" and "maxSupportedTransactionVersion" in str(exc) and self.max_tx_version > 0:
                self.max_tx_version = 0
            if "429" in str(exc):
                self.stats["rpc_429"] += 1
                await asyncio.sleep(5)
            return None

    async def get_tx(self, sig: str, *, retries: int = 1) -> Optional[Dict[str, Any]]:
        for i in range(retries + 1):
            tx = await self._rpc("getTransaction", [sig, {"encoding": "jsonParsed", "commitment": "confirmed",
                                                          "maxSupportedTransactionVersion": self.max_tx_version}])
            if tx:
                return tx
            self.stats["rpc_null_tx"] += 1
            if i < retries:
                await asyncio.sleep(2.0)
        return None

    # ---------------- live worker ----------------
    async def _worker(self) -> None:
        assert self.queue is not None
        while True:
            key, sig, recv_ms = await self.queue.get()
            try:
                await self._handle(key, sig, recv_ms)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._err("worker", exc)

    async def _handle(self, key: str, sig: str, recv_ms: int) -> None:
        w = watchlist.get(key) or watchlist.get(watchlist.by_ata.get(key, ""))
        if not w:
            return
        tx = await self.get_tx(sig)
        res = parser.classify(tx, w.wallet, sol_usd=market.sol_usd(), signature=sig)
        self.stats[f"kind_{res['kind']}"] += 1
        if not res.get("fomo"):
            return
        watchlist.apply_live(w, res)
        bt_ms = int((res.get("block_time") or time.time()) * 1000)
        rec = {"ts_ms": int(time.time() * 1000), "handle": w.handle, "wallet": w.wallet, "kind": res["kind"],
               "mint": res.get("mint"), "usd": res.get("usd"), "sold_fraction": res.get("sold_fraction"),
               "fill_price_usd": res.get("fill_price_usd"), "quote": res.get("quote_ccy"), "signature": sig,
               "block_ms": bt_ms, "detect_lag_ms": int(time.time() * 1000) - bt_ms,
               "notify_lag_ms": recv_ms - bt_ms, "quality": w.quality}
        self.trades.appendleft(rec)
        logger.info("fomo-onchain %s %s %s $%s", w.handle, res["kind"], (res.get("mint") or "")[:8], res.get("usd"))
        try:                                  # v1.19 fomo_top_paper (PAPER ONLY; never signs / never touches live)
            from app.paper_fomo_top import paper as _ftp
            if res["kind"] == "buy" and res.get("mint"):
                rec["paper_fomo_top"] = _ftp.on_wallet_buy(w.wallet, w.handle, res, signature=sig, block_ms=bt_ms,
                                                           detect_lag_ms=rec["detect_lag_ms"])
            elif res["kind"] == "sell" and res.get("mint"):
                rec["paper_fomo_top"] = _ftp.on_wallet_sell(w.wallet, w.handle, res["mint"],
                                                            sold_fraction=res.get("sold_fraction"), usd=res.get("usd"),
                                                            signature=sig, block_ms=bt_ms)
        except Exception as exc:  # noqa: BLE001
            self._err("paper_fomo_top", exc)
        if res["kind"] == "buy" and res.get("mint"):  # v1.22 momentum_paper cluster input (PAPER ONLY)
            try:
                from app.paper_momentum import paper as _mom
                rec["momentum"] = _mom.on_wallet_buy(w.wallet, res["mint"], source="fomo", label=w.handle, ts_ms=bt_ms)
            except Exception as exc:  # noqa: BLE001
                self._err("momentum", exc)
        if res["kind"] == "buy" and res.get("mint"):
            self.stats["buys"] += 1
            rec["ingest"] = await self._post_buy(w, res, bt_ms, rec)
        elif res["kind"] == "sell" and res.get("mint"):
            self.stats["sells"] += 1
            from app.fomo_onchain.strategy import strategy
            rec["exit_signal"] = strategy.on_trader_sell(wallet=w.wallet, handle=w.handle, mint=res["mint"],
                                                         sold_fraction=res.get("sold_fraction"), usd=res.get("usd"),
                                                         signature=sig, block_ms=bt_ms)
        if self.stats["buys"] + self.stats["sells"] and (self.stats["buys"] + self.stats["sells"]) % 10 == 0:
            watchlist.save()

    async def _post_buy(self, w: Wallet, res: Dict[str, Any], bt_ms: int, rec: Dict[str, Any]) -> Dict[str, Any]:
        """Send to the canonical /api/ingest/buy (source fomo-onchain). In-process fallback if HTTP fails."""
        from app.config import settings
        body = {"trader_id": w.wallet, "trader_handle": w.handle, "token_address": res["mint"], "chain": "solana",
                "usd_amount": res.get("usd"), "price": res.get("fill_price_usd"), "occurred_at_ms": bt_ms,
                "source": SOURCE, "source_event_id": res.get("signature"),
                "meta": {"wallet": w.wallet, "wallet_quality": w.quality, "fill_price_usd": res.get("fill_price_usd"),
                         "signature": res.get("signature"), "quote_ccy": res.get("quote_ccy"),
                         "sol_spent": res.get("sol_spent"), "detect_lag_ms": rec["detect_lag_ms"],
                         "fomo_cosigned": True}}
        url = _env("FOMO_ONCHAIN_INGEST_URL", f"http://127.0.0.1:{settings.port}/api/ingest/buy")
        headers = {"X-Ingest-Secret": settings.ingest_secret} if settings.ingest_secret else {}
        try:
            assert self.http is not None
            r = await self.http.post(url, json=body, headers=headers, timeout=5.0)
            self.stats["ingest_http_ok" if r.status_code == 200 else "ingest_http_fail"] += 1
            if r.status_code == 200:
                return {"via": "http", "status": 200, "strategy": (r.json() or {}).get("fomo_top_trader")}
        except Exception as exc:  # noqa: BLE001
            self._err("ingest_http", exc)
        from app.ingest.buy import BuyIngestBody, ingest_buy
        out = await ingest_buy(BuyIngestBody(**body), None)
        self.stats["ingest_inproc"] += 1
        return {"via": "in-process", "strategy": out.get("fomo_top_trader")}

    # ---------------- verification sweep (quality score) ----------------
    async def sweep_wallet(self, w: Wallet) -> None:
        """Verify a seeded wallet without paying for dust: signatures on the wallet ∩ signatures on its USDC account
        = txs the wallet took part in that moved its USDC (its own FOMO trades; referral-fee inflows and dust drop
        out). Fetch only the newest FOMO_ONCHAIN_SWEEP_TXS of those and verify co-signer + wallet signature.
        Public RPC keeps ~18 h of address history, so "dropped" means "no co-signed trade in that window"; the
        sweep repeats every FOMO_ONCHAIN_SWEEP_TTL_H."""
        a = await self._rpc("getSignaturesForAddress", [w.wallet, {"limit": 300, "commitment": "confirmed"}])
        if a is None:
            return   # RPC failure: stay pending, retry next sweep
        b = await self._rpc("getSignaturesForAddress", [w.usdc_ata, {"limit": 300, "commitment": "confirmed"}]) if a else []
        if b is None:
            return
        in_ata = {x["signature"] for x in b}
        ok = [x for x in a if x.get("err") is None and x["signature"] in in_ata][: self.sweep_txs]
        w.sweep_window_sigs = len(a)
        parsed = []
        for s in ok:
            while self.queue is not None and not self.queue.empty():   # live trades first
                await asyncio.sleep(0.2)
            tx = await self.get_tx(s["signature"], retries=0)
            if tx is None:
                continue
            parsed.append(parser.classify(tx, w.wallet, sol_usd=market.sol_usd(), signature=s["signature"]))
        if ok and not parsed:
            return   # all fetches failed: inconclusive, keep pending
        watchlist.apply_sweep(w, parsed)

    async def _sweep_loop(self) -> None:
        await asyncio.sleep(1.0)
        while True:
            todo = sorted([w for w in watchlist.wallets.values()
                           if w.status == "pending" or not w.swept_at_ms
                           or time.time() * 1000 - w.swept_at_ms > self.sweep_ttl_s * 1000], key=lambda w: w.rank)
            self.sweep_state.update(running=True, done=0, total=len(todo))
            for w in todo:
                try:
                    await self.sweep_wallet(w)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    self._err("sweep", exc)
                self.sweep_state["done"] += 1
                self._sync_subs()
                if self.sweep_state["done"] % 10 == 0:
                    watchlist.save()
            watchlist.save()
            self._sync_subs()
            self.sweep_state.update(running=False, last_finished_ms=int(time.time() * 1000))
            pending = any(w.status == "pending" for w in watchlist.wallets.values())
            await asyncio.sleep(120 if pending else self.sweep_ttl_s)

    async def _sol_loop(self) -> None:
        while True:
            try:
                await market.marks([], client=self.http, force=True)
            except Exception as exc:  # noqa: BLE001
                self._err("sol_price", exc)
            await asyncio.sleep(60)

    # ---------------- views ----------------
    def status(self) -> Dict[str, Any]:
        now = time.time()
        up = (now * 1000 - self.started_ms) / 1000 if self.started_ms else 0
        last60 = sum(1 for t in self.msg_times if now - t <= 60)
        return {
            "enabled": enabled(), "running": bool(self.tasks), "uptime_s": round(up, 1),
            "ws_host": _host(self.ws_url), "rpc_host": _host(self.rpc_url), "wallets": watchlist.counts(),
            "subscriptions": {"wanted": sum(len(s["want"]) for s in self.shards),
                              "acked": sum(len(s["subs"]) for s in self.shards),
                              "connections": len(self.shards),
                              "connected": sum(1 for s in self.shards if s["connected"]),
                              "capped_out": self.stats.get("subs_capped", 0)},
            "ws_msg_rate_per_s_60s": round(last60 / 60, 3),
            "ws_msg_rate_per_s_avg": round(self.stats["ws_msgs"] / up, 3) if up else 0.0,
            "queue_depth": self.queue.qsize() if self.queue else 0,
            "sweep": dict(self.sweep_state), "stats": dict(self.stats), "market": dict(market.stats),
            "sol_usd": market.sol_usd(), "last_errors": list(self.last_errors),
        }


manager = Manager()
