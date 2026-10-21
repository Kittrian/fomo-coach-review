"""v1.18 exit-variants PAPER test: alternative post-2x exit splits simulated side by side on every scout paper entry.

PAPER ONLY / ADD-ON. It never imports or calls the live engine, never changes live config, never buys, never sends
a transaction and makes no RPC call of its own: it is fed by the scout paper engine's existing price marks
(the same curve / DexScreener feed the paper positions already use) and by the scout-sell events the scout
websocket already decodes.

Every variant uses the same entry (the scout paper fill at the smallest latency, default +2 s) and the live
pre-2x rules (max hold, trader sells, hard stop, minute-10 check) plus the v1.14.1 runner rule after 2x (a runner
— TP done / high ≥ 2x and price ≥ entry — exits only on its trail). Rule numbers are read from the live config
(read-only), so variant A always mirrors live exactly.

  A  (control, mirrors live)  50% at 2x, then the rest on one LIVE_TRAIL_PCT trail (0.30)
  B   50% at 2x, then one quarter on a 20% trail and one quarter on a 40% trail
  C   50% at 2x, then quarters on 30% and 50% trails
  C2  50% at 2x, then quarters on 25% and 50% trails
  E   50% at 2x, then the rest on a 20% trail
  F   50% at 2x, then the rest on a 40% trail

Cost model (same as the 2026-10-08 backtest): every exit fill is EXIT_VARIANTS_EXIT_HAIRCUT (4%) worse than the
mark; a trail / hard-stop line crossed by less than LIVE_GAP_PCT (10%) fills at the line, a gap further past the
line fills at the mark; EXIT_VARIANTS_OVERHEAD (1.5%) of the size is charged per trade. PnL is reported per
0.10 SOL. Closed variants → data/exit_variants.jsonl; open trackers persist in data/exit_variants_state.json
(restored after a restart and flagged restart_gap, which the summary leaves out by default).
Off switch: EXIT_VARIANTS_ENABLED=false (no trackers opened, no marks requested; the summary still reads the log).
"""
from __future__ import annotations

import json
import logging
import os
import statistics
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.paths import data_path

logger = logging.getLogger("coach.scout.exit_variants")

SIZE_SOL = 0.10
# variant → list of (trail width or None = live trail_pct, fraction of the post-TP remainder; last takes the rest)
VARIANTS: Dict[str, List[Tuple[Optional[float], float]]] = {
    "A": [(None, 1.0)],
    "B": [(0.20, 0.5), (0.40, 0.5)],
    "C": [(0.30, 0.5), (0.50, 0.5)],
    "C2": [(0.25, 0.5), (0.50, 0.5)],
    "E": [(0.20, 1.0)],
    "F": [(0.40, 1.0)],
    "T30": [(None, 1.0)],        # v1.21 (pre-2x behaviour differs, see PRE2X)
    "T30F": [(None, 1.0)],
    "T15": [(0.15, 1.0)],        # v2 (Oct 15) trail-width study shadows, PAPER ONLY
    "T25": [(0.25, 1.0)],
    "T35": [(0.35, 1.0)],
    "T50": [(0.50, 1.0)],
    "CHASE5": [(None, 1.0)],     # v1.32: control A, but only opened when chase at entry <= the paper cap (default 5%)
}
# v1.32 (Andrew approved Oct 14 8:06 PM CT) PAPER ONLY entry gates: variant → max chase % at entry. The cap is read
# from app.learn_overrides (bounded 3-15%, default 5) so the daily learner can tune it on paper only.
CHASE_GATED = {"CHASE5"}
# v1.21 (Sky 2026-10-08 11:43 PM CT) pre-2x overrides, PAPER ONLY: ignore the pre-2x scout sell, ride a 30% peak
# trail from entry until 2x (35% hard stop kept), optionally with the fast cut (-15% within 30 s of entry).
PRE2X: Dict[str, Dict[str, Any]] = {
    "T30": {"ignore_trader": True, "trail": 0.30, "fast_cut": False},
    "T30F": {"ignore_trader": True, "trail": 0.30, "fast_cut": True},
}
LABELS = {
    "A": "control (mirrors live): 50% at 2x, rest on the live trail (30%)",
    "B": "50% at 2x, quarter on 20% trail + quarter on 40% trail",
    "C": "50% at 2x, quarter on 30% trail + quarter on 50% trail",
    "C2": "50% at 2x, quarter on 25% trail + quarter on 50% trail",
    "E": "50% at 2x, rest on 20% trail",
    "F": "50% at 2x, rest on 40% trail",
    "T15": "50% at 2x, rest on 15% trail (v2 shadow)",
    "T25": "50% at 2x, rest on 25% trail (v2 shadow)",
    "T35": "50% at 2x, rest on 35% trail (v2 shadow)",
    "T50": "50% at 2x, rest on 50% trail (v2 shadow)",
    "T30": "ignore pre-2x scout sell; 30% peak trail from entry before 2x; then 50% at 2x + rest on 30% trail",
    "T30F": "T30 + fast cut (sell all at -15% within 30 s of entry, before 2x)",
    "CHASE5": "control A, only entered when chase at entry <= paper cap (default 5%, learner-tuned 3-15%)",
}
CT = timezone(timedelta(hours=-5))


# ---------------- v1.27 structure variants (Sky 2026-10-09 1:07 PM CT), PAPER ONLY ----------------
# Candles are built from the scout paper engine's own price marks (finest source available: one mark every
# SCOUT_MARK_S, default 2 s; curve price while on the bonding curve, DexScreener after migration). No extra RPC.
#   CANDLE_<tf>  trailing stop = previous COMPLETED <tf> candle's low − EXIT_VAR_CANDLE_BUFFER_PCT (3%), ratchets up
#                only; from entry on the whole position.
#   DIAG         rising support through the two most recent higher swing lows on EXIT_VAR_DIAG_TF (30 s) candles;
#                exit when a completed candle CLOSES below the line − EXIT_VAR_DIAG_BUFFER_PCT (2%). Armed only once
#                two higher lows exist; before that a 30% peak trail.
#   HALF2X_C30   (backtest 1:10 PM) pre-2x exactly as live (hard stop, scout-sell exit, minute-10, max hold); at 2x
#                sell half (live tp_frac), then the 30 s candle stop (−3%) on the rest.
#   HALF2X_C30_NOSCOUT  the same but the pre-2x scout sell is ignored.
#   CANDLE15_ARM30  full position; 15 s candle stop −EXIT_VAR_ARM_BUFFER_PCT (5%) armed once price reached
#                EXIT_VAR_ARM_MULT (1.3x) entry; before arming only the hard stop + minute-10 check.
# Every stop ratchets up only (after each completed candle → that candle's low − buffer). All keep the 35% hard stop
# (live hard_stop_pct), exit on a rug flag (on_rug: the live engine's creator-dump / liquidity-drain flags for the
# same mint) and the 24 h paper max-age cap. CANDLE_*/DIAG use no other rule.
def _tf_list() -> List[int]:
    out: List[int] = []
    for x in os.getenv("EXIT_VAR_CANDLE_TFS", "15,30,60").split(","):
        x = x.strip().lower()
        if not x:
            continue
        try:
            sec = int(float(x[:-1]) * 60) if x.endswith("m") else int(float(x.rstrip("s")))
        except ValueError:
            continue
        if sec >= 4 and sec not in out:
            out.append(sec)
    return out or [15, 30, 60]


def _tfname(sec: int) -> str:
    return f"{sec // 60}m" if sec % 60 == 0 else f"{sec}s"


def struct_cfg() -> Dict[str, Any]:
    return {"tfs": _tf_list(), "buf": _f("EXIT_VAR_CANDLE_BUFFER_PCT", 3) / 100,
            "diag_tf": max(4, int(_f("EXIT_VAR_DIAG_TF", 30))), "diag_buf": _f("EXIT_VAR_DIAG_BUFFER_PCT", 2) / 100,
            "diag_pre_trail": 0.30, "half_tf": 30,
            "arm_mult": _f("EXIT_VAR_ARM_MULT", 1.3), "arm_buf": _f("EXIT_VAR_ARM_BUFFER_PCT", 5) / 100, "arm_tf": 15}


STRUCT: Dict[str, Dict[str, Any]] = {}


def _register_struct() -> None:
    sc = struct_cfg()
    b = round(sc["buf"] * 100, 2)
    for sec in sc["tfs"]:
        n = f"CANDLE_{_tfname(sec)}"
        STRUCT[n] = {"kind": "candle", "tf": sec}
        LABELS[n] = f"stop at previous completed {_tfname(sec)} candle low −{b:g}% (trailing, from entry); 35% hard stop + rug"
    STRUCT["DIAG"] = {"kind": "diag", "tf": sc["diag_tf"]}
    LABELS["DIAG"] = (f"rising support through the last two higher swing lows on {_tfname(sc['diag_tf'])} candles; exit on a "
                      f"close below line −{round(sc['diag_buf'] * 100, 2):g}%; 30% trail until armed; 35% hard stop + rug")
    STRUCT["HALF2X_C30"] = {"kind": "candle", "tf": sc["half_tf"], "half2x": True, "pre": "live", "scout_exit": True}
    LABELS["HALF2X_C30"] = (f"pre-2x as live (scout sell, hard stop, min-10, max hold); half at 2x, then the 30s candle "
                            f"stop (−{b:g}%) on the rest")
    STRUCT["HALF2X_C30_NOSCOUT"] = {"kind": "candle", "tf": sc["half_tf"], "half2x": True, "pre": "live",
                                    "scout_exit": False}
    LABELS["HALF2X_C30_NOSCOUT"] = (f"HALF2X_C30 but the pre-2x scout sell is ignored (hard stop, min-10, max hold); "
                                    f"half at 2x, 30s candle stop (−{b:g}%) on the rest")
    STRUCT["CANDLE15_ARM30"] = {"kind": "candle", "tf": sc["arm_tf"], "buf": sc["arm_buf"], "arm_mult": sc["arm_mult"],
                                "pre": "min10"}
    LABELS["CANDLE15_ARM30"] = (f"full position on the 15s candle stop −{round(sc['arm_buf'] * 100, 2):g}%, armed once "
                                f"price reached {sc['arm_mult']:g}x; before that 35% hard stop + minute-10")
    STRUCT["T60_CANDLE15"] = {"kind": "candle", "tf": 15, "scout_exit_after_s": 60}
    LABELS["T60_CANDLE15"] = (f"v1.32: R4 sell ignored for the first 60 s (exit on it after 60 s); 15s candle stop "
                              f"(−{b:g}%) from entry; 35% hard stop + rug")
    for n in STRUCT:
        VARIANTS[n] = [(None, 1.0)]



class Candles:
    """Per-mint OHLC from marks, one series per timeframe (seconds). Completed candles kept (last 60)."""

    def __init__(self) -> None:
        self.cur: Dict[int, Dict[str, Any]] = {}
        self.done: Dict[int, List[Dict[str, Any]]] = {}

    def update(self, price: float, now_ms: int, tfs) -> None:
        for tf in tfs:
            t0 = (now_ms // (tf * 1000)) * tf * 1000
            c = self.cur.get(tf)
            if c and c["t0"] != t0:
                lst = self.done.setdefault(tf, [])
                lst.append(c)
                del lst[:-60]
                c = None
            if c is None:
                self.cur[tf] = {"t0": t0, "t1": t0 + tf * 1000, "o": price, "h": price, "l": price, "c": price}
            else:
                c["h"], c["l"], c["c"] = max(c["h"], price), min(c["l"], price), price

    def completed(self, tf: int) -> List[Dict[str, Any]]:
        return self.done.get(tf, [])


def swing_lows(cs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [cs[i] for i in range(1, len(cs) - 1) if cs[i]["l"] < cs[i - 1]["l"] and cs[i]["l"] <= cs[i + 1]["l"]]


def diag_line(cs: List[Dict[str, Any]]) -> Optional[Dict[str, float]]:
    """Line through the two most recent swing lows if the later one is higher (time = candle close)."""
    lows = swing_lows(cs)
    if len(lows) < 2 or lows[-1]["l"] <= lows[-2]["l"]:
        return None
    a, b = lows[-2], lows[-1]
    return {"t1": a["t1"], "l1": a["l"], "t2": b["t1"], "l2": b["l"]}


def line_at(d: Dict[str, float], t_ms: float) -> float:
    return d["l2"] + (d["l2"] - d["l1"]) * (t_ms - d["t2"]) / max(1.0, d["t2"] - d["t1"])


def _f(name: str, d: float) -> float:
    try:
        return float(os.getenv(name, str(d)).strip())
    except ValueError:
        return d


_register_struct()


def enabled() -> bool:
    return os.getenv("EXIT_VARIANTS_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}


def log_path():
    return data_path("EXIT_VARIANTS_LOG_PATH", "exit_variants.jsonl")


def state_path():
    return data_path("EXIT_VARIANTS_STATE_PATH", "exit_variants_state.json")


def params(realistic: bool = False) -> Dict[str, Any]:
    """Live exit numbers, read-only (variant A must mirror live), plus the paper cost model.

    realistic=True (v1.38): the live-calibrated cost model from app.paper_realism instead of the flat 4% haircut /
    1.5% overhead: exit fee = live fee bps, overhead = live priority + tip lamports per side, hard-stop fills at the
    line × live p50 ratio with gaps floored at the live p10 fill."""
    try:
        from app.live.config import config as live_config
        lc = live_config()
    except Exception:  # noqa: BLE001
        lc = {}
    return {
        "tp_mult": float(lc.get("tp_mult", 2.0)), "tp_frac": float(lc.get("tp_frac", 0.5)),
        "trail_pct": float(lc.get("trail_pct", 0.30)), "hard_stop_pct": float(lc.get("hard_stop_pct", 0.35)),
        "min10_s": float(lc.get("min10_s", 600)), "max_hold_s": float(lc.get("max_hold_s", 3600)),
        "runner_max_hold_s": float(lc.get("runner_max_hold_s", 0) or 0), "gap_pct": float(lc.get("gap_pct", 0.10)),
        "haircut": _f("EXIT_VARIANTS_EXIT_HAIRCUT", 0.04), "overhead": _f("EXIT_VARIANTS_OVERHEAD", 0.015),
        "max_age_s": _f("EXIT_VARIANTS_MAX_AGE_H", 24) * 3600,
        "fast_cut_pct": float(lc.get("fast_cut_pct", 0.15)), "fast_cut_window_s": float(lc.get("fast_cut_window_s", 30)),
        **(_real_costs() if realistic else {}),
    }


def _real_costs() -> Dict[str, Any]:
    try:
        from app import paper_realism as pr
        R = pr.params("scout_solo")
    except Exception:  # noqa: BLE001
        return {}
    return {"haircut": R["fee_bps"] / 1e4,
            "overhead": 2 * (R["priority_lamports"] + R["tip_lamports"]) / 1e9 / SIZE_SOL,
            "real_stop": (R["hard_stop_ratio"], R["hard_stop_ratio_p10"]), "realistic": True}


def _stop_fill(price: float, line: float, P: Dict[str, Any]) -> float:
    """Hard-stop fill. Legacy: line unless gapped > gap_pct. Realistic: line × live p50, gaps floored at live p10."""
    rs = P.get("real_stop")
    if rs:
        return min(line * rs[0], max(price, line * rs[1]))
    return _fill_line(price, line, P["gap_pct"])


@dataclass
class Tracker:
    signal_id: str
    arm: str
    mint: str
    variant: str
    lat_s: int
    entry_ms: int
    entry_price_sol: float
    scout_wallet: Optional[str] = None
    high: float = 0.0
    last_price: Optional[float] = None
    last_mark_ms: Optional[int] = None
    tp_done: bool = False
    min10_done: bool = False
    trader_sold: bool = False
    rem: float = 1.0                      # fraction of the original position still held
    proceeds: float = 0.0                 # Σ fraction × fill / entry
    tranches_done: List[int] = field(default_factory=list)
    exits: List[Dict[str, Any]] = field(default_factory=list)
    status: str = "open"
    flags: List[str] = field(default_factory=list)
    stop_line: Optional[float] = None     # v1.27 candle stop (ratchets up)
    last_candle_t0: int = 0               # v1.27 last completed candle already applied
    diag: Optional[Dict[str, float]] = None
    armed_ms: Optional[int] = None        # v1.27 when the candle stop became active (None = from entry / not yet)
    realistic: bool = False               # v1.38 live-calibrated twin (entry never below the trader, live costs)
    legacy_entry_price_sol: Optional[float] = None
    trader_price_sol: Optional[float] = None

    @property
    def key(self) -> str:
        return f"{self.signal_id}:{self.variant}" + (":real" if self.realistic else "")

    def pnl_sol(self, overhead: float) -> float:
        return round(SIZE_SOL * (self.proceeds - 1.0 - overhead), 6)


def _fill_line(price: float, line: float, gap: float) -> float:
    """Line crossed by < gap → fills at the line; a gap further past the line fills at the mark."""
    return price if price <= line * (1 - gap) else line


def evaluate_struct(t: Tracker, price: float, now_ms: int, P: Dict[str, Any], candles: Optional["Candles"]) -> bool:
    """v1.27 CANDLE_* / DIAG / HALF2X_C30 (see STRUCT). Pure apart from the tracker."""
    S = STRUCT[t.variant]
    sc = struct_cfg()
    e = t.entry_price_sol
    t.last_price, t.last_mark_ms = price, now_ms
    t.high = max(t.high or e, price)
    age = (now_ms - t.entry_ms) / 1000
    hc = P["haircut"]

    def sell(frac: Optional[float], px: float, reason: str) -> None:
        f = t.rem if frac is None else min(frac, t.rem)
        if f <= 0:
            return
        fill = px * (1 - hc)
        t.proceeds += f * fill / e
        t.rem = max(0.0, t.rem - f)
        if t.rem < 1e-9:
            t.rem = 0.0
        t.exits.append({"ts_ms": now_ms, "reason": reason, "frac": round(f, 4), "mark_mult": round(price / e, 4),
                        "fill_mult": round(fill / e, 4)})

    cs = [c for c in (candles.completed(S["tf"]) if candles else []) if c["t1"] > t.entry_ms]
    new = [c for c in cs if c["t0"] > t.last_candle_t0]
    if new:
        t.last_candle_t0 = new[-1]["t0"]
    rug = next((f for f in t.flags if f.startswith("rug:")), None)
    hard = e * (1 - P["hard_stop_pct"])
    if age >= P["max_age_s"]:
        sell(None, price, "variant_max_age")
        t.flags.append("max_age_mtm")
    elif rug:
        sell(None, price, "rug_" + rug[4:])
    elif price <= hard:
        sell(None, _stop_fill(price, hard, P), "hard_stop")
    else:
        pre_done = False
        if S.get("half2x") and not t.tp_done and price >= e * P["tp_mult"]:
            t.tp_done = True
            t.armed_ms = now_ms
            sell(P["tp_frac"], price, f"tp_{P['tp_mult']:g}x")
        if S.get("arm_mult") and t.armed_ms is None and t.high >= e * S["arm_mult"]:
            t.armed_ms = now_ms
        gated = bool(S.get("half2x") or S.get("arm_mult"))
        if gated and t.armed_ms is None:                 # pre-arm rules
            if S.get("pre") == "live" and age >= P["max_hold_s"]:
                sell(None, price, "max_hold")
            elif S.get("scout_exit") and t.trader_sold:
                sell(None, price, "trader_sold")
            elif not t.min10_done and age >= P["min10_s"]:
                t.min10_done = True
                if price < e:
                    sell(None, price, "below_entry_min10")
            pre_done = True
        if (not pre_done and S.get("scout_exit_after_s") is not None and t.trader_sold
                and age >= S["scout_exit_after_s"]):
            sell(None, price, "trader_sold_after_60s")
            pre_done = True
        if S["kind"] == "candle" and not pre_done:
            buf = S.get("buf", sc["buf"])
            for c in new:
                if t.armed_ms is None or c["t1"] > t.armed_ms:
                    t.stop_line = max(t.stop_line or 0.0, c["l"] * (1 - buf))
            if t.rem > 0 and t.stop_line and price <= t.stop_line:
                sell(None, _fill_line(price, t.stop_line, P["gap_pct"]), f"candle_{_tfname(S['tf'])}_stop")
        elif S["kind"] == "candle":
            pass
        else:                                            # DIAG
            d = diag_line(cs)
            if d:
                t.diag = d
            if t.diag:
                for c in new:
                    if c["t1"] > t.diag["t2"] and c["c"] < line_at(t.diag, c["t1"]) * (1 - sc["diag_buf"]):
                        sell(None, price, "diag_break")
                        break
            elif price <= t.high * (1 - sc["diag_pre_trail"]):
                line = t.high * (1 - sc["diag_pre_trail"])
                sell(None, _fill_line(price, line, P["gap_pct"]), "diag_pre_trail_30")
    if t.rem <= 0:
        t.status = "closed"
        return True
    return False


def evaluate(t: Tracker, price: float, now_ms: int, P: Dict[str, Any], candles: Optional["Candles"] = None) -> bool:
    """Apply one mark to one tracker (pure apart from the tracker). Returns True when it closed."""
    if t.status != "open" or not price or price <= 0 or not t.entry_price_sol:
        return False
    if t.variant in STRUCT:
        return evaluate_struct(t, price, now_ms, P, candles)
    e = t.entry_price_sol
    t.last_price, t.last_mark_ms = price, now_ms
    t.high = max(t.high or e, price)
    age = (now_ms - t.entry_ms) / 1000
    hc = P["haircut"]

    def sell(frac: Optional[float], px: float, reason: str) -> None:
        f = t.rem if frac is None else min(frac, t.rem)
        if f <= 0:
            return
        fill = px * (1 - hc)
        t.proceeds += f * fill / e
        t.rem = max(0.0, t.rem - f)
        if t.rem < 1e-9:
            t.rem = 0.0
        t.exits.append({"ts_ms": now_ms, "reason": reason, "frac": round(f, 4), "mark_mult": round(price / e, 4),
                        "fill_mult": round(fill / e, 4)})

    def trails() -> None:
        tranches = VARIANTS[t.variant]
        base = 1.0 - P["tp_frac"]
        for i, (w, part) in enumerate(tranches):
            if i in t.tranches_done or t.rem <= 0:
                continue
            width = P["trail_pct"] if w is None else w
            line = t.high * (1 - width)
            if price <= line:
                last = all(j in t.tranches_done for j in range(len(tranches)) if j != i)
                sell(None if last else base * part, _fill_line(price, line, P["gap_pct"]), f"trail_{int(round(width * 100))}")
                t.tranches_done.append(i)

    def tp() -> bool:
        if not t.tp_done and price >= e * P["tp_mult"]:
            t.tp_done = True
            sell(P["tp_frac"], price, f"tp_{P['tp_mult']:g}x")
            return True
        return False

    if age >= P["max_age_s"]:
        sell(None, price, "variant_max_age")          # paper-only cap so a dead runner is not marked forever
        t.flags.append("max_age_mtm")
    else:
        armed = t.tp_done or t.high >= e * P["tp_mult"]
        if armed and price >= e:                        # v1.14.1 runner: only TP + trail (opt. runner max hold)
            if P["runner_max_hold_s"] and age >= P["runner_max_hold_s"]:
                sell(None, price, "runner_max_hold")
            elif not tp():
                trails()
        elif age >= P["max_hold_s"]:
            sell(None, price, "max_hold")
        elif t.trader_sold and not (PRE2X.get(t.variant) or {}).get("ignore_trader"):
            sell(None, price, "trader_sold")
        elif price <= e * (1 - P["hard_stop_pct"]):
            sell(None, _stop_fill(price, e * (1 - P["hard_stop_pct"]), P), "hard_stop")
        elif ((PRE2X.get(t.variant) or {}).get("fast_cut") and not armed and age <= P.get("fast_cut_window_s", 30)
              and price <= e * (1 - P.get("fast_cut_pct", 0.15))):
            sell(None, price, "fast_cut")
        elif PRE2X.get(t.variant) and not armed and price <= t.high * (1 - PRE2X[t.variant]["trail"]):
            line = t.high * (1 - PRE2X[t.variant]["trail"])
            sell(None, _fill_line(price, line, P["gap_pct"]), f"pre2x_trail_{int(round(PRE2X[t.variant]['trail'] * 100))}")
        else:
            m10 = False
            if not t.min10_done and age >= P["min10_s"]:
                t.min10_done = True
                if price < e:
                    sell(None, price, "below_entry_min10")
                    m10 = True
            if not m10 and not tp() and t.tp_done:
                trails()
    if t.rem <= 0:
        t.status = "closed"
        return True
    return False


# ---------------- v1.32 absurd paper mark guard (PAPER ONLY) ----------------
# Rejects a mark that jumps > PAPER_MARK_MAX_STEP (20x) from the previous accepted mark of the same mint, or sits
# > PAPER_MARK_MAX_MULT (200x) above the entry. Oct 9-14 paper_v3 had 3 Gucci closes at 1,655-7,046x (+1,229 SOL
# fake). Rejected marks are logged to data/paper_anomalies.jsonl and simply not applied (next good mark is used).
def mark_anomaly(prev: Optional[float], entries: List[float], price: float) -> Optional[str]:
    step = _f("PAPER_MARK_MAX_STEP", 20)
    mult = _f("PAPER_MARK_MAX_MULT", 200)
    if prev and prev > 0 and price > prev * step:
        return f"jump {price / prev:.1f}x vs previous mark (> {step:g}x)"
    e = min([x for x in entries if x and x > 0], default=None)
    if e and price > e * mult:
        return f"{price / e:.0f}x entry (> {mult:g}x)"
    return None


_anom_seen: Dict[str, float] = {}


def log_anomaly(source: str, mint: str, price: float, why: str, **extra: Any) -> None:
    now = time.time()
    k = f"{source}:{mint}"
    if now - _anom_seen.get(k, 0) < 60:          # at most one row per source+mint per minute
        return
    _anom_seen[k] = now
    try:
        p = data_path("PAPER_ANOMALY_LOG_PATH", "paper_anomalies.jsonl")
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as fh:
            fh.write(json.dumps({"ts_ms": int(now * 1000), "kind": "mark_rejected", "source": source, "mint": mint,
                                 "price_sol": price, "why": why, **extra}, default=str) + "\n")
    except Exception:  # noqa: BLE001
        pass


class ExitVariants:
    def __init__(self) -> None:
        self.trackers: Dict[str, Tracker] = {}
        self.stats: Dict[str, int] = {"opened": 0, "closed": 0, "restored": 0, "errors": 0}
        self.loaded = False
        self._dirty = False
        self._saved_at = 0.0
        self._first_open_ms: Optional[int] = None
        self.candles: Dict[str, Candles] = {}
        self._last_mark: Dict[str, float] = {}

    def _log_extra(self, row: Dict[str, Any]) -> None:
        try:
            p = log_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a") as fh:
                fh.write(json.dumps({"ts_ms": int(time.time() * 1000), **row}, default=str) + "\n")
        except Exception as exc:  # noqa: BLE001
            self.stats["errors"] += 1
            logger.warning("exit variants log failed: %s", exc)

    # ---- inputs (called by the scout paper engine) ----
    def open_signal(self, *, arm: str, mint: str, signal_id: str, lat_s: int, entry_ms: int, entry_price_sol: float,
                    scout_wallet: Optional[str], chase_pct: Optional[float] = None,
                    ref_price_sol: Optional[float] = None) -> int:
        if not enabled() or not entry_price_sol or entry_price_sol <= 0:
            return 0
        real_px = None
        try:                                    # v1.38 realistic twin: the +2 s curve fill, never below the trader
            from app import paper_realism as pr
            if pr.enabled():
                ok_ref = ref_price_sol if (ref_price_sol and 0.2 <= ref_price_sol / entry_price_sol <= 5) else None
                real_px = max(entry_price_sol, ok_ref or 0.0)
        except Exception:  # noqa: BLE001
            real_px = None
        n = 0
        cap = None
        try:
            from app import learn_overrides
            cap = float(learn_overrides.get()["scout_chase_cap_pct"])
        except Exception:  # noqa: BLE001
            cap = 5.0
        for v in VARIANTS:
            if v in CHASE_GATED:
                passed = chase_pct is not None and chase_pct <= cap
                self._log_extra({"kind": "variant_gate", "paper": True, "signal_id": signal_id, "arm": arm,
                                 "mint": mint, "variant": v, "chase_pct": chase_pct, "cap_pct": cap, "passed": passed,
                                 "entry_price_sol": entry_price_sol, "entry_ms": entry_ms})
                if not passed:
                    continue
            t = Tracker(signal_id=signal_id, arm=arm, mint=mint, variant=v, lat_s=lat_s, entry_ms=entry_ms,
                        entry_price_sol=entry_price_sol, scout_wallet=scout_wallet, high=entry_price_sol)
            if t.key in self.trackers:
                continue
            self.trackers[t.key] = t
            n += 1
            if real_px:
                rt = Tracker(signal_id=signal_id, arm=arm, mint=mint, variant=v, lat_s=lat_s, entry_ms=entry_ms,
                             entry_price_sol=real_px, scout_wallet=scout_wallet, high=real_px, realistic=True,
                             legacy_entry_price_sol=entry_price_sol, trader_price_sol=ref_price_sol)
                self.trackers.setdefault(rt.key, rt)
        if n:
            self.stats["opened"] += 1
            self._save(force=True)
        return n

    def on_scout_sell(self, wallet: str, mint: str) -> int:
        n = 0
        for t in self.trackers.values():
            if t.status == "open" and t.mint == mint and t.scout_wallet and t.scout_wallet == wallet and not t.trader_sold:
                t.trader_sold = True
                n += 1
        if n:
            self._dirty = True
        return n

    def on_rug(self, mint: str, reason: str) -> int:
        """v1.27: rug flag for a mint (from the live engine's creator-dump / liquidity-drain checks). Only the structure
        variants act on it (next mark); the original A–T30F rules are left exactly as they were."""
        n = 0
        for t in self.trackers.values():
            if t.status == "open" and t.mint == mint and t.variant in STRUCT and not any(f.startswith("rug:") for f in t.flags):
                t.flags.append("rug:" + str(reason)[:40])
                n += 0 if getattr(t, "realistic", False) else 1   # v1.38: twins flagged too, count = legacy (unchanged)
                self._dirty = True
        if n:
            self._dirty = True
        return n

    def open_mints(self) -> set:
        if not enabled():
            return set()
        return {t.mint for t in self.trackers.values() if t.status == "open"}

    def on_mark(self, mint: str, price: Optional[float], now_ms: int) -> int:
        if not enabled() or not price:
            return 0
        bad = mark_anomaly(self._last_mark.get(mint),
                           [t.entry_price_sol for t in self.trackers.values() if t.status == "open" and t.mint == mint],
                           price)
        if bad:
            self.stats["marks_rejected"] = self.stats.get("marks_rejected", 0) + 1
            log_anomaly("exit_variants", mint, price, bad, prev=self._last_mark.get(mint))
            return 0
        self._last_mark[mint] = price
        P = params()
        PR = params(realistic=True)
        closed = 0
        open_here = [t for t in self.trackers.values() if t.status == "open" and t.mint == mint]
        tfs = sorted({STRUCT[t.variant]["tf"] for t in open_here if t.variant in STRUCT})
        cd = None
        if tfs:
            cd = self.candles.setdefault(mint, Candles())
            cd.update(price, now_ms, tfs)
        for t in open_here:
            try:
                PP = PR if t.realistic else P
                if evaluate(t, price, now_ms, PP, cd):
                    self._write_row(t, PP)
                    closed += 1
                self._dirty = True
            except Exception as exc:  # noqa: BLE001 — a paper add-on must never break the scout engine
                self.stats["errors"] += 1
                logger.warning("exit variant %s failed: %s", t.key, exc)
        for k in [k for k, t in self.trackers.items() if t.status != "open"]:
            self.trackers.pop(k, None)
        live_mints = {t.mint for t in self.trackers.values()}
        for m in [m for m in self.candles if m not in live_mints]:
            self.candles.pop(m, None)
        for m in [m for m in self._last_mark if m not in live_mints]:
            self._last_mark.pop(m, None)
        self._save(force=bool(closed))
        return closed

    # ---- persistence ----
    def _write_row(self, t: Tracker, P: Dict[str, Any]) -> None:
        self.stats["closed"] += 1
        row = {"ts_ms": int(time.time() * 1000), "kind": "variant_closed_real" if t.realistic else "variant_closed",
               "paper": True, "realistic": t.realistic,
               "signal_id": t.signal_id, "arm": t.arm, "mint": t.mint, "variant": t.variant,
               "plan": LABELS.get(t.variant), "lat_s": t.lat_s, "entry_ms": t.entry_ms,
               "entry_price_sol": t.entry_price_sol, "exit_ms": t.last_mark_ms,
               "exit_reason": t.exits[-1]["reason"] if t.exits else None, "exits": t.exits,
               "reached_2x": t.tp_done, "peak_mult": round(t.high / t.entry_price_sol, 4) if t.entry_price_sol else None,
               "pnl_sol": t.pnl_sol(P["overhead"]), "size_sol": SIZE_SOL,
               "restart_gap": "restart_gap" in t.flags, "flags": t.flags,
               "cost_model": {"exit_haircut": P["haircut"], "overhead": P["overhead"], "gap_pct": P["gap_pct"],
                              **({"stop_fill_p50_p10": list(P["real_stop"])} if P.get("real_stop") else {})}}
        if t.realistic:
            row.update(legacy_entry_price_sol=t.legacy_entry_price_sol, trader_price_sol=t.trader_price_sol,
                       entry_vs_legacy_pct=round((t.entry_price_sol / t.legacy_entry_price_sol - 1) * 100, 2)
                       if t.legacy_entry_price_sol else None)
        try:
            p = log_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a") as fh:
                fh.write(json.dumps(row, default=str) + "\n")
        except Exception as exc:  # noqa: BLE001
            self.stats["errors"] += 1
            logger.warning("exit variants log failed: %s", exc)

    def _save(self, force: bool = False) -> None:
        now = time.time()
        if not force and (not self._dirty or now - self._saved_at < 10):
            return
        try:
            p = state_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(p.suffix + ".tmp")
            tmp.write_text(json.dumps({"saved_ms": int(now * 1000),
                                       "open": [asdict(t) for t in self.trackers.values() if t.status == "open"]}))
            os.replace(tmp, p)
            self._dirty, self._saved_at = False, now
        except Exception as exc:  # noqa: BLE001
            self.stats["errors"] += 1
            logger.warning("exit variants state save failed: %s", exc)

    def load(self) -> None:
        if self.loaded:
            return
        self.loaded = True
        try:
            p = state_path()
            if not p.exists():
                return
            d = json.loads(p.read_text())
            for raw in d.get("open") or []:
                try:
                    t = Tracker(**{k: v for k, v in raw.items() if k in Tracker.__dataclass_fields__})
                except Exception:  # noqa: BLE001
                    continue
                if "restart_gap" not in t.flags:
                    t.flags.append("restart_gap")       # marks were missed while the app was down
                self.trackers[t.key] = t
                self.stats["restored"] += 1
        except Exception as exc:  # noqa: BLE001
            self.stats["errors"] += 1
            logger.warning("exit variants load failed: %s", exc)

    # ---- summary ----
    def rows(self, kind: str = "variant_closed") -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        try:
            p = log_path()
            if not p.exists():
                return out
            with p.open() as fh:
                for line in fh:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if r.get("kind") == kind:
                        out.append(r)
        except Exception as exc:  # noqa: BLE001
            logger.warning("exit variants read failed: %s", exc)
        return out

    def summary(self, include_restart_gap: bool = False) -> Dict[str, Any]:
        rows = self.rows()
        gap_rows = [r for r in rows if r.get("restart_gap")]
        use = rows if include_restart_gap else [r for r in rows if not r.get("restart_gap")]
        live_like = {a.strip() for a in os.getenv("EXIT_VARIANTS_LIVE_LIKE_ARMS", "scout_solo").split(",") if a.strip()}
        P = params()
        PR = params(realistic=True)
        real = [r for r in self.rows("variant_closed_real") if include_restart_gap or not r.get("restart_gap")]
        return {
            "ok": True, "paper_only": True, "enabled": enabled(),
            "headline_basis": "realistic",
            "realistic": {"note": "v1.38 live-calibrated twins: entry = +2 s curve fill never below the trader's fill; "
                                  "exit fee = live fee bps, overhead = live priority + tip, hard stop at line × live "
                                  "p50 (gaps floored at live p10). Legacy keys below are the old 4% haircut model.",
                          "cost_model": {k: PR.get(k) for k in ("haircut", "overhead", "real_stop")},
                          "all_arms": _split(real),
                          "live_like": {"arms": sorted(live_like), **_split([r for r in real if r.get("arm") in live_like])},
                          "by_arm": {a: _split([r for r in real if r.get("arm") == a])
                                     for a in sorted({r.get("arm") for r in real})}},
            "variants": {v: LABELS[v] for v in VARIANTS},
            "rules": {"pre_2x": "live: max hold, trader sells (scout arms), hard stop, minute-10 check "
                                "(T30/T30F: no trader-sell exit, 30% peak trail from entry; T30F + fast cut)",
                      "after_2x": "v1.14.1 runner rule: only TP + trails while price >= entry",
                      "structure_v127": {"variants": {k: dict(v) for k, v in STRUCT.items()}, **struct_cfg(),
                                         "candle_source": "scout paper marks every SCOUT_MARK_S (2 s); curve price, "
                                                          "DexScreener after migration",
                                         "note": "CANDLE_*/DIAG: 35% hard stop + rug + paper max-age only. HALF2X_C30: "
                                                 "pre-2x live rules incl. scout sell; HALF2X_C30_NOSCOUT ignores the "
                                                 "scout sell; CANDLE15_ARM30: hard stop + minute-10 until 1.3x"},
                      "live_numbers": {k: P[k] for k in ("tp_mult", "tp_frac", "trail_pct", "hard_stop_pct",
                                                          "min10_s", "max_hold_s", "gap_pct")},
                      "cost_model": {"exit_haircut": P["haircut"], "overhead": P["overhead"],
                                     "pnl_per_sol": SIZE_SOL, "max_age_h": P["max_age_s"] / 3600}},
            "test_window": {"started": os.getenv("EXIT_VARIANTS_STARTED",
                                                 "2026-10-08 (v1.18 deploy; exact time in the Banana handoff note)"),
                            "end_target": os.getenv("EXIT_VARIANTS_END_TARGET", "~3:00 PM CT Oct 15, 2026")},
            "open_signals": len({t.signal_id for t in self.trackers.values() if t.status == "open" and not t.realistic}),
            "restart_gap_rows_excluded": 0 if include_restart_gap else len(gap_rows),
            "stats": dict(self.stats),
            "all_arms": _split(use),
            "live_like": {"arms": sorted(live_like), **_split([r for r in use if r.get("arm") in live_like])},
            "by_arm": {a: _split([r for r in use if r.get("arm") == a]) for a in sorted({r.get("arm") for r in use})},
        }


def _ct(ms: Optional[int]) -> Optional[str]:
    return datetime.fromtimestamp(ms / 1000, CT).strftime("%Y-%m-%d %H:%M CT") if ms else None


def _split(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_sig: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for r in rows:
        by_sig.setdefault(str(r.get("signal_id")), {})[r.get("variant")] = r
    entries = [r.get("entry_ms") for r in rows if r.get("entry_ms")]
    out: Dict[str, Any] = {"signals": len(by_sig),
                           "reached_2x": sum(1 for v in by_sig.values() if (v.get("A") or {}).get("reached_2x")),
                           "date_range": {"first_entry": _ct(min(entries)) if entries else None,
                                          "last_entry": _ct(max(entries)) if entries else None},
                           "variants": {}}
    for v in VARIANTS:
        pn = [float(r["pnl_sol"]) for r in rows if r.get("variant") == v and r.get("pnl_sol") is not None]
        pairs = [(d[v]["pnl_sol"], d["A"]["pnl_sol"]) for d in by_sig.values() if v in d and "A" in d]
        r2 = [float(d[v]["pnl_sol"]) for d in by_sig.values() if v in d and (d.get("A") or {}).get("reached_2x")]
        out["variants"][v] = {
            "n": len(pn), "total_sol": round(sum(pn), 6) if pn else 0.0,
            "mean_sol": round(statistics.mean(pn), 6) if pn else None,
            "median_sol": round(statistics.median(pn), 6) if pn else None,
            "win_rate": round(sum(1 for x in pn if x > 0) / len(pn), 3) if pn else None,
            "best_sol": round(max(pn), 6) if pn else None, "worst_sol": round(min(pn), 6) if pn else None,
            "beats_A": None if v == "A" else sum(1 for a, b in pairs if a > b + 1e-9),
            "loses_to_A": None if v == "A" else sum(1 for a, b in pairs if a < b - 1e-9),
            "delta_vs_A_sol": None if v == "A" else round(sum(a - b for a, b in pairs), 6),
            "reached_2x_total_sol": round(sum(r2), 6) if r2 else 0.0,
            "reached_2x_median_sol": round(statistics.median(r2), 6) if r2 else None,
        }
    return out


variants = ExitVariants()
