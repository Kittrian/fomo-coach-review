#!/usr/bin/env python3
"""v1.38 realistic paper book for the daily self-learner (PAPER / OFFLINE ONLY).

Run by learn_daily.py as a SUBPROCESS (so the learner itself never imports app code). Reads the paper logs and the
coach's non-secret snapshot (data/paper_realism_snapshot.json: per-arm realism params + live exit/gate numbers; the
learner cannot read .env) and writes ONE file: data/learn/realistic_book.json with

  books.<arm>   every paper position re-priced the way live fills: realistic entry (never below the trader's fill,
                live slippage), the live twin's exit rules, live sell ratios, fees, priority + tip, drain proxy, gate
                tags. A position that already has a logged v1.37 realistic close (track real_*) uses that row as-is;
                older positions are replayed on the marks the arm logged (scripts/paper_realism_replay.fomo_family).
  variants      exit_variants rows: logged v1.38 realistic twins (variant_closed_real) as-is; older rows
                entry-adjusted (entry = max(+2 s curve fill, scout's own fill); legacy 4% haircut / 1.5% overhead
                swapped for the live cost model; exits kept, so this part is approximate).
  costs         the realistic cost model for learn_v2's tick replays.

Uses only the pure exit-decision function from the live engine module (no engine instance, no network: the
learner unit has PrivateNetwork=yes) and never writes outside data/learn.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(os.getenv("FOMO_COACH_DIR", str(Path(__file__).resolve().parents[1])))
CODE = Path(__file__).resolve().parents[1]
DATA = Path(os.getenv("FOMO_DATA_DIR", str(ROOT / "data")))
OUT = DATA / "learn"
sys.path.insert(0, str(CODE))
sys.path.insert(0, str(CODE / "scripts"))
# The learner unit makes .env inaccessible (InaccessiblePaths) and app/config.py calls load_dotenv() at import, which
# would raise PermissionError there. This process must never read .env anyway (live numbers come from the coach's
# non-secret snapshot), so app's dotenv load is a no-op here.
try:
    import dotenv as _dotenv
    _dotenv.load_dotenv = lambda *a, **k: False
except Exception:  # pragma: no cover
    pass
os.environ.setdefault("PAPER_REALISM_ONCHAIN_ENABLED", "false")
os.environ.setdefault("PAPER_REALISM_SNAPSHOT_ENABLED", "false")
os.environ.setdefault("COACH_DATA_DIR", str(DATA))
os.environ.setdefault("PAPER_REALISM_CALIB_PATH", str(DATA / "paper_realism_calib.json"))

ARTIFACT_MULT = 50.0
BOOKS = {  # arm log → (realism arm, legacy comparison track, realistic record track logged by v1.37)
    "kol_confluence": ("kol_confluence.jsonl", "kol_confluence", "half2x_c30", "real_live_exit"),
    "paper_v3": ("paper_v3.jsonl", "paper_v3_new", "half2x_c30", "real_follow_wallet"),
    "paper_top4": ("paper_top4.jsonl", "paper_top4", "half2x_c30", "real_follow_wallet"),
}


def rows(p: Path, kinds=None):
    out = []
    if not p.exists():
        return out
    with p.open(errors="replace") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if kinds is None or r.get("kind") in kinds:
                out.append(r)
    return out


def load_snapshot():
    try:
        return json.loads((DATA / "paper_realism_snapshot.json").read_text())
    except Exception:  # noqa: BLE001
        return None


def arm_params(pr, snap, arm):
    """Realism params for arm with the LIVE config from the coach snapshot (falls back to code defaults)."""
    P = pr.params(arm)
    if snap:
        sp = (snap.get("params") or {}).get(arm) or {}
        P.update({k: v for k, v in sp.items() if k != "live_cfg"})
        lc = dict(P.get("live_cfg") or {})
        lc.update(snap.get("live_cfg") or {})
        P["live_cfg"] = lc
    return P


def book(R, pr, snap, name):
    log, arm, legacy_track, real_track = BOOKS[name]
    P = arm_params(pr, snap, arm)
    lc = P["live_cfg"]
    now = int(time.time() * 1000)
    rep = R.fomo_family(arm, DATA / log, 0, now + 1, legacy_track, P, lc)
    logged = {r["id"]: r for r in rows(DATA / log, {"close"}) if r.get("realistic") and r.get("track") == real_track}
    out = []
    for r in rep:
        if (r.get("legacy_max_multiple") or 0) > ARTIFACT_MULT or (r.get("legacy_pnl_sol") or 0) > 10:
            continue                                          # same artifact rule as the legacy learner
        lg = logged.get(r["id"])
        rec = {"id": r["id"], "mint": r["mint"], "entry_ms": r["ts_ms"], "ts_ms": r.get("close_ms") or r["ts_ms"],
               "handles": r.get("handles") or [], "legacy_track": legacy_track,
               "legacy_pnl_sol": r.get("legacy_pnl_sol"), "size_sol": P["size_sol"]}
        if lg:
            rec.update(pnl_sol=lg.get("pnl_sol") or 0.0, gated=lg.get("gated"), basis="logged_" + real_track,
                       reason=lg.get("reason"))
        elif r.get("skipped"):
            rec.update(skipped=r["skipped"], basis="replayed")
        else:
            rec.update(pnl_sol=round(r["real_pnl_sol"], 6), gated=r.get("gated"), basis="replayed",
                       reason=r.get("real_reason"), entry_vs_legacy_pct=round(r.get("real_entry_vs_legacy_pct") or 0, 2))
        out.append(rec)
    seen = {r["id"] for r in out}
    for pid, lg in logged.items():                           # realistic closes whose legacy close is still open
        if pid not in seen:
            out.append({"id": pid, "mint": lg.get("mint"), "ts_ms": lg["ts_ms"], "handles": lg.get("handles") or [],
                        "pnl_sol": lg.get("pnl_sol") or 0.0, "gated": lg.get("gated"), "basis": "logged_" + real_track,
                        "reason": lg.get("reason"), "size_sol": P["size_sol"], "legacy_track": legacy_track})
    return out, {k: v for k, v in P.items() if k != "live_cfg"}


def variants(pr, snap):
    P = arm_params(pr, snap, "scout_solo")
    size = 0.10                                              # exit_variants reports per 0.10 SOL
    hc_l, ovh_l = 0.04, 0.015
    hc_r = P["fee_bps"] / 1e4
    ovh_r = 2 * (P["priority_lamports"] + P["tip_lamports"]) / 1e9 / size
    ref = {}
    for r in rows(DATA / "scout_trades.jsonl", {"pos"}):
        p = r.get("pos") or {}
        if p.get("signal_id") and p.get("ref_price_sol"):
            ref.setdefault(p["signal_id"], p["ref_price_sol"])
    out = []
    for r in rows(DATA / "exit_variants.jsonl", {"variant_closed", "variant_closed_real"}):
        if r.get("restart_gap"):
            continue
        if r["kind"] == "variant_closed_real":
            out.append({**{k: r.get(k) for k in ("signal_id", "arm", "variant", "ts_ms", "entry_ms", "exit_reason",
                                                 "mint", "exit_ms")},
                        "pnl_sol": r.get("pnl_sol") or 0.0, "real_entry_px": r.get("entry_price_sol"),
                        "legacy_entry_px": r.get("legacy_entry_price_sol"), "basis": "logged_real"})
            continue
        e = r.get("entry_price_sol")
        tp = ref.get(r.get("signal_id")) if e else None
        tp = tp if (tp and 0.2 <= tp / e <= 5) else None
        e_real = max(e, tp or 0.0) if e else None
        legacy = r.get("pnl_sol") or 0.0
        # undo the legacy cost model → gross proceeds multiple vs legacy entry, then re-price at the realistic entry
        gross = (legacy / size + 1 + ovh_l) / (1 - hc_l)
        real = size * (gross * ((e / e_real) if e else 1.0) * (1 - hc_r) - 1 - ovh_r)
        out.append({**{k: r.get(k) for k in ("signal_id", "arm", "variant", "ts_ms", "entry_ms", "exit_reason", "mint",
                                             "exit_ms")},
                    "pnl_sol": round(real, 6), "legacy_pnl_sol": legacy, "real_entry_px": e_real, "legacy_entry_px": e,
                    "trader_px": tp, "basis": "entry_adjusted" if e else "cost_adjusted"})
    have_real = {(r["signal_id"], r["variant"]) for r in out if r["basis"] == "logged_real"}
    out = [r for r in out if r["basis"] == "logged_real" or (r["signal_id"], r["variant"]) not in have_real]
    costs = {"hc": hc_r, "ovh": ovh_r, "real_stop": [P["hard_stop_ratio"], P["hard_stop_ratio_p10"]],
             "legacy": {"hc": hc_l, "ovh": ovh_l}}
    return out, costs


def main():
    from app import paper_realism as pr
    import paper_realism_replay as R
    snap = load_snapshot()
    res = {"generated_ms": int(time.time() * 1000), "version": "v1.38",
           "snapshot": {"present": bool(snap), "generated_ms": (snap or {}).get("generated_ms")},
           "books": {}, "params": {}}
    for name in BOOKS:
        res["books"][name], res["params"][name] = book(R, pr, snap, name)
    res["variants"], res["costs"] = variants(pr, snap)
    OUT.mkdir(parents=True, exist_ok=True)
    tmp = OUT / "realistic_book.tmp"
    tmp.write_text(json.dumps(res, default=str))
    os.replace(tmp, OUT / "realistic_book.json")
    print(json.dumps({k: len(v) for k, v in res["books"].items()} | {"variants": len(res["variants"]),
                                                                       "snapshot": bool(snap)}))


if __name__ == "__main__":
    main()
