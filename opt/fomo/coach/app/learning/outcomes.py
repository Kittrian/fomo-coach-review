"""Shadow outcomes for every logged candidate: price at +0, +1, +5, +15, +60 min (LEARNING_HORIZONS_S).

Price source order: pump.fun bonding curve (one getMultipleAccounts per batch of due mints, SOL-quoted curves
only), then a Jupiter quote fallback (0.1 SOL → token, no taker, nothing signed) for migrated / non-SOL-quote /
non-pump coins, capped at LEARNING_JUP_PER_MIN. Own httpx client + token bucket (LEARNING_RPS, default 1/s) on the
scout's free RPC, so it never touches the Telegram CA lane's budget. At +0 it also records cheap holder features
(top-10 % excl. the bonding curve, creator %) from one getTokenLargestAccounts call (LEARNING_HOLDERS_PER_MIN).

Rows (data/candidate_outcomes.jsonl):
  {"ts_ms","cid","mint","kind":"outcome","horizon_s","price_sol","src":"curve|jupiter|none|missed","late_ms",
   "ret"(vs the +0 price observed by this job; ref_price_sol when +0 missed),"ret_vs_ref","complete"}
  {"ts_ms","cid","mint","kind":"holders","top10_pct","creator_pct","holders_seen"}
"""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

import httpx

from app.paths import data_path

logger = logging.getLogger("coach.learning.outcomes")


def _env(name: str, d: str) -> str:
    return os.getenv(name, d).strip()


def horizons() -> List[int]:
    try:
        return sorted({int(x) for x in _env("LEARNING_HORIZONS_S", "0,60,300,900,3600").split(",") if x.strip()})
    except ValueError:
        return [0, 60, 300, 900, 3600]


def path() -> Path:
    return data_path("CANDIDATE_OUTCOMES_PATH", "candidate_outcomes.jsonl")


class _Rate:
    """Simple per-minute cap."""

    def __init__(self, per_min: float) -> None:
        self.per_min = per_min
        self.ts: Deque[float] = collections.deque()

    def ok(self) -> bool:
        now = time.time()
        while self.ts and now - self.ts[0] > 60:
            self.ts.popleft()
        if len(self.ts) >= self.per_min:
            return False
        self.ts.append(now)
        return True


class OutcomeTracker:
    def __init__(self) -> None:
        self.items: Dict[str, Dict[str, Any]] = {}      # cid -> {mint, t0_ms, ref, due:[h], quote_is_sol, p0}
        self.stats: Dict[str, int] = collections.defaultdict(int)
        self.http: Optional[httpx.AsyncClient] = None
        self.task: Optional[asyncio.Task] = None
        self.bucket = None
        self.jup = _Rate(float(_env("LEARNING_JUP_PER_MIN", "12")))
        self.holders = _Rate(float(_env("LEARNING_HOLDERS_PER_MIN", "10")))
        self.resumed = False

    # ---------------- inputs ----------------
    def track(self, cid: str, mint: str, *, t0_ms: int, ref_price_sol: Optional[float],
              quote_is_sol: Optional[bool] = None, due: Optional[List[int]] = None) -> None:
        self.items[cid] = {"mint": mint, "t0_ms": int(t0_ms), "ref": ref_price_sol, "quote_is_sol": quote_is_sol,
                           "due": list(due if due is not None else horizons()), "p0": None}
        self.stats["tracked"] += 1
        self.ensure_loop()

    def ensure_loop(self) -> None:
        if _env("LEARNING_OUTCOMES_ENABLED", "true").lower() not in {"1", "true", "yes", "on"}:
            return
        if self.task and not self.task.done():
            return
        try:
            self.task = asyncio.get_running_loop().create_task(self._loop())
        except RuntimeError:
            pass

    def resume(self) -> int:
        """After a restart: re-queue candidates whose horizons are not all recorded yet."""
        if self.resumed:
            return 0
        self.resumed = True
        from app.learning.candidates import rows as cand_rows
        hs = horizons()
        since = int(time.time() * 1000) - (max(hs) + 900) * 1000
        done: Dict[str, set] = collections.defaultdict(set)
        p0: Dict[str, float] = {}
        for r in self._outcome_rows(since):
            if r.get("kind") == "outcome":
                done[r["cid"]].add(int(r["horizon_s"]))
                if int(r["horizon_s"]) == 0 and r.get("price_sol"):
                    p0[r["cid"]] = r["price_sol"]
        n = 0
        for c in cand_rows(since):
            if c.get("stage"):
                continue                       # v1.17: later-stage rows have no outcomes of their own
            left = [h for h in hs if h not in done[c["cid"]]]
            if left:
                self.track(c["cid"], c["mint"], t0_ms=c.get("decision_ms") or c["ts_ms"], ref_price_sol=c.get("ref_price_sol"),
                           quote_is_sol=(c.get("features") or {}).get("quote_is_sol"), due=left)
                self.items[c["cid"]]["p0"] = p0.get(c["cid"])
                n += 1
        self.stats["resumed"] += n
        return n

    def _outcome_rows(self, since_ms: int):
        p = path()
        if not p.exists():
            return
        with p.open() as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if int(r.get("ts_ms") or 0) >= since_ms:
                    yield r

    def _write(self, row: Dict[str, Any]) -> None:
        try:
            p = path()
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a") as fh:
                fh.write(json.dumps({"ts_ms": int(time.time() * 1000), **row}, default=str) + "\n")
        except Exception as exc:  # noqa: BLE001
            logger.warning("outcome write failed: %s", type(exc).__name__)

    # ---------------- loop ----------------
    async def _loop(self) -> None:
        from app.fomo_onchain.source import TokenBucket
        self.bucket = self.bucket or TokenBucket(float(_env("LEARNING_RPS", "1")))
        while self.items:
            try:
                await self.step()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.stats["loop_errors"] += 1
                logger.warning("outcome loop error: %s", type(exc).__name__)
            await asyncio.sleep(float(_env("LEARNING_TICK_S", "5")))

    def due_now(self, now_ms: Optional[int] = None) -> List[tuple]:
        now_ms = now_ms or int(time.time() * 1000)
        out = []
        for cid, it in list(self.items.items()):
            for h in list(it["due"]):
                if now_ms >= it["t0_ms"] + h * 1000:
                    out.append((cid, h))
        return out

    async def step(self, now_ms: Optional[int] = None) -> int:
        now_ms = now_ms or int(time.time() * 1000)
        due = self.due_now(now_ms)
        if not due:
            return 0
        live, missed = [], []
        for cid, h in due:
            it = self.items[cid]
            late = now_ms - (it["t0_ms"] + h * 1000)
            (missed if late > max(120_000, h * 250) else live).append((cid, h, late))
        for cid, h, late in missed:
            self._write({"cid": cid, "mint": self.items[cid]["mint"], "kind": "outcome", "horizon_s": h,
                         "price_sol": None, "src": "missed", "late_ms": late})
            self._done(cid, h)
            self.stats["missed"] += 1
        if live:
            mints = sorted({self.items[c]["mint"] for c, _, _ in live})
            prices = await self.prices(mints)
            for cid, h, late in live:
                it = self.items[cid]
                pr = prices.get(it["mint"]) or {}
                px = pr.get("price_sol")
                if h == 0 and px:
                    it["p0"] = px
                # ret is measured against the +0 price observed by this same job (same pipeline/units); the decision-time
                # ref price (often FOMO/DEX mcap-derived, can lag) is kept separately as ret_vs_ref.
                base = it.get("p0") or it["ref"]
                self._write({"cid": cid, "mint": it["mint"], "kind": "outcome", "horizon_s": h, "price_sol": px,
                             "src": pr.get("src", "none"), "late_ms": late, "complete": pr.get("complete"),
                             "ret": round(px / base - 1, 4) if px and base else None,
                             "ret_base": "p0" if it.get("p0") else ("ref" if it["ref"] else None),
                             "ret_vs_ref": round(px / it["ref"] - 1, 4) if px and it["ref"] else None})
                self.stats["recorded" if px else "no_price"] += 1
                if h == 0:
                    await self._holders(cid, it["mint"], pr.get("creator"))
                self._done(cid, h)
        return len(due)

    def _done(self, cid: str, h: int) -> None:
        it = self.items.get(cid)
        if not it:
            return
        if h in it["due"]:
            it["due"].remove(h)
        if not it["due"]:
            self.items.pop(cid, None)

    # ---------------- prices ----------------
    async def _client(self) -> httpx.AsyncClient:
        if self.http is None or self.http.is_closed:
            self.http = httpx.AsyncClient(timeout=8.0, limits=httpx.Limits(max_connections=3))
        return self.http

    def _rpc_url(self) -> str:
        return _env("LEARNING_RPC_URL", _env("SCOUT_RPC_URL", _env("FOMO_ONCHAIN_RPC_URL", "https://solana-rpc.publicnode.com")))

    async def _rpc(self, method: str, params: List[Any]) -> Any:
        from app.fomo_onchain.source import TokenBucket
        from app.pump import rpc
        self.bucket = self.bucket or TokenBucket(float(_env("LEARNING_RPS", "1")))
        await self.bucket.take()
        self.stats["rpc_calls"] += 1
        try:
            return await rpc.call(method, params, client=await self._client(), url=self._rpc_url(), timeout=8.0)
        except Exception as exc:  # noqa: BLE001
            self.stats["rpc_errors"] += 1
            if "429" in str(exc):
                self.stats["rpc_429"] += 1
            return None

    async def prices(self, mints: List[str]) -> Dict[str, Dict[str, Any]]:
        from app.pump import rpc
        from app.pump.curve import curve_price_sol, decode_curve
        from app.pump.solana_pda import PUMP_PROGRAM_ID, bonding_curve_pda
        out: Dict[str, Dict[str, Any]] = {}
        for i in range(0, len(mints), 100):
            chunk = mints[i:i + 100]
            res = await self._rpc("getMultipleAccounts", [[bonding_curve_pda(m) for m in chunk],
                                                          {"encoding": "base64", "commitment": "confirmed"}])
            for m, v in zip(chunk, (res or {}).get("value") or [None] * len(chunk)):
                acc = rpc._decode_account(v)
                if not acc or acc.get("owner") != PUMP_PROGRAM_ID:
                    continue
                try:
                    c = decode_curve(acc["data"])
                except ValueError:
                    continue
                if c.get("complete") or not c.get("quote_is_sol", True):
                    out[m] = {"complete": c.get("complete"), "creator": c.get("creator"), "need_fallback": True}
                    continue
                px = curve_price_sol(c)
                if px:
                    out[m] = {"price_sol": px, "src": "curve", "complete": False, "creator": c.get("creator")}
        for m in mints:
            if (out.get(m) or {}).get("price_sol"):
                continue
            if not self.jup.ok():
                self.stats["jup_capped"] += 1
                continue
            q = await self._jupiter_price(m)
            if q:
                out[m] = {**(out.get(m) or {}), "price_sol": q, "src": "jupiter"}
                out[m].pop("need_fallback", None)
        return out

    async def _jupiter_price(self, mint: str) -> Optional[float]:
        try:
            from app.pump.paper import _jupiter_quote
            from app.pump.solana_pda import WSOL_MINT
            lam = int(float(_env("LEARNING_QUOTE_SOL", "0.1")) * 1e9)
            q = await asyncio.wait_for(asyncio.to_thread(_jupiter_quote, WSOL_MINT, mint, lam), timeout=10)
            self.stats["jup_calls"] += 1
            if q.get("ok") and q.get("out_amount"):
                dec = await self._decimals(mint)
                return (lam / 1e9) / (q["out_amount"] / 10 ** dec)
        except Exception:  # noqa: BLE001
            self.stats["jup_errors"] += 1
        return None

    async def _decimals(self, mint: str) -> int:
        """Token decimals for the Jupiter fallback (pump.fun = 6; other launchpads may differ). Cached per mint.
        v1.17: read from the mint account (byte 44) with getAccountInfo — the free publicnode RPC refuses
        getTokenSupply ("indexed request"), which silently left every non-6-decimal coin at 6."""
        cache = self.__dict__.setdefault("_dec", {})
        if mint in cache:
            return cache[mint]
        dec = 6
        try:
            import base64
            res = await self._rpc("getAccountInfo", [mint, {"encoding": "base64", "dataSlice": {"offset": 44, "length": 1},
                                                            "commitment": "confirmed"}])
            raw = base64.b64decode((((res or {}).get("value") or {}).get("data") or [""])[0])
            if raw and 0 <= raw[0] <= 12:
                dec = raw[0]
                cache[mint] = dec
        except Exception:  # noqa: BLE001
            self.stats["decimals_errors"] += 1
        return dec

    async def _holders(self, cid: str, mint: str, creator: Optional[str]) -> None:
        if _env("LEARNING_HOLDERS_ENABLED", "true").lower() not in {"1", "true", "yes", "on"} or not self.holders.ok():
            return
        if _env("LEARNING_HOLDERS_SRC", "jupiter").lower() == "jupiter":
            await self._holders_jup(cid, mint)
            return
        from app.pump.solana_pda import (TOKEN_2022_PROGRAM_ID, TOKEN_PROGRAM_ID, associated_token_address,
                                         bonding_curve_pda)
        res = await self._rpc("getTokenLargestAccounts", [mint, {"commitment": "confirmed"}])
        vals = (res or {}).get("value") or []
        if not vals:
            return
        bc = bonding_curve_pda(mint)
        skip = {associated_token_address(bc, mint, p) for p in (TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID)}
        cre = {associated_token_address(creator, mint, p) for p in (TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID)} if creator else set()
        supply = 1_000_000_000 * 10 ** int(vals[0].get("decimals") or 6)
        amts = [(v.get("address"), int(v.get("amount") or 0)) for v in vals]
        others = [a for addr, a in amts if addr not in skip]
        top10 = sum(sorted(others, reverse=True)[:10])
        cr = sum(a for addr, a in amts if addr in cre)
        self._write({"cid": cid, "mint": mint, "kind": "holders", "top10_pct": round(top10 / supply * 100, 2),
                     "creator_pct": round(cr / supply * 100, 3) if creator else None,
                     "creator_in_top20": bool(cr) if creator else None, "holders_seen": len(vals)})
        self.stats["holders"] += 1

    async def _holders_jup(self, cid: str, mint: str) -> None:
        """v1.17: holder features from Jupiter's free token audit (top holders %, dev balance %, dev mints, holder
        count). v1.16 used getTokenLargestAccounts, which the free publicnode RPC refuses (HTTP 403), so no holder
        row was ever written."""
        try:
            from app.live.rugcheck import jup_audit
            a = await jup_audit(mint, await self._client())
        except Exception:  # noqa: BLE001
            a = None
        if not a:
            self.stats["holders_unavailable"] += 1
            return
        self._write({"cid": cid, "mint": mint, "kind": "holders", "src": "jupiter_audit",
                     "top10_pct": a.get("top_holders_pct"), "creator_pct": a.get("dev_balance_pct"),
                     "dev_mints": a.get("dev_mints"), "holders_seen": a.get("holder_count")})
        self.stats["holders"] += 1

    def status(self) -> Dict[str, Any]:
        return {"pending": len(self.items), "horizons_s": horizons(), "stats": dict(self.stats),
                "running": bool(self.task and not self.task.done())}

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        if self.http:
            await self.http.aclose()
            self.http = None


tracker = OutcomeTracker()
