#!/usr/bin/env python3
"""v1.41 read-only: lead (on-chain blockTime from tick files) + realistic per-handle edge (paper books), split-half."""
import json, os, sys, ast, collections, statistics as st
sys.path.insert(0, "/opt/fomo/coach")
D = "/opt/fomo/coach/data"
roster = {}
for r in json.load(open(f"{D}/scout_wallets.json"))["wallets"]:
    roster[r["address"]] = (r["label"], r["role"])
try:
    from app.paper_v3 import cfg
    c = cfg()
    for a, h in c["targets"].items(): roster.setdefault(a, (h, "paper_v3"))
    for a, h in c["watch"].items(): roster.setdefault(a, (h, "watch"))
except Exception as e:
    print("paper_v3 cfg err", e, file=sys.stderr)
w8 = {a[:8]: v for a, v in roster.items()}
# lead from ticks
lead = collections.defaultdict(lambda: {"shared": 0, "first": 0, "s_ahead": []})
nmint = 0
for f in os.listdir(f"{D}/learn/ticks"):
    tk = json.load(open(f"{D}/learn/ticks/{f}")).get("ticks") or []
    fb = {}
    for t, px, side, sol, w in tk:
        if side in ("b", "buy") and w in w8 and w not in fb: fb[w] = t
    if len(fb) < 2: continue
    nmint += 1
    tmin = min(fb.values())
    for w, t in fb.items():
        others = [x for ww, x in fb.items() if ww != w]
        L = lead[w]; L["shared"] += 1; L["first"] += (t == tmin); L["s_ahead"].append(st.median(others) - t)
# edge from realistic books (single-handle rows with pnl)
book = json.load(open(f"{D}/learn/realistic_book.json"))
rows = []
for bk in ("paper_v3", "paper_top4"):
    v = book["books"][bk]; rr = v["rows"] if isinstance(v, dict) else v
    for r in rr:
        if r.get("pnl_sol") in (None, "") or r.get("gated") in ("False", False): continue
        try: hs = ast.literal_eval(r["handles"]) if isinstance(r["handles"], str) else r["handles"]
        except Exception: continue
        if len(hs) != 1: continue
        rows.append((int(r["entry_ms"]), hs[0], float(r["pnl_sol"]) / float(r.get("size_sol") or 0.2), bk, r["mint"]))
seen = set(); dd = []
for x in sorted(rows):
    k = (x[1], x[4])
    if k in seen: continue
    seen.add(k); dd.append(x)
mid = dd[len(dd) // 2][0]
pw = collections.defaultdict(lambda: {"H1": [], "H2": []})
for t, h, fr, bk, m in dd:
    pw[h]["H1" if t < mid else "H2"].append(fr)
def rule(h1, lo=0.05, mid_=0.10, hi=0.20):
    if len(h1) < 10: return mid_
    m = st.mean(h1)
    return hi if m > 0.03 else mid_ if m > 0 else lo
wf = collections.Counter(); per = {}
for h, d in pw.items():
    s = rule(d["H1"]); h2 = sum(d["H2"])
    wf["flat_0.10"] += h2 * 0.10; wf["sized"] += h2 * s; wf["stake_flat"] += 0.10 * len(d["H2"]); wf["stake_sized"] += s * len(d["H2"])
    per[h] = {"n1": len(d["H1"]), "m1": round(st.mean(d["H1"]), 4) if d["H1"] else None, "s1": round(sum(d["H1"]), 2),
              "n2": len(d["H2"]), "m2": round(st.mean(d["H2"]), 4) if d["H2"] else None, "s2": round(sum(d["H2"]), 2), "size_from_h1": s}
pairs = [(st.mean(d["H1"]), st.mean(d["H2"])) for d in pw.values() if len(d["H1"]) >= 10 and len(d["H2"]) >= 10]
out = {"split_ms": mid, "n_rows": len(dd), "walkforward_H2_sol": {k: round(v, 3) for k, v in wf.items()},
       "persistence": {"wallets": len(pairs), "same_sign": sum(1 for a, b in pairs if (a > 0) == (b > 0)),
                       "corr": round(st.correlation(*zip(*pairs)), 3) if len(pairs) > 2 else None},
       "per_handle": dict(sorted(per.items(), key=lambda kv: -(kv[1]["s1"] + kv[1]["s2"])))}
lt = []
for w, L in lead.items():
    lab, role = w8[w]
    e = per.get(lab, {})
    lt.append({"label": lab, "role": role, "shared": L["shared"], "first_pct": round(L["first"] / L["shared"], 2),
               "median_slots_ahead": round(st.median(L["s_ahead"]) * 2.5, 1), "edge_frac_h1": e.get("m1"), "edge_frac_h2": e.get("m2"),
               "n": (e.get("n1") or 0) + (e.get("n2") or 0)})
lt.sort(key=lambda x: -x["median_slots_ahead"])
out["lead_mints"] = nmint; out["lead"] = lt
json.dump(out, sys.stdout, indent=1)
