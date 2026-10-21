#!/usr/bin/env python3
"""v1.39 routine check for the scout sniper path. Read-only; appends one summary row to data/sniper_check.jsonl.
Usage: sniper_check.py [hours=24]
Checks: fee_plan rows on buys, detect-to-land latency of live scout buys (direct vs Jupiter), exit websocket health,
txsub shadow lead vs the coach's detection, signer refusals by reason (program-not-allowed must stay 0)."""
import collections, json, os, subprocess, sys, time, urllib.request
from datetime import datetime, timezone, timedelta

D = os.getenv("FOMO_DATA_DIR", "/opt/fomo/coach/data")
H = float(sys.argv[1]) if len(sys.argv) > 1 else 24
NOW = time.time() * 1000
SINCE = NOW - H * 3600 * 1000
CT = timezone(timedelta(hours=-5))


def rows(f, pred=lambda r: True, key="ts_ms"):
    out = []
    try:
        with open(os.path.join(D, f), encoding="utf-8") as fh:
            for l in fh:
                try:
                    r = json.loads(l)
                except ValueError:
                    continue
                if (r.get(key) or 0) >= SINCE and pred(r):
                    out.append(r)
    except OSError:
        pass
    return out


def q(v):
    v = sorted(x for x in v if isinstance(x, (int, float)))
    return {"n": len(v), "p50": round(v[len(v) // 2], 1), "p90": round(v[int(len(v) * .9)], 1),
            "max": round(v[-1], 1)} if v else {"n": 0}


rep = {"ts_ms": int(NOW), "ct": datetime.fromtimestamp(NOW / 1000, CT).strftime("%Y-%m-%d %I:%M %p CT"), "hours": H}
# 1) live scout buys + fee plans
led = rows("live_trades.jsonl")
buys = [r for r in led if r.get("kind") == "buy" and str(r.get("arm", "")).startswith("scout")]
def _fp(b):
    m = b.get("meta") or {}
    return (m.get("order") or {}).get("fee_plan") or (m.get("direct") or {}).get("fee_plan") or m.get("fee_plan")
fps = [_fp(b) for b in buys]
plans = rows("fee_plans.jsonl")
rep["scout_buys"] = {"n": len(buys), "by_path": dict(collections.Counter(
    ("direct" if (b.get("router") or "").startswith("pump") or (b.get("meta") or {}).get("direct") else b.get("router") or "?")
    for b in buys)),
    "with_fee_plan": sum(1 for f in fps if f), "fee_plan_rows": len(plans),
    "fee_pct_of_size": q([float(p["pct_of_size"]) for p in plans if p.get("pct_of_size") is not None]),
    "fee_cap_ok": all(float(p.get("pct_of_size") or 0) <= 0.5001 for p in plans)}
# 2) detect-to-land
lat = rows("latency_timelines.jsonl", lambda r: str(r.get("source", "")).startswith("live:scout"), key="started_ms")
by = collections.defaultdict(list)
for r in lat:
    by[(r.get("info") or {}).get("path") or "?"].append((r.get("durations_ms") or {}).get("detect_to_open_ms"))
rep["detect_to_land_ms"] = {k: q(v) for k, v in by.items()}
# 3) exit websocket
try:
    h = json.load(urllib.request.urlopen("http://127.0.0.1:8790/health", timeout=5))
    lv = h.get("live") or {}
    w = ((lv.get("exits") or {}).get("watch")) or {}
    rep["exit_ws"] = {"open_positions": lv.get("open_positions"), "connected": w.get("connected"),
                      "healthy": w.get("healthy"), "accounts": w.get("accounts"),
                      "unwatched_positions": w.get("unwatched_positions"),
                      "verdict": ("n/a (no open position)" if not lv.get("open_positions")
                                  else "ok" if w.get("healthy") and not w.get("unwatched_positions") else "CHECK")}
    rep["kill"] = lv.get("kill")
except Exception as exc:  # noqa: BLE001
    rep["exit_ws"] = {"error": type(exc).__name__}
closes = [r for r in led if r.get("kind") == "close" and str(r.get("arm", "")).startswith("scout")]
rep["scout_close_mark_source"] = dict(collections.Counter((r.get("peak") or {}).get("mark_source") for r in closes))
# 4) txsub shadow lead
try:
    out = subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "txsub_shadow_report.py"),
                          str(H)], capture_output=True, text=True, timeout=120).stdout
    t = json.loads(out)
    rep["txsub"] = {k: t.get(k) for k in ("shadow_detects", "coach_detects", "matched", "shadow_lead_ms_vs_coach",
                                          "shadow_slot_lag_at_recv", "pnl_sol_by_budget", "sol_per_block_saved_10_to_2")}
except Exception as exc:  # noqa: BLE001
    rep["txsub"] = {"error": type(exc).__name__}
# 5) signer refusals
ab = [r for r in led if r.get("kind") in ("buy_aborted", "sell_failed")]
rc = collections.Counter()
for r in ab:
    s = str(r.get("reason") or r.get("error") or "")
    for k in ("program not allowed", "24h buy volume cap", "non-allowed destination", "rate limit", "paper-only",
              "reserved for scouts"):
        if k in s:
            rc[k] += 1
rep["refusals"] = dict(rc)
pb = [r for r in led if r.get("kind") == "post_buy_check_fail"]
rep["post_buy"] = {"check_fails": len(pb), "by_check": dict(collections.Counter(r.get("check") for r in pb)),
                   "auto_exits": sum(1 for r in led if r.get("kind") == "close" and str(r.get("reason", "")).startswith(
                       ("rugcheck_post_buy", "mesh_block_late"))),
                   "mesh_late_answers": sum(1 for r in led if r.get("kind") == "mesh_late_answer"),
                   "confluence_gate_skips": sum(1 for r in led if r.get("kind") == "gate_skipped_confluence")}
rep["ok"] = rc.get("program not allowed", 0) == 0 and rc.get("non-allowed destination", 0) == 0 \
    and rep["scout_buys"]["fee_cap_ok"] and rep.get("exit_ws", {}).get("verdict") != "CHECK"
try:
    with open(os.path.join(D, "sniper_check.jsonl"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rep) + "\n")
except OSError:
    pass
print(json.dumps(rep, indent=1))
