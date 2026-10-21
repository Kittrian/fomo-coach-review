"""v1.17.3 Day Recap ("what happened today") — READ-ONLY, off the hot path.

GET /recap                     → static/recap.html (dark single-page view, no CDN)
GET /api/recap/today           → structured JSON for today's America/Chicago calendar day
GET /api/recap?date=YYYY-MM-DD → same for any past day

Rules:
* Only READS the coach's own files (live ledger, learning log, scout/paper logs, processed trial, exit variants)
  plus an in-memory snapshot of the live engine summary (the same non-secret block /health shows).
* Never writes to any trading file, never calls the live engine's mutating methods, never touches .env.
* File parsing runs in a worker thread and is cached by (path, mtime, size); responses are cached ~20 s.
* Optional enrichment (DEX Screener price/symbol, public-RPC slot lookups) runs as a background asyncio task with
  short timeouts, is cached, soft-fails, and never uses the Helius key (keyless publicnode only).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import statistics
import threading
import time
from datetime import date as _date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse

from app.paths import ROOT, data_dir, data_path

logger = logging.getLogger("coach.recap")
router = APIRouter()

TZ = ZoneInfo(os.getenv("LIVE_DAY_TZ", "America/Chicago") or "America/Chicago")
STATIC_HTML = ROOT / "static" / "recap.html"
DEX_URL = "https://api.dexscreener.com/tokens/v1/solana/"
# Deploys worth splitting the speed panel on (CT wall clock). Later days just show "current build".
DEPLOYS: List[Tuple[str, str]] = [
    ("2026-10-08 12:01:10", "fastpath: parallel gates + early scout-sell catch + timing logs"),
    ("2026-10-08 12:23:20", "Helius buy/sell/rug path + log redaction"),
    ("2026-10-08 13:32:52", "mesh during swap + Helius detection + exit tweaks + processed trial"),
]

REASON_TEXT = {
    "tp_2x": "Took half off at 2×",
    "tp": "Took profit",
    "trail": "Trailing stop: sold the rest off the high",
    "trailing_stop": "Trailing stop: sold the rest off the high",
    "trader_sold": "Scout sold, so we followed him out",
    "scout_sold": "Scout sold, so we followed him out",
    "rug_flag_liquidity_drain": "Rug alarm: liquidity was draining",
    "below_entry_min10": "Still below entry after 10 min",
    "hard_stop": "Hard stop loss",
    "stop": "Stop loss",
    "kill": "Kill switch sell",
    "max_hold": "Max hold time reached",
}


def _env_bool(k: str, d: bool) -> bool:
    v = os.getenv(k)
    return d if v is None or v.strip() == "" else v.strip().lower() in ("1", "true", "yes", "on")


def _net_enabled() -> bool:
    return _env_bool("RECAP_NET_ENABLED", True)


def _rpc_url() -> str:
    # deliberately NOT SCOUT_RPC_URL / HELIUS_*: keyless public RPC only, so no key can ever leak or burn credits
    return os.getenv("RECAP_RPC_URL", "https://solana-rpc.publicnode.com")


def reason_text(reason: Optional[str]) -> str:
    if not reason:
        return ""
    r = str(reason)
    if r in REASON_TEXT:
        return REASON_TEXT[r]
    for k, v in REASON_TEXT.items():
        if r.startswith(k):
            return v
    if r.startswith("rug"):
        return "Rug alarm: " + r.replace("rug_flag_", "").replace("_", " ")
    return r.replace("_", " ")


def exit_category(reason: Optional[str]) -> str:
    r = str(reason or "")
    if r.startswith("tp"):
        return "tp"
    if r.startswith("trail"):
        return "trail"
    if r in ("trader_sold", "scout_sold") or "scout" in r:
        return "scout"
    if r.startswith("rug"):
        return "rug"
    if "stop" in r or "below_entry" in r or r.startswith("kill"):
        return "stop"
    return "other"


# ------------------------------------------------------------------ day helpers
def parse_day(s: Optional[str]) -> _date:
    if not s or s == "today":
        return datetime.now(TZ).date()
    try:
        return _date.fromisoformat(s)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD") from exc


def day_bounds(d: _date) -> Tuple[int, int]:
    start = datetime(d.year, d.month, d.day, tzinfo=TZ)
    end = datetime.combine(d + timedelta(days=1), datetime.min.time(), tzinfo=TZ)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def ct_label(ms: Optional[int]) -> Optional[str]:
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, TZ).strftime("%-I:%M:%S %p CT")


def _deploy_ms() -> List[Tuple[int, str, str]]:
    out = []
    for s, what in DEPLOYS:
        try:
            dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)
            out.append((int(dt.timestamp() * 1000), dt.strftime("%-I:%M %p CT"), what))
        except ValueError:
            pass
    return sorted(out)


# ------------------------------------------------------------------ cached JSONL reading
_file_cache: Dict[Tuple[str, str], Tuple[Tuple[float, int], List[Dict[str, Any]]]] = {}
_file_lock = threading.Lock()


def _ts_of_line(line: str) -> Optional[int]:
    if line.startswith('{"ts_ms": '):
        try:
            return int(line[10:23])
        except ValueError:
            return None
    return None


def read_jsonl(path: Path, *, start_ms: Optional[int] = None, end_ms: Optional[int] = None,
               must_contain: Optional[str] = None, any_of: Optional[Iterable[str]] = None,
               tag: str = "") -> List[Dict[str, Any]]:
    """Parse rows from a JSONL file (read-only). Lines outside [start_ms, end_ms) are skipped WITHOUT json.loads when
    the ts can be read from the line prefix. Cached by (mtime, size) per (path, query tag)."""
    try:
        st = path.stat()
    except OSError:
        return []
    anys = tuple(sorted(set(any_of))) if any_of else None
    key = (str(path), f"{tag}|{start_ms}|{end_ms}|{must_contain}|{hash(anys)}")
    sig = (st.st_mtime, st.st_size)
    with _file_lock:
        hit = _file_cache.get(key)
        if hit and hit[0] == sig:
            return hit[1]
    rows: List[Dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if must_contain and must_contain not in line:
                    continue
                if anys and not any(a in line for a in anys):
                    continue
                ts = _ts_of_line(line)
                if ts is not None and ((start_ms is not None and ts < start_ms) or (end_ms is not None and ts >= end_ms)):
                    continue
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(r, dict):
                    continue
                t = r.get("ts_ms")
                if isinstance(t, (int, float)) and ((start_ms is not None and t < start_ms) or (end_ms is not None and t >= end_ms)):
                    continue
                rows.append(r)
    except OSError:
        return []
    with _file_lock:
        if len(_file_cache) > 64:
            _file_cache.clear()
        _file_cache[key] = (sig, rows)
    return rows


def _p(env: str, name: str) -> Path:
    return data_path(env, name)


def ledger_path() -> Path:
    return Path(os.getenv("LIVE_LEDGER_PATH", "").strip() or str(data_dir() / "live_trades.jsonl"))


# ------------------------------------------------------------------ enrichment caches (background, soft-fail)
class _Enrich:
    def __init__(self) -> None:
        self.dex: Dict[str, Tuple[float, Optional[Dict[str, Any]]]] = {}
        self.slots: Dict[str, Optional[int]] = {}
        self._loaded = False
        self._task: Optional[asyncio.Task] = None
        self.stats: Dict[str, int] = {"dex_calls": 0, "dex_errors": 0, "rpc_calls": 0, "rpc_errors": 0}

    def cache_path(self) -> Path:
        return _p("RECAP_CACHE_PATH", "recap_cache.json")

    def load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            j = json.loads(self.cache_path().read_text())
            self.slots = {k: v for k, v in (j.get("slots") or {}).items() if isinstance(v, int)}
        except Exception:  # noqa: BLE001
            pass

    def save(self) -> None:
        try:
            p = self.cache_path()
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps({"slots": {k: v for k, v in self.slots.items() if isinstance(v, int)}}))
            tmp.replace(p)
        except Exception:  # noqa: BLE001
            pass

    def dex_for(self, mint: str) -> Optional[Dict[str, Any]]:
        v = self.dex.get(mint)
        return v[1] if v else None

    def wants(self, mints: Iterable[str], sigs: Iterable[str], ttl_s: float) -> Tuple[List[str], List[str]]:
        now = time.time()
        m = [x for x in dict.fromkeys(mints) if x and (x not in self.dex or now - self.dex[x][0] > ttl_s)]
        s = [x for x in dict.fromkeys(sigs) if x and x not in self.slots]
        return m, s

    def schedule(self, mints: List[str], sigs: List[str]) -> Optional[asyncio.Task]:
        if not _net_enabled() or (not mints and not sigs):
            return None
        if self._task and not self._task.done():
            return self._task
        try:
            self._task = asyncio.get_running_loop().create_task(self._run(mints, sigs))
        except RuntimeError:
            return None
        return self._task

    async def _run(self, mints: List[str], sigs: List[str]) -> None:
        import httpx
        try:
            async with httpx.AsyncClient(timeout=6.0, headers={"user-agent": "fomo-signal-coach-recap"}) as c:
                for i in range(0, len(mints), 30):
                    chunk = mints[i:i + 30]
                    self.stats["dex_calls"] += 1
                    try:
                        r = await c.get(DEX_URL + ",".join(chunk))
                        pairs = r.json() if r.status_code == 200 else None
                    except Exception:  # noqa: BLE001
                        pairs = None
                    if not isinstance(pairs, list):
                        self.stats["dex_errors"] += 1
                        continue
                    best: Dict[str, Dict[str, Any]] = {}
                    for p in pairs:
                        if not isinstance(p, dict):
                            continue
                        m = ((p.get("baseToken") or {}).get("address")) or ""
                        liq = float(((p.get("liquidity") or {}).get("usd")) or 0)
                        if m in chunk and (m not in best or liq > best[m]["_liq"]):
                            best[m] = {"_liq": liq, "symbol": (p.get("baseToken") or {}).get("symbol"),
                                       "name": (p.get("baseToken") or {}).get("name"),
                                       "price_sol": _f(p.get("priceNative")), "price_usd": _f(p.get("priceUsd")),
                                       "mcap_usd": _f(p.get("marketCap") or p.get("fdv")), "liquidity_usd": liq,
                                       "chg_h1": _f((p.get("priceChange") or {}).get("h1")),
                                       "chg_h24": _f((p.get("priceChange") or {}).get("h24")),
                                       "dex": p.get("dexId"), "url": p.get("url")}
                    now = time.time()
                    for m in chunk:
                        b = best.get(m)
                        if b:
                            b.pop("_liq", None)
                        self.dex[m] = (now, b)
                    await asyncio.sleep(0.25)                      # be gentle; this is low priority
                for i in range(0, len(sigs), 100):
                    chunk = sigs[i:i + 100]
                    self.stats["rpc_calls"] += 1
                    try:
                        r = await c.post(_rpc_url(), json={"jsonrpc": "2.0", "id": 1, "method": "getSignatureStatuses",
                                                           "params": [chunk, {"searchTransactionHistory": True}]})
                        vals = ((r.json() or {}).get("result") or {}).get("value") if r.status_code == 200 else None
                    except Exception:  # noqa: BLE001
                        vals = None
                    if not isinstance(vals, list):
                        self.stats["rpc_errors"] += 1
                        continue
                    for s, v in zip(chunk, vals):
                        if isinstance(v, dict) and isinstance(v.get("slot"), int):
                            self.slots[s] = v["slot"]
                    await asyncio.sleep(0.25)
            self.save()
        except Exception as exc:  # noqa: BLE001
            logger.info("recap enrichment soft-failed: %s", type(exc).__name__)


enrich = _Enrich()


def _f(v: Any) -> Optional[float]:
    try:
        return None if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return None


def _med(xs: List[float]) -> Optional[float]:
    xs = [x for x in xs if isinstance(x, (int, float))]
    return round(statistics.median(xs), 3) if xs else None


def short(addr: Optional[str], n: int = 4) -> str:
    if not addr:
        return ""
    return f"{addr[:n]}…{addr[-n:]}" if len(addr) > 2 * n + 1 else addr


# ------------------------------------------------------------------ core build (pure: files + snapshot in, dict out)
def _scout_labels() -> Dict[str, str]:
    try:
        j = json.loads(_p("SCOUT_WALLETS_PATH", "scout_wallets.json").read_text())
        return {w.get("address"): w.get("label") for w in j.get("wallets") or [] if w.get("address")}
    except Exception:  # noqa: BLE001
        return {}


def _trades_from_ledger(rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    tr: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        k, tid = r.get("kind"), r.get("id")
        if k == "buy" and tid:
            tr[tid] = {"buy": r, "sells": [], "close": None}
        elif k == "sell" and tid in tr:
            tr[tid]["sells"].append(r)
        elif k == "close" and tid in tr:
            tr[tid]["close"] = r
    return tr


def _block_category(reason: str) -> str:
    r = reason.lower()
    if r.startswith("rugcheck") or "rugcheck" in r:
        return "rug_check"
    if "mesh" in r:
        return "mesh"
    if "sol reserve" in r:
        return "reserve"
    if "kill" in r:
        return "kill"
    return "other"


def _block_label(cat: str) -> str:
    return {"rug_check": "Rug check", "mesh": "Mesh risk gate", "reserve": "SOL reserve", "kill": "Kill switch",
            "tg_refused": "Telegram call refused", "other": "Gate"}.get(cat, cat)


def _outcome_verdict(peak: Optional[float], last: Optional[float]) -> Tuple[str, str]:
    if peak is None and last is None:
        return "unknown", "no outcome data yet"
    if peak is not None and peak >= 1.0:
        return "missed_winner", f"ran to {1 + peak:.1f}× after the block"
    if last is not None and last <= -0.5:
        return "dodged_rug", f"fell {last * 100:.0f}% after the block"
    if last is not None and last <= -0.25:
        return "dodged_dump", f"fell {last * 100:.0f}% after the block"
    if peak is not None and peak >= 0.4:
        return "missed_pump", f"peaked +{peak * 100:.0f}% after the block"
    return "flat", "roughly flat after the block"


def _xv_from_rows(rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    tot: Dict[str, Dict[str, float]] = {}
    labels: Dict[str, str] = {}
    by_sig: Dict[str, Dict[str, float]] = {}

    def add(name: str, v: Any, label: Any = None, sig: Any = None) -> None:
        pnl = v if isinstance(v, (int, float)) else None
        if isinstance(v, dict):
            for k in ("pnl_sol", "realized_sol", "net_sol", "pnl"):
                x = v.get(k)
                if isinstance(x, dict):
                    x = x.get("pnl_sol") or x.get("realized_sol")
                if isinstance(x, (int, float)):
                    pnl = x
                    break
            label = label or v.get("plan") or v.get("label") or v.get("name")
        if pnl is None:
            return
        t = tot.setdefault(str(name), {"pnl_sol": 0.0, "n": 0, "wins": 0})
        t["pnl_sol"] += float(pnl)
        t["n"] += 1
        t["wins"] += 1 if pnl > 0 else 0
        if label:
            labels[str(name)] = str(label)
        if sig is not None:
            by_sig.setdefault(str(sig), {})[str(name)] = float(pnl)

    for r in rows:
        if str(r.get("kind") or "").lower() in ("open", "mark", "tick", "pos", "start") or r.get("restart_gap"):
            continue
        if isinstance(r.get("variants"), dict):
            for name, v in r["variants"].items():
                add(name, v)
        elif isinstance(r.get("results"), dict):
            for name, v in r["results"].items():
                add(name, v)
        elif r.get("variant") is not None:
            add(r["variant"], r, r.get("plan") or r.get("label"), r.get("signal_id"))
    if not tot:
        return None
    base_key = "A" if "A" in tot else sorted(tot)[0]
    base = tot[base_key]["pnl_sol"]
    out = []
    for k, v in tot.items():
        pairs = [(d[k], d[base_key]) for d in by_sig.values() if k in d and base_key in d]
        out.append({"variant": k, "label": labels.get(k), "pnl_sol": round(v["pnl_sol"], 6), "n": int(v["n"]),
                    "win_rate": round(v["wins"] / v["n"], 3) if v["n"] else None,
                    "vs_base_sol": round(v["pnl_sol"] - base, 6),
                    "beats_base": None if k == base_key or not pairs else sum(1 for x, y in pairs if x > y + 1e-9),
                    "loses_to_base": None if k == base_key or not pairs else sum(1 for x, y in pairs if x < y - 1e-9)})
    out.sort(key=lambda x: -x["pnl_sol"])
    return {"base": base_key, "variants": out, "signals": len(by_sig) or None}


def _exit_variants(start_ms: int, end_ms: int) -> Optional[Dict[str, Any]]:
    """Tolerant reader for the v1.18 paper-only exit-variant test (app/scout/exit_variants.py owns the writer).
    Restart-gap rows are left out, same as its own summary."""
    path = _p("EXIT_VARIANTS_LOG_PATH", "exit_variants.jsonl")
    if not os.getenv("EXIT_VARIANTS_LOG_PATH") and os.getenv("EXIT_VARIANTS_PATH"):
        path = Path(os.environ["EXIT_VARIANTS_PATH"])
    if not path.exists():
        return None
    day_rows = read_jsonl(path, start_ms=start_ms, end_ms=end_ms, tag="xv")
    all_rows = read_jsonl(path, tag="xvall")
    day = _xv_from_rows(day_rows)
    allt = _xv_from_rows(all_rows)
    res: Dict[str, Any] = {"present": True, "rows": len(day_rows), "base": (day or allt or {}).get("base", "A"),
                           "variants": (day or {}).get("variants") or [], "signals": (day or {}).get("signals"),
                           "all_time": allt}
    if not day:
        res["note"] = "no closed variant results for this day yet"
    return res


def build_recap(d: _date, live: Optional[Dict[str, Any]] = None,
                open_positions: Optional[List[Dict[str, Any]]] = None,
                version: Optional[str] = None) -> Dict[str, Any]:
    t_build = time.time()
    start_ms, end_ms = day_bounds(d)
    now_ms = int(time.time() * 1000)
    is_today = d == datetime.now(TZ).date()
    enrich.load()
    labels = _scout_labels()

    # ---- live ledger (whole file is small; trades may span midnight)
    ledger = read_jsonl(ledger_path(), tag="ledger")
    trades_all = _trades_from_ledger(ledger)
    day_mints: List[str] = []

    # scout signals (paper log) for the day → scout label + scout_sig per mint (for slots-behind)
    sig_rows = read_jsonl(_p("SCOUT_LOG_PATH", "scout_trades.jsonl"), start_ms=start_ms - 3_600_000, end_ms=end_ms,
                          must_contain='"kind": "signal"', tag="scoutsig")
    scout_sig_by_mint: Dict[str, Dict[str, Any]] = {}
    for r in sig_rows:
        if r.get("scout_sig") and r.get("decision") == "enter":
            scout_sig_by_mint.setdefault(r.get("mint"), r)

    trades: List[Dict[str, Any]] = []
    sells_today: List[Dict[str, Any]] = []
    for tid, t in trades_all.items():
        b, sells, close = t["buy"], t["sells"], t["close"]
        entry_ms = int(b.get("entry_ms") or b.get("ts_ms") or 0)
        close_ms = int(close["ts_ms"]) if close else None
        in_day = start_ms <= entry_ms < end_ms or (close_ms and start_ms <= close_ms < end_ms) or \
            any(start_ms <= int(s.get("ts_ms") or 0) < end_ms for s in sells)
        if not in_day:
            continue
        meta = b.get("meta") or {}
        timing = meta.get("timing") or {}
        detect_ms = meta.get("detect_ms")
        d2o = timing.get("detect_to_open_ms")
        if d2o is None and detect_ms:
            d2o = entry_ms - int(detect_ms)
        size = float(b.get("size_sol") or 0)
        spent = (b.get("spent_lamports") or 0) / 1e9 or size
        pnl = None
        if close:
            pnl = (close.get("pnl") or {}).get("pnl_sol")
        if pnl is None:
            pnl = round(sum(float(s.get("pnl_sol") or 0) for s in sells), 6) if sells else None
        tokens0 = float(b.get("tokens_initial") or 0)
        exits = []
        for s in sorted(sells, key=lambda x: x.get("ts_ms") or 0):
            exits.append({"ts_ms": s.get("ts_ms"), "time": ct_label(s.get("ts_ms")), "reason": s.get("reason"),
                          "reason_text": reason_text(s.get("reason")), "category": exit_category(s.get("reason")),
                          "sol": s.get("sol"), "pnl_sol": s.get("pnl_sol"),
                          "fraction": round(float(s.get("tokens") or 0) / tokens0, 3) if tokens0 else None,
                          "signature": s.get("signature"), "after_s": round(((s.get("ts_ms") or 0) - entry_ms) / 1000, 1)})
            if start_ms <= int(s.get("ts_ms") or 0) < end_ms:
                sells_today.append({**s, "_tid": tid})
        traders = b.get("trader_wallets") or []
        scout_w = traders[0] if traders else None
        ss = scout_sig_by_mint.get(b.get("mint")) if str(b.get("arm", "")).startswith("scout") else None
        scout_label = labels.get(scout_w) or (ss or {}).get("scout")
        final_reason = (close or {}).get("reason") or (exits[-1]["reason"] if exits else None)
        received = sum(float(s.get("sol") or 0) for s in sells)
        trades.append({
            "id": tid, "arm": b.get("arm"), "mint": b.get("mint"), "symbol": meta.get("symbol"),
            "entry_ms": entry_ms, "entry_time": ct_label(entry_ms), "size_sol": size, "spent_sol": round(spent, 6),
            "entry_price_sol": b.get("entry_price_sol"), "scout_wallet": scout_w, "scout_label": scout_label,
            "source_group": meta.get("source_group"), "router": b.get("router"), "buy_sig": b.get("buy_sig"),
            "status": "closed" if close else "open", "close_ms": close_ms, "close_time": ct_label(close_ms),
            "hold_s": round((close_ms - entry_ms) / 1000, 1) if close_ms else round((now_ms - entry_ms) / 1000, 1),
            "exits": exits, "received_sol": round(received, 6),
            "pnl_sol": pnl, "pnl_pct": (close.get("pnl") or {}).get("pnl_pct") if close else
            (round(pnl / spent * 100, 2) if pnl is not None and spent else None),
            "multiple": round(received / spent, 3) if close and spent else None,
            "exit_reason": final_reason, "exit_text": reason_text(final_reason), "exit_category": exit_category(final_reason),
            "speed": {"detect_to_open_ms": d2o, "swap_ms": timing.get("swap_ms") or meta.get("buy_ms"),
                      "gates_ms": timing.get("gates_ms"), "pre_swap_ms": timing.get("pre_swap_ms"),
                      "mesh_ms": timing.get("mesh_ms"), "mesh_wait_ms": timing.get("mesh_wait_ms"),
                      "rug_ms": timing.get("rug_ms"), "post_fill_ms": timing.get("post_fill_ms"),
                      "mesh_overlap": timing.get("mesh_overlap"), "prewarmed": timing.get("prewarmed"),
                      "edge_gap_pct": meta.get("edge_gap_pct"), "has_stage_timing": bool(timing)},
            "scout_sig": (ss or {}).get("scout_sig"),
        })
        day_mints.append(b.get("mint"))
    trades.sort(key=lambda x: x["entry_ms"])

    # ---- exit slots: our trader_sold sell vs the scout's sell (scout_detections)
    det_rows = read_jsonl(_p("SCOUT_DETECTIONS_PATH", "scout_detections.jsonl"), start_ms=start_ms - 3_600_000,
                          end_ms=end_ms, any_of=[m for m in day_mints if m] or ["\x00none"], tag="det")
    scout_sells: Dict[str, List[Dict[str, Any]]] = {}
    for r in det_rows:
        if r.get("is_buy") is False and r.get("sig"):
            scout_sells.setdefault(r.get("mint"), []).append(r)
    want_sigs: List[str] = []
    for t in trades:
        if t["buy_sig"] and t["scout_sig"]:
            want_sigs += [t["buy_sig"], t["scout_sig"]]
        for e in t["exits"]:
            if e["category"] == "scout" and e.get("signature"):
                cands = [r for r in scout_sells.get(t["mint"], []) if (r.get("wallet") == t["scout_wallet"] or not t["scout_wallet"])
                         and (r.get("ts_ms") or 0) <= (e["ts_ms"] or 0) + 2000]
                if cands:
                    e["scout_sell_sig"] = max(cands, key=lambda r: r.get("ts_ms") or 0)["sig"]
                    want_sigs += [e["signature"], e["scout_sell_sig"]]
    for t in trades:
        sb, ss_ = enrich.slots.get(t["buy_sig"] or ""), enrich.slots.get(t["scout_sig"] or "")
        t["speed"]["slots_behind_scout"] = (sb - ss_) if isinstance(sb, int) and isinstance(ss_, int) else None
        ex_slots = []
        for e in t["exits"]:
            a, b2 = enrich.slots.get(e.get("signature") or ""), enrich.slots.get(e.get("scout_sell_sig") or "")
            if isinstance(a, int) and isinstance(b2, int):
                e["slots_behind_scout"] = a - b2
                ex_slots.append(a - b2)
        t["speed"]["exit_slots_behind_scout"] = ex_slots[0] if ex_slots else None

    # ---- realized P&L timeline (sell events in the day)
    sells_today.sort(key=lambda s: s.get("ts_ms") or 0)
    cum, timeline = 0.0, []
    tmap = {t["id"]: t for t in trades}
    for s in sells_today:
        cum += float(s.get("pnl_sol") or 0)
        tt = tmap.get(s["_tid"]) or {}
        timeline.append({"ts_ms": s.get("ts_ms"), "cum_pnl_sol": round(cum, 6), "pnl_sol": s.get("pnl_sol"),
                         "category": exit_category(s.get("reason")), "reason_text": reason_text(s.get("reason")),
                         "mint": s.get("mint"), "trade_id": s["_tid"], "symbol": tt.get("symbol")})
    realized = round(cum, 6)
    buys_today = [t for t in trades if start_ms <= t["entry_ms"] < end_ms]
    closed = [t for t in trades if t["status"] == "closed" and t["close_ms"] and start_ms <= t["close_ms"] < end_ms]
    wins = [t for t in closed if (t["pnl_sol"] or 0) > 0]
    deployed = sum(t["spent_sol"] for t in buys_today) or None

    # ---- dodged / skipped (live gates) + learning-log outcomes
    blocks: List[Dict[str, Any]] = []
    for r in ledger:
        ts = int(r.get("ts_ms") or 0)
        if not (start_ms <= ts < end_ms):
            continue
        if r.get("kind") == "buy_aborted":
            reason = str(r.get("reason") or "")
            cat = _block_category(reason)
            blocks.append({"ts_ms": ts, "time": ct_label(ts), "mint": r.get("mint"), "category": cat,
                           "label": _block_label(cat), "reasons": [x.strip() for x in reason.split(";") if x.strip()][:6]})
        elif r.get("kind") == "tg_buy_refused":
            blocks.append({"ts_ms": ts, "time": ct_label(ts), "mint": r.get("mint"), "category": "tg_refused",
                           "label": _block_label("tg_refused"), "arm": r.get("arm"), "reasons": (r.get("reasons") or [])[:8]})
    cands = read_jsonl(_p("CANDIDATES_PATH", "candidates.jsonl"), start_ms=start_ms, end_ms=end_ms, tag="cand")
    block_mints = [b["mint"] for b in blocks if b.get("mint")]
    outs = read_jsonl(_p("CANDIDATE_OUTCOMES_PATH", "candidate_outcomes.jsonl"), start_ms=start_ms,
                      end_ms=end_ms + 3 * 3_600_000, any_of=block_mints or ["\x00none"], tag="outs")
    by_mint: Dict[str, Dict[int, Dict[str, Any]]] = {}
    for o in outs:
        if o.get("kind") == "outcome" and isinstance(o.get("horizon_s"), int):
            by_mint.setdefault(o.get("mint"), {}).setdefault(o["horizon_s"], o)
    for b in blocks:
        m = b.get("mint")
        if not b.get("arm"):
            near = [c for c in cands if c.get("mint") == m and abs((c.get("ts_ms") or 0) - b["ts_ms"]) < 120_000]
            if near:
                b["arm"] = near[0].get("arm")
        hz = by_mint.get(m) or {}
        rets = {h: o.get("ret") for h, o in sorted(hz.items()) if isinstance(o.get("ret"), (int, float))}
        peak = max(rets.values()) if rets else None
        peak_h = max(rets, key=lambda h: rets[h]) if rets else None
        last = rets[max(rets)] if rets else None
        verdict, why = _outcome_verdict(peak, last)
        base_price = (hz.get(0) or {}).get("price_sol")
        b.update({"outcome": {"returns_by_horizon_s": rets, "peak_ret": peak, "last_ret": last,
                              "peak_horizon_s": peak_h, "verdict": verdict, "verdict_text": why,
                              "base_price_sol": base_price}})
        day_mints.append(m)

    # ---- learning log + paper summaries (descriptive)
    arms: Dict[str, Dict[str, int]] = {}
    skip_reasons: Dict[str, int] = {}
    for c in cands:
        a = arms.setdefault(c.get("arm") or "?", {"signals": 0, "enter": 0, "skip": 0})
        a["signals"] += 1
        if c.get("decision") in ("enter", "skip"):
            a[c["decision"]] += 1
        if c.get("decision") == "skip" and c.get("skip_reason"):
            skip_reasons[c["skip_reason"]] = skip_reasons.get(c["skip_reason"], 0) + 1
    paper_rows = read_jsonl(_p("PAPER_LOG_PATH", "paper_trades.jsonl"), start_ms=start_ms, end_ms=end_ms,
                            must_contain='"kind": "close"', tag="paperclose")
    paper: Dict[str, Dict[str, Any]] = {}
    for r in paper_rows:
        pr = paper.setdefault(r.get("profile") or "?", {"closed": 0, "pnl_sol": 0.0, "wins": 0})
        v = float((r.get("pnl") or {}).get("pnl_sol") or 0)
        pr["closed"] += 1
        pr["pnl_sol"] = round(pr["pnl_sol"] + v, 6)
        pr["wins"] += 1 if v > 0 else 0
    trial = read_jsonl(_p("SCOUT_TRIAL_LOG", "processed_trial.jsonl"), start_ms=start_ms, end_ms=end_ms, tag="trial")
    trial_sum: Dict[str, int] = {}
    for r in trial:
        trial_sum[r.get("verdict") or "?"] = trial_sum.get(r.get("verdict") or "?", 0) + 1
    bad_trial = sum(1 for r in trial if r.get("verdict") != "landed" and r.get("live_filled"))

    # ---- symbols / now-prices from the DEX cache (filled in by the background task)
    for t in trades:
        dx = enrich.dex_for(t["mint"])
        if dx and not t["symbol"]:
            t["symbol"] = dx.get("symbol")
        t["now"] = {k: dx.get(k) for k in ("price_sol", "mcap_usd", "liquidity_usd", "chg_h1")} if dx else None
    for b in blocks:
        dx = enrich.dex_for(b.get("mint") or "")
        b["symbol"] = (dx or {}).get("symbol")
        bp = (b.get("outcome") or {}).get("base_price_sol")
        b["now"] = {"price_sol": dx.get("price_sol"), "mcap_usd": dx.get("mcap_usd"), "liquidity_usd": dx.get("liquidity_usd"),
                    "chg_since_block_pct": round((dx["price_sol"] / bp - 1) * 100, 1) if dx.get("price_sol") and bp else None} if dx else None
    for p in timeline:
        p["symbol"] = p.get("symbol") or (tmap.get(p["trade_id"]) or {}).get("symbol")

    # ---- speed panel (buckets split on the deploys that fall inside this day)
    deps = [x for x in _deploy_ms() if start_ms <= x[0] < end_ms]
    edges = [start_ms] + [x[0] for x in deps] + [end_ms]
    names = ["before " + deps[0][1] if deps else "all day"] + [f"after {x[1]}" for x in deps]
    buckets = []
    for i in range(len(edges) - 1):
        ts_ = [t for t in buys_today if edges[i] <= t["entry_ms"] < edges[i + 1]]
        buckets.append({"label": names[i], "what": deps[i - 1][2] if i > 0 else None, "trades": len(ts_),
                        "median_detect_to_open_s": _med([t["speed"]["detect_to_open_ms"] / 1000 for t in ts_
                                                         if t["speed"]["detect_to_open_ms"] is not None]),
                        "median_swap_ms": _med([t["speed"]["swap_ms"] for t in ts_ if t["speed"]["swap_ms"]]),
                        "median_gates_ms": _med([t["speed"]["gates_ms"] for t in ts_ if t["speed"]["gates_ms"]]),
                        "median_slots_behind": _med([t["speed"]["slots_behind_scout"] for t in ts_
                                                     if t["speed"]["slots_behind_scout"] is not None]),
                        "median_exit_slots_behind": _med([t["speed"]["exit_slots_behind_scout"] for t in ts_
                                                          if t["speed"]["exit_slots_behind_scout"] is not None]),
                        "median_fill_vs_scout_pct": _med([t["speed"]["edge_gap_pct"] for t in ts_
                                                          if t["speed"]["edge_gap_pct"] is not None])})
    allb = [t for t in buys_today if t["speed"]["detect_to_open_ms"] is not None]
    speed = {"median_detect_to_open_s": _med([t["speed"]["detect_to_open_ms"] / 1000 for t in allb]),
             "median_slots_behind": _med([t["speed"]["slots_behind_scout"] for t in buys_today
                                          if t["speed"]["slots_behind_scout"] is not None]),
             "median_exit_slots_behind": _med([t["speed"]["exit_slots_behind_scout"] for t in buys_today
                                               if t["speed"]["exit_slots_behind_scout"] is not None]),
             "median_fill_vs_scout_pct": _med([t["speed"]["edge_gap_pct"] for t in buys_today
                                               if t["speed"]["edge_gap_pct"] is not None]),
             "stage_timed_trades": sum(1 for t in buys_today if t["speed"]["has_stage_timing"]),
             "deploys": [{"ts_ms": x[0], "time": x[1], "what": x[2]} for x in deps], "buckets": buckets}

    best = max(closed, key=lambda t: t["pnl_sol"] or 0, default=None)
    worst = min(closed, key=lambda t: t["pnl_sol"] or 0, default=None)
    xv = _exit_variants(start_ms, end_ms)
    tiles = {"realized_pnl_sol": realized, "realized_pnl_pct": round(realized / deployed * 100, 2) if deployed else None,
             "deployed_sol": round(deployed, 4) if deployed else 0.0, "trades": len(buys_today), "closed": len(closed),
             "wins": len(wins), "losses": len(closed) - len(wins),
             "win_rate": round(len(wins) / len(closed), 3) if closed else None,
             "best": _mini(best), "worst": _mini(worst),
             "open_positions": (live or {}).get("open_positions") if is_today else 0,
             "blocks": len(blocks)}
    out = {"ok": True, "read_only": True, "date": d.isoformat(), "is_today": is_today, "tz": str(TZ),
           "day_start_ms": start_ms, "day_end_ms": end_ms, "generated_ms": now_ms, "generated_time": ct_label(now_ms),
           "version": version, "tiles": tiles, "status": _status_chips(live) if is_today else None,
           "open": open_positions if is_today else [],
           "timeline": timeline, "markers": {"buys": [{"ts_ms": t["entry_ms"], "mint": t["mint"], "symbol": t["symbol"],
                                                       "arm": t["arm"], "trade_id": t["id"]} for t in buys_today],
                                             "blocks": [{"ts_ms": b["ts_ms"], "mint": b["mint"], "category": b["category"],
                                                         "symbol": b.get("symbol")} for b in blocks]},
           "trades": trades, "blocks": blocks, "speed": speed, "exit_variants": xv,
           "learning": {"arms": arms, "top_skip_reasons": sorted(skip_reasons.items(), key=lambda x: -x[1])[:8],
                        "signals": len(cands)},
           "paper": paper, "processed_trial": {"rows": len(trial), "verdicts": trial_sum, "bad_trigger_live_buys": bad_trial},
           "enrichment": {"net": _net_enabled(), "dex_cached": sum(1 for m in set(day_mints) if m in enrich.dex),
                          "stats": dict(enrich.stats)},
           "_want": {"mints": list(dict.fromkeys(m for m in day_mints if m)), "sigs": list(dict.fromkeys(want_sigs))}}
    out["take"] = coach_take(out)
    out["build_ms"] = round((time.time() - t_build) * 1000, 1)
    return out


def _mini(t: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not t:
        return None
    return {"id": t["id"], "mint": t["mint"], "symbol": t["symbol"], "pnl_sol": t["pnl_sol"], "pnl_pct": t["pnl_pct"],
            "exit_text": t["exit_text"], "arm": t["arm"]}


def _status_chips(live: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not live:
        return None
    return {k: live.get(k) for k in ("buys_enabled", "kill", "arms", "open_positions", "inflight_buys", "pending_buys",
                                     "trades_today", "realized_pnl_today_sol", "unrealized_open_sol", "trade_cap",
                                     "scout_bench")} | {
        "warnings": len(live.get("warnings") or []),
        "trail_pct": ((live.get("exits") or {}).get("trail_pct"))}


def _name(t: Dict[str, Any]) -> str:
    return t.get("symbol") or short(t.get("mint"), 4)


def _sol(x: float) -> str:
    return f"{'+' if x >= 0 else '−'}{abs(x):.4f} SOL"


def coach_take(r: Dict[str, Any]) -> List[str]:
    """Deterministic plain-English summary (no LLM)."""
    tl, trades, blocks = r["tiles"], r["trades"], r["blocks"]
    lines: List[str] = []
    if not tl["trades"] and not tl["closed"]:
        lines.append("No live trades " + ("yet today." if r["is_today"] else "on this day."))
    else:
        pct = f" ({tl['realized_pnl_pct']:+.1f}% on {tl['deployed_sol']:.2f} SOL deployed)" if tl.get("realized_pnl_pct") is not None else ""
        wl = f", {tl['wins']} win{'s' if tl['wins'] != 1 else ''} / {tl['losses']} loss{'es' if tl['losses'] != 1 else ''}" if tl["closed"] else ""
        lines.append(f"{'Up' if tl['realized_pnl_sol'] >= 0 else 'Down'} {abs(tl['realized_pnl_sol']):.4f} SOL{pct} "
                     f"on {tl['trades']} trade{'s' if tl['trades'] != 1 else ''}{wl}.")
    closed = [t for t in trades if t["status"] == "closed" and t.get("pnl_sol") is not None]
    if closed:
        best = max(closed, key=lambda t: t["pnl_sol"])
        pos_sum = sum(t["pnl_sol"] for t in closed if t["pnl_sol"] > 0)
        if best["pnl_sol"] > 0:
            cats = [e["category"] for e in best["exits"]]
            how = "a 2× take-profit + trailing stop" if "tp" in cats and "trail" in cats else (best["exit_text"] or "its exit").lower()
            carried = best["pnl_sol"] >= 0.6 * pos_sum and tl["realized_pnl_sol"] < best["pnl_sol"]
            lines.append(f"{_name(best)} {'carried the day' if carried else 'was the best trade'} with {how}: "
                         f"{_sol(best['pnl_sol'])}" + (f" ({best['pnl_pct']:+.0f}%)." if best.get("pnl_pct") is not None else "."))
        sc = [t for t in closed if t["exit_category"] == "scout"]
        if sc:
            s = sum(t["pnl_sol"] for t in sc)
            lines.append(f"Following the scout out of {len(sc)} trade{'s' if len(sc) != 1 else ''} "
                         f"{'cost' if s < 0 else 'made'} {abs(s):.4f} SOL — he usually sells within seconds to a minute.")
        rg = [t for t in closed if t["exit_category"] == "rug"]
        if rg:
            lines.append(f"The rug alarm pulled us out of {len(rg)} coin{'s' if len(rg) != 1 else ''} "
                         f"({_sol(sum(t['pnl_sol'] for t in rg))}).")
    if blocks:
        v = [b["outcome"]["verdict"] for b in blocks]
        dodged = sum(1 for x in v if x in ("dodged_rug", "dodged_dump"))
        missed = [b for b in blocks if b["outcome"]["verdict"] == "missed_winner"]
        txt = f"Safety gates turned down {len(blocks)} buy{'s' if len(blocks) != 1 else ''}"
        bits = []
        if dodged:
            bits.append(f"{dodged} fell hard afterward (dodged)")
        if missed:
            m = max(missed, key=lambda b: b["outcome"]["peak_ret"] or 0)
            ph = m["outcome"].get("peak_horizon_s")
            within = f" within {ph // 60} min" if ph and ph >= 60 else ""
            bits.append(f"{len(missed)} {'was a missed winner' if len(missed) == 1 else 'were missed winners'} "
                        f"({m.get('symbol') or short(m.get('mint'))} hit {1 + (m['outcome']['peak_ret'] or 0):.1f}× "
                        f"the block price{within})")
        lines.append(txt + (": " + ", ".join(bits) if bits else "") + ".")
    sp = r["speed"]
    if sp.get("median_detect_to_open_s") is not None:
        s = f"Median entry: {sp['median_detect_to_open_s']:.1f} s from the scout's buy to our position"
        if sp.get("median_slots_behind") is not None:
            s += f", {sp['median_slots_behind']:.0f} slots behind him"
        bk = [b for b in sp["buckets"] if b["median_detect_to_open_s"] is not None]
        if len(bk) >= 2:
            s += f" ({bk[0]['median_detect_to_open_s']:.1f} s {bk[0]['label']} → {bk[-1]['median_detect_to_open_s']:.1f} s {bk[-1]['label']})"
        lines.append(s + ".")
    elif sp.get("deploys") and r["is_today"]:
        lines.append("No trades since today's speed deploys yet, so the new entry speed isn't measured.")
    if r["is_today"]:
        try:
            from app.recap_wallet import wallet_brief
            wb = wallet_brief()
        except Exception:  # noqa: BLE001
            wb = None
        if wb and wb.get("sol") is not None:
            usd = f" (~${wb['sol'] * wb['sol_usd']:,.0f})" if wb.get("sol_usd") else ""
            lines.append(f"Trading wallet {wb['short']} holds {wb['sol']:.4f} SOL{usd}.")
        st = r.get("status") or {}
        n = st.get("open_positions") or 0
        if st:
            lines.append(("Nothing open right now" if not n else f"{n} position{'s' if n != 1 else ''} open "
                          f"({_sol(st.get('unrealized_open_sol') or 0)} unrealized)") +
                         (" · kill switch OFF, live buys ON." if st.get("buys_enabled") and not st.get("kill")
                          else " · kill switch ON." if st.get("kill") else " · live buys OFF."))
    return lines


# ------------------------------------------------------------------ cache + endpoints
_resp_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}


def _live_snapshot() -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    """Runs ON the event loop (like /health) so it never races the engine's dicts from another thread."""
    try:
        from app.live.engine import engine as live_engine
        s = live_engine.summary()
        ops = []
        for p in list(live_engine.open_positions()):
            try:
                pn = p.pnl()
            except Exception:  # noqa: BLE001
                pn = {}
            ops.append({"arm": p.arm, "mint": p.mint, "entry_ms": p.entry_ms, "entry_time": ct_label(p.entry_ms),
                        "size_sol": p.size_sol, "pnl_sol": pn.get("pnl_sol"), "pnl_pct": pn.get("pnl_pct"),
                        "tp_done": getattr(p, "tp_done", None)})
        return s, ops
    except Exception as exc:  # noqa: BLE001
        logger.info("recap live snapshot unavailable: %s", type(exc).__name__)
        return None, []


async def get_recap(d: _date, *, wait_enrich_s: float = 2.5) -> Dict[str, Any]:
    key = d.isoformat()
    is_today = d == datetime.now(TZ).date()
    ttl = float(os.getenv("RECAP_CACHE_S", "20")) if is_today else 300.0
    hit = _resp_cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    try:
        from app import __version__ as ver
    except Exception:  # noqa: BLE001
        ver = None
    live, ops = _live_snapshot() if is_today else (None, [])
    r = await run_in_threadpool(build_recap, d, live, ops, ver)
    want = r.pop("_want", {})
    mints, sigs = enrich.wants(want.get("mints") or [], want.get("sigs") or [], ttl_s=300 if is_today else 3600)
    task = enrich.schedule(mints, sigs)
    if task is not None and (mints or sigs) and wait_enrich_s > 0:
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=wait_enrich_s)
            r = await run_in_threadpool(build_recap, d, live, ops, ver)
            r.pop("_want", None)
        except Exception:  # noqa: BLE001
            pass                                                   # enrichment lands on the next refresh
    if len(_resp_cache) > 30:
        _resp_cache.clear()
    _resp_cache[key] = (time.time(), r)
    return r


@router.get("/api/recap/today")
async def recap_today() -> Dict[str, Any]:
    return await get_recap(parse_day("today"))


@router.get("/api/recap")
async def recap_day(date: Optional[str] = Query(default=None, description="YYYY-MM-DD (America/Chicago); default today")) -> Dict[str, Any]:
    return await get_recap(parse_day(date))


@router.get("/recap", include_in_schema=False)
def recap_page() -> FileResponse:
    if not STATIC_HTML.exists():
        raise HTTPException(status_code=404, detail="recap.html missing")
    return FileResponse(str(STATIC_HTML), media_type="text/html", headers={"Cache-Control": "no-cache"})


# v1.17.4 read-only wallet panel (/api/recap/wallet) — public addresses only
from app.recap_wallet import router as _wallet_router  # noqa: E402
router.include_router(_wallet_router)
