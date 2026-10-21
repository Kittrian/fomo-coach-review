"""Grade paper_fresh5 against the normal bar: profitable in both halves with the best 3 removed. Read-only."""
import json, sys
from pathlib import Path
p = Path(__file__).resolve().parents[1] / "data" / "paper_fresh5.jsonl"
rows = [json.loads(l) for l in p.open(errors="replace")] if p.exists() else []
c = sorted((r for r in rows if r.get("kind") == "close"), key=lambda r: r["ts_ms"])
v = [r["pnl_sol"] for r in c]
ex3 = lambda a: sum(sorted(a)[:-3]) if len(a) > 3 else 0.0
h = len(v) // 2
res = {"n": len(v), "skips": sum(r.get("kind") == "skip" for r in rows), "total": round(sum(v), 4),
       "ex3": round(ex3(v), 4), "h1_ex3": round(ex3(v[:h]), 4), "h2_ex3": round(ex3(v[h:]), 4),
       "win_rate": round(sum(x > 0 for x in v) / len(v), 3) if v else None,
       "days": round((c[-1]["ts_ms"] - c[0]["ts_ms"]) / 864e5, 2) if c else 0}
res["passes_bar"] = bool(len(v) >= 40 and res["ex3"] > 0 and res["h1_ex3"] > 0 and res["h2_ex3"] > 0)
print(json.dumps(res))
