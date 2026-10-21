"""v2 tick replay engine for the Fomo self-learner (PAPER / OFFLINE ONLY).

Replays one entry on a stored trade-tick path (data/learn/ticks/<mint>.json: [t_s, price_sol, side, sol, wallet8])
with a parameter set. Mirrors app/scout/exit_variants.evaluate() for the control (50% at 2x, rest on a 30% peak
trail while price >= entry; before 2x: trader sell, 35% hard stop, below-entry at 10 min, 1 h max hold), and the
same cost model (4% exit haircut, 1.5% overhead, stop lines fill at the line unless gapped >10%).
External events (trader_sold, rug flag) are passed in as times (s) taken from the logged paper/live rows.
"""
from __future__ import annotations
import json, os
from pathlib import Path

CONTROL = {"tp_mult": 2.0, "tp_frac": 0.5, "trail": 0.30, "trail_hi": None, "trail_step_x": None,
           "trail_time_s": None, "trail_after_time": None, "hard_stop": 0.35, "min10_s": 600, "max_hold_s": 3600,
           "trader_honor": (0, 1e9), "rug_delay_s": 0, "rug_honor": True, "pre_trail": None,
           "candle_stop_s": None, "candle_buf": 0.03, "ladder": (), "gap": 0.10, "hc": 0.04, "ovh": 0.015,
           "real_stop": None}


def load_ticks(mint, dirs):
    for d in dirs:
        p = Path(d) / f"{mint}.json"
        if p.exists():
            try:
                return json.loads(p.read_text())
            except ValueError:
                return None
    return None


def width(P, hm, since2x):
    w = P["trail"]
    if P["trail_step_x"] and hm >= P["trail_step_x"]:
        w = P["trail_hi"]
    if P["trail_time_s"] is not None and since2x is not None and since2x >= P["trail_time_s"]:
        w = P["trail_after_time"]
    return w


def replay(path, entry_px, entry_s, P, trader_s=None, rug_s=None, horizon_s=3 * 3600):
    """Returns dict(mult=net proceeds multiple incl. costs, pnl_frac=mult-1, reason, hold_s, peak) or None if no path."""
    P = {**CONTROL, **P}
    e = entry_px
    ticks = [(t, p) for t, p, *_ in path if entry_s <= t <= entry_s + horizon_s and p > 0]
    if len(ticks) < 2:
        return None
    rem, proceeds, hi, tp_done, t2x, m10 = 1.0, 0.0, e, False, None, False
    lad = sorted(P["ladder"])
    c_lo, c_t0, prev_lo = None, None, None
    reason = "open_end"
    hc, gap = P["hc"], P["gap"]

    def fill(px, line):
        return (line if px >= line * (1 - gap) else px) * (1 - hc)

    last_t, last_p = ticks[0]
    for t, p in ticks:
        last_t, last_p = t, p
        age = t - entry_s
        hi = max(hi, p)
        hm = hi / e
        cs = P["candle_stop_s"]
        if cs:
            b = int(age // cs)
            if c_t0 is None or b != c_t0:
                if c_lo is not None:
                    prev_lo = c_lo if prev_lo is None else max(prev_lo, c_lo)   # ratchet
                c_t0, c_lo = b, p
            else:
                c_lo = min(c_lo, p)
        armed = tp_done or hm >= P["tp_mult"]
        if armed and t2x is None:
            t2x = t
        out = None
        if armed and p >= e:
            if not tp_done and p >= e * P["tp_mult"]:
                tp_done = True
                proceeds += P["tp_frac"] * p * (1 - hc) / e
                rem -= P["tp_frac"]
            while lad and p >= lad[0][0] * e and rem > 1e-9:
                x, fr = lad.pop(0)
                fr = min(fr, rem)
                proceeds += fr * p * (1 - hc) / e
                rem -= fr
            w = width(P, hm, t - t2x)
            if p <= hi * (1 - w):
                out = (fill(p, hi * (1 - w)), f"trail_{round(w * 100)}")
        elif age >= P["max_hold_s"]:
            out = (p * (1 - hc), "max_hold")
        elif rug_s is not None and P["rug_honor"] and t >= rug_s + P["rug_delay_s"]:
            out = (p * (1 - hc), "rug_flag")
        elif (trader_s is not None and t >= trader_s and P["trader_honor"][0] <= trader_s - entry_s < P["trader_honor"][1]):
            out = (p * (1 - hc), "trader_sold")
        elif P["hard_stop"] and p <= e * (1 - P["hard_stop"]):
            line = e * (1 - P["hard_stop"])
            if P.get("real_stop"):        # v1.38 realistic: live stop fills (line × p50, gaps floored at live p10)
                out = (min(line * P["real_stop"][0], max(p, line * P["real_stop"][1])) * (1 - hc), "hard_stop")
            else:
                out = (fill(p, line), "hard_stop")
        elif P["pre_trail"] and not armed and p <= hi * (1 - P["pre_trail"]):
            out = (fill(p, hi * (1 - P["pre_trail"])), "pre_trail")
        elif cs and not armed and prev_lo is not None and p <= prev_lo * (1 - P["candle_buf"]):
            out = (fill(p, prev_lo * (1 - P["candle_buf"])), "candle_stop")
        elif not m10 and age >= P["min10_s"]:
            m10 = True
            if p < e:
                out = (p * (1 - hc), "below_entry_min10")
        if out:
            proceeds += rem * out[0] / e
            rem = 0
            reason = out[1]
            break
    if rem > 0:
        proceeds += rem * last_p * (1 - hc) / e
    mult = proceeds - P["ovh"]
    return {"mult": mult, "pnl_frac": mult - 1, "reason": reason, "hold_s": last_t - entry_s, "peak": hi / e,
            "reached_2x": tp_done}
