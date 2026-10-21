#!/usr/bin/env python3
"""v1.37 paper realism calibration (Sky, Oct 18 2026: "have paper act more like live").

Reads REAL live data only (data/live_trades.jsonl + data/direct_pump.jsonl) and writes
data/paper_realism_calib.json, which app.paper_realism loads (mtime-cached) for every paper arm.

Per route group (kol_confluence / scout) and buy path (jupiter / pump_direct):
  * signal→land latency  = meta.timing.detect_to_open_ms (p50 / p75)
  * entry slippage        = our fill vs the trader's own fill (meta.timing.fill_vs_scout_pct, else entry/ref_price)
  * priority lamports     = meta.order.priority_lamports (p50)
Sells (all live closes in the window):
  * hard_stop fill ratio  = realized multiple / (1 - LIVE_HARD_STOP_PCT)       (p50, capped at 1.0)
  * drain multiple        = realized multiple on rug_flag_liquidity_drain sells   (p50, vs entry)
  * generic sell ratio    = direct-path actual SOL out / expected SOL out          (p50, capped at 1.0)
  * sell latency          = direct-path sell confirm + build time                 (p50 / p75)
Read-only; never touches live state. Usage: python3 scripts/paper_realism_calibrate.py [--days 14] [--out PATH]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def q(v, p):
    v = sorted(x for x in v if x is not None)
    if not v:
        return None
    return v[min(len(v) - 1, int(p * len(v)))]


def group_of(arm: str) -> str:
    return "kol_confluence" if arm == "kol_confluence" else "scout" if str(arm).startswith("scout") else "other"


def calibrate(data_dir: Path, days: float, hard_stop_pct: float = 0.35) -> dict:
    rows = []
    with (data_dir / "live_trades.jsonl").open() as fh:
        for line in fh:
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    now = max(r.get("ts_ms", 0) for r in rows) if rows else int(time.time() * 1000)
    cut = now - days * 86400_000
    buys = {r["id"]: r for r in rows if r.get("kind") == "buy" and r.get("id")}
    acc = defaultdict(lambda: defaultdict(list))
    for b in buys.values():
        if b.get("ts_ms", 0) < cut:
            continue
        m = b.get("meta") or {}
        t = m.get("timing") or {}
        o = m.get("order") or {}
        g = group_of(b.get("arm"))
        path = t.get("path") or "jupiter"
        slip = t.get("fill_vs_scout_pct")
        if slip is None and m.get("ref_price_sol") and b.get("entry_price_sol"):
            slip = (b["entry_price_sol"] / m["ref_price_sol"] - 1) * 100
        for key in ((g, path), (g, "all"), ("all", "all")):
            a = acc[key]
            a["n"].append(1)
            if t.get("detect_to_open_ms") is not None:
                a["lat"].append(float(t["detect_to_open_ms"]))
            if slip is not None:
                a["slip"].append(float(slip))
            if o.get("priority_lamports") is not None:
                a["prio"].append(int(o["priority_lamports"]))
            if o.get("fee_bps") is not None:
                a["fee"].append(int(o["fee_bps"]))
    routes = {}
    for (g, path), a in acc.items():
        routes.setdefault(g, {})[path] = {
            "n": len(a["n"]), "lat_ms_p50": q(a["lat"], .5), "lat_ms_p75": q(a["lat"], .75),
            "slip_pct_p25": q(a["slip"], .25), "slip_pct_p50": q(a["slip"], .5), "slip_pct_p75": q(a["slip"], .75),
            "priority_lamports_p50": q(a["prio"], .5), "fee_bps_p50": q(a["fee"], .5)}
    # sells
    sells = defaultdict(list)
    for s in rows:
        if s.get("kind") != "sell" or s.get("ts_ms", 0) < cut:
            continue
        b = buys.get(s.get("id"))
        if not b or not s.get("tokens") or not b.get("entry_price_sol"):
            continue
        mult = (s["sol"] / (s["tokens"] / 10 ** b.get("decimals", 6))) / b["entry_price_sol"]
        sells[s.get("reason")].append(mult)
        sells[(group_of(b.get("arm")), s.get("reason"))].append(mult)
    hs = sells.get("hard_stop") or []
    level = 1 - hard_stop_pct
    direct_ratio, sell_lat = [], []
    try:
        with (data_dir / "direct_pump.jsonl").open() as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("kind") != "live" or not d.get("ok") or d.get("ts_ms", 0) < cut - 7 * 86400_000:
                    continue
                tm = d.get("timing") or {}
                if d.get("side") == "sell":
                    exp = (d.get("order") or {}).get("out_amount")
                    if exp and d.get("total_out"):
                        direct_ratio.append(d["total_out"] / exp)
                    tot = sum(x for x in (tm.get(k) for k in ("fetch_ms", "build_ms", "sign_ms", "sim_ms", "send_ms",
                                                                 "confirm_ms")) if x)
                    if tot:
                        sell_lat.append(tot)
    except FileNotFoundError:
        pass
    by_reason = {str(k): {"n": len(v), "mult_p25": q(v, .25), "mult_p50": q(v, .5), "mult_p75": q(v, .75)}
                 for k, v in sells.items() if isinstance(k, str)}
    return {
        "version": "v1.37", "generated_ms": int(time.time() * 1000), "window_days": days,
        "window_end_ms": now, "source": "data/live_trades.jsonl + data/direct_pump.jsonl (live fills only)",
        "routes": routes,
        "sell": {
            "hard_stop_ratio_p50": min(1.0, q(hs, .5) / level) if hs else None,
            "hard_stop_ratio_p25": min(1.0, q(hs, .25) / level) if hs else None,
            "hard_stop_ratio_p10": min(1.0, q(hs, .10) / level) if hs else None,
            "drain_mult_p10": q(sells.get("rug_flag_liquidity_drain") or [], .10),
            "drain_mult_p50": q(sells.get("rug_flag_liquidity_drain") or [], .5),
            "generic_ratio_p50": min(1.0, q(direct_ratio, .5)) if direct_ratio else None,
            "latency_ms_p50": q(sell_lat, .5), "latency_ms_p75": q(sell_lat, .75),
            "by_reason": by_reason},
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=14.0)
    ap.add_argument("--data", default=os.getenv("COACH_DATA_DIR") or str(ROOT / "data"))
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    d = Path(a.data)
    out = Path(a.out) if a.out else d / "paper_realism_calib.json"
    res = calibrate(d, a.days, float(os.getenv("LIVE_HARD_STOP_PCT", "0.35")))
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(res, indent=1))
    tmp.replace(out)
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
