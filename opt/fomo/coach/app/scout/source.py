"""Scout websocket + RPC (free, isolated from the Fluffy/Telegram CA lane).

* Own websocket connection(s) (default public mainnet-beta). Per watched wallet:
  - logsSubscribe {mentions:[pump.fun volume-accumulator PDA]} (all wallets, as before);
  - v1.16, scouts + confirmers only: logsSubscribe on the PumpSwap volume-accumulator PDA (PumpSwap buys/sells
    and `multi_hop_swap`) and accountSubscribe on the wallet itself. The account sub fires on lamport changes
    only; a change of >= SCOUT_LAMPORT_TRIGGER_SOL triggers a signature lookup + getTransaction, which catches
    router / `multi_hop_curve_swap` / aggregator buys that never touch either volume accumulator.
  SOL-quoted pump.fun TradeEvents for the wallet are decoded straight from the notification logs (no RPC per
  trade). Anything else (non-SOL-quote coin, PumpSwap event, a different `user`, truncated logs) is resolved
  from the full transaction's balance changes (app/scout/txparse.py).
* Own HTTPS endpoint + httpx client (default publicnode) with token buckets: `fast` (fill-time curve reads,
  marks, token age, v1.16 trade resolution) and `bg` (first-20-buyer scans). Never the CA lane's RPC/client.
* Handlers only enqueue into the engine; the engine runs its own tasks.
"""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import time
from typing import Any, Callable, Deque, Dict, List, Optional

import httpx

from app.fomo_onchain.source import TokenBucket, _host
from app.pump import rpc
from app.pump.curve import decode_curve
from app.pump.events import logs_truncated, parse_all_logs
from app.pump.solana_pda import PUMP_PROGRAM_ID, bonding_curve_pda
from app.scout import detections
from app.scout.roster import roster
from app.scout.txparse import parse_wallet_trades

logger = logging.getLogger("coach.scout.source")


def _env(name: str, d: str) -> str:
    return os.getenv(name, d).strip()


def _ws_commitment() -> str:
    """SCOUT_WS_COMMITMENT: 'confirmed' (default, unchanged) or 'processed' (earlier notifications; a tx on a
    dropped fork can still show up, so the age/curve/mesh/rug gates and the live engine's own re-reads still apply)."""
    v = _env("SCOUT_WS_COMMITMENT", "confirmed").lower()
    return v if v in ("processed", "confirmed") else "confirmed"


def enabled() -> bool:
    return _env("SCOUT_ENABLED", "true").lower() in {"1", "true", "yes", "on"}


def _on(name: str, d: str = "false") -> bool:
    return _env(name, d).lower() in {"1", "true", "yes", "on"}


def _helius_ws_url() -> str:
    """v1.17.2: SCOUT_WS_HELIUS=true + HELIUS_API_KEY → the scout websocket's primary endpoint is Helius.
    The key lives only in the URL handed to websockets.connect — never logged (status shows the host only,
    error text is redacted)."""
    key = _env("HELIUS_API_KEY", "")
    if not _on("SCOUT_WS_HELIUS") or not key:
        return ""
    return _env("SCOUT_HELIUS_WS_BASE", "wss://mainnet.helius-rpc.com/") + "?api-key=" + key


def _redact(text: str) -> str:
    try:
        from app.logredact import redact
        return redact(text)
    except Exception:  # noqa: BLE001
        return "<redacted>"


class ScoutSource:
    def __init__(self) -> None:
        self.ws_url = _env("SCOUT_WS_URL", _env("FOMO_ONCHAIN_WS_URL", "wss://api.mainnet-beta.solana.com"))
        # v1.17.2: optional Helius primary for the websocket only; self.ws_url stays the fallback.
        self.helius_ws_url = _helius_ws_url()
        self.helius_cooldown_until = 0.0
        self.helius_fail_streak = 0
        self.helius_day: Dict[str, Any] = {"day": "", "bytes": 0}
        self.helius_max_mb_day = float(_env("SCOUT_WS_HELIUS_MAX_MB_PER_DAY", "300"))
        self.rpc_url = _env("SCOUT_RPC_URL", _env("FOMO_ONCHAIN_RPC_URL", "https://solana-rpc.publicnode.com"))
        self.per_conn = int(_env("SCOUT_SUBS_PER_CONN", "40"))
        # v1.34: cryptohub (paper/log only) subs live on their OWN shards, packed denser, so the extra wallets add at
        # most a couple of websocket connections and never share a connection with live scouts / confirmers.
        self.per_conn_ch = int(_env("CRYPTOHUB_SUBS_PER_CONN", "180"))
        self.fast = TokenBucket(float(_env("SCOUT_RPS", "4")))
        # v1.40: hot-path reads (token age / curve / tx decode) on Helius Developer RPC; publicnode stays the fallback
        _hk = _env("HELIUS_API_KEY", "") if _env("SCOUT_HOT_RPC", "").lower() == "helius" else ""
        self.hot_url = (_env("SCOUT_HOT_RPC_BASE", "https://mainnet.helius-rpc.com/") + "?api-key=" + _hk) if _hk else ""
        self.hot = TokenBucket(float(_env("SCOUT_HOT_RPS", "20")))
        self.hot_max_day = int(_env("SCOUT_HOT_MAX_CALLS_DAY", "40000"))
        self.hot_day: Dict[str, Any] = {"day": "", "n": 0}
        self.hot_cool_until = 0.0
        self.hot_ms: Deque[float] = collections.deque(maxlen=500)
        self.age_cache: "collections.OrderedDict[str, Dict[str, Any]]" = collections.OrderedDict()
        # v1.40d: full txs streamed by Helius transactionSubscribe (scouts+confirmers), keyed by signature
        self.txcache: "collections.OrderedDict[str, Any]" = collections.OrderedDict()
        self.txsub: Dict[str, Any] = {"promoted": False, "demoted": False, "compares": 0, "match": 0, "mismatch": 0,
                                      "used": 0, "verify_ok": 0, "verify_bad": 0, "saved_ms": collections.deque(maxlen=500)}
        self.bg = TokenBucket(float(_env("SCOUT_BG_RPS", "1")))
        # gap-fill poller: the free public websocket drops some logsSubscribe notifications, so scouts/confirmers
        # are also polled (getSignaturesForAddress on both UVA PDAs) on their own small budget.
        self.poll = TokenBucket(float(_env("SCOUT_POLL_RPS", "2")))
        self.max_tx_version = 1
        self.http: Optional[httpx.AsyncClient] = None
        self.shards: List[Dict[str, Any]] = []
        self.tasks: List[asyncio.Task] = []
        self.listeners: List[Callable[..., None]] = []
        self.msg_times: Deque[float] = collections.deque(maxlen=20000)
        self.stats: Dict[str, Any] = collections.defaultdict(int)
        self.last_errors: Deque[str] = collections.deque(maxlen=10)
        self.started_ms: Optional[int] = None
        self.seen: "collections.OrderedDict[str, None]" = collections.OrderedDict()
        self.sub_method: Dict[str, str] = {}        # key -> "logs" | "account"
        self.sub_kind: Dict[str, str] = {}          # key -> "pump_uva" | "amm_uva" | "wallet"
        self.lamports: Dict[str, int] = {}          # wallet -> last seen lamports (account sub)
        self._resolving: set = set()
        self._spawned: set = set()
        self.recent: Deque[Dict[str, Any]] = collections.deque(maxlen=200)

    def _err(self, where: str, exc: Any) -> None:
        self.stats[f"{where}_errors"] += 1
        self.last_errors.appendleft(_redact(f"{time.strftime('%H:%M:%S')} {where}: {type(exc).__name__} {str(exc)[:90]}"))

    # ---------------- v1.17.2 websocket endpoint choice (Helius primary, current endpoint fallback) ----------------
    def _helius_today(self) -> Dict[str, Any]:
        day = time.strftime("%Y-%m-%d", time.gmtime())
        if self.helius_day.get("day") != day:
            self.helius_day = {"day": day, "bytes": 0}
        return self.helius_day

    def _helius_usable(self) -> bool:
        if not self.helius_ws_url or time.time() < self.helius_cooldown_until:
            return False
        if self._helius_today()["bytes"] > self.helius_max_mb_day * 1e6:   # credit guard (2 credits / 0.1 MB)
            self.stats["helius_ws_daily_cap_hit"] = 1
            return False
        return True

    def _pick_ws(self) -> "tuple[str, str]":
        if self._helius_usable():
            return self.helius_ws_url, "helius"
        return self.ws_url, "fallback" if self.helius_ws_url else "default"

    def _helius_failed(self, why: str) -> None:
        self.helius_fail_streak += 1
        self.stats["helius_ws_failures"] += 1
        if self.helius_fail_streak >= int(_env("SCOUT_WS_HELIUS_FAILS_TO_FALLBACK", "2")):
            self.helius_cooldown_until = time.time() + float(_env("SCOUT_WS_HELIUS_COOLDOWN_S", "600"))
            self.helius_fail_streak = 0
            self.stats["helius_ws_fallbacks"] += 1
            logger.warning("scout ws: Helius unavailable (%s) — using the fallback endpoint for %ss", why,
                           _env("SCOUT_WS_HELIUS_COOLDOWN_S", "600"))

    def _spawn(self, coro: Any) -> None:
        try:
            t = asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            coro.close()
            return
        self._spawned.add(t)
        t.add_done_callback(self._spawned.discard)

    # ---------------- lifecycle ----------------
    def start(self) -> bool:
        if not enabled() or self.tasks or self.shards:
            return False
        roster.load()
        roster.add_fomo_wallets()
        roster.add_paper_v3_wallets()
        n_ch = roster.add_cryptohub_wallets()                # v1.34 cryptohub (paper/log only)
        self.http = httpx.AsyncClient(timeout=8.0, limits=httpx.Limits(max_connections=6))
        self.started_ms = int(time.time() * 1000)
        self._sync_subs()
        ch = [w for w in roster.by_wallet.values() if w.role == "cryptohub"]
        logger.info("scout ws: cryptohub watch %d wallets (%d added now), %d subs; roster %s", len(ch), n_ch,
                    sum(len(roster.sub_keys(w)) for w in ch), roster.counts())
        loop = asyncio.get_running_loop()
        self.tasks.append(loop.create_task(self._roster_refresh()))
        self.tasks.append(loop.create_task(self._heartbeat()))
        if float(_env("SCOUT_POLL_RPS", "2")) > 0:
            self.tasks.append(loop.create_task(self._gap_poll()))
        if self._txsub_mode() and _env("HELIUS_API_KEY", ""):
            self.tasks.append(loop.create_task(self._txsub_loop()))
        return True

    # ---------------- v1.40d: router / PumpSwap decode from the transactionSubscribe stream ----------------
    def _txsub_mode(self) -> str:
        m = _env("SCOUT_TXSUB_DECODE", "").strip().lower()
        return m if m in ("shadow", "auto", "live") else ""

    def _txsub_live(self) -> bool:
        m = self._txsub_mode()
        return m == "live" or (m == "auto" and self.txsub["promoted"] and not self.txsub["demoted"])

    def _txsub_wallets(self) -> List[str]:
        from app.scout.roster import trigger_roles
        roles = trigger_roles()
        return sorted(w.wallet for w in roster.by_wallet.values() if w.role in roles)

    async def _txsub_loop(self) -> None:
        import websockets
        url = _env("SCOUT_TXSUB_WS_BASE", "wss://mainnet.helius-rpc.com/") + "?api-key=" + _env("HELIUS_API_KEY", "")
        while self._txsub_mode():
            wallets = self._txsub_wallets()
            self.txsub_set = set(wallets)
            try:
                async with websockets.connect(url, open_timeout=10, ping_interval=20, max_size=2 ** 24) as ws:
                    await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "transactionSubscribe", "params": [
                        {"accountInclude": wallets, "failed": False, "vote": False},
                        {"commitment": "processed", "encoding": "jsonParsed", "transactionDetails": "full",
                         "maxSupportedTransactionVersion": 1}]}))
                    self.stats["txsub_connects"] += 1
                    t_chk = time.time()
                    while True:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                        except asyncio.TimeoutError:
                            raw = None
                        if raw:
                            m = json.loads(raw)
                            if m.get("method") == "transactionNotification":
                                res = (m.get("params") or {}).get("result") or {}
                                sig, inner = res.get("signature"), res.get("transaction") or {}
                                if sig and inner.get("meta") is not None:
                                    self.txcache[sig] = (int(time.time() * 1000), {
                                        "slot": res.get("slot"), "transaction": inner.get("transaction"),
                                        "meta": inner.get("meta")})
                                    self.stats["txsub_msgs"] += 1
                                    while len(self.txcache) > 3000:
                                        self.txcache.popitem(last=False)
                        if time.time() - t_chk > 600:
                            t_chk = time.time()
                            if self._txsub_wallets() != wallets:
                                break                          # roster changed -> resubscribe
            except Exception as exc:  # noqa: BLE001   type only: never the URL (it carries the key)
                self.stats["txsub_errors"] += 1
                self.last_errors.appendleft(f"{time.strftime('%H:%M:%S')} txsub: {type(exc).__name__}")
            await asyncio.sleep(2)

    async def _txsub_wait(self, sig: str) -> Optional[Dict[str, Any]]:
        wait = float(_env("SCOUT_TXSUB_WAIT_MS", "250")) / 1000
        t0 = time.time()
        while True:
            hit = self.txcache.get(sig)
            if hit is not None:
                return hit[1]
            if time.time() - t0 >= wait:
                return None
            await asyncio.sleep(0.01)

    @staticmethod
    def _trade_key(trades: List[Dict[str, Any]]) -> List[Any]:
        return sorted((t.get("mint"), bool(t.get("is_buy")), int(t.get("sol_amount") or 0), int(t.get("token_amount") or 0),
                       t.get("quote_mint"), t.get("venue")) for t in trades)

    def _txsub_log(self, row: Dict[str, Any]) -> None:
        try:
            from app.paths import data_dir
            p = _env("SCOUT_TXSUB_LOG", "") or os.path.join(str(data_dir()), "txsub_decode.jsonl")
            with open(p, "a") as fh:
                fh.write(json.dumps({"ts_ms": int(time.time() * 1000), **row}) + "\n")
        except Exception:  # noqa: BLE001
            pass

    def _txsub_compare(self, wallet: str, sig: str, streamed: Dict[str, Any], fetched: Dict[str, Any],
                       fetch_ms: float, *, verify: bool = False) -> bool:
        a, b = self._trade_key(parse_wallet_trades(streamed, wallet)), self._trade_key(parse_wallet_trades(fetched, wallet))
        ok = a == b
        st = self.txsub
        if verify:
            st["verify_ok" if ok else "verify_bad"] += 1
            if not ok:
                st["demoted"] = True                         # back to fetching until restart
        else:
            st["compares"] += 1
            st["match" if ok else "mismatch"] += 1
            st["saved_ms"].append(fetch_ms)
            n = int(_env("SCOUT_TXSUB_PROMOTE_N", "30"))
            if (self._txsub_mode() == "auto" and not st["promoted"] and st["mismatch"] == 0 and st["match"] >= n):
                st["promoted"] = True
                self._txsub_log({"kind": "promoted", "matches": st["match"]})
                logger.info("txsub decode promoted after %d matching txs", st["match"])
        self._txsub_log({"kind": "verify" if verify else "compare", "sig": sig, "match": ok, "fetch_ms": round(fetch_ms),
                         **({} if ok else {"streamed": a[:4], "fetched": b[:4]})})
        return ok

    async def _txsub_verify(self, wallet: str, sig: str, streamed: Dict[str, Any]) -> None:
        await asyncio.sleep(2.0)
        t0 = time.perf_counter()
        fetched = await self.call("getTransaction", self._get_tx_params(sig), bg=True)
        if fetched:
            self._txsub_compare(wallet, sig, streamed, fetched, (time.perf_counter() - t0) * 1000, verify=True)

    def txsub_status(self) -> Dict[str, Any]:
        st = self.txsub
        v = sorted(st["saved_ms"])
        return {"mode": self._txsub_mode() or "off", "using_stream": self._txsub_live(), "cache": len(self.txcache),
                **{k: st[k] for k in ("promoted", "demoted", "compares", "match", "mismatch", "used", "verify_ok",
                                      "verify_bad")},
                "fetch_ms_p50_avoidable": round(v[len(v) // 2]) if v else None}

    async def _heartbeat(self) -> None:
        while True:
            detections.append({"kind": "heartbeat", "connected": sum(1 for s in self.shards if s["connected"]),
                               "shards": len(self.shards)})
            await asyncio.sleep(detections.heartbeat_s())

    async def _poll_call(self, method: str, params: List[Any]) -> Any:
        await self.poll.take()
        self.stats["rpc_poll_calls"] += 1
        try:
            return await rpc.call(method, params, client=self.http, url=self.rpc_url, timeout=6.0)
        except Exception as exc:  # noqa: BLE001
            self._err("poll", exc)
            if "429" in str(exc):
                self.stats["rpc_429"] += 1
            return None

    def _get_tx_params(self, sig: str) -> List[Any]:
        return [sig, {"encoding": "json", "commitment": "confirmed", "maxSupportedTransactionVersion": self.max_tx_version}]

    async def _gap_poll(self) -> None:
        last: Dict[str, str] = {}
        while True:
            try:
                keys = [(w, k) for w in roster.by_wallet.values() if w.role in ("scout", "confirmer")
                        for k, method, _ in roster.sub_keys(w) if method == "logs"]
                if not keys:
                    await asyncio.sleep(10)
                    continue
                for w, key in keys:
                    opts: Dict[str, Any] = {"limit": 15, "commitment": "confirmed"}
                    if key in last:
                        opts["until"] = last[key]
                    else:
                        opts["limit"] = 1
                    res = await self._poll_call("getSignaturesForAddress", [key, opts])
                    if not isinstance(res, list) or not res:
                        continue
                    first = key not in last
                    last[key] = res[0]["signature"]
                    if first:
                        continue
                    now = time.time()
                    for x in reversed(res):
                        sig = x.get("signature")
                        if x.get("err") or not sig or sig in self.seen:
                            continue
                        if x.get("blockTime") and now - x["blockTime"] > 180:
                            continue
                        self.stats["poll_new_sigs"] += 1
                        tx = None
                        for attempt in range(2):
                            tx = await self._poll_call("getTransaction", self._get_tx_params(sig))
                            if tx or sig in self.seen:
                                break
                            await asyncio.sleep(1.5)
                        if sig in self.seen:
                            self.stats["poll_ws_won"] += 1
                            continue
                        logs = ((tx or {}).get("meta") or {}).get("logMessages") or []
                        if logs:
                            self.stats["poll_recovered"] += 1
                            self._on_logs(key, sig, logs, int(time.time() * 1000), path="poll", tx=tx)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._err("poll_loop", exc)
                await asyncio.sleep(5)

    async def stop(self) -> None:
        ts = self.tasks + [s["task"] for s in self.shards if s.get("task")] + list(self._spawned)
        for t in ts:
            t.cancel()
        await asyncio.gather(*ts, return_exceptions=True)
        self.tasks, self.shards = [], []
        if self.http:
            await self.http.aclose()
            self.http = None

    async def _roster_refresh(self) -> None:
        while True:
            await asyncio.sleep(300)
            if roster.add_fomo_wallets():
                self._sync_subs()

    def _sync_subs(self) -> None:
        order = {"scout": 0, "confirmer": 1, "fomo": 2, "paper_v3": 3, "watch": 4, "cryptohub": 5}
        assigned = {k for sh in self.shards for k in sh["want"]}
        for w in sorted(roster.by_wallet.values(), key=lambda w: order.get(w.role, 3)):
            for key, method, kind in roster.sub_keys(w):
                self.sub_method[key], self.sub_kind[key] = method, kind
                if key in assigned:
                    continue
                pool = "cryptohub" if w.role == "cryptohub" else "main"
                cap = self.per_conn_ch if pool == "cryptohub" else self.per_conn
                sh = next((s for s in self.shards if s.get("pool", "main") == pool and len(s["want"]) < cap), None)
                if sh is None:
                    sh = {"idx": len(self.shards), "want": [], "subs": {}, "connected": False, "reconnects": 0,
                          "pool": pool}
                    self.shards.append(sh)
                    try:
                        sh["task"] = asyncio.get_running_loop().create_task(self._ws_shard(sh))
                    except RuntimeError:
                        pass
                sh["want"].append(key)
                assigned.add(key)

    def _sub_msg(self, rid: int, key: str) -> Dict[str, Any]:
        if self.sub_method.get(key) == "account":
            return {"jsonrpc": "2.0", "id": rid, "method": "accountSubscribe",
                    "params": [key, {"encoding": "base64", "commitment": _ws_commitment()}]}
        return {"jsonrpc": "2.0", "id": rid, "method": "logsSubscribe",
                "params": [{"mentions": [key]}, {"commitment": _ws_commitment()}]}

    async def _ws_shard(self, sh: Dict[str, Any]) -> None:
        import websockets
        base_backoff = float(_env("SCOUT_WS_BACKOFF_S", "2.0"))
        backoff = base_backoff
        while True:
            pending: Dict[int, str] = {}
            sh["subs"], sh["connected"] = {}, False
            t_conn = time.time()
            url, provider = self._pick_ws()
            sh["provider"], sub_errs, opened = provider, 0, False
            try:
                async with websockets.connect(url, max_size=4 * 2**20, open_timeout=10,
                                              ping_interval=20, ping_timeout=20) as ws:
                    sh["connected"], t_conn, opened = True, time.time(), True
                    self.stats["ws_connects"] += 1
                    self.stats[f"ws_connects_{provider}"] += 1
                    next_id = 1
                    while True:
                        if provider == "helius" and not self._helius_usable():
                            break                                    # daily cap / cooldown → reconnect on fallback
                        if provider == "fallback" and self._helius_usable() and time.time() - t_conn > float(
                                _env("SCOUT_WS_HELIUS_RETRY_S", "600")):
                            break                                    # Helius back → move over on the next connect
                        subscribed = set(sh["subs"].values()) | set(pending.values())
                        for key in [k for k in sh["want"] if k not in subscribed][:10]:
                            pending[next_id] = key
                            await ws.send(json.dumps(self._sub_msg(next_id, key)))
                            next_id += 1
                            await asyncio.sleep(0.05)
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                        except asyncio.TimeoutError:
                            continue
                        recv_ms = int(time.time() * 1000)
                        self.stats["ws_msgs"] += 1
                        nbytes = len(raw)
                        self.stats["ws_bytes"] += nbytes
                        if provider == "helius":
                            self.stats["ws_bytes_helius"] += nbytes
                            self._helius_today()["bytes"] += nbytes
                        self.msg_times.append(time.time())
                        msg = json.loads(raw)
                        if "id" in msg and msg["id"] in pending:
                            key = pending.pop(msg["id"])
                            if "result" in msg:
                                sh["subs"][msg["result"]] = key
                                self.stats["sub_acks"] += 1
                                if provider == "helius":
                                    self.helius_fail_streak = 0
                            else:
                                self.stats["sub_errors"] += 1
                                sub_errs += 1
                                if provider == "helius" and sub_errs >= 3:   # e.g. plan subscription limit
                                    self.helius_fail_streak = 10
                                    self._helius_failed("subscription errors")
                                    break
                            continue
                        method = msg.get("method")
                        p = msg.get("params") or {}
                        key = sh["subs"].get(p.get("subscription"))
                        if method == "accountNotification":
                            res = p.get("result") or {}
                            val = res.get("value") or {}
                            if key and isinstance(val.get("lamports"), int):
                                self._on_account(key, val["lamports"], (res.get("context") or {}).get("slot"), recv_ms)
                            continue
                        if method != "logsNotification":
                            continue
                        val = (p.get("result") or {}).get("value") or {}
                        if not key or val.get("err") is not None:
                            self.stats["notifications_skipped"] += 1
                            continue
                        self._on_logs(key, val.get("signature"), val.get("logs") or [], recv_ms,
                                      path=self.sub_kind.get(key, "logs"))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._err("ws", exc)
                if provider == "helius" and (not opened or time.time() - t_conn < 60):
                    self._helius_failed(type(exc).__name__)
            sh["connected"] = False
            sh["subs"] = {}
            sh["reconnects"] += 1
            self.stats["ws_reconnects"] += 1
            if time.time() - t_conn > 60:   # was healthy: public RPC recycles sockets — reconnect fast
                backoff = base_backoff
            await asyncio.sleep(backoff)
            backoff = min(120.0, backoff * 2)

    # ---------------- triggers ----------------
    def _on_account(self, key: str, lamports: int, slot: Optional[int], recv_ms: int) -> None:
        """Wallet lamports changed. Big enough change → find the tx and parse it (router / multi-hop buys)."""
        w = roster.by_key.get(key)
        if not w or key != w.wallet:
            return
        self.stats["account_notifications"] += 1
        prev = self.lamports.get(w.wallet)
        self.lamports[w.wallet] = lamports
        if prev is None:
            return                                   # first notification after (re)subscribe: baseline only
        delta = lamports - prev
        if abs(delta) < float(_env("SCOUT_LAMPORT_TRIGGER_SOL", "0.05")) * 1e9:
            self.stats["lamport_small"] += 1
            return
        self.stats["lamport_triggers"] += 1
        self._spawn(self._resolve_wallet(w, slot, delta, recv_ms))

    async def _resolve_wallet(self, w: Any, slot: Optional[int], delta: int, recv_ms: int) -> None:
        await asyncio.sleep(float(_env("SCOUT_LAMPORT_RESOLVE_DELAY_S", "0.2")))   # let the logs path win first (v1.17.2: 0.6 → 0.2)
        limit = int(_env("SCOUT_SIG_LOOKUP_LIMIT", "200"))
        for attempt in range(2):
            res = await self.call("getSignaturesForAddress", [w.wallet, {"limit": limit, "commitment": "confirmed"}])
            cands = [x for x in (res or []) if not x.get("err") and x.get("signature")
                     and (slot is None or (x.get("slot") or 0) >= slot - 2)]
            fresh = [x["signature"] for x in reversed(cands) if x["signature"] not in self.seen
                     and x["signature"] not in self._resolving]
            if cands and not fresh:
                self.stats["lamport_already_seen"] += 1
                return
            if fresh:
                for sig in fresh[:3]:
                    await self._resolve_sig(w, sig, recv_ms, path="lamports")
                return
            await asyncio.sleep(1.5)
        self.stats["lamport_unresolved"] += 1

    async def _resolve_sig(self, w: Any, sig: str, recv_ms: int, *, path: str,
                           tx: Optional[Dict[str, Any]] = None) -> None:
        if sig in self._resolving:
            return
        self._resolving.add(sig)
        try:
            self._mark_seen(sig)
            streamed = None
            if tx is None and self._txsub_mode() and w.wallet in getattr(self, "txsub_set", ()):   # v1.40d stream
                streamed = await self._txsub_wait(sig)
                if streamed is None:
                    self.stats["txsub_miss"] += 1
                elif self._txsub_live():
                    tx, streamed = streamed, None
                    self.txsub["used"] += 1
                    self.stats["txsub_used"] += 1
                    if self.txsub["used"] % 10 == 1:              # 1-in-10 background re-verify vs getTransaction
                        self._spawn(self._txsub_verify(w.wallet, sig, tx))
            if tx is None:
                # v1.40: the tx is usually not 'confirmed' yet at processed-detect time; poll fast instead of 1.0 s steps
                try:
                    delays = [float(x) for x in _env("SCOUT_TX_RETRY_DELAYS", "0.25,0.25,0.3,0.4,0.5,0.75,1.0").split(",")
                              if x.strip()]
                except ValueError:
                    delays = [1.0, 1.0]
                t_tx = time.perf_counter()
                for attempt in range(len(delays) + 1):
                    tx = await self.call_hot("getTransaction", self._get_tx_params(sig))
                    if tx or attempt >= len(delays):
                        break
                    await asyncio.sleep(delays[attempt])
                if tx:
                    self.stats["tx_fetch_attempts_" + str(min(attempt + 1, 9))] += 1
                    self.tx_fetch_ms = getattr(self, "tx_fetch_ms", collections.deque(maxlen=500))
                    self.tx_fetch_ms.append((time.perf_counter() - t_tx) * 1000)
            if not tx:
                self.stats["tx_resolve_failed"] += 1
                return
            if streamed is not None:                                # v1.40d shadow: does the stream match the fetch?
                try:
                    self._txsub_compare(w.wallet, sig, streamed, tx, (time.perf_counter() - t_tx) * 1000)
                except Exception as exc:  # noqa: BLE001
                    self.stats["txsub_compare_errors"] += 1
                    self.last_errors.appendleft(f"{time.strftime('%H:%M:%S')} txsub_compare: {type(exc).__name__}")
            trades = parse_wallet_trades(tx, w.wallet)
            self.stats["tx_resolved"] += 1
            if not trades:
                self.stats["tx_no_trade"] += 1
                return
            self.stats["notifications"] += 1
            for ev in trades:
                self._emit(w, ev, sig, recv_ms, path)
        finally:
            self._resolving.discard(sig)

    def _mark_seen(self, sig: str) -> None:
        self.seen[sig] = None
        while len(self.seen) > 20000:
            self.seen.popitem(last=False)

    def _on_logs(self, key: str, sig: Optional[str], logs: List[str], recv_ms: int, *, path: str = "logs",
                 tx: Optional[Dict[str, Any]] = None) -> None:
        w = roster.by_key.get(key)
        if not w or not sig:
            return
        if sig in self.seen or sig in self._resolving:
            return
        evs = parse_all_logs(logs)
        mine = [e for e in evs if e.get("user") == w.wallet]
        direct = [e for e in mine if e.get("kind") == "pump_trade" and e.get("quote_is_sol", True)
                  and e.get("sol_amount", 0) > 0]
        needs_tx = (not direct and (mine or self.sub_kind.get(key) in ("pump_uva", "amm_uva"))) \
            or any(e not in direct for e in mine) or logs_truncated(logs)
        if direct and not needs_tx:
            self._mark_seen(sig)
            self.stats["notifications"] += 1
            for ev in direct:
                self._emit(w, {**ev, "decode_path": "logs"}, sig, recv_ms, path)
            return
        if not mine and self.sub_kind.get(key) not in ("pump_uva", "amm_uva") and not tx:
            self._mark_seen(sig)
            self.stats["no_trade_event"] += 1
            return
        # non-SOL quote / PumpSwap / other user / truncated / no event on our own UVA → full tx balance parse
        self.stats["tx_path"] += 1
        if tx is not None:
            self._spawn(self._resolve_sig(w, sig, recv_ms, path=path, tx=tx))
        else:
            self._spawn(self._resolve_sig(w, sig, recv_ms, path=path))

    def _emit(self, w: Any, ev: Dict[str, Any], sig: str, recv_ms: int, path: str) -> None:
        self.stats["buys" if ev["is_buy"] else "sells"] += 1
        self.stats[f"via_{path}"] += 1
        if ev["is_buy"]:
            w.buys += 1
        else:
            w.sells += 1
        w.last_ms = recv_ms
        row = {"kind": "trade", "wallet": w.wallet, "label": w.label, "role": w.role, "sig": sig, "mint": ev["mint"],
               "is_buy": ev["is_buy"], "sol": round((ev.get("sol_amount") or 0) / 1e9, 6), "path": path,
               "decode_path": ev.get("decode_path"), "venue": ev.get("venue"), "quote_mint": ev.get("quote_mint"),
               "quote_is_sol": ev.get("quote_is_sol"), "ix_name": ev.get("ix_name"),
               "event_version": ev.get("event_version"), "recv_ms": recv_ms}
        self.recent.appendleft(row)
        if w.role in ("scout", "confirmer"):
            detections.append(row)
        for fn in self.listeners:
            try:
                fn(w, ev, sig, recv_ms)
            except Exception as exc:  # noqa: BLE001
                self._err("listener", exc)

    # ---------------- RPC helpers (own endpoint) ----------------
    async def call(self, method: str, params: List[Any], *, bg: bool = False, timeout: float = 6.0) -> Any:
        if bg:
            from app.pump import fast_lane
            t0 = time.time()
            while fast_lane._tasks and time.time() - t0 < 15:   # background work yields to the CA lane
                await asyncio.sleep(0.25)
        await (self.bg if bg else self.fast).take()
        self.stats["rpc_bg_calls" if bg else "rpc_calls"] += 1
        try:
            return await rpc.call(method, params, client=self.http, url=self.rpc_url, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            self._err("rpc", exc)
            if "429" in str(exc):
                self.stats["rpc_429"] += 1
            return None

    async def call_hot(self, method: str, params: List[Any], *, timeout: float = 4.0) -> Any:
        """v1.40: latency-critical read. Helius Developer RPC when configured (own bucket + daily cap + 60 s cooldown on
        errors), else / on error the normal publicnode lane. A legit null (tx not landed yet) is returned as None."""
        now = time.time()
        day = time.strftime("%Y-%m-%d", time.gmtime())
        if self.hot_day["day"] != day:
            self.hot_day = {"day": day, "n": 0}
        if self.hot_url and now >= self.hot_cool_until and self.hot_day["n"] < self.hot_max_day:
            await self.hot.take()
            self.hot_day["n"] += 1
            self.stats["rpc_hot_calls"] += 1
            t0 = time.perf_counter()
            try:
                res = await rpc.call(method, params, client=self.http, url=self.hot_url, timeout=timeout)
                self.hot_ms.append((time.perf_counter() - t0) * 1000)
                return res
            except Exception as exc:  # noqa: BLE001   never log the URL (it carries the key)
                self.stats["rpc_hot_errors"] += 1
                self.last_errors.appendleft(f"{time.strftime('%H:%M:%S')} rpc_hot: {type(exc).__name__}")
                if "429" in str(exc) or "http 4" in str(exc) or "http 5" in str(exc):
                    self.hot_cool_until = now + 60
        return await self.call(method, params, timeout=max(timeout, 6.0))

    def hot_status(self) -> Dict[str, Any]:
        v = sorted(self.hot_ms)
        return {"configured": bool(self.hot_url), "calls_today": self.hot_day["n"], "max_day": self.hot_max_day,
                "cooldown_s": max(0, round(self.hot_cool_until - time.time())), "age_cache": len(self.age_cache),
                "p50_ms": round(v[len(v) // 2]) if v else None, "p90_ms": round(v[int(len(v) * 0.9)]) if v else None}

    async def read_curve(self, mint: str) -> Optional[Dict[str, Any]]:
        res = await self.call_hot("getAccountInfo", [bonding_curve_pda(mint), {"encoding": "base64",
                                                                                "commitment": "processed"}])
        acc = rpc._decode_account((res or {}).get("value"))
        if not acc or acc.get("owner") != PUMP_PROGRAM_ID:
            return None
        try:
            c = decode_curve(acc["data"])
        except ValueError:
            return None
        c["read_at_ms"] = int(time.time() * 1000)
        return c

    async def read_curves(self, mints: List[str]) -> Dict[str, Optional[Dict[str, Any]]]:
        out: Dict[str, Optional[Dict[str, Any]]] = {}
        for i in range(0, len(mints), 100):
            chunk = mints[i:i + 100]
            res = await self.call("getMultipleAccounts", [[bonding_curve_pda(m) for m in chunk],
                                                          {"encoding": "base64", "commitment": "processed"}])
            vals = (res or {}).get("value") if res else None
            if vals is None:
                continue
            now = int(time.time() * 1000)
            for m, v in zip(chunk, vals):
                acc = rpc._decode_account(v)
                try:
                    out[m] = {**decode_curve(acc["data"]), "read_at_ms": now} if acc and acc.get("owner") == PUMP_PROGRAM_ID else None
                except ValueError:
                    out[m] = None
        return out

    async def token_age(self, mint: str, *, max_pages: int = 3) -> Dict[str, Any]:
        """Creation time from the bonding-curve PDA's signature history (oldest = create).
        v1.40: Helius hot lane + per-mint cache (creation time never changes; 'older than' only grows)."""
        hit = self.age_cache.get(mint)
        if hit is not None:
            self.stats["age_cache_hits"] += 1
            if hit.get("older_than_s") and hit.get("_at"):
                return {**hit, "older_than_s": hit["older_than_s"] + time.time() - hit["_at"]}
            return dict(hit)
        r = await self._token_age(mint, max_pages=max_pages)
        if r.get("ok") and (r.get("created_s") or r.get("older_than_s")):
            self.age_cache[mint] = {**r, "_at": time.time()}
            while len(self.age_cache) > 20000:
                self.age_cache.popitem(last=False)
        return r

    async def _token_age(self, mint: str, *, max_pages: int = 3) -> Dict[str, Any]:
        pda = bonding_curve_pda(mint)
        if self.hot_url and _env("SCOUT_AGE_GTFA", "true").lower() != "false":
            fast = await self._token_age_gtfa(pda)           # v1.40b (ms-audit A): one call instead of 1-3 pages
            if fast is not None:
                return fast
            self.stats["age_gtfa_fallback"] += 1
        return await self._token_age_pager(pda, max_pages=max_pages)

    async def _token_age_gtfa(self, pda: str) -> Optional[Dict[str, Any]]:
        if time.time() < self.hot_cool_until or self.hot_day["n"] >= self.hot_max_day:
            return None
        await self.hot.take()
        self.hot_day["n"] += 1
        self.stats["age_gtfa_calls"] += 1
        t0 = time.perf_counter()
        try:
            res = await rpc.call("getTransactionsForAddress",
                                 [pda, {"transactionDetails": "signatures", "sortOrder": "asc", "limit": 21,
                                        "commitment": "confirmed"}], client=self.http, url=self.hot_url, timeout=4.0)
        except Exception as exc:  # noqa: BLE001   never log the URL
            self.stats["age_gtfa_errors"] += 1
            self.last_errors.appendleft(f"{time.strftime('%H:%M:%S')} age_gtfa: {type(exc).__name__}")
            return None
        rows = (res or {}).get("data") if isinstance(res, dict) else None
        if not rows or not rows[0].get("blockTime"):
            return None
        ms = (time.perf_counter() - t0) * 1000
        self.stats["age_gtfa_ok"] += 1
        self.age_ms = getattr(self, "age_ms", collections.deque(maxlen=500))
        self.age_ms.append(ms)
        return {"ok": True, "created_s": rows[0]["blockTime"], "create_sig": rows[0].get("signature"), "tx_count": None,
                "first_sigs": [r.get("signature") for r in rows if r.get("signature")][:21], "via": "gtfa"}

    async def _token_age_pager(self, pda: str, *, max_pages: int = 3) -> Dict[str, Any]:
        before = None
        oldest = None
        page_sigs: List[Dict[str, Any]] = []
        for page in range(max_pages):
            opts: Dict[str, Any] = {"limit": 1000, "commitment": "confirmed"}
            if before:
                opts["before"] = before
            res = await self.call_hot("getSignaturesForAddress", [pda, opts], timeout=6.0)
            if res is None:
                return {"ok": False, "reason": "rpc"}
            if not res:
                break
            page_sigs = res
            oldest = res[-1]
            if len(res) < 1000:
                return {"ok": True, "created_s": oldest.get("blockTime"), "create_sig": oldest.get("signature"),
                        "tx_count": page * 1000 + len(res), "first_sigs": [s["signature"] for s in res[::-1][:21]]}
            if oldest.get("blockTime") and time.time() - oldest["blockTime"] > 3600:
                return {"ok": True, "created_s": None, "older_than_s": time.time() - oldest["blockTime"],
                        "tx_count_gt": (page + 1) * 1000}
            before = oldest["signature"]
        if oldest is None:
            return {"ok": False, "reason": "no history"}
        return {"ok": True, "created_s": None, "unknown": True, "tx_count_gt": max_pages * 1000,
                "oldest_seen_s": oldest.get("blockTime")}

    async def tx_signer(self, sig: str) -> Optional[str]:
        tx = await self.call("getTransaction", [sig, {"encoding": "json", "commitment": "confirmed",
                                                      "maxSupportedTransactionVersion": self.max_tx_version}],
                             bg=True, timeout=8.0)
        keys = (((tx or {}).get("transaction") or {}).get("message") or {}).get("accountKeys") or []
        return keys[0] if keys else None

    def status(self) -> Dict[str, Any]:
        now = time.time()
        up = (now * 1000 - self.started_ms) / 1000 if self.started_ms else 0
        return {"enabled": enabled(), "running": bool(self.shards), "uptime_s": round(up, 1),
                "ws_host": _host(self.ws_url), "rpc_host": _host(self.rpc_url),
                "hot_rpc": {**self.hot_status(), "tx_fetch_p50_ms": (lambda v: round(v[len(v) // 2]) if v else None)(
                    sorted(getattr(self, "tx_fetch_ms", []) or [])),
                    "age_gtfa_p50_ms": (lambda v: round(v[len(v) // 2]) if v else None)(
                    sorted(getattr(self, "age_ms", []) or []))},
                "txsub_decode": self.txsub_status(),
                "ws_helius": {"configured": bool(self.helius_ws_url),
                              "usable_now": self._helius_usable() if self.helius_ws_url else False,
                              "shards_on_helius": sum(1 for s in self.shards if s.get("provider") == "helius"
                                                      and s.get("connected")),
                              "cooldown_s": max(0, round(self.helius_cooldown_until - now)),
                              "mb_today": round(self._helius_today()["bytes"] / 1e6, 2),
                              "max_mb_per_day": self.helius_max_mb_day},
                "subscription_key": "pump UVA PDA (all) + PumpSwap UVA PDA + wallet lamports (scouts/confirmers)",
                "subscription_kinds": dict(collections.Counter(self.sub_kind.get(k, "?") for s in self.shards
                                                               for k in s["want"])),
                "recent_detections": list(self.recent)[:15],
                "wallets": roster.counts(),
                "subscriptions": {"wanted": sum(len(s["want"]) for s in self.shards),
                                  "acked": sum(len(s["subs"]) for s in self.shards),
                                  "connections": len(self.shards),
                                  "connected": sum(1 for s in self.shards if s["connected"])},
                "ws_msg_rate_per_s_60s": round(sum(1 for t in self.msg_times if now - t <= 60) / 60, 3),
                "ws_msg_rate_per_s_avg": round(self.stats["ws_msgs"] / up, 3) if up else 0.0,
                "stats": dict(self.stats), "last_errors": list(self.last_errors)}


source = ScoutSource()
