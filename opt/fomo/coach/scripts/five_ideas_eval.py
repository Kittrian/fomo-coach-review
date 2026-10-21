#!/usr/bin/env python3
"""v1.41 read-only evaluation of the five ideas on realistic data (split-half by entry time)."""
import json, os, sys, statistics as st, collections, bisect
D = sys.argv[1] if len(sys.argv) > 1 else "/opt/fomo/coach/data"
LAT = float(os.environ.get("EVAL_SELL_LAT_S", "2.0"))
HC, OVH = 0.0135, 0.0005
SIZE = 0.2

def jl(p):
    with open(p) as f:
        for ln in f:
            try: yield json.loads(ln)
            except Exception: pass

sig = {}
for r in jl(f"{D}/scout_trades.jsonl"):
    if r.get("kind") == "signal" and r.get("decision") == "enter" and r.get("arm") in ("scout_solo", "scout_confirmed") \
            and not r.get("excluded") and r.get("scout_wallet") and len(r.get("mint", "")) > 30:
        sig[r["id"]] = r
book = json.load(open(f"{D}/learn/realistic_book.json"))
var = collections.defaultdict(dict)
for r in book["variants"]:
    if r["signal_id"] in sig:
        var[r["signal_id"]][r["variant"]] = r

def ticks_for(mint):
    p = f"{D}/learn/ticks/{mint}.json"
    if not os.path.exists(p): return None
    return json.load(open(p)).get("ticks") or None

def whale_sells(tk, w8, entry_s):
    """[(t, frac of whale holdings sold)] after entry, using whale buys/sells in the tick stream."""
    hold, out = 0.0, []
    for t, px, side, sol, w in tk:
        if w != w8 or not px: continue
        tok = (sol or 0) / px
        if side in ("b", "buy"): hold += tok
        elif side in ("s", "sell"):
            fr = 1.0 if hold <= 0 else min(1.0, tok / hold)
            hold = max(0.0, hold - tok)
            if t >= entry_s: out.append((t, fr))
    return out

def px_at(ts, ps, t):
    i = bisect.bisect_left(ts, t)
    return ps[min(i, len(ps) - 1)]

def sim(tk, e, es, sells, mode):
    """Live-like scout exit: 35% stop, 15s candle stop -3% (pre-arm), half at 2x then 15% trail, min-10, 1h max.
    mode: dict(win=(lo,hi) s after entry for honoring whale sells, prop=bool, full_at=frac)."""
    ticks = [(t, p) for t, p, *_ in tk if es <= t <= es + 3600 and p > 0]
    if len(ticks) < 2: return None
    ts = [t for t, _ in ticks]; ps = [p for _, p in ticks]
    rem, proc, hi, armed, m10 = 1.0, 0.0, e, False, False
    c_t0 = c_lo = prev_lo = None
    si = 0; reason = "open_end"
    def ex(t, frac, why):
        nonlocal rem, proc
        fp = px_at(ts, ps, t + LAT)
        proc += frac * fp * (1 - HC) / e; rem -= frac
    for t, p in ticks:
        age = t - es; hi = max(hi, p)
        b = int(age // 15)
        if c_t0 is None or b != c_t0:
            if c_lo is not None: prev_lo = c_lo if prev_lo is None else max(prev_lo, c_lo)
            c_t0, c_lo = b, p
        else: c_lo = min(c_lo, p)
        if not armed and p >= 2 * e:
            armed = True; proc += 0.5 * p * (1 - HC) / e; rem -= 0.5
        if armed:
            if p <= hi * 0.85: ex(t, rem, "trail"); reason = "trail"; break
            continue
        while si < len(sells) and sells[si][0] <= t:
            st_, fr = sells[si]; si += 1
            lo, hi_w = mode["win"]
            if lo <= st_ - es < hi_w and rem > 1e-9:
                if mode.get("prop") and fr < mode.get("full_at", 0.9):
                    ex(t, rem * fr, "trader_part")
                else:
                    ex(t, rem, "trader_sold"); reason = "trader_sold"
        if rem <= 1e-9: break
        if p <= e * 0.65: ex(t, rem, "stop"); reason = "stop"; break
        if prev_lo is not None and p <= prev_lo * 0.97: ex(t, rem, "candle"); reason = "candle"; break
        if not m10 and age >= 600:
            m10 = True
            if p < e: ex(t, rem, "min10"); reason = "min10"; break
        if age >= 3600: ex(t, rem, "max_hold"); reason = "max_hold"; break
    if rem > 1e-9: proc += rem * ps[-1] * (1 - HC) / e
    return proc - 1 - OVH, reason

MODES = {
    "live_30s_full": {"win": (0, 30)},
    "never": {"win": (0, -1)},
    "always_full": {"win": (0, 1e9)},
    "win60_full": {"win": (0, 60)},
    "win120_full": {"win": (0, 120)},
    "win300_full": {"win": (0, 300)},
    "always_prop": {"win": (0, 1e9), "prop": True, "full_at": 0.9},
    "always_prop50": {"win": (0, 1e9), "prop": True, "full_at": 0.5},
    "30full_then_prop": None,
}
rows = []
for sid, s in sig.items():
    v = var.get(sid, {})
    base = v.get("A") or next(iter(v.values()), None)
    if not base: continue
    e = float(base["real_entry_px"]); tp = float(base.get("trader_px") or 0)
    es = int(base["entry_ms"]) / 1000
    tk = ticks_for(s["mint"])
    rec = {"sid": sid, "mint": s["mint"], "es": es, "arm": s["arm"], "label": s.get("scout") or s["scout_wallet"][:6],
           "wallet": s["scout_wallet"], "prem": (e / tp - 1) if tp > 0 else None,
           "var": {k: float(x["pnl_sol"]) for k, x in v.items()}, "sim": None, "age_s": None}
    if tk:
        w8 = s["scout_wallet"][:8]
        buys = [t for t, px, side, sol, w in tk if w == w8 and side in ("b", "buy") and t <= es + 1]
        if buys: rec["age_s"] = es - max(buys)
        sells = whale_sells(tk, w8, es)
        rec["n_sells"] = len(sells)
        res = {}
        for m, cfg in MODES.items():
            if cfg is None:
                r1 = sim(tk, e, es, sells, {"win": (0, 1e9), "prop": True, "full_at": 0.9}) if True else None
                # first 30 s full, after that proportional
                ss = [(t, (1.0 if t - es < 30 else fr)) for t, fr in sells]
                r1 = sim(tk, e, es, ss, {"win": (0, 1e9), "prop": True, "full_at": 0.9})
            else:
                r1 = sim(tk, e, es, sells, cfg)
            if r1: res[m] = r1[0]
        if res: rec["sim"] = res
    rows.append(rec)
rows.sort(key=lambda r: r["es"])
mid = rows[len(rows) // 2]["es"] if rows else 0
def half(r): return "H1" if r["es"] < mid else "H2"
out = {"n_signals": len(rows), "split_ct_epoch": mid, "sell_lat_s": LAT}

# ---- 1. whale sells (tick sim)
simr = [r for r in rows if r["sim"]]
agg = collections.defaultdict(lambda: {"H1": 0.0, "H2": 0.0, "n1": 0, "n2": 0})
for r in simr:
    h = half(r)
    for m, x in r["sim"].items():
        agg[m][h] += x * SIZE; agg[m]["n" + h[1]] += 1
out["whale_sells"] = {m: {k: round(v, 3) if isinstance(v, float) else v for k, v in a.items()} for m, a in agg.items()}
out["whale_sells_n_with_sells"] = sum(1 for r in simr if r.get("n_sells"))

# ---- 2. late entry: premium X, age Y (slots ~ 0.4 s)
def sweep(key, ths, getter, pnlget):
    res = {}
    for th in ths:
        a = {"H1": 0.0, "H2": 0.0, "k1": 0, "k2": 0, "skip1": 0, "skip2": 0}
        for r in rows:
            x = getter(r); pnl = pnlget(r)
            if pnl is None: continue
            h = half(r)
            if x is not None and x > th: a["skip" + h[1]] += 1; continue
            a[h] += pnl; a["k" + h[1]] += 1
        res[str(th)] = {k: round(v, 3) if isinstance(v, float) else v for k, v in a.items()}
    return res
pv = lambda r: r["var"].get("CANDLE15_ARM30")
ps_ = lambda r: (r["sim"]["live_30s_full"] * SIZE) if r["sim"] and "live_30s_full" in r["sim"] else None
out["chase_X_variants"] = sweep("prem", [0.0, 0.01, 0.02, 0.03, 0.05, 0.08, 0.10, 0.15, 1e9], lambda r: r["prem"], pv)
out["chase_X_ticks"] = sweep("prem", [0.0, 0.01, 0.02, 0.03, 0.05, 0.08, 0.10, 0.15, 1e9], lambda r: r["prem"], ps_)
out["age_Y_slots_ticks"] = sweep("age", [2, 3, 5, 8, 12, 20, 40, 1e9],
                                 lambda r: (r["age_s"] / 0.4) if r["age_s"] is not None else None, ps_)
prem_known = [r["prem"] for r in rows if r["prem"] is not None]
out["prem_quantiles"] = [round(q, 4) for q in st.quantiles(prem_known, n=10)] if len(prem_known) > 10 else None
ages = [r["age_s"] / 0.4 for r in rows if r["age_s"] is not None]
out["age_slot_quantiles"] = [round(q, 1) for q in st.quantiles(ages, n=10)] if len(ages) > 10 else None

# ---- 3/4. per-wallet edge (variant CANDLE15_ARM30 = closest to live) + walk-forward sizing
pw = collections.defaultdict(lambda: {"H1": [], "H2": []})
for r in rows:
    x = pv(r)
    if x is None: continue
    pw[r["label"]][half(r)].append(x / SIZE)   # pnl fraction
def size_rule(h1):
    if len(h1) < 5: return 0.10
    m = st.mean(h1)
    return 0.20 if m > 0.05 else 0.15 if m > 0 else 0.05
wf = {"flat_0.20": 0.0, "flat_0.10": 0.0, "sized": 0.0, "staked_sized": 0.0, "staked_flat20": 0.0}
per = {}
for lab, d in pw.items():
    s = size_rule(d["H1"]); h2 = sum(d["H2"])
    wf["flat_0.20"] += h2 * 0.20; wf["flat_0.10"] += h2 * 0.10; wf["sized"] += h2 * s
    wf["staked_sized"] += len(d["H2"]) * s; wf["staked_flat20"] += len(d["H2"]) * 0.2
    per[lab] = {"n1": len(d["H1"]), "mean1": round(st.mean(d["H1"]), 4) if d["H1"] else None,
                "n2": len(d["H2"]), "mean2": round(st.mean(d["H2"]), 4) if d["H2"] else None,
                "sum_all_sol_at_0.2": round((sum(d["H1"]) + h2) * 0.2, 3), "h1_size": s}
out["sizing_walkforward_H2_sol"] = {k: round(v, 3) for k, v in wf.items()}
out["per_wallet"] = dict(sorted(per.items(), key=lambda kv: -kv[1]["sum_all_sol_at_0.2"]))
# sign agreement H1 vs H2
pairs = [(st.mean(d["H1"]), st.mean(d["H2"])) for d in pw.values() if len(d["H1"]) >= 5 and len(d["H2"]) >= 5]
out["edge_persistence"] = {"wallets": len(pairs), "same_sign": sum(1 for a, b in pairs if (a > 0) == (b > 0)),
                           "corr": round(st.correlation([a for a, _ in pairs], [b for _, b in pairs]), 3) if len(pairs) > 2 else None}

# ---- 3. lead wallets from detections (first buy per wallet per mint; recv_ms; tx-path decodes excluded)
first = collections.defaultdict(dict); lab = {}
for r in jl(f"{D}/scout_detections.jsonl"):
    if r.get("kind") != "trade" or not r.get("is_buy"): continue
    if r.get("decode_path") not in (None, "logs"): continue
    w = r["wallet"]; lab[w] = (r.get("label"), r.get("role"))
    m = r["mint"]
    if w not in first[m] or r["recv_ms"] < first[m][w]: first[m][w] = r["recv_ms"]
lead = collections.defaultdict(lambda: {"shared": 0, "first": 0, "slots_ahead": []})
for m, d in first.items():
    if len(d) < 2: continue
    srt = sorted(d.items(), key=lambda kv: kv[1])
    for i, (w, t) in enumerate(srt):
        others = [x for ww, x in d.items() if ww != w]
        L = lead[w]; L["shared"] += 1; L["first"] += (i == 0)
        L["slots_ahead"].append((st.median(others) - t) / 400)
lt = []
for w, L in lead.items():
    if L["shared"] < 3: continue
    l, role = lab.get(w, (None, None))
    lt.append({"label": l or w[:6], "role": role, "shared": L["shared"], "first_pct": round(L["first"] / L["shared"], 2),
               "median_slots_ahead": round(st.median(L["slots_ahead"]), 1),
               "realistic_sol_at_0.2": per.get(l or "", {}).get("sum_all_sol_at_0.2")})
lt.sort(key=lambda x: -x["median_slots_ahead"])
out["lead_wallets"] = lt[:25]
json.dump(out, sys.stdout, indent=1, default=str)
