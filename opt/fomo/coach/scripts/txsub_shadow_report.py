#!/usr/bin/env python3
"""v1.38b report: txsub shadow vs the coach's current detection, and what each saved block is worth.
Read-only. Usage: txsub_shadow_report.py [hours=24]"""
import json, os, sys, time, statistics as st
D = "/opt/fomo/coach/data"
H = float(sys.argv[1]) if len(sys.argv) > 1 else 24
since = (time.time() - H * 3600) * 1000
def rows(f, pred=lambda r: True):
    out = []
    try:
        for l in open(os.path.join(D, f)):
            try:
                r = json.loads(l)
            except ValueError:
                continue
            if r.get("ts_ms", 0) >= since and pred(r):
                out.append(r)
    except OSError:
        pass
    return out
def q(v):
    v = sorted(x for x in v if x is not None)
    return {"n": len(v), "p50": round(v[len(v) // 2], 2), "p90": round(v[int(len(v) * .9)], 2)} if v else {"n": 0}
sh = rows("txsub_shadow.jsonl")
det = {r["sig"]: r for r in sh if r.get("kind") == "detect"}
bud = [r for r in sh if r.get("kind") == "budget" and r.get("prices", {}).get("0")]
coach = {r["sig"]: r for r in rows("scout_detections.jsonl", lambda r: r.get("kind") == "trade")}
both = [s for s in det if s in coach]
lead = [coach[s]["recv_ms"] - det[s]["recv_ms"] for s in both]
rep = {"hours": H, "shadow_detects": len(det), "coach_detects": len(coach), "matched": len(both),
       "shadow_only": len([s for s in det if s not in coach]), "coach_only": len([s for s in coach if s not in det]),
       "shadow_lead_ms_vs_coach": q(lead), "shadow_slot_lag_at_recv": q([det[s].get("slot_lag") for s in det])}
# value per block: entry price at +N vs +10 (today's p50 landing is ~10 blocks), applied to realistic/live exits
real = {}
for r in rows("scout_real.jsonl", lambda r: r.get("kind") == "real_close"):
    real.setdefault(r["mint"], r)
live = {}
for r in rows("live_trades.jsonl", lambda r: r.get("kind") == "close" and str(r.get("arm", "")).startswith("scout")):
    live.setdefault(r["mint"], r)
per = {n: [] for n in ("0", "2", "4", "10")}
pnl = {n: [] for n in per}
for b in bud:
    p = b["prices"]
    if "10" not in p:
        continue
    for n in per:
        if n in p:
            per[n].append(100 * (p[n] / p["10"] - 1))
    ex = live.get(b["mint"]) or real.get(b["mint"])
    if ex:
        size = float(ex.get("size_sol") or 0.2)
        mult = 1 + (ex.get("pnl", {}).get("pnl_pct") if isinstance(ex.get("pnl"), dict) else ex.get("pnl_pct", 0)) / 100
        for n in per:                        # tokens scale with p10/pN at the same exit
            if n in p:
                pnl[n].append(size * mult * p["10"] / p[n] - size)
rep["entry_vs_10_blocks_pct"] = {n: q(v) for n, v in per.items()}
rep["pnl_sol_by_budget"] = {n: {"n": len(v), "sum": round(sum(v), 4)} for n, v in pnl.items()}
if pnl["10"] and pnl["2"]:
    rep["sol_per_block_saved_10_to_2"] = round((sum(pnl["2"]) - sum(pnl["10"])) / 8, 5)
    rep["sol_per_trade_10_to_2"] = round((sum(pnl["2"]) - sum(pnl["10"])) / len(pnl["2"]), 5)
print(json.dumps(rep, indent=1))
