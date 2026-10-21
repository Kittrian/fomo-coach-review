"""v1.17 wallet scoring — ADVISORY ONLY. Nothing in the coach reads this to change weights, add or drop wallets.

Scores every watched wallet (scouts, confirmers, kept FOMO wallets) from real data:
  * on-chain: the wallet's recent pump.fun / PumpSwap trades (signatures on its two user_volume_accumulator PDAs — only
    trades touch them, so the dust spam that floods the wallet address itself is skipped — plus the USDC account for
    FOMO wallets), parsed with the v1.16 tx parser; prices +5/+15/+60 min after each buy from free GeckoTerminal
    minute candles (pump.fun curve pool = bonding-curve PDA, PumpSwap pool = canonical pool PDA);
  * our learning-log outcomes (candidates whose scout_wallet is this wallet; +5/+15/+60 min shadow returns);
  * our paper/scout books (positions attributed to the wallet; rows marked `excluded` are dropped).

Returns are the copier's view: entry = close of the minute candle containing the wallet's buy, exit = last close at or
before entry + horizon (a minute with no trades carries the previous close). Returns are clipped to [-100%, +500%]
for means so one moonshot cannot dominate; medians are unclipped.

Output: data/wallet_scores.json (daily), GET /api/wallets/scores, scripts/wallet_report.py.
Dead-wallet flags are RECOMMENDATIONS for Andrew: no trade in WALLET_DEAD_DAYS (7) days, or n ≥ 20 scored buys with
a 95% interval that says the record is poor. No automatic weighting, adding or dropping.
"""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import math
import os
import random
import statistics
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

from app.paths import data_dir, data_path

logger = logging.getLogger("coach.learning.wallets")

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
WSOL = "So11111111111111111111111111111111111111112"
HORIZONS_MIN = (5, 15, 60)
GT_BASE = "https://api.geckoterminal.com/api/v2/networks/solana"
CLIP = (-1.0, 5.0)
MIGRATION_NEAR_PX = 3.0e-7


def _env(name: str, d: str) -> str:
    return os.getenv(name, d).strip()


def _f(name: str, d: float) -> float:
    try:
        return float(_env(name, str(d)))
    except ValueError:
        return d


ALGO = 3   # bump when the scoring method changes; the daily job recomputes a file written by an older method


def cfg() -> Dict[str, Any]:
    # mainnet-beta keeps full address history; publicnode refuses these "indexed" calls without a token and only
    # serves ~1 day of getTransaction, so it is NOT a valid scoring endpoint
    rpc_url = _env("WALLET_SCORES_RPC_URL", "https://api.mainnet-beta.solana.com")
    urls = [u.strip() for u in _env("WALLET_SCORES_RPC_URLS", "").split(",") if u.strip()] or [rpc_url]
    return {
        "enabled": _env("WALLET_SCORES_ENABLED", "true").lower() in {"1", "true", "yes", "on"},
        "rpc_url": urls[0],
        "rpc_urls": urls,                                   # each endpoint gets its own rps budget + 429 cooldown
        "rps": _f("WALLET_SCORES_RPS", 1.5),                # per endpoint
        "concurrency": max(1, int(_f("WALLET_SCORES_CONCURRENCY", 1))),   # wallets fetched in parallel
        "gt_window": max(75, min(1000, int(_f("WALLET_SCORES_GT_WINDOW", 1000)))),  # candles per grouped GT call
        "gt_lead_min": max(0, int(_f("WALLET_SCORES_GT_LEAD_MIN", 240))),   # early fetches reach this far past a buy
        "days": _f("WALLET_SCORES_DAYS", 7),
        "max_txs": int(_f("WALLET_SCORES_MAX_TXS", 40)),
        "max_scored_buys": int(_f("WALLET_SCORES_MAX_BUYS", 25)),
        "gt_per_min": _f("WALLET_SCORES_GT_PER_MIN", 20),
        "refresh_h": _f("WALLET_SCORES_REFRESH_H", 24),
        "dead_days": _f("WALLET_DEAD_DAYS", 7),
        "poor_min_n": int(_f("WALLET_POOR_MIN_N", 20)),
        "prior_n": _f("WALLET_SCORE_PRIOR_N", 10),
    }


def path() -> Path:
    return data_path("WALLET_SCORES_PATH", "wallet_scores.json")


def cache_path() -> Path:
    return data_path("WALLET_SCORES_CACHE_PATH", "wallet_scores_cache.json")


# ---------------- stats helpers ----------------
def wilson(k: int, n: int, z: float = 1.96) -> Optional[List[float]]:
    if n <= 0:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(max(0.0, c - h), 4), round(min(1.0, c + h), 4)]


def boot_ci(xs: List[float], fn=statistics.median, *, iters: int = 1000, seed: int = 7) -> Optional[List[float]]:
    if len(xs) < 3:
        return None
    rnd = random.Random(seed)
    vals = sorted(fn(rnd.choices(xs, k=len(xs))) for _ in range(iters))
    return [round(vals[int(0.025 * iters)], 4), round(vals[int(0.975 * iters) - 1], 4)]


def clip(x: float) -> float:
    return max(CLIP[0], min(CLIP[1], x))


# ---------------- roster ----------------
def roster() -> List[Dict[str, Any]]:
    """Watched wallets: scout_wallets.json (scouts + confirmers) + kept FOMO wallets (fomo_onchain_wallets.json)."""
    from app.scout.roster import amm_uva_pda, uva_pda
    out: List[Dict[str, Any]] = []
    seen = set()
    try:
        seed = json.loads((data_dir() / "scout_wallets.json").read_text())
        for w in seed.get("wallets", []):
            a = w.get("address")
            if a and a not in seen:
                seen.add(a)
                out.append({"wallet": a, "label": w.get("label"), "role": w.get("role"),
                            "keys": [uva_pda(a), amm_uva_pda(a)]})
    except Exception as exc:  # noqa: BLE001
        logger.warning("wallet roster: scout seed unreadable (%s)", type(exc).__name__)
    try:
        st = json.loads((data_dir() / "fomo_onchain_wallets.json").read_text())
        for w in st.get("wallets", []):
            a = w.get("wallet")
            if not a or a in seen or w.get("status") != "kept":
                continue
            seen.add(a)
            keys = [uva_pda(a), amm_uva_pda(a)]
            out.append({"wallet": a, "label": w.get("handle"), "role": "fomo", "keys": keys,
                        "usdc_ata": w.get("usdc_ata"),
                        "fomo_rank": w.get("rank"), "fomo_quality": w.get("quality")})
    except Exception as exc:  # noqa: BLE001
        logger.warning("wallet roster: fomo state unreadable (%s)", type(exc).__name__)
    try:                       # v1.34 cryptohub Discord wallets: scored daily like the rest (ADVISORY; live weight 0)
        ch = json.loads((data_dir() / "cryptohub_wallets.json").read_text())
        for w in ch.get("wallets", []):
            a = w.get("address")
            if not a or a in seen:
                continue
            seen.add(a)
            out.append({"wallet": a, "label": w.get("label"), "role": "cryptohub", "keys": [uva_pda(a), amm_uva_pda(a)],
                        "x_handle": w.get("x_handle"), "prior": w.get("prior"), "paper_only": True, "live_weight": 0.0})
    except FileNotFoundError:
        pass
    except Exception as exc:  # noqa: BLE001
        logger.warning("wallet roster: cryptohub seed unreadable (%s)", type(exc).__name__)
    return out


# ---------------- internal books ----------------
def _jsonl(p: Path):
    if not p.exists():
        return
    with p.open() as fh:
        for line in fh:
            try:
                yield json.loads(line)
            except ValueError:
                continue


def internal_by_wallet(since_ms: int) -> Dict[str, Dict[str, Any]]:
    """Learning-log outcomes + paper/scout positions attributed to a wallet (excluded rows dropped)."""
    out: Dict[str, Dict[str, Any]] = collections.defaultdict(lambda: {"cand": [], "paper": []})
    try:
        from app.learning.candidates import rows as cand_rows
        from app.learning.outcomes import path as out_path
        cands = {}
        for r in cand_rows(since_ms):
            w = (r.get("features") or {}).get("scout_wallet")
            if w and not r.get("stage"):
                cands[r["cid"]] = (w, r)
        rets: Dict[str, Dict[int, float]] = collections.defaultdict(dict)
        for o in _jsonl(out_path()):
            if o.get("kind") == "outcome" and o.get("cid") in cands and o.get("ret") is not None:
                rets[o["cid"]][int(o["horizon_s"])] = o["ret"]
        for cid, (w, r) in cands.items():
            out[w]["cand"].append({"arm": r.get("arm"), "mint": r.get("mint"), "entered": r.get("entered"),
                                   "ret_5m": rets[cid].get(300), "ret_15m": rets[cid].get(900),
                                   "ret_60m": rets[cid].get(3600)})
    except Exception as exc:  # noqa: BLE001
        logger.warning("wallet scores: learning log unreadable (%s)", type(exc).__name__)
    # scout book (paper) — positions carry scout_wallet
    for r in _jsonl(data_dir() / "scout_trades.jsonl"):
        if r.get("kind") != "pos" or r.get("excluded") or (r.get("pos") or {}).get("excluded"):
            continue
        p = r.get("pos") or {}
        if p.get("scout_wallet") and p.get("status") == "closed" and int(p.get("entry_ms") or 0) >= since_ms:
            out[p["scout_wallet"]]["paper"].append({"book": "scout", "id": p.get("id"),
                                                    "pnl_pct": (r.get("pnl") or {}).get("pnl_pct")})
    # FOMO paper book — open rows carry meta.source_wallets; close rows carry the result
    opens, excluded = {}, set()
    for r in _jsonl(data_dir() / "paper_trades.jsonl"):
        if r.get("kind") == "excluded" or r.get("excluded"):
            excluded.add(r.get("id"))
        elif r.get("kind") == "open":
            opens[r.get("id")] = (r.get("meta") or {}).get("source_wallets") or []
    for r in _jsonl(data_dir() / "paper_trades.jsonl"):
        if r.get("kind") == "close" and r.get("id") not in excluded and int(r.get("ts_ms") or 0) >= since_ms:
            for w in opens.get(r.get("id")) or []:
                out[w]["paper"].append({"book": "fomo", "id": r.get("id"),
                                        "pnl_pct": (r.get("pnl") or {}).get("pnl_pct")})
    return out


# ---------------- on-chain + candles ----------------
class Collector:
    """Rate-limited, cached collector. RPC: own client + token bucket (never the live/scout budget)."""

    def __init__(self, c: Optional[Dict[str, Any]] = None) -> None:
        self.c = c or cfg()
        self.http: Optional[httpx.AsyncClient] = None
        self._next_rpc = 0.0
        self._next_gt = 0.0
        self.eps = [{"url": u, "next": 0.0, "cool": 0.0, "fails": 0} for u in (self.c.get("rpc_urls") or [self.c["rpc_url"]])]
        self._lock = asyncio.Lock()
        self._gt_lock = asyncio.Lock()
        self.stats: Dict[str, int] = collections.defaultdict(int)
        self.cache = self._load_cache()

    def _load_cache(self) -> Dict[str, Any]:
        try:
            d = json.loads(cache_path().read_text())
            if isinstance(d, dict):
                d.setdefault("tx", {})
                d.setdefault("candles", {})
                # pre-fix entries dropped non-SOL-quote candles to [] — refetch those
                d["candles"] = {k: v for k, v in d["candles"].items()
                                if (v or {}).get("c") or "q" in (v or {}) or (v or {}).get("seg")}
                return d
        except Exception:  # noqa: BLE001
            pass
        return {"tx": {}, "candles": {}}

    def save_cache(self, keep_days: float = 10) -> None:
        cut = time.time() - keep_days * 86400
        tx = {k: v for k, v in self.cache["tx"].items() if (v or {}).get("bt", 0) >= cut}
        cd = {k: v for k, v in self.cache["candles"].items() if (v or {}).get("t", 0) >= cut}
        p = cache_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps({"tx": tx, "candles": cd}))
        tmp.replace(p)

    async def client(self) -> httpx.AsyncClient:
        if self.http is None or self.http.is_closed:
            self.http = httpx.AsyncClient(timeout=20.0, limits=httpx.Limits(max_connections=4))
        return self.http

    async def close(self) -> None:
        if self.http:
            await self.http.aclose()
            self.http = None

    async def _wait(self, attr: str, interval: float, lock: asyncio.Lock) -> None:
        async with lock:
            now = time.monotonic()
            nxt = getattr(self, attr)
            if nxt > now:
                await asyncio.sleep(nxt - now)
            setattr(self, attr, max(now, nxt) + interval)

    async def _endpoint(self) -> Dict[str, Any]:
        """Next endpoint with budget: each URL keeps its own min interval (1/rps) and its own 429 cooldown."""
        interval = 1.0 / max(0.1, self.c["rps"])
        async with self._lock:
            while True:
                now = time.monotonic()
                ep = min(self.eps, key=lambda e: max(e["next"], e["cool"]))
                ready = max(ep["next"], ep["cool"])
                if ready <= now:
                    ep["next"] = now + interval
                    return ep
                await asyncio.sleep(ready - now)

    async def rpc(self, method: str, params: List[Any], *, tries: int = 4) -> Any:
        from app.pump import rpc as _rpc
        for i in range(tries):
            ep = await self._endpoint()
            self.stats["rpc_calls"] += 1
            try:
                res = await _rpc.call(method, params, client=await self.client(), url=ep["url"], timeout=20.0)
                ep["fails"] = 0
                return res
            except Exception as exc:  # noqa: BLE001
                self.stats["rpc_errors"] += 1
                msg = str(exc)
                if "429" in msg or "403" in msg or "http 5" in msg or isinstance(exc, httpx.TimeoutException):
                    # back off THIS endpoint (4 s, 8 s, … ≤ 60 s); the others keep going
                    self.stats["rpc_429"] += 1
                    ep["fails"] += 1
                    ep["cool"] = time.monotonic() + min(60.0, 2.0 * 2 ** ep["fails"])
                    if len(self.eps) == 1:
                        await asyncio.sleep(2 + 3 * i)
                else:
                    return None
        return None

    async def gt(self, url: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        for i in range(3):
            await self._wait("_next_gt", 60.0 / max(1.0, self.c["gt_per_min"]), self._gt_lock)
            self.stats["gt_calls"] += 1
            try:
                r = await (await self.client()).get(url, params=params, headers={"accept": "application/json"})
            except Exception:  # noqa: BLE001
                self.stats["gt_errors"] += 1
                continue
            if r.status_code == 429:
                self.stats["gt_429"] += 1
                await asyncio.sleep(15 * (i + 1))
                continue
            if r.status_code == 404:
                return None
            if r.status_code != 200:
                self.stats["gt_errors"] += 1
                continue
            try:
                return r.json()
            except ValueError:
                return None
        return None

    # -------- trades --------
    async def wallet_trades(self, w: Dict[str, Any], since_s: float) -> Dict[str, Any]:
        if w.get("role") == "fomo" and w.get("usdc_ata"):
            return await self.fomo_trades(w, since_s)
        from app.scout.txparse import parse_wallet_trades
        sigs: Dict[str, Dict[str, Any]] = {}
        newest_any = None
        hist_ok = False
        for key in w["keys"]:
            res = await self.rpc("getSignaturesForAddress", [key, {"limit": 200, "commitment": "confirmed"}])
            if res is None:
                continue
            hist_ok = True
            for s in res:
                bt = s.get("blockTime") or 0
                if key in w["keys"][:2]:                 # the two UVA PDAs: only trades touch them
                    newest_any = max(newest_any or 0, bt)
                if s.get("err") is None and bt >= since_s:
                    sigs.setdefault(s["signature"], {"bt": bt, "key": key})
        order = sorted(sigs.items(), key=lambda kv: -kv[1]["bt"])[: self.c["max_txs"]]
        trades: List[Dict[str, Any]] = []
        for sig, info in order:
            cached = self.cache["tx"].get(f"{w['wallet']}:{sig}")
            if cached is None:
                tx = await self.rpc("getTransaction", [sig, {"encoding": "json", "maxSupportedTransactionVersion": 1,
                                                            "commitment": "confirmed"}])
                if tx is None:
                    self.stats["tx_missing"] += 1
                    continue
                evs = parse_wallet_trades(tx, w["wallet"])
                cached = {"bt": tx.get("blockTime") or info["bt"],
                          "t": [{k: e.get(k) for k in ("mint", "is_buy", "sol_amount", "price_sol", "venue",
                                                       "quote_is_sol", "stable_leg")} for e in evs]}
                self.cache["tx"][f"{w['wallet']}:{sig}"] = cached
            for e in cached["t"]:
                trades.append({**e, "sig": sig, "bt": cached["bt"]})
        return {"trades": trades, "history_ok": hist_ok, "sigs_in_window": len(sigs),
                "newest_sig_s": newest_any, "txs_fetched": len(order)}

    async def _parse(self, w: Dict[str, Any], sig: str, bt: int) -> Optional[Dict[str, Any]]:
        from app.scout.txparse import parse_wallet_trades
        cached = self.cache["tx"].get(f"{w['wallet']}:{sig}")
        if cached is None:
            tx = await self.rpc("getTransaction", [sig, {"encoding": "json", "maxSupportedTransactionVersion": 1,
                                                        "commitment": "confirmed"}])
            if tx is None:
                self.stats["tx_missing"] += 1
                return None
            evs = parse_wallet_trades(tx, w["wallet"])
            cached = {"bt": tx.get("blockTime") or bt,
                      "t": [{k: e.get(k) for k in ("mint", "is_buy", "sol_amount", "price_sol", "venue",
                                                   "quote_is_sol", "stable_leg")} for e in evs]}
            self.cache["tx"][f"{w['wallet']}:{sig}"] = cached
        return cached

    async def fomo_trades(self, w: Dict[str, Any], since_s: float) -> Dict[str, Any]:
        """FOMO-app wallets trade USDC-funded through routers (DFlow, Jupiter, …), which never touch the pump
        user-volume PDAs, and their USDC account is flooded by referral-fee inflows. Same method as the coach's
        verification sweep: signatures on the wallet ∩ signatures on its USDC account = its own trades (fee
        distributions and dust drop out), plus the UVA PDAs for direct SOL pump trades. "Dead" is only claimed when
        the whole 7-day window was covered and every candidate tx was read; otherwise the flag is activity_unknown."""
        lim = 1000
        a = await self.rpc("getSignaturesForAddress", [w["wallet"], {"limit": lim, "commitment": "confirmed"}])
        if a is None:
            return {"trades": [], "history_ok": False, "sigs_in_window": 0, "newest_sig_s": None, "txs_fetched": 0}
        b = await self.rpc("getSignaturesForAddress", [w["usdc_ata"], {"limit": lim, "commitment": "confirmed"}]) or []
        in_b = {x["signature"] for x in b}
        b_floor = min((x.get("blockTime") or 0 for x in b), default=0) if len(b) >= lim else 0
        window_covered = len(a) < lim or min((x.get("blockTime") or 0 for x in a), default=0) < since_s
        cands: Dict[str, int] = {}
        for x in a:
            bt = x.get("blockTime") or 0
            if x.get("err") is None and bt >= since_s and (x["signature"] in in_b or bt < b_floor):
                cands[x["signature"]] = bt
        for key in w["keys"]:                                # UVA PDAs: only trades touch them
            res = await self.rpc("getSignaturesForAddress", [key, {"limit": 200, "commitment": "confirmed"}]) or []
            for x in res:
                bt = x.get("blockTime") or 0
                if x.get("err") is None and bt >= since_s:
                    cands.setdefault(x["signature"], bt)
        order = sorted(cands.items(), key=lambda kv: -kv[1])[: self.c["max_txs"]]
        trades: List[Dict[str, Any]] = []
        for sig, bt in order:
            cached = await self._parse(w, sig, bt)
            if cached is None:
                continue
            for e in cached["t"]:
                trades.append({**e, "sig": sig, "bt": cached["bt"]})
        newest = max((t["bt"] for t in trades), default=None)
        return {"trades": trades, "history_ok": True, "sigs_in_window": len(cands), "newest_sig_s": newest,
                "txs_fetched": len(order), "method": "wallet∩usdc_ata+uva",
                "window_covered": bool(window_covered), "sample_complete": len(order) >= len(cands)}

    # -------- candles --------
    async def _ohlcv(self, pool: str, before_s: int, limit: int) -> Optional[List[List[float]]]:
        key = f"{pool}:{before_s // 60}:{limit}"
        hit = self.cache["candles"].get(key)
        if hit is not None:
            return hit.get("c")
        wide = self._from_wide(pool, before_s, limit)
        if wide is not None:
            self.stats["gt_wide_hits"] += 1
            return wide
        j = await self.gt(f"{GT_BASE}/pools/{pool}/ohlcv/minute",
                          {"aggregate": 1, "before_timestamp": before_s, "limit": limit, "currency": "token"})
        if j is None:
            self.cache["candles"][key] = {"t": time.time(), "c": None}
            return None
        lst = (((j or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
        # currency=token → price in the pool's QUOTE token. Returns are ratios, so a non-SOL quote (USDC, NVDAx…)
        # is fine for scoring; it is recorded so the SOL-only migration heuristic can skip it.
        quote = (((j or {}).get("meta") or {}).get("quote") or {}).get("address")
        self.cache["candles"][key] = {"t": time.time(), "c": lst, "q": quote}
        return lst

    # -------- grouped candles: one GT call (≤ gt_window minute candles) serves every buy of a pool in that span --------
    def _from_wide(self, pool: str, before_s: int, limit: int) -> Optional[List[List[float]]]:
        w = self.cache["candles"].get(f"wide:{pool}")
        for seg in (w or {}).get("seg") or []:
            # usable when the segment reaches before_s and starts at least `limit` minutes earlier (or holds the pool's
            # whole history) — then it contains every candle the narrow call would have returned
            if seg["to"] >= before_s and (seg["complete"] or seg["from"] <= before_s - limit * 60):
                cs = [c for c in seg["c"] or [] if c and int(c[0]) < before_s]
                return sorted(cs, key=lambda c: int(c[0]))[-limit:]
        return None

    async def prefetch(self, pool: str, times: List[int], now_s: float, lead_s: int = 0) -> int:
        """Fetch grouped minute candles for every buy time on this pool (fewest GT calls). Returns calls made.
        lead_s > 0 (early fetch while other wallets are still loading) extends the window past the last known buy so
        later-seen buys of the same coin are usually covered too."""
        win = int(self.c.get("gt_window", 1000))
        lead_s = max(0, min(int(lead_s), (win - 200) * 60))
        span = (win - 80) * 60 - lead_s              # room for 13 min before the first buy + 62 min after the last
        calls = 0
        ts = sorted({int(t) for t in times})
        chunks: List[List[int]] = []
        for t in ts:
            if chunks and t - chunks[-1][0] <= span:
                chunks[-1].append(t)
            else:
                chunks.append([t])
        for ch in chunks:
            before = int(min(now_s, ch[-1] + 61 * 60 + 60 + lead_s))
            if self._from_wide(pool, before, 75) is not None and all(
                    self._from_wide(pool, int(min(now_s, t + 61 * 60 + 60)), 75) is not None for t in ch):
                continue
            j = await self.gt(f"{GT_BASE}/pools/{pool}/ohlcv/minute",
                              {"aggregate": 1, "before_timestamp": before, "limit": win, "currency": "token"})
            calls += 1
            w = self.cache["candles"].setdefault(f"wide:{pool}", {"t": time.time(), "seg": [], "q": None})
            w["t"] = time.time()
            if j is None:
                w["seg"].append({"from": before, "to": before, "complete": True, "c": []})   # 404: pool unknown to GT
                continue
            lst = (((j or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
            w["q"] = (((j or {}).get("meta") or {}).get("quote") or {}).get("address")
            frm = min((int(c[0]) for c in lst if c), default=before)
            w["seg"].append({"from": frm, "to": before, "complete": len(lst) < win, "c": lst})
        return calls

    async def prefetch_candles(self, by_mint: Dict[str, List[int]], now_s: float, lead_s: int = 0) -> None:
        from app.pump.solana_pda import bonding_curve_pda, canonical_pumpswap_pool
        for mint, times in by_mint.items():
            try:
                if not _pumpish(mint):                   # other launchpads: GT's top pool only (skips 2 sure 404s)
                    pool = await self._top_pool(mint)
                    if pool:
                        await self.prefetch(pool, times, now_s, lead_s)
                    continue
                curve = bonding_curve_pda(mint)
                await self.prefetch(curve, times, now_s, lead_s)
                need_ps = []
                for t in times:
                    c = self._from_wide(curve, int(min(now_s, t + 61 * 60 + 60)), 75)
                    if not c or max((x[2] for x in c if len(x) > 2), default=0) >= MIGRATION_NEAR_PX:
                        need_ps.append(t)
                if need_ps:
                    await self.prefetch(canonical_pumpswap_pool(mint, WSOL), need_ps, now_s, lead_s)
            except Exception as exc:  # noqa: BLE001
                self.stats["prefetch_errors"] += 1
                logger.debug("wallet scores prefetch %s: %s", mint[:6], type(exc).__name__)

    async def returns_after(self, mint: str, t_s: int, now_s: Optional[float] = None) -> Dict[str, Any]:
        """{'p0','ret_5m','ret_15m','ret_60m','src'} from minute candles of the curve pool + PumpSwap pool."""
        from app.pump.solana_pda import bonding_curve_pda, canonical_pumpswap_pool
        now_s = now_s or time.time()
        before = int(min(now_s, t_s + 61 * 60 + 60))
        candles: List[List[float]] = []
        srcs = []
        c = await self._ohlcv(bonding_curve_pda(mint), before, 75) if _pumpish(mint) else None
        if c:
            candles += c
            srcs.append("pump_curve")
        # PumpSwap only when the coin was not on the curve or got near graduation (SOL curve completes at ≈4.1e-7
        # SOL/token) — halves the free-API calls for the usual curve-only coin
        if _pumpish(mint) and (not c or max((x[2] for x in c if len(x) > 2), default=0) >= MIGRATION_NEAR_PX):
            c2 = await self._ohlcv(canonical_pumpswap_pool(mint, WSOL), before, 75)
            if c2:
                candles += c2
                srcs.append("pumpswap")
        if not candles:
            # other launchpads / non-SOL curves / Raydium etc.: ask GeckoTerminal for the token's top pool
            pool = await self._top_pool(mint)
            if pool:
                c3 = await self._ohlcv(pool, before, 75)
                if c3:
                    candles += c3
                    srcs.append("gt_top_pool")
        if not candles:
            return {"src": None}
        return {**returns_from_candles(candles, t_s, now_s), "src": "+".join(srcs)}

    async def _top_pool(self, mint: str) -> Optional[str]:
        key = f"pools:{mint}"
        hit = self.cache["candles"].get(key)
        if hit is not None:
            return hit.get("pool")
        j = await self.gt(f"{GT_BASE}/tokens/{mint}/pools", {"page": 1})
        pool = None
        for d in ((j or {}).get("data") or [])[:1]:
            pool = ((d.get("attributes") or {}).get("address")) or str(d.get("id") or "").split("_", 1)[-1] or None
        self.cache["candles"][key] = {"t": time.time(), "pool": pool, "q": None}
        return pool

    async def close_all(self) -> None:
        await self.close()


def _pumpish(mint: str) -> bool:
    """pump.fun mints end in 'pump' (vanity suffix since 2024); anything else has no pump curve / canonical PumpSwap
    pool, so those two GeckoTerminal lookups would only 404 — go straight to the token's top pool."""
    return str(mint).endswith("pump")


def returns_from_candles(candles: List[List[float]], t_s: int, now_s: float) -> Dict[str, Any]:
    """Pure: entry = close of the minute containing t_s; exit = last close at or before t_s + h."""
    cs = sorted({int(c[0]): c for c in candles if c and len(c) >= 5 and c[4]}.values(), key=lambda c: c[0])
    t0 = int(t_s) // 60 * 60
    entry = next((c[4] for c in cs if int(c[0]) == t0), None)
    if entry is None:
        prior = [c for c in cs if int(c[0]) <= t0]
        entry = prior[-1][4] if prior else None
    out: Dict[str, Any] = {"p0": entry}
    for h in HORIZONS_MIN:
        if not entry or t_s + h * 60 > now_s:
            out[f"ret_{h}m"] = None
            continue
        upto = [c for c in cs if int(c[0]) <= t0 + h * 60]
        if not upto or int(upto[-1][0]) < t0:
            out[f"ret_{h}m"] = None
            continue
        out[f"ret_{h}m"] = round(upto[-1][4] / entry - 1, 4)
    return out


# ---------------- scoring ----------------
def _hstats(rets: List[float]) -> Dict[str, Any]:
    n = len(rets)
    if not n:
        return {"n": 0}
    wins = sum(1 for r in rets if r > 0)
    cl = [clip(r) for r in rets]
    return {"n": n, "win_rate": round(wins / n, 4), "win_rate_ci95": wilson(wins, n),
            "median": round(statistics.median(rets), 4), "median_ci95": boot_ci(rets),
            "mean_clipped": round(statistics.fmean(cl), 4), "mean_clipped_ci95": boot_ci(cl, statistics.fmean)}


def score_wallet(w: Dict[str, Any], buys: List[Dict[str, Any]], meta: Dict[str, Any],
                 internal: Dict[str, Any], c: Dict[str, Any], now_s: float) -> Dict[str, Any]:
    by_h = {h: [b[f"ret_{h}m"] for b in buys if b.get(f"ret_{h}m") is not None] for h in HORIZONS_MIN}
    hs = {f"{h}m": _hstats(v) for h, v in by_h.items()}
    trades = meta.get("trades") or []
    last_trade = max((t["bt"] for t in trades), default=None)
    last_any = meta.get("newest_sig_s")
    last = last_trade or None
    days_since = round((now_s - last) / 86400, 2) if last else None
    # realized round trips inside the window (both legs visible, SOL-quoted)
    legs: Dict[str, Dict[str, float]] = collections.defaultdict(lambda: {"in": 0.0, "out": 0.0})
    for t in trades:
        if t.get("sol_amount"):
            legs[t["mint"]]["in" if t["is_buy"] else "out"] += t["sol_amount"] / 1e9
    rts = [v for v in legs.values() if v["in"] > 0 and v["out"] > 0]
    rt_wins = sum(1 for v in rts if v["out"] > v["in"])
    r15 = by_h[15]
    k = c["prior_n"]
    shrunk = sum(clip(r) for r in r15) / (len(r15) + k) if r15 else 0.0    # shrink toward 0 with k pseudo-obs
    cand = internal.get("cand") or []
    paper = internal.get("paper") or []
    ic15 = [x["ret_15m"] for x in cand if x.get("ret_15m") is not None]
    flags: List[str] = []
    recs: List[str] = []
    if not meta.get("history_ok"):
        flags.append("history_unavailable")
    elif last is None and not (meta.get("window_covered", True) and meta.get("sample_complete", True)):
        flags.append("activity_unknown")       # no trade in the txs read, but the 7-day window was not fully read
    elif last is None or (now_s - last) > c["dead_days"] * 86400:
        flags.append("dead_inactive")
        recs.append(f"no trade seen on-chain in {c['dead_days']:.0f} days — consider dropping")
    s15 = hs["15m"]
    if s15.get("n", 0) >= c["poor_min_n"]:
        med_hi = (s15.get("median_ci95") or [None, None])[1]
        win_hi = (s15.get("win_rate_ci95") or [None, None])[1]
        if (med_hi is not None and med_hi < 0) or (win_hi is not None and win_hi < 0.35):
            flags.append("dead_poor_record")
            recs.append(f"n={s15['n']} buys, +15m median CI {s15.get('median_ci95')}, win CI {s15.get('win_rate_ci95')} "
                        f"— statistically poor; consider dropping or demoting")
    elif s15.get("n", 0) < c["poor_min_n"]:
        flags.append("insufficient_n")
    return {
        "wallet": w["wallet"], "label": w.get("label"), "role": w.get("role"),
        "fomo_rank": w.get("fomo_rank"), "prior": w.get("prior"), "x_handle": w.get("x_handle"),
        "live_weight": 0.0 if w.get("role") == "cryptohub" else w.get("live_weight"),
        "onchain": {"buys_7d": sum(1 for t in trades if t["is_buy"]), "sells_7d": sum(1 for t in trades if not t["is_buy"]),
                    "txs_fetched": meta.get("txs_fetched"), "sigs_in_window": meta.get("sigs_in_window"),
                    "method": meta.get("method", "uva_pdas"),
                    "scored_buys": len(buys), "distinct_coins_bought": len({t["mint"] for t in trades if t["is_buy"]}), **hs,
                    "round_trips": len(rts), "round_trip_win_rate": round(rt_wins / len(rts), 4) if rts else None,
                    "round_trip_win_ci95": wilson(rt_wins, len(rts))},
        "activity": {"last_trade_ms": int(last * 1000) if last else None, "days_since_last_trade": days_since,
                     "last_any_sig_ms": int(last_any * 1000) if last_any else None,
                     "trades_per_day": round(len(trades) / c["days"], 2)},
        "learning_log": {"n": len(cand), "entered": sum(1 for x in cand if x.get("entered")),
                         **{f"ret15_{k2}": v for k2, v in _hstats(ic15).items() if k2 in ("n", "median", "win_rate")}},
        "books": {"n": len(paper), "wins": sum(1 for x in paper if (x.get("pnl_pct") or 0) > 0),
                  "mean_pnl_pct": round(statistics.fmean([x.get("pnl_pct") or 0 for x in paper]), 2) if paper else None},
        "score": round(shrunk, 4),
        "score_note": f"clipped mean +15m return shrunk toward 0 with {k:g} pseudo-buys (higher = better copy target)",
        "flags": flags, "recommendations": recs,
    }


async def compute(*, c: Optional[Dict[str, Any]] = None, wallets: Optional[List[Dict[str, Any]]] = None,
                  progress: Optional[Any] = None) -> Dict[str, Any]:
    c = c or cfg()
    now_s = time.time()
    since_s = now_s - c["days"] * 86400
    col = Collector(c)
    ws = wallets if wallets is not None else roster()
    internal = internal_by_wallet(int(since_s * 1000))
    rows: List[Dict[str, Any]] = []
    try:
        # phase 1: every wallet's trades (wallets in parallel up to `concurrency`; the RPC budget is shared). Each
        # wallet's scored buys (one per coin, its FIRST buy in the window — split/DCA buys are not new evidence —
        # newest first) are queued for a candle worker right away, so GeckoTerminal (its own rate limit) works in
        # parallel with the RPC instead of after it.
        metas: List[Optional[Dict[str, Any]]] = [None] * len(ws)
        firsts: List[List[Dict[str, Any]]] = [[] for _ in ws]
        by_mint: Dict[str, List[int]] = collections.defaultdict(list)
        queue: asyncio.Queue = asyncio.Queue()
        sem = asyncio.Semaphore(c.get("concurrency", 1))
        done = 0

        async def candle_worker() -> None:
            while True:
                mint = await queue.get()
                if mint is None:
                    return
                await col.prefetch_candles({mint: list(by_mint[mint])}, now_s, c.get("gt_lead_min", 240) * 60)

        async def one(i: int, w: Dict[str, Any]) -> None:
            nonlocal done
            async with sem:
                meta = metas[i] = await col.wallet_trades(w, since_s)
                first: Dict[str, Dict[str, Any]] = {}
                for x in sorted(meta["trades"], key=lambda t: t["bt"]):
                    if x.get("is_buy") and x.get("mint") and x["mint"] not in first:
                        first[x["mint"]] = x
                firsts[i] = sorted(first.values(), key=lambda t: -t["bt"])[: c["max_scored_buys"]]
                for t in firsts[i]:
                    if t["mint"] not in by_mint:
                        queue.put_nowait(t["mint"])
                    by_mint[t["mint"]].append(int(t["bt"]))
                done += 1
                if progress:
                    progress(done, len(ws) * 2, w, None)
                if done % 5 == 0:
                    col.save_cache()
        worker = asyncio.create_task(candle_worker())
        try:
            await asyncio.gather(*(one(i, w) for i, w in enumerate(ws)))
            queue.put_nowait(None)
            await worker
        finally:
            if not worker.done():
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
        col.save_cache()
        # phase 2: anything the early fetches did not cover (no lead) → then score
        await col.prefetch_candles(by_mint, now_s)
        col.save_cache()
        for i, (w, meta, chosen) in enumerate(zip(ws, metas, firsts)):
            buys = []
            for t in chosen:
                r = await col.returns_after(t["mint"], int(t["bt"]), now_s)
                if r.get("p0"):
                    buys.append({**t, **r})
            rows.append(score_wallet(w, buys, meta, internal.get(w["wallet"], {}), c, now_s))
            if progress:
                progress(len(ws) + i + 1, len(ws) * 2, w, rows[-1])
        col.save_cache()
    finally:
        await col.close()
    rows.sort(key=lambda r: (-(r["score"] or 0), r["label"] or ""))
    for i, r in enumerate(rows):
        r["rank"] = i + 1
    out = {"generated_ms": int(now_s * 1000), "algo": ALGO, "advisory_only": True,
           "note": "Advisory only: nothing in the coach reads this to weight, add or drop wallets.",
           "window_days": c["days"], "horizons_min": list(HORIZONS_MIN), "wallets": rows,
           "counts": {"wallets": len(rows), "dead_inactive": sum("dead_inactive" in r["flags"] for r in rows),
                      "dead_poor_record": sum("dead_poor_record" in r["flags"] for r in rows),
                      "insufficient_n": sum("insufficient_n" in r["flags"] for r in rows),
                      "activity_unknown": sum("activity_unknown" in r["flags"] for r in rows)},
           "recommendations": [{"wallet": r["wallet"], "label": r["label"], "role": r["role"], "why": r["recommendations"]}
                               for r in rows if r["recommendations"]],
           "sources": {"rpc": ",".join(u.split("://")[-1].split("/")[0] for u in c.get("rpc_urls") or [c["rpc_url"]]), "candles": "geckoterminal (free, public)",
                       "collector_stats": dict(col.stats)}}
    return out


def write(out: Dict[str, Any]) -> None:
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(out, default=str))
    tmp.replace(p)


def read() -> Optional[Dict[str, Any]]:
    try:
        return json.loads(path().read_text())
    except Exception:  # noqa: BLE001
        return None


class DailyJob:
    """Background refresh: once at startup if the file is older than WALLET_SCORES_REFRESH_H, then every day."""

    def __init__(self) -> None:
        self.task: Optional[asyncio.Task] = None
        self.running = False
        self.last_error: Optional[str] = None
        self.progress: Dict[str, Any] = {}
        self.rugger: Dict[str, Any] = {}

    def start(self) -> None:
        if not cfg()["enabled"] or (self.task and not self.task.done()):
            return
        try:
            self.task = asyncio.get_running_loop().create_task(self._loop())
        except RuntimeError:
            pass

    async def _loop(self) -> None:
        await asyncio.sleep(_f("WALLET_SCORES_START_DELAY_S", 600))
        while True:
            c = cfg()
            cur = read()
            age_h = (time.time() * 1000 - (cur or {}).get("generated_ms", 0)) / 3.6e6
            if age_h >= c["refresh_h"] or (cur or {}).get("algo") != ALGO:
                self.running = True
                try:
                    out = await compute(c=c, progress=lambda i, n, w, r: self.progress.update(done=i, total=n))
                    write(out)
                    self.last_error = None
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    self.last_error = type(exc).__name__
                    logger.warning("wallet scores job failed: %s", self.last_error)
                finally:
                    self.running = False
            try:                                   # v1.17 rug checks: refresh the local rugger list (file reads only)
                from app.live.rugcheck import build_rugger_list
                self.rugger = build_rugger_list()
            except Exception as exc:  # noqa: BLE001
                self.rugger = {"error": type(exc).__name__}
            await asyncio.sleep(3600)

    def status(self) -> Dict[str, Any]:
        cur = read()
        return {"enabled": cfg()["enabled"], "running": self.running, "progress": self.progress,
                "last_error": self.last_error, "generated_ms": (cur or {}).get("generated_ms"),
                "rugger_list": self.rugger}

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)


job = DailyJob()
