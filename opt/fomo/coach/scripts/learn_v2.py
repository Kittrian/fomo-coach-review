#!/usr/bin/env python3
"""v2 Fomo self-learner (PAPER / OFFLINE ONLY). Added Oct 15, 2026; called from learn_daily.py after v1.

Re-tunes every parameter family on stored data with walk-forward (train on all earlier CT days, test on the next
day, roll forward), runs the full shadow grid as offline paper replays on stored trade ticks, writes:
  data/learn/v2/scores_<date>.json        every family x variant: full-sample + walk-forward out-of-sample
  data/learn/v2/proposals_<date>.json     ranked LIVE change proposals with evidence (status awaiting_andrew)
  data/learn/v2/shadow_spec.json          the current paper/shadow winners (for the coach exit_variants engine)
  data/learn/paper_overrides_v2.json      paper-only winners per family (never read by the live engine)
  data/learn/notes/<date> Fomo self-learn v2.md
Never touches live config, sizes, caps, arms, signer, .env, KILL/HALT. No network (ticks come from fomo-learn-ticks).

v1.38 (Sky approved Oct 18, 2026 6:58 PM CT): REALISTIC basis. Paper entries use the realistic entry from
data/learn/realistic_book.json (never below the scout's own fill), every tick replay uses the live cost model
(live fee, priority + tip, live hard-stop fills) instead of the 4% haircut / 1.5% overhead, logged paper PnL and
wallet weights come from the realistic book. The legacy grid is still run and reported side by side
(proposals_<date>.legacy.json, scores["legacy"], note section "Realistic vs legacy"); only realistic proposals rank.
"""
from __future__ import annotations
import json, os, sys, time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from learn_replay import replay, load_ticks  # noqa: E402

CT = ZoneInfo("America/Chicago")
ROOT = Path(os.getenv("FOMO_COACH_DIR", "/opt/fomo/coach"))
DATA = Path(os.getenv("FOMO_DATA_DIR", str(ROOT / "data")))
OUT = DATA / "learn"
V2 = OUT / "v2"
TICK_DIRS = [OUT / "ticks"] + [Path(p) for p in os.getenv("LEARN_EXTRA_TICK_DIRS", "").split(":") if p]
MIN_N = int(os.getenv("LEARN_MIN_N", "15"))
MIN_TRAIN_DAYS = 2

# ---------------------------------------------------------------- parameter families (shadow grid)
FAMILIES = {
    "trail": {
        "control": {}, "trail15": {"trail": .15}, "trail20": {"trail": .20}, "trail25": {"trail": .25},
        "trail35": {"trail": .35}, "trail40": {"trail": .40}, "trail50": {"trail": .50},
        "step30_to20_at3x": {"trail": .30, "trail_step_x": 3.0, "trail_hi": .20},
        "step40_to20_at3x": {"trail": .40, "trail_step_x": 3.0, "trail_hi": .20},
        "step20_to30_at4x": {"trail": .20, "trail_step_x": 4.0, "trail_hi": .30},
        "time30_then20_after5m": {"trail": .30, "trail_time_s": 300, "trail_after_time": .20},
        "time20_then30_after5m": {"trail": .20, "trail_time_s": 300, "trail_after_time": .30},
        "ladder3x25_trail20": {"trail": .20, "ladder": ((3.0, .25),)},
        "ladder3x25_5x12_trail30": {"ladder": ((3.0, .25), (5.0, .125))},
    },
    "hard_stop": {"control": {}, "hs25": {"hard_stop": .25}, "hs30": {"hard_stop": .30}, "hs45": {"hard_stop": .45},
                  "hs55": {"hard_stop": .55}, "candle15s": {"candle_stop_s": 15}, "candle30s": {"candle_stop_s": 30},
                  "pre_trail30": {"pre_trail": .30}},
    "trader_sell": {"control": {}, "never": {"trader_honor": (1e9, 1e9)},
                    "honor_first30s_only": {"trader_honor": (0, 30)},
                    "ignore_first60s": {"trader_honor": (60, 1e9)}, "ignore_first90s": {"trader_honor": (90, 1e9)},
                    "honor_first30s_then_candle15": {"trader_honor": (0, 30), "candle_stop_s": 15},
                    "never_candle15": {"trader_honor": (1e9, 1e9), "candle_stop_s": 15}},
    "rug": {"control": {}, "rug_delay30s": {"rug_delay_s": 30}, "rug_ignore": {"rug_honor": False}},
}
CHASE_CAPS = [3.0, 5.0, 7.5, 10.0, 15.0]
TOD_BLOCKS = {"none": set(), "skip_22_06": set(range(22, 24)) | set(range(0, 6)),
              "skip_00_06": set(range(0, 6)), "skip_15_22": set(range(15, 22))}


def ct(ms):
    return datetime.fromtimestamp(ms / 1000, CT).strftime("%a %b %-d %-I:%M %p CT") if ms else "—"


def day(ms):
    return datetime.fromtimestamp(ms / 1000, CT).strftime("%Y-%m-%d")


def rows(name, kinds=None):
    p = DATA / name
    if not p.exists():
        return []
    out = []
    with p.open(errors="replace") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if kinds is None or r.get("kind") in kinds:
                out.append(r)
    return out


# ---------------------------------------------------------------- entries (paper signals + live trades)
def load_book():
    try:
        return json.loads((OUT / "realistic_book.json").read_text())
    except Exception:  # noqa: BLE001
        return None


def entries(book=None):
    """book given → realistic paper entries/PnL (variant A twins / entry-adjusted history); None → legacy."""
    real = {}
    if book:
        for r in book.get("variants") or []:
            if r.get("variant") == "A":
                real[r.get("signal_id")] = r
    E = []
    chase = {r.get("signal_id"): r["chase_pct"] for r in rows("exit_variants.jsonl", {"variant_gate"})
             if isinstance(r.get("chase_pct"), (int, float))}
    for r in rows("exit_variants.jsonl", {"variant_closed"}):
        if r.get("variant") != "A" or r.get("restart_gap") or not r.get("entry_price_sol"):
            continue
        ex = r.get("exit_reason")
        rr = real.get(r.get("signal_id")) if book else None
        if book and not rr:
            continue
        E.append({"src": "paper", "arm": r.get("arm"), "id": r.get("signal_id"), "mint": r.get("mint"),
                  "entry_ms": r["entry_ms"], "entry_px": (rr or {}).get("real_entry_px") or r["entry_price_sol"],
                  "size": r.get("size_sol") or 0.1,
                  "trader_s": r["exit_ms"] / 1000 if ex == "trader_sold" else None, "rug_s": None,
                  "logged_pnl_frac": ((rr["pnl_sol"] if rr else r.get("pnl_sol")) or 0) / (r.get("size_sol") or 0.1),
                  "chase": chase.get(r.get("signal_id")), "exit": ex})
    buys = {}
    for r in rows("live_trades.jsonl", {"buy", "close"}):
        if r["kind"] == "buy":
            buys[r.get("id")] = r
            continue
        b = buys.get(r.get("id"))
        if not b:
            continue
        m = b.get("meta") or {}
        pk = r.get("peak") or {}
        px = pk.get("entry_price_sol") or b.get("entry_price_sol") or (m.get("chase") or {}).get("quote_price_sol")
        ent = b.get("entry_ms") or b["ts_ms"]
        why = str(r.get("reason") or "")
        E.append({"src": "live", "arm": b.get("arm"), "id": r.get("id"), "mint": b.get("mint"), "entry_ms": ent,
                  "entry_px": px, "size": b.get("size_sol") or 0.2,
                  "trader_s": r["ts_ms"] / 1000 if why.startswith("trader_sold") else None,
                  "rug_s": r["ts_ms"] / 1000 if why.startswith("rug_flag") else None,
                  "logged_pnl_frac": ((r.get("pnl") or {}).get("pnl_sol") or 0) / (b.get("size_sol") or 0.2),
                  "chase": (m.get("chase") or {}).get("premium_pct", m.get("edge_gap_pct")), "exit": why})
    return sorted(E, key=lambda e: e["entry_ms"])


def run_grid(E, costs=None):
    """Replay every entry with every family variant. Returns {(family, variant): {entry_idx: pnl_frac}}.
    costs (v1.38 realistic) = {"hc", "ovh", "real_stop"} overlaid on every parameter set."""
    C = costs or {}
    res = defaultdict(dict)
    cache = {}
    have = 0
    for i, e in enumerate(E):
        if not e["entry_px"]:
            continue
        if e["mint"] not in cache:
            cache[e["mint"]] = load_ticks(e["mint"], TICK_DIRS)
        tk = cache[e["mint"]]
        if not tk or not tk.get("complete"):
            continue
        base = replay(tk["ticks"], e["entry_px"], e["entry_ms"] / 1000, dict(C), e["trader_s"], e["rug_s"])
        if base is None:
            continue
        have += 1
        for fam, vs in FAMILIES.items():
            for v, P in vs.items():
                r = base if v == "control" else replay(tk["ticks"], e["entry_px"], e["entry_ms"] / 1000, {**C, **P},
                                                      e["trader_s"], e["rug_s"])
                if r:
                    res[(fam, v)][i] = r["pnl_frac"]
        res[("_meta", "reached_2x")][i] = 1.0 if base["reached_2x"] else 0.0
    return res, have


def summarize(vals):
    n = len(vals)
    if not n:
        return {"n": 0}
    s = sorted(vals, reverse=True)
    return {"n": n, "sum": round(sum(vals), 4), "mean": round(sum(vals) / n, 5), "wins": sum(v > 0 for v in vals),
            "sum_ex_top3": round(sum(s[3:]), 4)}


def walk_forward(E, idx_vals_by_variant, eligible):
    """Train on all earlier CT days, test the training winner on the next day. Returns OOS stats per family."""
    days = sorted({day(E[i]["entry_ms"]) for i in eligible})
    oos_pick, oos_ctrl, picks = [], [], []
    for k, d in enumerate(days):
        if k < MIN_TRAIN_DAYS:
            continue
        train = [i for i in eligible if day(E[i]["entry_ms"]) < d]
        test = [i for i in eligible if day(E[i]["entry_ms"]) == d]
        if not train or not test:
            continue
        best = max(idx_vals_by_variant, key=lambda v: sum(idx_vals_by_variant[v].get(i, 0) for i in train))
        picks.append((d, best))
        oos_pick += [idx_vals_by_variant[best].get(i, 0) for i in test]
        oos_ctrl += [idx_vals_by_variant["control"].get(i, 0) for i in test]
    stable = None
    if picks:
        from collections import Counter
        top, cnt = Counter(p for _, p in picks).most_common(1)[0]
        stable = {"most_picked": top, "share": round(cnt / len(picks), 2)}
    return {"folds": len(picks), "picks": picks, "oos_pick": summarize(oos_pick), "oos_control": summarize(oos_ctrl),
            "oos_delta": round(sum(oos_pick) - sum(oos_ctrl), 4), "stable": stable}


def family_scores(E, res):
    out = {}
    for fam in FAMILIES:
        vv = {v: res.get((fam, v), {}) for v in FAMILIES[fam]}
        elig = sorted(set(vv["control"]))
        if fam == "trail":
            elig = [i for i in elig if res[("_meta", "reached_2x")].get(i)]
        if fam == "trader_sell":
            elig = [i for i in elig if E[i]["trader_s"] is not None]
        if fam == "rug":
            elig = [i for i in elig if E[i]["rug_s"] is not None]
        full = {}
        for v, d in vv.items():
            vals = [d.get(i, 0) for i in elig]
            ctrl = [vv["control"].get(i, 0) for i in elig]
            s = summarize(vals)
            s["delta_vs_control"] = round(sum(vals) - sum(ctrl), 4)
            s["better"] = sum(a > b + 1e-9 for a, b in zip(vals, ctrl))
            s["worse"] = sum(a < b - 1e-9 for a, b in zip(vals, ctrl))
            s["live_n"] = sum(1 for i in elig if E[i]["src"] == "live")
            s["live_delta"] = round(sum(d.get(i, 0) - vv["control"].get(i, 0) for i in elig if E[i]["src"] == "live"), 4)
            full[v] = s
        out[fam] = {"n_eligible": len(elig), "variants": full, "walk_forward": walk_forward(E, vv, elig)}
    return out


def filter_scores(E):
    """Entry filters scored on LOGGED pnl (no ticks needed): chase cap and time-of-day, live + paper separately."""
    out = {"chase_cap": {}, "time_of_day": {}}
    for src in ("live", "paper"):
        es = [e for e in E if e["src"] == src]
        wc = [e for e in es if e["chase"] is not None]
        base = summarize([e["logged_pnl_frac"] for e in wc])
        out["chase_cap"][src] = {"none": base}
        for cap in CHASE_CAPS:
            kept = [e["logged_pnl_frac"] for e in wc if e["chase"] <= cap]
            skipped = [e["logged_pnl_frac"] for e in wc if e["chase"] > cap]
            s = summarize(kept)
            s["skipped_n"], s["skipped_sum"] = len(skipped), round(sum(skipped), 4)
            out["chase_cap"][src][f"cap{cap:g}"] = s
        out["time_of_day"][src] = {}
        for name, hrs in TOD_BLOCKS.items():
            kept = [e["logged_pnl_frac"] for e in es if datetime.fromtimestamp(e["entry_ms"] / 1000, CT).hour not in hrs]
            skipped = [e["logged_pnl_frac"] for e in es if datetime.fromtimestamp(e["entry_ms"] / 1000, CT).hour in hrs]
            s = summarize(kept)
            s["skipped_n"], s["skipped_sum"] = len(skipped), round(sum(skipped), 4)
            out["time_of_day"][src][name] = s
        # walk-forward for the chase cap on this source
        days = sorted({day(e["entry_ms"]) for e in wc})
        oos_p, oos_c, picks = [], [], []
        for k, d in enumerate(days):
            if k < MIN_TRAIN_DAYS:
                continue
            tr = [e for e in wc if day(e["entry_ms"]) < d]
            te = [e for e in wc if day(e["entry_ms"]) == d]
            caps = CHASE_CAPS + [1e9]
            best = max(caps, key=lambda c: sum(e["logged_pnl_frac"] for e in tr if e["chase"] <= c))
            picks.append((d, best))
            oos_p += [e["logged_pnl_frac"] for e in te if e["chase"] <= best]
            oos_c += [e["logged_pnl_frac"] for e in te]
        out["chase_cap"][src]["walk_forward"] = {"picks": picks, "oos_pick_sum": round(sum(oos_p), 4),
                                                 "oos_control_sum": round(sum(oos_c), 4),
                                                 "oos_delta": round(sum(oos_p) - sum(oos_c), 4)}
    return out


def wallet_weights(book=None):
    """paper_v3 per-handle walk-forward weights: shrunk mean PnL (prior n0=20 at 0), normalized to mean 1, clipped.
    v1.38: book given → realistic book rows (one row per position); else legacy logged closes (all tracks, as before)."""
    g = defaultdict(list)
    src = ([r for r in (book.get("books") or {}).get("paper_v3", []) if r.get("pnl_sol") is not None] if book
           else [r for r in rows("paper_v3.jsonl", {"close"}) if not r.get("realistic")])
    for r in src:
        if (r.get("max_multiple") or 0) > 50 or (r.get("pnl_sol") or 0) > 10:
            continue
        g[(r.get("handles") or ["?"])[0]].append((r["ts_ms"], r.get("pnl_sol") or 0.0))
    now = int(time.time() * 1000)
    w = {}
    for h, xs in g.items():
        xs.sort()
        cut = now - 3 * 86400_000
        tr = [p for t, p in xs if t < cut]
        te = [p for t, p in xs if t >= cut]
        all_ = [p for _, p in xs]
        shr = sum(all_) / (len(all_) + 20)
        w[h] = {"n": len(all_), "shrunk_mean": round(shr, 5),
                "train_mean": round(sum(tr) / len(tr), 5) if tr else None,
                "test_last3d_mean": round(sum(te) / len(te), 5) if te else None,
                "holds_oos": bool(tr and te and (sum(tr) > 0) == (sum(te) > 0))}
    pos = [v["shrunk_mean"] for v in w.values() if v["shrunk_mean"] > 0]
    ref = (sum(pos) / len(pos)) if pos else 1
    for v in w.values():
        v["weight"] = round(max(0.0, min(2.0, v["shrunk_mean"] / ref)), 2) if ref else 0
    return dict(sorted(w.items(), key=lambda kv: -kv[1]["shrunk_mean"]))


def arm_sizing(E):
    """Per-arm expectancy on logged pnl (live and paper) -> suggested size multiplier (0 / 0.5 / 1 / 1.5)."""
    g = defaultdict(list)
    for e in E:
        g[(e["src"], e["arm"])].append(e["logged_pnl_frac"])
    out = {}
    for (src, arm), v in sorted(g.items()):
        s = summarize(v)
        m = s["sum"] / (s["n"] + 10)              # shrunk per-trade return
        mult = 0.0 if m < -0.01 else 0.5 if m < 0.005 else 1.0 if m < 0.03 else 1.5
        s.update(shrunk_mean=round(m, 4), size_mult_suggested=mult)
        out[f"{src}/{arm}"] = s
    return out


def proposals_from(fs, flt, arms, wts, now):
    P = []

    def add(kind, text, ev, score):
        P.append({"ts_ms": now, "kind": f"v2_{kind}", "text": text, "evidence": ev, "score": round(score, 4),
                  "status": "awaiting_andrew"})

    for fam, d in fs.items():
        wf = d["walk_forward"]
        vs = d["variants"]
        best = max((v for v in vs if v != "control"), key=lambda v: vs[v]["delta_vs_control"], default=None)
        if not best:
            continue
        b = vs[best]
        ok = (d["n_eligible"] >= MIN_N and b["delta_vs_control"] > 0 and b["better"] > b["worse"]
              and wf["folds"] >= 1 and wf["oos_delta"] > 0)
        if ok:
            add(fam, f"{fam}: switch LIVE from control to {best} (+{b['delta_vs_control']:.3f} size-units over "
                     f"{d['n_eligible']} trades; walk-forward OOS {wf['oos_delta']:+.3f} over {wf['folds']} days)",
                {"n": d["n_eligible"], "best": b, "control": vs["control"], "walk_forward": {k: wf[k] for k in ("folds", "oos_delta", "stable")}},
                wf["oos_delta"])
    lc = flt["chase_cap"]["live"]
    wf = lc.get("walk_forward") or {}
    capbest = max((k for k in lc if k.startswith("cap")), key=lambda k: lc[k].get("sum", -9), default=None)
    if capbest and lc[capbest].get("n", 0) >= MIN_N and lc[capbest]["sum"] > lc["none"].get("sum", 0) and wf.get("oos_delta", 0) > 0:
        add("chase_cap", f"Lower LIVE max chase from 15% to {capbest[3:]}% (kept {lc[capbest]['n']} trades sum "
                         f"{lc[capbest]['sum']:+.3f} vs all {lc['none']['sum']:+.3f}; skipped {lc[capbest]['skipped_n']} "
                         f"summing {lc[capbest]['skipped_sum']:+.3f}; WF OOS {wf['oos_delta']:+.3f})",
            {"n": lc[capbest]["n"], "cap": lc[capbest], "none": lc["none"], "walk_forward": wf}, wf["oos_delta"])
    lt = flt["time_of_day"]["live"]
    for name, s in lt.items():
        if name != "none" and s.get("skipped_n", 0) >= 10 and s["skipped_sum"] < 0:
            pp = flt["time_of_day"]["paper"].get(name, {})
            if pp.get("skipped_sum", 0) < 0:
                add("time_of_day", f"LIVE: pause new entries {name.replace('skip_', '').replace('_', '-')} CT "
                                   f"(live skipped {s['skipped_n']} trades summed {s['skipped_sum']:+.3f}; paper agrees "
                                   f"{pp['skipped_sum']:+.3f})", {"n": s["skipped_n"], "live": s, "paper": pp},
                    -s["skipped_sum"] / 2)
    for k, s in arms.items():
        if k.startswith("live/") and s["n"] >= MIN_N and s["size_mult_suggested"] != 1.0:
            add("arm_size", f"{k}: size x{s['size_mult_suggested']:g} (shrunk mean {s['shrunk_mean']:+.4f}/trade, n={s['n']})",
                s, abs(s["shrunk_mean"]))
    top = [h for h, v in wts.items() if v["n"] >= 50 and v["weight"] >= 1.2 and v["holds_oos"]][:3]
    if top:
        add("wallet_weights", "Raise LIVE wallet weight for " + ", ".join(f"{h} (w={wts[h]['weight']})" for h in top),
            {"n": sum(wts[h]["n"] for h in top), "wallets": {h: wts[h] for h in top}}, 0.0)
    P.sort(key=lambda p: -p["score"])
    for i, p in enumerate(P, 1):
        p["rank"] = i
    return P


def render(today, now, have, nE, fs, flt, arms, wts, P, cmp_=None, basis="legacy"):
    L = [f"# {today} Fomo self-learn v2", "",
         f"**Basis: {basis}** (v1.38: realistic entries + live cost model; legacy shown for comparison).", "",
         f"Stamp: **Fomo**. Automatic (fomo-learn.timer, {ct(now)}). PAPER/OFFLINE ONLY. Every parameter family is "
         f"replayed on stored trade ticks ({have} of {nE} paper+live entries have a complete tick path), ranked on the "
         f"full sample and walk-forward (train on earlier CT days, test on the next). Nothing live was changed.", ""]
    for fam, d in fs.items():
        wf = d["walk_forward"]
        L += [f"## {fam} (n={d['n_eligible']}; WF folds {wf['folds']}, OOS delta {wf['oos_delta']:+.3f}, "
              f"most picked {((wf['stable'] or {}).get('most_picked'))})", "",
              "| variant | n | sum (size units) | Δ vs control | better/worse | live n | live Δ |", "|---|---|---|---|---|---|---|"]
        for v, s in sorted(d["variants"].items(), key=lambda kv: -kv[1].get("delta_vs_control", 0)):
            if s.get("n"):
                L.append(f"| {v} | {s['n']} | {s['sum']:+.3f} | {s['delta_vs_control']:+.3f} | {s['better']}/{s['worse']} | "
                         f"{s['live_n']} | {s['live_delta']:+.3f} |")
        L.append("")
    for src in ("live", "paper"):
        L += [f"## Chase cap ({src}, logged PnL)", "", "| cap | kept n | kept sum | skipped n | skipped sum |", "|---|---|---|---|---|"]
        for k, s in flt["chase_cap"][src].items():
            if k.startswith("cap"):
                L.append(f"| {k[3:]}% | {s.get('n', 0)} | {s.get('sum', 0):+.3f} | {s['skipped_n']} | {s['skipped_sum']:+.3f} |")
        L += ["", f"WF: {flt['chase_cap'][src].get('walk_forward')}", ""]
        L += [f"## Time of day ({src})", "", "| rule | kept n | kept sum | skipped n | skipped sum |", "|---|---|---|---|---|"]
        for k, s in flt["time_of_day"][src].items():
            L.append(f"| {k} | {s.get('n', 0)} | {s.get('sum', 0):+.3f} | {s['skipped_n']} | {s['skipped_sum']:+.3f} |")
        L.append("")
    L += ["## Arm sizing (shrunk expectancy)", "", "| arm | n | sum | shrunk mean | size × |", "|---|---|---|---|---|"]
    L += [f"| {k} | {s['n']} | {s['sum']:+.3f} | {s['shrunk_mean']:+.4f} | {s['size_mult_suggested']:g} |" for k, s in arms.items()]
    L += ["", "## Wallet weights (paper_v3, top 12)", "", "| handle | n | shrunk mean SOL | weight | holds OOS |", "|---|---|---|---|---|"]
    L += [f"| {h} | {v['n']} | {v['shrunk_mean']:+.5f} | {v['weight']} | {v['holds_oos']} |" for h, v in list(wts.items())[:12]]
    if cmp_:
        L += ["", "## Realistic vs legacy", "", "| family | realistic best (Δ, OOS) | legacy best (Δ, OOS) |", "|---|---|---|"]
        for f, d in cmp_["families"].items():
            L.append(f"| {f} | {d['realistic_best']} ({d['realistic_delta']:+.3f}, {d['realistic_oos']:+.3f}) | "
                     f"{d['legacy_best']} ({(d['legacy_delta'] or 0):+.3f}, {(d['legacy_oos'] or 0):+.3f}) |")
        L += ["", f"Weight bumps: realistic {cmp_['weight_bump_realistic'] or 'none'} · legacy {cmp_['weight_bump_legacy'] or 'none'}"]
        L += [f"- Still proposed: {t}" for t in cmp_["both"]]
        L += [f"- **Dropped** under realistic pricing: {t}" for t in cmp_["legacy_only"]]
        L += [f"- **New** under realistic pricing: {t}" for t in cmp_["realistic_only"]]
    L += ["", "## Ranked LIVE proposals (awaiting Andrew, NOT applied)"]
    L += [f"{p['rank']}. [{p['kind']}] {p['text']}" for p in P] or ["- None today."]
    L += ["", "Files: backend `/opt/fomo/coach/data/learn/v2/`. Related: [[FOMO MOC]] · "
          "[[2026-10-15 Trailing stop + tactic re-exam + self-learn v2 — Fomo]]", ""]
    return "\n".join(L)


def _basis_run(now, book):
    E = entries(book)
    res, have = run_grid(E, (book or {}).get("costs") and {k: v for k, v in book["costs"].items() if k != "legacy"})
    fs = family_scores(E, res)
    flt = filter_scores(E)
    arms = arm_sizing(E)
    wts = wallet_weights(book)
    P = proposals_from(fs, flt, arms, wts, now)
    for p in P:
        p["basis"] = "realistic" if book else "legacy"
    return E, have, fs, flt, arms, wts, P


def compare(P, LP, fs, lfs, wts, lwts):
    key = lambda p: (p["kind"], p["text"].split("(")[0].split(":")[0])          # noqa: E731
    kp, kl = {key(p) for p in P}, {key(p) for p in LP}
    fam = {}
    for f in fs:
        b = max(fs[f]["variants"], key=lambda v: fs[f]["variants"][v].get("delta_vs_control", 0))
        lb = max(lfs.get(f, {}).get("variants", {"control": {}}),
                 key=lambda v: lfs[f]["variants"][v].get("delta_vs_control", 0)) if f in lfs else None
        fam[f] = {"realistic_best": b, "realistic_delta": fs[f]["variants"][b].get("delta_vs_control"),
                  "realistic_oos": fs[f]["walk_forward"]["oos_delta"],
                  "legacy_best": lb, "legacy_delta": lfs[f]["variants"][lb].get("delta_vs_control") if lb else None,
                  "legacy_oos": lfs[f]["walk_forward"]["oos_delta"] if f in lfs else None}
    top = lambda w: [h for h, v in w.items() if v["n"] >= 50 and v["weight"] >= 1.2 and v["holds_oos"]][:3]  # noqa: E731
    return {"both": [p["text"] for p in P if key(p) in kl],
            "legacy_only": [p["text"] for p in LP if key(p) not in kp],
            "realistic_only": [p["text"] for p in P if key(p) not in kl],
            "families": fam, "weight_bump_realistic": top(wts), "weight_bump_legacy": top(lwts),
            "weights": {h: {"realistic": wts.get(h, {}).get("weight"), "realistic_mean": wts.get(h, {}).get("shrunk_mean"),
                            "legacy": lwts.get(h, {}).get("weight"), "legacy_mean": lwts.get(h, {}).get("shrunk_mean")}
                        for h in sorted(set(wts) | set(lwts))}}


def run(now=None, write_note=True, book=None):
    now = now or int(time.time() * 1000)
    today = datetime.fromtimestamp(now / 1000, CT).strftime("%Y-%m-%d")
    V2.mkdir(parents=True, exist_ok=True)
    book = book if book is not None else load_book()
    LE, lhave, lfs, lflt, larms, lwts, LP = _basis_run(now, None)
    if book:
        E, have, fs, flt, arms, wts, P = _basis_run(now, book)
    else:
        E, have, fs, flt, arms, wts, P = LE, lhave, lfs, lflt, larms, lwts, LP
    cmp_ = compare(P, LP, fs, lfs, wts, lwts) if book else None
    (V2 / f"proposals_{today}.legacy.json").write_text(json.dumps(LP, indent=1, default=str))
    (V2 / f"scores_{today}.json").write_text(json.dumps({"run_ms": now, "basis": "realistic" if book else "legacy",
                                                         "entries": len(E), "with_ticks": have,
                                                         "families": fs, "filters": flt, "arms": arms,
                                                         "wallet_weights": wts, "comparison": cmp_,
                                                         "legacy": {"entries": len(LE), "with_ticks": lhave,
                                                                    "families": lfs, "filters": lflt, "arms": larms,
                                                                    "wallet_weights": lwts}},
                                                        indent=1, default=str))
    (V2 / f"proposals_{today}.json").write_text(json.dumps(P, indent=1, default=str))
    winners = {f: max(d["variants"], key=lambda v: d["variants"][v].get("delta_vs_control", 0)) for f, d in fs.items()}
    spec = {"updated_ms": now, "paper_only": True, "winners": winners,
            "params": {f: FAMILIES[f][v] for f, v in winners.items()}, "grid": FAMILIES}
    (V2 / "shadow_spec.json").write_text(json.dumps(spec, indent=1, default=str))
    (OUT / "paper_overrides_v2.json").write_text(json.dumps({"updated_ms": now, "updated_ct": ct(now), "paper_only": True,
                                                             "winners": winners, "params": spec["params"]}, indent=1, default=str))
    if write_note:
        (OUT / "notes").mkdir(exist_ok=True)
        (OUT / "notes" / f"{today} Fomo self-learn v2.md").write_text(render(today, now, have, len(E), fs, flt, arms, wts, P,
                                                                            cmp_, "realistic" if book else "legacy"))
    with (OUT / "learn.log").open("a") as fh:
        fh.write(f"{ct(now)} v2 ok basis={'realistic' if book else 'legacy'} entries={len(E)} with_ticks={have} "
                 f"proposals={len(P)} legacy_proposals={len(LP)}\n")
    return {"entries": len(E), "with_ticks": have, "families": fs, "filters": flt, "arms": arms, "wallets": wts,
            "proposals": P, "legacy_proposals": LP, "comparison": cmp_, "basis": "realistic" if book else "legacy"}


if __name__ == "__main__":
    r = run(write_note="--no-note" not in sys.argv)
    print(json.dumps({"entries": r["entries"], "with_ticks": r["with_ticks"], "proposals": len(r["proposals"])}))
