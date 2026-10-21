#!/usr/bin/env python3
"""v1.32 Fomo daily self-learning job (PAPER ONLY). Andrew approved Oct 14, 2026 8:06 PM CT.

Runs from fomo-learn.timer (~4:07 AM CT) as user `fomo` with no credentials and no network.
READS ONLY: data/live_trades.jsonl, paper_v3.jsonl, paper_top4.jsonl, kol_confluence.jsonl, cryptohub_wallets.json,
            exit_variants.jsonl, scout_bench.jsonl,
            paper_anomalies.jsonl, latency_timelines.jsonl, candidates.jsonl (+ KILL/HALT existence, never contents).
WRITES ONLY data/learn/: scores_<date>.json, paper_overrides.json (paper arms only, clamped by app.learn_overrides
bounds), live_proposals.jsonl (suggestions for Andrew, never applied), state.json, notes/<date> … .md, learn.log.
Never imports the live engine / signer, never touches .env, KILL/HALT, live sizes, caps or arms.

v1.38 (Sky approved Oct 18, 2026 6:58 PM CT): the learner now decides on REALISTIC paper (entries priced like live
fills, live exit rules, live costs; see scripts/learn_realism.py, run as a subprocess so this file never imports app
code). The old optimistic basis (paper_v3/top4/kol half2x_c30 track, exit variants with the 4% haircut model) is still
computed and shown side by side as "legacy" in scores_<date>.json and the note; it no longer drives anything.
If the realistic book cannot be built, the run falls back to the legacy basis and says so under Anomalies.

Rules: groups with n < MIN_N (15) are reported but never acted on. At most one change per override key per day.
Every applied change is re-checked after UNDO_DAYS (7); if the paper metric it was meant to improve got worse,
it is undone automatically and the undo is written to the note.
"""
from __future__ import annotations

import json
import os
import statistics as st
import re
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")
ROOT = Path(os.getenv("FOMO_COACH_DIR", "/opt/fomo/coach"))
DATA = Path(os.getenv("FOMO_DATA_DIR", str(ROOT / "data")))
OUT = DATA / "learn"
MIN_N = int(os.getenv("LEARN_MIN_N", "15"))
UNDO_DAYS = float(os.getenv("LEARN_UNDO_DAYS", "7"))
ARTIFACT_MULT = 50.0          # paper close with max_multiple above this is treated as a mark artifact
CHASE_STEPS = [3.0, 5.0, 7.5, 10.0, 15.0]
BOUNDS = {"scout_chase_cap_pct": (3.0, 15.0)}
DEFAULTS = {"scout_chase_cap_pct": 5.0, "top4_handles": ["Yenni", "Lynk", "Inquixit", "Zef"]}
V3_HANDLES = {"Numer0", "Inquixit", "Lynk", "peacefuldestroy", "Tom", "brunowsky", "Zef", "Veloce", "Gucci", "Yenni",
              "Mitch"}
DAY = 86400_000


def ct(ms):
    return datetime.fromtimestamp(ms / 1000, CT).strftime("%a %b %-d %-I:%M %p CT") if ms else "—"


def rows(name, kinds=None):
    p = DATA / name
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


def stats(pnls):
    n = len(pnls)
    if not n:
        return {"n": 0}
    s = sorted(pnls, reverse=True)
    ex3 = s[3:]
    gw = sum(x for x in pnls if x > 0)
    gl = -sum(x for x in pnls if x < 0)
    gw3 = sum(x for x in ex3 if x > 0)
    gl3 = -sum(x for x in ex3 if x < 0)
    wins = sum(1 for x in pnls if x > 0)
    return {"n": n, "wins": wins, "wr": round(wins / n, 3), "sum": round(sum(pnls), 4),
            "mean": round(sum(pnls) / n, 5), "median": round(st.median(pnls), 5),
            "pf": round(gw / gl, 2) if gl else None, "sum_ex_top3": round(sum(ex3), 4),
            "pf_ex_top3": round(gw3 / gl3, 2) if gl3 else None,
            "bayes_wr": round((wins + 2) / (n + 7), 3),           # Beta(2,5) prior
            "actionable": n >= MIN_N}


def bucket(v, edges, labels):
    if v is None:
        return "unknown"
    for e, l in zip(edges, labels):
        if v < e:
            return l
    return labels[-1]


# ------------------------------------------------------------------ live
def live_closes():
    buys, closes = {}, {}
    for r in rows("live_trades.jsonl", {"buy", "close"}):
        (buys if r["kind"] == "buy" else closes)[r.get("id")] = r
    cands = {}
    for r in rows("candidates.jsonl"):
        if r.get("entered") or r.get("decision") == "enter":
            cands.setdefault(r.get("mint"), r.get("features") or {})
    out = []
    for i, c in closes.items():
        b = buys.get(i)
        if not b:
            continue
        m = b.get("meta") or {}
        f = cands.get(b.get("mint"), {})
        chase = (m.get("chase") or {}).get("premium_pct", m.get("edge_gap_pct"))
        pk = c.get("peak") or {}
        hour = datetime.fromtimestamp(b["entry_ms"] / 1000, CT).hour
        out.append({"id": i, "mint": b.get("mint"), "arm": b.get("arm"),
                    "source": f.get("scout") or m.get("source_group") or ((b.get("trader_wallets") or ["?"])[0] or "?")[:6],
                    "entry_ms": b["entry_ms"], "close_ms": c["ts_ms"], "hour": hour, "chase": chase,
                    "age_s": f.get("age_s"), "curve": f.get("curve_progress_pct"), "scout_sol": f.get("scout_sol") or m.get("scout_sol"),
                    "size": b.get("size_sol"), "hold_s": (c["ts_ms"] - b["entry_ms"]) / 1000, "exit": c.get("reason"),
                    "pnl": (c.get("pnl") or {}).get("pnl_sol", 0.0), "peak_x": pk.get("peak_multiple")})
    return sorted(out, key=lambda t: t["entry_ms"])


def live_scores(T):
    g = defaultdict(list)
    for t in T:
        g[("source", f"{t['arm']}/{t['source']}")].append(t["pnl"])
        g[("exit", t["exit"])].append(t["pnl"])
        g[("chase", bucket(t["chase"], [0.0001, 5, 10], ["<=0", "0-5", "5-10", ">=10"]))].append(t["pnl"])
        g[("age_s", bucket(t["age_s"], [30, 90], ["<30", "30-90", ">=90"]))].append(t["pnl"])
        g[("curve", bucket(t["curve"], [50, 65], ["<50", "50-65", ">=65"]))].append(t["pnl"])
        g[("hold", bucket(t["hold_s"], [30, 90], ["<30s", "30-90s", ">=90s"]))].append(t["pnl"])
        h = t["hour"]
        tod = "22-03" if h >= 22 or h < 3 else "06-09" if 6 <= h < 9 else "09-15" if 9 <= h < 15 else "15-22" if h >= 15 else "03-06"
        g[("time_ct", tod)].append(t["pnl"])
        g[("scout_sol", bucket(t["scout_sol"], [1.5], ["~1", "~2"]))].append(t["pnl"])
    return {f"{a}:{b}": stats(v) for (a, b), v in sorted(g.items())}


# ------------------------------------------------------------------ paper
def paper_closes(name, since_ms=0):
    out, art = [], []
    for r in rows(name, {"close"}):
        if r["ts_ms"] < since_ms:
            continue
        if (r.get("max_multiple") or 0) > ARTIFACT_MULT or (r.get("pnl_sol") or 0) > 10:
            art.append(r)
            continue
        out.append(r)
    return out, art


def wallet_track_scores(closes):
    g = defaultdict(list)
    for r in closes:
        g[((r.get("handles") or ["?"])[0], r.get("track"))].append(r.get("pnl_sol") or 0.0)
    return {f"{h}/{t}": stats(v) for (h, t), v in sorted(g.items())}


def variant_scores(since_ms=0):
    g = defaultdict(list)
    for r in rows("exit_variants.jsonl", {"variant_closed"}):
        if r["ts_ms"] < since_ms or r.get("restart_gap"):
            continue
        g[(r.get("arm"), r.get("variant"))].append(r.get("pnl_sol") or 0.0)
    return {f"{a}/{v}": stats(x) for (a, v), x in sorted(g.items())}


def chase_curve(since_ms=0):
    """Control-A paper PnL per scout_solo signal, joined with the exact chase logged by the CHASE5 gate rows."""
    chase = {}
    for r in rows("exit_variants.jsonl", {"variant_gate"}):
        if isinstance(r.get("chase_pct"), (int, float)):
            chase[r.get("signal_id")] = r["chase_pct"]
    a = {}
    for r in rows("exit_variants.jsonl", {"variant_closed"}):
        if (r.get("arm") == "scout_solo" and r.get("variant") == "A" and r["ts_ms"] >= since_ms
                and not r.get("restart_gap")):
            a[r.get("signal_id")] = r.get("pnl_sol") or 0.0
    return chase, a


# ------------------------------------------------------------------ state / overrides
# ------------------------------------------------------------------ v1.34 kol_confluence + cryptohub (PAPER ONLY)
PUMP_EDGES = [0.0, 10.0, 25.0, 50.0, 100.0, 200.0]
PUMP_LABELS = ["<0%", "0-10%", "10-25%", "25-50%", "50-100%", "100-200%", ">=200%"]
PROMOTE_MIN_N = int(os.getenv("LEARN_PROMOTE_MIN_N", "50"))
PROMOTE_MIN_PF_EX3 = float(os.getenv("LEARN_PROMOTE_MIN_PF_EX3", "1.3"))


def cryptohub_labels():
    try:
        d = json.loads((DATA / "cryptohub_wallets.json").read_text())
        return {w.get("label") for w in d.get("wallets", []) if w.get("label")}
    except Exception:  # noqa: BLE001
        return set()


def kol_confluence_scores(track="half2x_c30", closes=None):
    """Per-wallet / per-pumped-bucket paper scores for the kol_confluence arm + a SUGGESTED skip threshold for the
    pumped_since_first_alert_pct feature (reported only; nothing is applied — the feature is not a filter).
    v1.38: closes= the realistic book rows (track label "realistic"); default = the legacy logged track."""
    if closes is None:
        closes, _art = paper_closes("kol_confluence.jsonl")
        closes = [r for r in closes if r.get("track") == track]
    opens = {r.get("id"): r for r in rows("kol_confluence.jsonl", {"open"})}
    by_w, by_b = defaultdict(list), defaultdict(list)
    for r in closes:
        for h in (r.get("handles") or []):
            by_w[h].append(r.get("pnl_sol") or 0.0)
        o = opens.get(r.get("id")) or {}
        pv = o.get("pumped_since_first_alert_pct_at_entry")
        if pv is None:
            pv = o.get("pumped_since_first_alert_pct")
        by_b[bucket(pv, PUMP_EDGES, PUMP_LABELS)].append(r.get("pnl_sol") or 0.0)
    buckets = {b: stats(by_b[b]) for b in PUMP_LABELS + ["unknown"] if by_b.get(b)}
    thr = None
    for e, lab in zip(PUMP_EDGES, PUMP_LABELS[1:]):        # lowest edge above which every actionable bucket loses
        above = [buckets[l] for l in PUMP_LABELS[PUMP_LABELS.index(lab):] if l in buckets]
        act = [s for s in above if s.get("actionable")]
        if act and all(s["mean"] < 0 for s in act):
            thr = e
            break
    return {"track": track, "n": len(closes), "by_wallet": {h: stats(v) for h, v in sorted(by_w.items())},
            "by_pumped_bucket": buckets, "suggested_skip_above_pct": thr,
            "note": "feature only — suggested threshold is NOT applied; a filter needs Andrew's OK"}


def load_json(p, d):
    try:
        return json.loads(p.read_text())
    except Exception:  # noqa: BLE001
        return d


def clamp(v):
    out = dict(DEFAULTS)
    out.update({k: v[k] for k in v if k in DEFAULTS})
    lo, hi = BOUNDS["scout_chase_cap_pct"]
    out["scout_chase_cap_pct"] = max(lo, min(hi, float(out["scout_chase_cap_pct"])))
    out["top4_handles"] = [h for h in out["top4_handles"] if h in V3_HANDLES][:6] or DEFAULTS["top4_handles"]
    return out


def metric_chase(since_ms, until_ms=None, B=None):
    if B and B.get("basis") == "realistic":
        rs = [r["pnl_sol"] for r in B["variants"] if r.get("variant") == "CHASE5" and r["ts_ms"] >= since_ms
              and (until_ms is None or r["ts_ms"] < until_ms)]
    else:
        rs = [r.get("pnl_sol") or 0.0 for r in rows("exit_variants.jsonl", {"variant_closed"})
              if r.get("variant") == "CHASE5" and r["ts_ms"] >= since_ms and not r.get("restart_gap")
              and (until_ms is None or r["ts_ms"] < until_ms)]
    return (sum(rs) / len(rs) if rs else None), len(rs)


def metric_top4(since_ms, until_ms=None, B=None):
    if B and B.get("basis") == "realistic":
        rs = [r["pnl_sol"] for r in B["top4_rows"] if r["ts_ms"] >= since_ms and (until_ms is None or r["ts_ms"] < until_ms)]
    else:
        c, _ = paper_closes("paper_top4.jsonl", since_ms)
        rs = [r.get("pnl_sol") or 0.0 for r in c if r.get("track") == "half2x_c30"
              and (until_ms is None or r["ts_ms"] < until_ms)]
    return (sum(rs) / len(rs) if rs else None), len(rs)


# ------------------------------------------------------------------ v1.38 realistic basis
def realistic_book():
    """Build data/learn/realistic_book.json in a subprocess (scripts/learn_realism.py) and load it. None on failure."""
    script = Path(__file__).resolve().parent / "learn_realism.py"
    err = None
    try:
        r = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=420,
                           env={**os.environ, "FOMO_DATA_DIR": str(DATA), "FOMO_COACH_DIR": str(ROOT)})
        if r.returncode != 0:
            err = (r.stderr or "").strip().splitlines()[-1:] or ["exit %d" % r.returncode]
            err = err[0][:200]
    except Exception as exc:  # noqa: BLE001
        err = f"{type(exc).__name__}: {exc}"[:200]
    if err:
        return None, err
    try:
        return json.loads((OUT / "realistic_book.json").read_text()), None
    except Exception as exc:  # noqa: BLE001
        return None, f"book unreadable: {type(exc).__name__}"


def _book_rows(book, name, since_ms=0):
    return [{**r, "track": "realistic"} for r in (book.get("books") or {}).get(name, [])
            if r.get("pnl_sol") is not None and r.get("ts_ms", 0) >= since_ms]


def basis_data(kind, book, now):
    """Everything the decisions read, on one basis. kind = "realistic" (needs book) or "legacy"."""
    if kind == "realistic":
        v3, v3_7 = _book_rows(book, "paper_v3"), _book_rows(book, "paper_v3", now - 7 * DAY)
        t4 = _book_rows(book, "paper_top4")
        kol = _book_rows(book, "kol_confluence")
        var = [r for r in book.get("variants") or []]
        g, g7 = defaultdict(list), defaultdict(list)
        for r in var:
            g[(r.get("arm"), r.get("variant"))].append(r["pnl_sol"])
            if r["ts_ms"] >= now - 7 * DAY:
                g7[(r.get("arm"), r.get("variant"))].append(r["pnl_sol"])
        vs = {f"{a}/{v}": stats(x) for (a, v), x in sorted(g.items())}
        vs7 = {f"{a}/{v}": stats(x) for (a, v), x in sorted(g7.items())}
        chase = {}
        for r in rows("exit_variants.jsonl", {"variant_gate"}):
            if isinstance(r.get("chase_pct"), (int, float)):
                chase[r.get("signal_id")] = r["chase_pct"]
        a = {r.get("signal_id"): r["pnl_sol"] for r in var if r.get("arm") == "scout_solo" and r.get("variant") == "A"
             and r["ts_ms"] >= now - 14 * DAY}
        return {"basis": "realistic", "track": "realistic", "ws_all": wallet_track_scores(v3),
                "ws7": wallet_track_scores(v3_7), "t4s": wallet_track_scores(t4), "top4_rows": t4,
                "vs": vs, "vs7": vs7, "chase": (chase, a), "variants": var,
                "kc": kol_confluence_scores("realistic", closes=kol),
                "n": {"paper_v3": len(v3), "paper_top4": len(t4), "kol_confluence": len(kol), "variants": len(var)}}
    v3, _ = paper_closes("paper_v3.jsonl")
    v3 = [r for r in v3 if not r.get("realistic")]
    v3_7 = [r for r in v3 if r["ts_ms"] >= now - 7 * DAY]
    t4, _ = paper_closes("paper_top4.jsonl")
    t4 = [r for r in t4 if not r.get("realistic")]
    return {"basis": "legacy", "track": "half2x_c30", "ws_all": wallet_track_scores(v3), "ws7": wallet_track_scores(v3_7),
            "t4s": wallet_track_scores(t4), "vs": variant_scores(), "vs7": variant_scores(now - 7 * DAY),
            "chase": chase_curve(now - 14 * DAY), "kc": kol_confluence_scores(),
            "n": {"paper_v3": len(v3), "paper_top4": len(t4)}}


def make_proposals(B, values, ls, now):
    """Live suggestions (never applied) on basis B."""
    out = []
    tr = B["track"]

    def propose(kind, text, evidence):
        out.append({"ts_ms": now, "kind": kind, "text": text, "evidence": evidence, "status": "awaiting_andrew",
                    "basis": B["basis"]})
    r4 = ls.get("source:scout_solo/R4") or {}
    if r4.get("n", 0) >= MIN_N and (r4.get("sum", 0) < 0 or (r4.get("pf_ex_top3") or 0) < 1):
        propose("bench_or_paper", "R4 (scout_solo) is net negative live; consider moving R4 to paper-only", r4)
    for h in values["top4_handles"]:
        s = B["t4s"].get(f"{h}/{tr}") or B["ws_all"].get(f"{h}/{tr}") or {}
        if s.get("n", 0) >= 50 and (s.get("pf_ex_top3") or 0) > 1.3:
            propose("promote_wallet", f"{h}: paper {'realistic pricing' if B['basis'] == 'realistic' else 'legacy (' + tr + ')'} passes the promotion bar; consider a LIVE scout "
                                      f"at current size", s)
    c5 = B["vs7"].get("scout_solo/CHASE5") or {}
    ca = B["vs7"].get("scout_solo/A") or {}
    if c5.get("n", 0) >= MIN_N and ca.get("n") and c5["mean"] > ca["mean"]:
        propose("chase_cap", f"Lower LIVE max chase to {values['scout_chase_cap_pct']:g}% (paper CHASE5 beats control A "
                             f"per trade, {B['basis']} basis)", {"CHASE5": c5, "A": ca})
    t60 = B["vs7"].get("scout_solo/T60_CANDLE15") or {}
    if t60.get("n", 0) >= MIN_N and ca.get("n") and t60["mean"] > ca["mean"]:
        propose("exit_rule", f"T60_CANDLE15 beats the live exit on R4 paper signals ({B['basis']} basis); consider it "
                             f"for live", {"T60": t60, "A": ca})
    kc = B["kc"]
    chl = cryptohub_labels()
    for h, s in kc["by_wallet"].items():
        if h in chl and s.get("n", 0) >= PROMOTE_MIN_N and (s.get("pf_ex_top3") or 0) > PROMOTE_MIN_PF_EX3:
            out.append({"ts_ms": now, "kind": "promote_wallet_cryptohub", "status": "awaiting_andrew",
                        "requires_andrew_approval": True, "live_weight_now": 0.0, "basis": B["basis"],
                        "text": f"{h} (cryptohub): kol_confluence paper {kc['track']} passes the bar "
                                f"(n ≥ {PROMOTE_MIN_N}, PF ex-top-3 > {PROMOTE_MIN_PF_EX3:g}); consider giving it a live weight",
                        "evidence": s})
    return out


def main():
    now = int(time.time() * 1000)
    today = datetime.fromtimestamp(now / 1000, CT).strftime("%Y-%m-%d")
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "notes").mkdir(exist_ok=True)
    state = load_json(OUT / "state.json", {"last_run_ms": 0, "changes": [], "runs": 0})
    last = state.get("last_run_ms") or 0
    ov_file = OUT / "paper_overrides.json"
    ov = load_json(ov_file, {})
    values = clamp(ov.get("values") or {})
    applied, undone, held, proposals, anomalies = [], [], [], [], []

    # ---- live
    T = live_closes()
    new_live = [t for t in T if t["close_ms"] > last]
    ls = live_scores(T)
    ls7 = live_scores([t for t in T if t["close_ms"] > now - 7 * DAY])

    # ---- paper (v1.38: decisions on the realistic basis; legacy kept for comparison)
    v3, v3_art = paper_closes("paper_v3.jsonl")
    t4, t4_art = paper_closes("paper_top4.jsonl")
    new_paper = sum(1 for r in v3 + t4 if r["ts_ms"] > last and not r.get("realistic"))
    book, book_err = realistic_book()
    LB = basis_data("legacy", None, now)
    B = basis_data("realistic", book, now) if book else LB
    if book_err:
        anomalies.append(f"realistic book unavailable ({book_err}) — this run decided on the LEGACY basis")
    elif not (book.get("snapshot") or {}).get("present"):
        anomalies.append("coach realism snapshot missing (data/paper_realism_snapshot.json) — realistic book used "
                         "code-default live exit numbers")
    ws_all, ws7, t4s, vs, vs7 = B["ws_all"], B["ws7"], B["t4s"], B["vs"], B["vs7"]
    TR = B["track"]

    # ---- auto-undo of changes older than UNDO_DAYS
    for ch in state.get("changes", []):
        if ch.get("status") != "active" or now - ch["ts_ms"] < UNDO_DAYS * DAY:
            continue
        fn = metric_chase if ch["key"] == "scout_chase_cap_pct" else metric_top4
        after, n_after = fn(ch["ts_ms"], None, B)
        before = ch.get("baseline_mean")
        if ch.get("basis", "legacy") != B["basis"]:      # v1.38: compare like with like (re-base the baseline)
            rb, _ = fn(ch["ts_ms"] - 7 * DAY, ch["ts_ms"], B)
            if rb is not None:
                before = rb
            else:                                        # no data to re-base: judge it on its own (legacy) basis
                after, n_after = fn(ch["ts_ms"], None, LB)
        if n_after < MIN_N or after is None:
            ch["status"] = "kept_insufficient_data"
            held.append(f"{ch['key']}: kept {ch['new']!r} (only {n_after} paper closes since the change, < {MIN_N})")
            continue
        if before is not None and after < before:
            values[ch["key"]] = ch["old"]
            ch["status"] = "undone"
            ch["undone_ms"] = now
            undone.append({"key": ch["key"], "from": ch["new"], "back_to": ch["old"],
                           "why": f"paper mean/trade {after:+.4f} after vs {before:+.4f} before (n={n_after})"})
        else:
            ch["status"] = "confirmed"
            held.append(f"{ch['key']}: confirmed {ch['new']!r} (mean/trade {after:+.4f} vs {before if before is None else round(before, 4)}, n={n_after})")
    changed_today = {c["key"] for c in state.get("changes", []) if now - c["ts_ms"] < DAY} | {u["key"] for u in undone}

    # ---- chase cap (paper only)
    chase, a_pnl = B["chase"]
    known = {k: v for k, v in chase.items() if k in a_pnl}
    curve = {cap: stats([a_pnl[k] for k, v in known.items() if v <= cap]) for cap in CHASE_STEPS}
    cur_cap = values["scout_chase_cap_pct"]
    if "scout_chase_cap_pct" not in changed_today:
        ok = {c: s for c, s in curve.items() if s.get("n", 0) >= MIN_N}
        if ok:
            best = max(ok, key=lambda c: (ok[c]["mean"], -c))
            if best != cur_cap and ok.get(cur_cap) and ok[best]["mean"] > ok[cur_cap]["mean"] + 0.002:
                i = CHASE_STEPS.index(cur_cap) if cur_cap in CHASE_STEPS else 1
                step = CHASE_STEPS[max(0, min(len(CHASE_STEPS) - 1, i + (1 if best > cur_cap else -1)))]
                base, _ = metric_chase(now - 7 * DAY, None, B)
                state.setdefault("changes", []).append({"ts_ms": now, "key": "scout_chase_cap_pct", "old": cur_cap,
                                                         "new": step, "status": "active", "baseline_mean": base,
                                                         "basis": B["basis"],
                                                         "evidence": {str(k): v for k, v in ok.items()}})
                applied.append({"key": "scout_chase_cap_pct", "from": cur_cap, "to": step,
                                "why": f"{B['basis']} paper control-A mean/trade at cap {best:g}%: {ok[best]['mean']:+.4f} vs "
                                       f"{ok[cur_cap]['mean']:+.4f} at {cur_cap:g}% (one step per day)"})
                values["scout_chase_cap_pct"] = step
            else:
                held.append(f"scout_chase_cap_pct: stays {cur_cap:g}% (no cap beats it by > 0.002 SOL/trade with n ≥ {MIN_N})")
        else:
            held.append(f"scout_chase_cap_pct: stays {cur_cap:g}% (no chase bucket has n ≥ {MIN_N} yet; {len(known)} signals with exact chase)")

    # ---- top4 wallet list (paper only): swap at most one handle per day
    if "top4_handles" not in changed_today:
        rank = {}
        for h in V3_HANDLES:
            s = ws7.get(f"{h}/{TR}") or {"n": 0}
            if s.get("n", 0) >= MIN_N:
                rank[h] = s["sum_ex_top3"]
        mem = list(values["top4_handles"])
        if rank:
            worst = min((h for h in mem if h in rank), key=lambda h: rank[h], default=None)
            cand = max((h for h in rank if h not in mem), key=lambda h: rank[h], default=None)
            if worst and cand and rank[worst] < 0 < rank[cand]:
                new = [cand if h == worst else h for h in mem]
                base, _ = metric_top4(now - 7 * DAY, None, B)
                state.setdefault("changes", []).append({"ts_ms": now, "key": "top4_handles", "old": mem, "new": new,
                                                         "status": "active", "baseline_mean": base, "basis": B["basis"],
                                                         "evidence": {worst: rank[worst], cand: rank[cand]}})
                applied.append({"key": "top4_handles", "from": mem, "to": new,
                                "why": f"7-day paper_v3 {TR} PnL ex-top-3: {cand} {rank[cand]:+.3f} vs {worst} {rank[worst]:+.3f}"})
                values["top4_handles"] = new
            else:
                held.append(f"top4_handles: stays {', '.join(mem)}")
        else:
            held.append(f"top4_handles: stays {', '.join(mem)} (no wallet with ≥ {MIN_N} closes in 7 days)")

    values = clamp(values)
    ov_out = {"updated_ms": now, "updated_ct": ct(now), "paper_only": True, "values": values,
              "bounds": {"scout_chase_cap_pct": list(BOUNDS["scout_chase_cap_pct"]), "top4_handles": "subset of paper_v3 roster"},
              "applied": applied, "undone": undone}
    tmp = ov_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(ov_out, indent=1))
    os.replace(tmp, ov_file)

    # ---- live suggestions (never applied). v1.38: decided on the realistic basis; the legacy basis is recomputed
    # with the same rules for comparison (written to scores + note only, not to live_proposals.jsonl).
    proposals.extend(make_proposals(B, values, ls, now))
    legacy_props = make_proposals(LB, values, ls, now) if B is not LB else []
    kc = B["kc"]
    def rk(p):   # same suggestion on both bases = same kind + subject, ignoring the basis wording
        return (p["kind"], re.sub(r"\b(realistic|legacy|basis|half2x_c30)\b|[^A-Za-z0-9%.]", "", p["text"].split(":")[0]))
    comparison = {"basis": B["basis"], "legacy_only": [p["text"] for p in legacy_props
                                                         if rk(p) not in {rk(q) for q in proposals}],
                  "realistic_only": [p["text"] for p in proposals if rk(p) not in {rk(q) for q in legacy_props}],
                  "both": [p["text"] for p in proposals if rk(p) in {rk(q) for q in legacy_props}]}
    with (OUT / "live_proposals.jsonl").open("a") as fh:
        for p in proposals:
            fh.write(json.dumps(p) + "\n")

    # ---- anomalies
    an = rows("paper_anomalies.jsonl", {"mark_rejected"})
    an_new = [r for r in an if r["ts_ms"] > last]
    if an_new:
        anomalies.append(f"{len(an_new)} absurd paper marks rejected since the last run (e.g. {an_new[-1].get('source')} "
                         f"{str(an_new[-1].get('mint'))[:6]}: {an_new[-1].get('why')})")
    arts = [r for r in v3_art + t4_art if r["ts_ms"] > last]
    if arts:
        anomalies.append(f"{len(arts)} paper closes excluded as artifacts (max multiple > {ARTIFACT_MULT:g}x or > 10 SOL)")
    lat = rows("latency_timelines.jsonl")
    lat_last = max((r.get("started_ms") or 0 for r in lat), default=0)
    if new_live and lat_last < new_live[0]["entry_ms"] - 60_000:
        anomalies.append("latency_timelines.jsonl has no row for the new live buys")
    if not lat:
        anomalies.append("latency_timelines.jsonl is empty")
    missing_peak = [t for t in new_live if t["peak_x"] is None]
    if missing_peak:
        anomalies.append(f"{len(missing_peak)} new live closes without a logged peak")
    for f, label in (("/opt/fomo/state/KILL", "KILL"), ("/var/lib/fomo-signer/HALT", "HALT")):
        try:
            if Path(f).exists():
                anomalies.append(f"{label} file is present (trading paused) — the learner never touches it")
        except OSError:
            pass
    if not new_live:
        anomalies.append("no new live closes since the last run")

    lchase, la = LB["chase"]
    lknown = {k: v for k, v in lchase.items() if k in la}
    lcurve = {cap: stats([la[k] for k, v in lknown.items() if v <= cap]) for cap in CHASE_STEPS}
    scores = {"run_ms": now, "run_ct": ct(now), "basis": B["basis"], "live_all": ls, "live_7d": ls7,
              "paper_v3_all": ws_all, "paper_v3_7d": ws7, "paper_top4": t4s, "exit_variants_all": vs,
              "exit_variants_7d": vs7, "chase_curve_14d": {str(k): v for k, v in curve.items()}, "overrides": values,
              "kol_confluence": kc, "realistic_n": B.get("n"),
              "legacy": {"paper_v3_all": LB["ws_all"], "paper_v3_7d": LB["ws7"], "paper_top4": LB["t4s"],
                         "exit_variants_7d": LB["vs7"], "chase_curve_14d": {str(k): v for k, v in lcurve.items()},
                         "kol_confluence": LB["kc"], "proposals": legacy_props},
              "proposal_comparison": comparison}
    (OUT / f"scores_{today}.json").write_text(json.dumps(scores, indent=1))
    state.update(last_run_ms=now, runs=(state.get("runs") or 0) + 1)
    (OUT / "state.json").write_text(json.dumps(state, indent=1))

    note = render_note(today, now, last, new_live, T, ls, ls7, ws_all, ws7, t4s, vs7, curve, values, applied, undone,
                       held, proposals, anomalies, new_paper, len(v3_art), B=B, LB=LB, comparison=comparison)
    np = OUT / "notes" / f"{today} Fomo self-learn daily.md"
    np.write_text(note)
    with (OUT / "learn.log").open("a") as fh:
        fh.write(f"{ct(now)} ok live_new={len(new_live)} paper_new={new_paper} applied={len(applied)} "
                 f"undone={len(undone)} proposals={len(proposals)} note={np.name}\n")
    print(np)
    # v2 (Oct 15, 2026): walk-forward re-tune of every parameter family on stored ticks + ranked live proposals.
    try:
        import learn_v2
        learn_v2.run(now, book=book or False)
    except Exception as exc:  # noqa: BLE001  v2 must never break the v1 run
        with (OUT / "learn.log").open("a") as fh:
            fh.write(f"{ct(now)} v2 FAILED {type(exc).__name__}: {exc}\n")


def _tbl(d, keys=None, top=None):
    items = [(k, v) for k, v in d.items() if v.get("n")]
    if keys:
        items = [(k, v) for k, v in items if any(k.startswith(x) for x in keys)]
    if top:
        items = sorted(items, key=lambda kv: -kv[1].get("sum_ex_top3", kv[1]["sum"]))[:top]
    lines = ["| group | n | W | WR | sum SOL | median | PF | sum ex-top3 | act? |", "|---|---|---|---|---|---|---|---|---|"]
    for k, v in items:
        lines.append(f"| {k} | {v['n']} | {v['wins']} | {v['wr']:.0%} | {v['sum']:+.3f} | {v['median']:+.4f} | "
                     f"{v['pf'] if v['pf'] is not None else '—'} | {v['sum_ex_top3']:+.3f} | {'yes' if v['actionable'] else 'n<' + str(MIN_N)} |")
    return "\n".join(lines)


def render_note(today, now, last, new_live, T, ls, ls7, ws_all, ws7, t4s, vs7, curve, values, applied, undone, held,
                proposals, anomalies, new_paper, n_art, B=None, LB=None, comparison=None):
    basis = (B or {}).get("basis", "legacy")
    L = [f"# {today} Fomo self-learn daily", "",
         f"Stamp: **Fomo**. Automatic note from `fomo-learn.timer` (run {ct(now)}; previous run {ct(last)}). "
         f"PAPER ONLY: this job changed paper settings at most. Live sizes, caps, arms, the signer, .env and KILL/HALT "
         f"were not touched. Groups with fewer than {MIN_N} trades are shown but never acted on.", "",
         f"**Basis: {basis}.** Since v1.38 (Oct 18, 2026) decisions use REALISTIC paper: entries priced like live "
         f"fills (never below the trader's fill, live slippage), the live twin's exit rules and live costs. The old "
         f"optimistic numbers are shown as *legacy* for comparison only.", "",
         "## Summary",
         f"- New live closes: **{len(new_live)}** (lifetime {len(T)}, net {sum(t['pnl'] for t in T):+.3f} SOL). "
         f"New paper closes (paper_v3 + paper_top4): **{new_paper}**.",
         f"- Paper changes applied: **{len(applied)}**, undone: **{len(undone)}**. Live suggestions awaiting Andrew: **{len(proposals)}**.",
         f"- Paper overrides now: chase cap **{values['scout_chase_cap_pct']:g}%**, top4 = **{', '.join(values['top4_handles'])}**.", ""]
    L += ["## New live closes", ""]
    if new_live:
        L += ["| entry | arm/source | chase % | age s | curve % | size | hold s | exit | peak× | PnL SOL |",
              "|---|---|---|---|---|---|---|---|---|---|"]
        for t in new_live:
            f = lambda v, n=1: "—" if v is None else f"{v:.{n}f}"
            L.append(f"| {ct(t['entry_ms'])} | {t['arm']}/{t['source']} | {f(t['chase'])} | {f(t['age_s'], 0)} | "
                     f"{f(t['curve'], 0)} | {t['size']} | {t['hold_s']:.0f} | {t['exit']} | {f(t['peak_x'], 2)} | {t['pnl']:+.4f} |")
    else:
        L.append("None since the previous run.")
    L += ["", "## Live scores (lifetime)", _tbl(ls), "", "## Live scores (last 7 days)", _tbl(ls7) if ls7 else "No live closes in 7 days.",
          "", "## Paper wallets — paper_v3 (last 7 days, artifacts removed, top 15 by sum ex-top-3)", _tbl(ws7, top=15),
          "", f"Lifetime artifacts excluded from paper_v3: {n_art}.",
          "", "## paper_top4 (lifetime)", _tbl(t4s) if t4s else "No paper_top4 closes yet.",
          "", "## Exit variants (last 7 days)", _tbl(vs7, keys=["scout_solo/"]) if vs7 else "No variant closes in 7 days.",
          "", "## Chase-cap curve (scout_solo control A, 14 days)",
          "| cap % | n | mean/trade | sum |", "|---|---|---|---|"]
    for c, s in curve.items():
        L.append(f"| {c:g} | {s.get('n', 0)} | {s.get('mean', 0):+.4f} | {s.get('sum', 0):+.3f} |" if s.get("n") else f"| {c:g} | 0 | — | — |")
    L += ["", "## Paper changes applied"]
    L += [f"- **{a['key']}**: {a['from']} → {a['to']}. {a['why']}. Auto-check in {UNDO_DAYS:g} days." for a in applied] or ["- None."]
    L += ["", "## Paper changes undone"]
    L += [f"- **{u['key']}**: {u['from']} → back to {u['back_to']}. {u['why']}." for u in undone] or ["- None."]
    L += ["", "## Held / checked"] + [f"- {h}" for h in held]
    if comparison:
        L += ["", "## Realistic vs legacy (what changed with realistic pricing)"]
        L += [f"- Still proposed on both bases: {t}" for t in comparison.get("both", [])] or ["- Nothing proposed on both bases."]
        L += [f"- **Dropped** under realistic pricing (legacy-only): {t}" for t in comparison.get("legacy_only", [])]
        L += [f"- **New** under realistic pricing: {t}" for t in comparison.get("realistic_only", [])]
        if LB and B and B is not LB:
            L += ["", "### paper_v3 wallets, 7 days — realistic vs legacy (sum ex-top-3, SOL)", "",
                  "| handle | realistic n | realistic sum ex-top3 | legacy (half2x_c30) n | legacy sum ex-top3 |",
                  "|---|---|---|---|---|"]
            hs = sorted({k.split("/")[0] for k in list(B["ws7"]) + list(LB["ws7"])})
            for h in hs:
                r = B["ws7"].get(f"{h}/realistic") or {}
                g = LB["ws7"].get(f"{h}/half2x_c30") or {}
                if r.get("n") or g.get("n"):
                    L.append(f"| {h} | {r.get('n', 0)} | {r.get('sum_ex_top3', 0):+.3f} | {g.get('n', 0)} | "
                             f"{g.get('sum_ex_top3', 0):+.3f} |")
    try:   # v1.48 paper_fresh5 (PAPER ONLY): fresh <10 min fomo_top_trader coins, sell at 5 min
        import subprocess as _sp
        _g = json.loads(_sp.run([sys.executable, str(Path(__file__).with_name("paper_fresh5_grade.py"))],
                                capture_output=True, text=True, timeout=60).stdout)
        L += ["", "## paper_fresh5 (PAPER ONLY, grade after ~7 days)",
              f"n={_g[n]} · days={_g[days]} · total {_g[total]:+.3f} · ex-best-3 {_g[ex3]:+.3f} · "
              f"half1 ex3 {_g[h1_ex3]:+.3f} · half2 ex3 {_g[h2_ex3]:+.3f} · win {_g[win_rate]} · "
              f"skips {_g[skips]} · passes bar: {_g[passes_bar]}"]
    except Exception as _e:  # noqa: BLE001
        L += ["", f"## paper_fresh5: grade failed ({type(_e).__name__})"]
    L += ["", "## Live suggestions awaiting Andrew (NOT applied)"]
    L += [f"- [{p['kind']}] {p['text']} — n={(p['evidence'] or {}).get('n', '…')}" for p in proposals] or ["- None today."]
    L += ["", "## Anomalies"] + ([f"- {a}" for a in anomalies] or ["- None."])
    L += ["", "Files: backend `/opt/fomo/coach/data/learn/` (scores_*.json, paper_overrides.json, live_proposals.jsonl, state.json).",
          "Related: [[2026-10-14 Winning trades analysis — Fomo]] · [[2026-10-14 v1.32 paper rules + self-learn — Fomo]] · [[FOMO MOC]]", ""]
    return "\n".join(L)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        OUT.mkdir(parents=True, exist_ok=True)
        with (OUT / "learn.log").open("a") as fh:
            fh.write(f"{ct(int(time.time() * 1000))} FAILED {type(exc).__name__}: {exc}\n")
        raise
