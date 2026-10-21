"""Weekly scorecard over the candidate log + shadow outcomes. DESCRIPTIVE ONLY: it never changes a threshold,
a size, an arm or any setting, and nothing in the coach reads its output to tune itself.

Honesty rules:
  * only newer data: rows from `since` (default the last 7 days) AND logged by version >= min_version
    (default 1.16.0-demo, the first build with this log), excluded rows dropped;
  * every rate comes with a Wilson 95% interval; medians with a seeded bootstrap 95% interval;
  * groups with n < 20 are flagged "insufficient" — read them as anecdotes;
  * an "older half vs newer half" check shows whether a group's direction held over time.
"""
from __future__ import annotations

import collections
import math
import random
import statistics
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

MIN_N = 20


def _ver(v: Optional[str]) -> Tuple[int, ...]:
    try:
        return tuple(int(x) for x in str(v or "0").split("-")[0].split("."))
    except ValueError:
        return (0,)


def wilson(k: int, n: int, z: float = 1.96) -> Optional[List[float]]:
    if n <= 0:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(max(0.0, c - h), 4), round(min(1.0, c + h), 4)]


def boot_median(xs: List[float], *, iters: int = 1000, seed: int = 7) -> Optional[List[float]]:
    if len(xs) < 3:
        return None
    rnd = random.Random(seed)
    meds = sorted(statistics.median(rnd.choices(xs, k=len(xs))) for _ in range(iters))
    return [round(meds[int(0.025 * iters)], 4), round(meds[int(0.975 * iters) - 1], 4)]


def load(since_ms: int, min_version: str) -> List[Dict[str, Any]]:
    from app.learning.candidates import rows as cand_rows
    from app.learning.outcomes import path as out_path
    import json
    cands = {r["cid"]: {**r, "out": {}, "holders": None} for r in cand_rows(since_ms)
             if _ver(r.get("version")) >= _ver(min_version)}
    p = out_path()
    if p.exists():
        with p.open() as fh:
            for line in fh:
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                c = cands.get(o.get("cid"))
                if not c:
                    continue
                if o.get("kind") == "outcome" and o.get("src") != "missed" and o.get("price_sol"):
                    h = int(o["horizon_s"])
                    if h == 0:
                        c["p0"] = o["price_sol"]
                    if "ret_base" in o:
                        if o.get("ret") is not None:
                            c["out"][h] = o["ret"]
                    elif c.get("p0"):   # first-hour v1.16.0 rows measured ret vs the ref price: re-base on the +0 price
                        c["out"][h] = round(o["price_sol"] / c["p0"] - 1, 4)
                elif o.get("kind") == "holders":
                    c["holders"] = o
    return list(cands.values())


def _group_stats(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    n = len(rows)
    out: Dict[str, Any] = {"n": n, "insufficient": n < MIN_N}
    for h, name in ((300, "5m"), (900, "15m"), (3600, "60m")):
        xs = [r["out"][h] for r in rows if h in r["out"]]
        out[f"n_{name}"] = len(xs)
        if xs:
            k = sum(1 for x in xs if x > 0)
            out[f"pos_rate_{name}"] = round(k / len(xs), 4)
            out[f"pos_rate_{name}_ci95"] = wilson(k, len(xs))
            out[f"median_ret_{name}"] = round(statistics.median(xs), 4)
            out[f"median_ret_{name}_ci95"] = boot_median(xs)
    with_any = [r for r in rows if r["out"]]
    if with_any:
        k2 = sum(1 for r in with_any if max(r["out"].values()) >= 1.0)
        out["hit_2x_rate"] = round(k2 / len(with_any), 4)
        out["hit_2x_ci95"] = wilson(k2, len(with_any))
        r60 = [r for r in with_any if 3600 in r["out"]]
        if r60:
            kr = sum(1 for r in r60 if r["out"][3600] <= -0.8)
            out["down_80_at_60m_rate"] = round(kr / len(r60), 4)
            out["down_80_at_60m_ci95"] = wilson(kr, len(r60))
    if n >= 2 * MIN_N:
        rs = sorted(rows, key=lambda r: r["ts_ms"])
        halves = [rs[: n // 2], rs[n // 2:]]
        med = []
        for hf in halves:
            xs = [r["out"][900] for r in hf if 900 in r["out"]]
            med.append(round(statistics.median(xs), 4) if xs else None)
        out["median_ret_15m_older_vs_newer"] = med
        out["direction_held"] = None if None in med else (med[0] > 0) == (med[1] > 0)
    return out


def summarize(*, days: float = 7.0, min_version: str = "1.16.0-demo", now_ms: Optional[int] = None) -> Dict[str, Any]:
    now_ms = now_ms or int(time.time() * 1000)
    since = now_ms - int(days * 86_400_000)
    allrows = load(since, min_version)
    rows = [r for r in allrows if not r.get("stage")]        # v1.17: later-stage rows reported separately
    stage_rows = [r for r in allrows if r.get("stage")]
    by_arm: Dict[str, List[Dict[str, Any]]] = collections.defaultdict(list)
    by_arm_dec: Dict[Tuple[str, str], List[Dict[str, Any]]] = collections.defaultdict(list)
    by_skip: Dict[Tuple[str, str], List[Dict[str, Any]]] = collections.defaultdict(list)
    for r in rows:
        by_arm[r["arm"]].append(r)
        by_arm_dec[(r["arm"], "entered" if r.get("entered") else "skipped")].append(r)
        if not r.get("entered"):
            by_skip[(r["arm"], r.get("skip_reason") or "?")].append(r)
    return {
        "descriptive_only": True, "auto_tune": False,
        "note": "No threshold, size, arm or setting is changed by this report. n<20 groups are anecdotes.",
        "window": {"days": days, "since_ms": since, "until_ms": now_ms, "min_version": min_version},
        "candidates": len(rows), "with_outcomes": sum(1 for r in rows if r["out"]),
        "by_arm": {a: _group_stats(v) for a, v in sorted(by_arm.items())},
        "by_arm_decision": {f"{a}|{d}": _group_stats(v) for (a, d), v in sorted(by_arm_dec.items())},
        "by_skip_reason": {f"{a}|{s}": _group_stats(v) for (a, s), v in
                           sorted(by_skip.items(), key=lambda kv: -len(kv[1]))[:40]},
        # v1.17: live pre-buy stage (rug checks, mesh, reserve …) — counts only; outcomes sit on the arm's decision row
        "live_prebuy": {
            "rows": len(stage_rows),
            "by_arm_reason": dict(collections.Counter(f"{r['arm']}|{r.get('skip_reason') or 'passed'}"
                                                      for r in stage_rows).most_common(40)),
            "rug_unknown_checks": dict(collections.Counter(u for r in stage_rows
                                                           for u in ((r.get("features") or {}).get("rug_unknown") or []))),
        },
    }


def to_markdown(s: Dict[str, Any]) -> str:
    def pct(x: Any) -> str:
        return "—" if x is None else f"{x * 100:+.1f}%"

    def ci(x: Any) -> str:
        return "" if not x else f" [{x[0] * 100:.0f}–{x[1] * 100:.0f}%]"

    w = s["window"]
    lines = [f"# FOMO scorecard — last {w['days']:g} days (version ≥ {w['min_version']})", "",
             f"_{s['note']}_", "", f"Candidates: {s['candidates']} · with outcomes: {s['with_outcomes']}", ""]
    for title, key in (("By arm", "by_arm"), ("Entered vs skipped", "by_arm_decision"), ("By skip reason", "by_skip_reason")):
        lines += [f"## {title}", "", "| group | n | +15m >0 (95% CI) | median +15m (95% CI) | median +60m | hit 2× | flag |",
                  "|---|---:|---|---|---|---|---|"]
        for g, st in s[key].items():
            lines.append(f"| {g} | {st['n']} | "
                         + (f"{st.get('pos_rate_15m', 0) * 100:.0f}%{ci(st.get('pos_rate_15m_ci95'))}" if st.get("n_15m") else "—")
                         + f" | {pct(st.get('median_ret_15m'))}{'' if not st.get('median_ret_15m_ci95') else ' [' + pct(st['median_ret_15m_ci95'][0]) + ', ' + pct(st['median_ret_15m_ci95'][1]) + ']'}"
                         + f" | {pct(st.get('median_ret_60m'))} | "
                         + (f"{st['hit_2x_rate'] * 100:.0f}%{ci(st.get('hit_2x_ci95'))}" if st.get("hit_2x_rate") is not None else "—")
                         + f" | {'insufficient (n<20)' if st['insufficient'] else ''} |")
        lines.append("")
    return "\n".join(lines)
