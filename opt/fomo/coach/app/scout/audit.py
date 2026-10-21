"""v1.16 scout detection audit: on-chain scout buys vs what the scout lane detected (read-only, free RPC).

Ground truth per scout wallet, inside the audit window AND inside periods when the lane was up (heartbeats in
data/scout_detections.jsonl):
  signatures from the pump.fun volume-accumulator PDA, the PumpSwap volume-accumulator PDA and the wallet's own
  history (bounded: SCOUT_AUDIT_WALLET_PAGES × 1000; flooded wallets are only partly covered and say so), each
  parsed with app/scout/txparse.py; a buy = tokens in + SOL out >= SCOUT_AUDIT_MIN_SOL.
Detected = a detection row with the same signature. Sized ok = detected SOL within 15% (or 0.02 SOL) of truth.
Runs daily (SCOUT_AUDIT_INTERVAL_S) and on demand (GET /api/scout/audit?run=true, rate-limited). Never trades.
"""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from app.paths import data_path
from app.scout import detections

logger = logging.getLogger("coach.scout.audit")


def _env(name: str, d: str) -> str:
    return os.getenv(name, d).strip()


def _f(name: str, d: float) -> float:
    try:
        return float(_env(name, str(d)))
    except ValueError:
        return d


def result_path() -> Path:
    return data_path("SCOUT_AUDIT_PATH", "scout_audit.json")


def history_path() -> Path:
    return data_path("SCOUT_AUDIT_HISTORY_PATH", "scout_audit_history.jsonl")


def sized_ok(detected_sol: Optional[float], truth_sol: float) -> bool:
    return detected_sol is not None and abs(detected_sol - truth_sol) <= max(0.15 * truth_sol, 0.02)


def compare(truth: List[Dict[str, Any]], detected: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Pure: truth buys [{sig, wallet, label, mint, sol, bt}] vs detection rows keyed by sig."""
    per: Dict[str, Dict[str, Any]] = collections.defaultdict(lambda: {"truth": 0, "detected": 0, "sized_ok": 0})
    missed, mis_sized = [], []
    for t in truth:
        p = per[t["label"]]
        p["truth"] += 1
        d = detected.get(t["sig"])
        if d:
            p["detected"] += 1
            if sized_ok(d.get("sol"), t["sol"]):
                p["sized_ok"] += 1
            else:
                mis_sized.append({**t, "detected_sol": d.get("sol"), "path": d.get("path")})
        else:
            missed.append(t)
    n = len(truth)
    det = sum(p["detected"] for p in per.values())
    ok = sum(p["sized_ok"] for p in per.values())
    return {"truth_buys": n, "detected": det, "sized_ok": ok,
            "detection_rate": round(det / n, 4) if n else None,
            "sized_ok_rate": round(ok / n, 4) if n else None,
            "per_wallet": dict(per), "missed": missed[:25], "mis_sized": mis_sized[:25]}


class Auditor:
    def __init__(self) -> None:
        self.last: Optional[Dict[str, Any]] = None
        self.running = False
        self.task: Optional[asyncio.Task] = None
        self.loop_task: Optional[asyncio.Task] = None
        self.loaded = False

    def load(self) -> None:
        if self.loaded:
            return
        self.loaded = True
        try:
            if result_path().exists():
                self.last = json.loads(result_path().read_text())
        except Exception:  # noqa: BLE001
            self.last = None

    def _save(self, res: Dict[str, Any]) -> None:
        try:
            p = result_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_name(p.name + ".tmp")
            tmp.write_text(json.dumps(res, default=str))
            tmp.replace(p)
            with history_path().open("a") as fh:
                fh.write(json.dumps({k: res.get(k) for k in ("at_ms", "window_h", "truth_buys", "detected", "sized_ok",
                                                             "detection_rate", "sized_ok_rate", "trigger")}) + "\n")
        except Exception as exc:  # noqa: BLE001
            logger.warning("audit save failed: %s", type(exc).__name__)

    # ---------------- scheduling ----------------
    def start_loop(self) -> None:
        if _env("SCOUT_AUDIT_ENABLED", "true").lower() not in {"1", "true", "yes", "on"}:
            return
        if self.loop_task and not self.loop_task.done():
            return
        try:
            self.loop_task = asyncio.get_running_loop().create_task(self._loop())
        except RuntimeError:
            pass

    async def _loop(self) -> None:
        await asyncio.sleep(_f("SCOUT_AUDIT_FIRST_DELAY_S", 3600))
        while True:
            last_at = (self.last or {}).get("at_ms") or 0
            if time.time() * 1000 - last_at >= _f("SCOUT_AUDIT_INTERVAL_S", 86400) * 1000 * 0.95:
                await self.run(trigger="daily")
            await asyncio.sleep(1800)

    def request(self, *, window_h: float) -> Dict[str, Any]:
        """On-demand trigger (GET). Returns immediately; at most one run per SCOUT_AUDIT_MIN_INTERVAL_S."""
        if self.running:
            return {"started": False, "reason": "an audit is already running"}
        last_at = (self.last or {}).get("at_ms") or 0
        if time.time() * 1000 - last_at < _f("SCOUT_AUDIT_MIN_INTERVAL_S", 900) * 1000:
            return {"started": False, "reason": f"last audit < {_f('SCOUT_AUDIT_MIN_INTERVAL_S', 900):.0f}s ago"}
        try:
            self.task = asyncio.get_running_loop().create_task(self.run(window_h=window_h, trigger="on_demand"))
        except RuntimeError:
            return {"started": False, "reason": "no event loop"}
        return {"started": True}

    # ---------------- run ----------------
    async def run(self, *, window_h: Optional[float] = None, trigger: str = "manual") -> Dict[str, Any]:
        from app.fomo_onchain.source import TokenBucket
        from app.pump import rpc
        from app.scout.roster import roster
        from app.scout.source import source
        from app.scout.txparse import parse_wallet_trades
        if self.running:
            return self.last or {}
        self.running = True
        t_start = time.time()
        window_h = window_h or _f("SCOUT_AUDIT_WINDOW_H", 24)
        bucket = TokenBucket(_f("SCOUT_AUDIT_RPS", 2))
        http = httpx.AsyncClient(timeout=12.0, limits=httpx.Limits(max_connections=2))
        calls = collections.Counter()

        async def call(method: str, params: List[Any]) -> Any:
            await bucket.take()
            calls[method] += 1
            try:
                return await rpc.call(method, params, client=http, url=source.rpc_url, timeout=12.0)
            except Exception:  # noqa: BLE001
                calls["errors"] += 1
                return None

        try:
            now_ms = int(time.time() * 1000)
            since_ms = now_ms - int(window_h * 3600 * 1000)
            settle_ms = 90_000                                   # give detection a moment
            cov = detections.coverage(since_ms, now_ms, live_since_ms=source.started_ms)
            det = {r["sig"]: r for r in detections.rows(since_ms - 600_000)
                   if r.get("kind") == "trade" and r.get("is_buy")}
            min_sol = _f("SCOUT_AUDIT_MIN_SOL", 0.05)
            pages = int(_f("SCOUT_AUDIT_WALLET_PAGES", 2))
            max_tx = int(_f("SCOUT_AUDIT_MAX_TX_PER_WALLET", 60))
            truth: List[Dict[str, Any]] = []
            wallets_info: Dict[str, Any] = {}
            scouts = [w for w in roster.by_wallet.values() if w.role == "scout"]
            for w in scouts:
                sigs: Dict[str, Dict[str, Any]] = {}
                for key, src in ((w.key, "pump_uva"), (w.amm_key, "amm_uva")):
                    res = await call("getSignaturesForAddress", [key, {"limit": 1000, "commitment": "confirmed"}])
                    for x in res or []:
                        if not x.get("err") and (x.get("blockTime") or 0) * 1000 >= since_ms:
                            sigs.setdefault(x["signature"], {"bt": x.get("blockTime"), "src": src})
                before, cov_from = None, None
                for _ in range(pages):
                    opts: Dict[str, Any] = {"limit": 1000, "commitment": "confirmed"}
                    if before:
                        opts["before"] = before
                    res = await call("getSignaturesForAddress", [w.wallet, opts])
                    if not res:
                        break
                    for x in res:
                        if not x.get("err") and (x.get("blockTime") or 0) * 1000 >= since_ms:
                            sigs.setdefault(x["signature"], {"bt": x.get("blockTime"), "src": "wallet"})
                    before, cov_from = res[-1]["signature"], res[-1].get("blockTime")
                    if len(res) < 1000 or (cov_from or 0) * 1000 < since_ms:
                        break
                wallet_cov_ms = max(since_ms, (cov_from or 0) * 1000) if cov_from else since_ms
                cand = [(s, v) for s, v in sigs.items() if v["bt"] and detections.covered(v["bt"] * 1000, cov)
                        and v["bt"] * 1000 <= now_ms - settle_ms]
                cand.sort(key=lambda kv: -(kv[1]["bt"] or 0))
                sampled = len(cand) > max_tx
                buys = 0
                for sig, v in cand[:max_tx]:
                    tx = await call("getTransaction", [sig, {"encoding": "json", "commitment": "confirmed",
                                                             "maxSupportedTransactionVersion": 1}])
                    for t in parse_wallet_trades(tx, w.wallet):
                        if t["is_buy"] and t["sol_amount"] >= min_sol * 1e9:
                            truth.append({"sig": sig, "wallet": w.wallet, "label": w.label, "mint": t["mint"],
                                          "sol": round(t["sol_amount"] / 1e9, 4), "bt": v["bt"], "src": v["src"],
                                          "venue": t.get("venue"), "quote_is_sol": t.get("quote_is_sol")})
                            buys += 1
                            break
                wallets_info[w.label] = {"sigs_in_window": len(sigs), "checked": min(len(cand), max_tx),
                                         "sampled": sampled, "buys": buys,
                                         "wallet_history_covered_from_ms": wallet_cov_ms,
                                         "wallet_history_partial": wallet_cov_ms > since_ms + 60_000}
            res = compare(truth, det)
            res.update({"at_ms": now_ms, "window_h": window_h, "trigger": trigger, "min_sol": min_sol,
                        "coverage": [[a, b] for a, b in cov][-20:],
                        "coverage_s": round(sum(b - a for a, b in cov) / 1000), "wallets": wallets_info,
                        "rpc_calls": dict(calls), "took_s": round(time.time() - t_start, 1),
                        "read_only": True})
            self.last = res
            self._save(res)
            return res
        except Exception as exc:  # noqa: BLE001
            logger.warning("scout audit failed: %s", type(exc).__name__)
            return {"ok": False, "error": type(exc).__name__}
        finally:
            await http.aclose()
            self.running = False

    # ---------------- health ----------------
    def health(self) -> Dict[str, Any]:
        self.load()
        last = self.last or {}
        warn: List[str] = []
        thr = _f("SCOUT_AUDIT_WARN_RATE", 0.8)
        min_n = int(_f("SCOUT_AUDIT_MIN_SAMPLE", 3))
        n = last.get("truth_buys") or 0
        if last and n >= min_n:
            if (last.get("detection_rate") or 0) < thr:
                warn.append(f"scout detection rate {last['detection_rate']:.0%} < {thr:.0%} "
                            f"({last['detected']}/{n} on-chain scout buys, audit {_age(last.get('at_ms'))} ago)")
            if (last.get("sized_ok_rate") or 0) < thr:
                warn.append(f"scout buys sized correctly {last['sized_ok_rate']:.0%} < {thr:.0%} ({last['sized_ok']}/{n})")
        if last.get("at_ms") and time.time() * 1000 - last["at_ms"] > 36 * 3600 * 1000:
            warn.append(f"scout detection audit is stale ({_age(last['at_ms'])} old)")
        return {"last_at_ms": last.get("at_ms"), "detection_rate": last.get("detection_rate"),
                "sized_ok_rate": last.get("sized_ok_rate"), "truth_buys": n, "running": self.running,
                "warn_below": thr, "warnings": warn}


def _age(at_ms: Optional[int]) -> str:
    if not at_ms:
        return "?"
    s = time.time() - at_ms / 1000
    return f"{s / 3600:.1f} h" if s >= 3600 else f"{s / 60:.0f} min"


auditor = Auditor()
