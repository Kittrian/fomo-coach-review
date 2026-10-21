#!/usr/bin/env python3
"""v1.37 replay validation: does REALISTIC paper land near LIVE over the same window? (read-only)

For each arm, the logged paper positions of the last N days are re-run through the SAME realism code the coach now uses
(app.paper_realism entry rules, app.live.engine.evaluate_exit with the live config of the live twin arm, live sell
ratios, live fees, price-proxy drain, per-arm gate ledger), on the price path the paper arm itself observed:
  * kol_confluence / paper_top4: every logged mark the arm wrote for that mint (sell + wallet_sell prices across all
    tracks) plus the logged peak (max_multiple) placed before the first trail exit;
  * scouts (scout_solo): the scout paper's own on-chain curve fills (+2 s entry, every rule's fill, high).
Historical on-chain reads at signal+latency do not exist, so replayed entries use the fallback rule
(max(trader fill × (1 + live p50 slip), the logged mark if fresh and not lagging)) — exactly what the coach does when the
on-chain read fails. Sparse paths can miss intra-path dips, so the replay is, if anything, still slightly optimistic.
Output: per arm legacy vs realistic vs live (all trades and mints traded by both), at the live trade size.
Usage: python3 scripts/paper_realism_replay.py [--days 7] [--json out.json]
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import statistics as st
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("PAPER_REALISM_ONCHAIN_ENABLED", "false")

from app import paper_realism as pr  # noqa: E402
from app.live.config import config as live_config  # noqa: E402
from app.live.engine import LivePos, evaluate_exit, is_partial  # noqa: E402


def rows(p: Path):
    out = []
    try:
        with p.open() as fh:
            for line in fh:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    except FileNotFoundError:
        pass
    return out


def simulate(arm: str, entry_px: float, entry_ms: int, path, P, lc, trader_wallets=(), trader_sell_ms=None):
    """Run the live rule set of the arm's live twin over a (ms, px) path. Returns (pnl_sol, pnl_pct, reason)."""
    live_arm = P["live_arm"]
    rules = list((lc.get("rules") or {}).get(live_arm) or [])
    spent, tokens = pr.entry_position(P, entry_px, P["size_sol"])
    p = LivePos(mint="x", arm=live_arm, size_sol=P["size_sol"], spent_lamports=spent, tokens_initial=tokens,
                tokens_left=tokens, entry_price_sol=entry_px, entry_ms=entry_ms, high_price_sol=entry_px,
                last_price_sol=entry_px, last_mark_ms=entry_ms, trader_wallets=list(trader_wallets))
    dw = pr.DrainWatch()
    reason = None
    prev = None
    for ms, px in path:
        if ms < entry_ms or not px or px <= 0:
            continue
        if prev and (px > prev * 20 or px > entry_px * 200):      # same absurd-mark guard as the live paper loop
            continue
        prev = px
        if trader_sell_ms and ms >= trader_sell_ms and not p.trader_sold:
            p.trader_sold = True
            p.meta["trader_sold_ms"] = trader_sell_ms
        p.last_price_sol, p.last_mark_ms = px, ms
        p.high_price_sol = max(p.high_price_sol, px)
        acts = evaluate_exit(p, px, ms, lc, rules)
        if not acts and dw.check("x", px, ms, P):
            acts = [("sell_all", 1.0, "rug_flag_liquidity_drain")]
        for kind, frac, rsn in acts:
            if kind == "sell_frac":
                p.tp_done = True
                p.ladder_done.append(rsn)
                tk = int(p.tokens_left * frac)
            else:
                tk = p.tokens_left
            if tk <= 0:
                continue
            spx = pr.exit_px(P, rsn, px, entry_px, lc)
            lam = pr.proceeds_lamports(P, tk, spx)
            p.tokens_left -= tk
            p.received_lamports += lam
            if p.tokens_left <= 0 or not is_partial(rsn):
                p.status, reason = "closed", rsn
                break
        if p.status == "closed":
            break
    if p.status != "closed":                       # path ended open: mark at the last observed price, sold now
        px = path[-1][1] if path else entry_px
        lam = pr.proceeds_lamports(P, p.tokens_left, px * P["generic_ratio"])
        p.received_lamports += lam
        p.tokens_left = 0
        reason = "end_of_path"
    pnl = (p.received_lamports - spent) / 1e9
    return pnl, pnl / P["size_sol"] * 100, reason


def fomo_family(arm: str, log: Path, t0: int, t1: int, legacy_track: str, P, lc):
    R = rows(log)
    opens = {r["id"]: r for r in R if r.get("kind") == "open" and t0 <= r["ts_ms"] < t1}
    fvt = {r["id"]: r for r in R if r.get("kind") == "fill_vs_trader"}
    pts = collections.defaultdict(list)
    closes = collections.defaultdict(list)
    wsell = {}
    for r in R:
        k = r.get("kind")
        if k in ("sell", "wallet_sell") and r.get("price_sol") and r.get("id") in opens:
            pts[r["id"]].append((r["ts_ms"], r["price_sol"]))
        if k == "wallet_sell" and r.get("id") in opens and r.get("first"):
            wsell.setdefault(r["id"], r["ts_ms"])
        if k == "close" and r.get("id") in opens and not r.get("realistic"):
            closes[r["id"]].append(r)
    out = []
    led = pr.GateLedger()
    for pid, o in sorted(opens.items(), key=lambda kv: kv[1]["ts_ms"]):
        f = fvt.get(pid) or {}
        tp = f.get("trader_price_sol")
        legacy_entry = f.get("entry_price_sol") or o.get("entry_price_sol")
        cl = closes.get(pid) or []
        leg = next((c for c in cl if c.get("track") == legacy_track), None)
        if not leg or not legacy_entry:
            continue
        mark = o.get("mark_price_sol") if o.get("entry_src") == "dexscreener" else None
        ef = pr.entry_fill(arm, tp, None, {"px": mark, "ms": o["ts_ms"]} if mark else None, P["size_sol"], P)
        rec = {"id": pid, "mint": o["mint"], "ts_ms": o["ts_ms"], "legacy_pnl_pct": leg.get("pnl_pct"),
               "legacy_reason": leg.get("reason"), "trader_px": tp, "handles": o.get("handles") or [],
               "close_ms": leg.get("ts_ms"), "legacy_pnl_sol": leg.get("pnl_sol"),
               "legacy_max_multiple": leg.get("max_multiple")}
        if not ef.get("ok"):
            rec["skipped"] = ef.get("reason")
            out.append(rec)
            continue
        epx = ef["px"]
        path = sorted(set(pts[pid]))
        peak = max([(c.get("max_multiple") or 0) for c in cl] + [0]) * legacy_entry
        if peak > 0:
            trail_ms = min([c["ts_ms"] for c in cl if c.get("reason") == "trail"] or [0])
            last_ms = max([c["ts_ms"] for c in cl] + [o["ts_ms"] + 1000])
            pk_ms = (trail_ms - 1000) if trail_ms else (o["ts_ms"] + last_ms) // 2
            path = sorted(path + [(max(pk_ms, o["ts_ms"] + 1), peak)])
        path = [(o["ts_ms"], mark or legacy_entry)] + path
        refused = led.refusals(arm, o["ts_ms"], would_live_buy=o.get("would_live_buy"), trader_px=tp, entry_px=epx, P=P)
        pnl, pct, why = simulate(arm, epx, o["ts_ms"], path, P, lc, o.get("wallets") or [], wsell.get(pid))
        if not refused:
            led.opened(pid, o["ts_ms"])
            exit_ms = max([c["ts_ms"] for c in cl] + [o["ts_ms"]])
            led.closed(pid, exit_ms, pnl)
        rec.update(real_pnl_sol=pnl, real_pnl_pct=pct, real_reason=why, gates_refused=refused, gated=not refused,
                   real_entry_vs_legacy_pct=(epx / legacy_entry - 1) * 100, entry_src=ef.get("src"))
        out.append(rec)
    return out


def scout_replay(t0: int, t1: int, P, lc, data: Path):
    R = rows(data / "scout_trades.jsonl")
    pos = {}
    for r in R:
        if r.get("kind") != "pos":
            continue
        p = r["pos"]
        if p.get("arm") != "scout_solo" or p.get("lat_s") != 2 or not (t0 <= p.get("entry_ms", 0) < t1):
            continue
        pos.setdefault(p["signal_id"], {})[p["rule"]] = p
    out = []
    led = pr.GateLedger()
    for sid, rs in sorted(pos.items(), key=lambda kv: min(x["entry_ms"] for x in kv[1].values())):
        any_p = next(iter(rs.values()))
        ent = any_p["entry_price_sol"]
        ref = any_p.get("ref_price_sol")
        legacy = rs.get("tp2x_trail")
        if not legacy or legacy.get("status") != "closed" or any("restart_gap" in (x.get("flags") or []) for x in rs.values()):
            continue
        path = [(any_p["entry_ms"], ent)]
        trader_sell_ms = None
        for x in rs.values():
            for f in x.get("fills") or []:
                path.append((f["ts_ms"], f["multiple"] * x["entry_price_sol"]))
                if f.get("reason") == "scout_sold":
                    trader_sell_ms = f["ts_ms"]
            if x.get("high_price"):
                hi_ms = min([f["ts_ms"] for f in (rs.get("tp2x_trail") or {}).get("fills") or []
                             if f.get("reason") == "trail"] or [x.get("exit_ms") or x["entry_ms"] + 1000]) - 1000
                path.append((max(hi_ms, x["entry_ms"] + 1), x["high_price"]))
        path = sorted(set(path))
        # on-chain curve fill at +2 s (≈ the live p50 latency), never below the scout's own fill
        epx = max(ent, ref or 0.0)
        refused = led.refusals("scout_solo", any_p["entry_ms"], would_live_buy=None, trader_px=ref, entry_px=epx, P=P)
        pnl, pct, why = simulate("scout_solo", epx, any_p["entry_ms"], path, P, lc,
                                 [any_p.get("scout_wallet")] if any_p.get("scout_wallet") else [], trader_sell_ms)
        if not refused:
            led.opened(sid, any_p["entry_ms"])
            led.closed(sid, max(x.get("exit_ms") or x["entry_ms"] for x in rs.values()), pnl)
        lp = legacy["pnl"]["pnl_pct"] if legacy.get("pnl") else None
        if lp is None:
            val = legacy.get("sol_out", 0) - legacy.get("fees_sol", 0)
            lp = (val / legacy["entry_sol"] - 1) * 100
        out.append({"id": sid, "mint": any_p["mint"], "ts_ms": any_p["entry_ms"], "legacy_pnl_pct": lp,
                    "real_pnl_sol": pnl, "real_pnl_pct": pct, "real_reason": why, "gates_refused": refused,
                    "gated": not refused, "real_entry_vs_legacy_pct": (epx / ent - 1) * 100})
    return out


def live_rows(data: Path, arm_pred, t0: int, t1: int):
    R = rows(data / "live_trades.jsonl")
    buys = {r["id"]: r for r in R if r.get("kind") == "buy"}
    out = []
    for c in R:
        if c.get("kind") != "close" or not (t0 <= c["ts_ms"] < t1) or not arm_pred(c.get("arm") or ""):
            continue
        b = buys.get(c["id"]) or {}
        out.append({"mint": c["mint"], "pnl_sol": c["pnl"]["pnl_sol"], "pnl_pct": c["pnl"]["pnl_pct"],
                    "size": b.get("size_sol"), "reason": c.get("reason")})
    return out


def summarize(name, paper, live, size):
    def avg(v):
        return round(st.mean(v), 2) if v else None
    traded = [r for r in paper if "real_pnl_pct" in r]
    gated = [r for r in traded if r.get("gated")]
    lm = {r["mint"]: r for r in live}
    matched = [r for r in traded if r["mint"] in lm]
    res = {
        "arm": name, "size_sol_used": size,
        "paper_positions": len(paper), "realistic_entries": len(traded),
        "realistic_skipped_no_data": sum(1 for r in paper if r.get("skipped")),
        "legacy_avg_pct": avg([r["legacy_pnl_pct"] for r in traded]),
        "realistic_avg_pct": avg([r["real_pnl_pct"] for r in traded]),
        "realistic_gated_n": len(gated), "realistic_gated_avg_pct": avg([r["real_pnl_pct"] for r in gated]),
        "legacy_total_sol_at_live_size": round(sum(r["legacy_pnl_pct"] for r in traded) / 100 * size, 3),
        "realistic_total_sol": round(sum(r["real_pnl_sol"] for r in traded), 3),
        "realistic_gated_total_sol": round(sum(r["real_pnl_sol"] for r in gated), 3),
        "live_n": len(live), "live_avg_pct": avg([r["pnl_pct"] for r in live]),
        "live_total_sol": round(sum(r["pnl_sol"] for r in live), 3),
        "live_avg_size": round(st.mean([r["size"] for r in live if r.get("size")]), 3) if live else None,
        "matched_n": len(matched),
        "matched_legacy_avg_pct": avg([r["legacy_pnl_pct"] for r in matched]),
        "matched_realistic_avg_pct": avg([r["real_pnl_pct"] for r in matched]),
        "matched_live_avg_pct": avg([lm[r["mint"]]["pnl_pct"] for r in matched]),
        "matched_legacy_total_sol_at_live_size": round(sum(r["legacy_pnl_pct"] for r in matched) / 100 * size, 3),
        "matched_realistic_total_sol": round(sum(r["real_pnl_sol"] for r in matched), 3),
        "matched_live_total_sol": round(sum(lm[r["mint"]]["pnl_sol"] for r in matched), 3),
        "realistic_exit_reasons": dict(collections.Counter(r["real_reason"] for r in traded).most_common(8)),
        "gates_refused": dict(collections.Counter(g.split(" ")[0] for r in traded for g in r.get("gates_refused") or [])),
        "median_real_entry_vs_legacy_pct": round(st.median([r["real_entry_vs_legacy_pct"] for r in traded]), 1)
        if traded else None,
    }
    if res["matched_n"]:
        res["matched_gap_legacy_vs_live_pct_pts"] = round(res["matched_legacy_avg_pct"] - res["matched_live_avg_pct"], 2)
        res["matched_gap_realistic_vs_live_pct_pts"] = round(res["matched_realistic_avg_pct"] - res["matched_live_avg_pct"], 2)
    return res


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=7)
    ap.add_argument("--data", default=os.getenv("COACH_DATA_DIR") or str(ROOT / "data"))
    ap.add_argument("--json", default=None)
    a = ap.parse_args()
    data = Path(a.data)
    lc = live_config()
    live_all = rows(data / "live_trades.jsonl")
    t1 = max(r.get("ts_ms", 0) for r in live_all) + 1
    t0 = int(t1 - a.days * 86400_000)
    out = {"window": {"start_ms": t0, "end_ms": t1, "days": a.days}, "arms": []}
    Pk = pr.params("kol_confluence")
    kol = fomo_family("kol_confluence", data / "kol_confluence.jsonl", t0, t1, "live_exit", Pk, lc)
    out["arms"].append(summarize("kol_confluence", kol, live_rows(data, lambda x: x == "kol_confluence", t0, t1),
                                 Pk["size_sol"]))
    Ps = pr.params("scout_solo")
    sc = scout_replay(t0, t1, Ps, lc, data)
    out["arms"].append(summarize("scout_solo (R4 + scouts)", sc, live_rows(data, lambda x: x.startswith("scout"), t0, t1),
                                 Ps["size_sol"]))
    Pt = pr.params("paper_top4")
    t4 = fomo_family("paper_top4", data / "paper_top4.jsonl", t0, t1, "follow_wallet", Pt, lc)
    out["arms"].append(summarize("paper_top4 (no live twin)", t4, [], Pt["size_sol"]))
    out["params"] = {k: {kk: vv for kk, vv in v.items() if kk != "live_cfg"} for k, v in
                     (("kol_confluence", Pk), ("scout_solo", Ps), ("paper_top4", Pt))}
    if a.json:
        Path(a.json).write_text(json.dumps(out, indent=1, default=str))
    print(json.dumps(out["arms"], indent=1, default=str))


if __name__ == "__main__":
    main()
