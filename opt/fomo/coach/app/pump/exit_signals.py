"""Early-dump exit-signal cards for pump.fun positions (recommend-only; never trades).

Triggers: creator sells · sells>buys while below high · curve SOL draining · max-hold timer
(price-independent) · migration detected · trailing stop (ARMS ONLY IN PROFIT) · hard stop.
"""
from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def config() -> Dict[str, float]:
    return {
        "flow_window_s": _f("EXIT_FLOW_WINDOW_S", 30),
        "flow_sell_buy_ratio": _f("EXIT_FLOW_SELL_BUY_RATIO", 1.0),
        "flow_below_high_pct": _f("EXIT_FLOW_BELOW_HIGH_PCT", 0.15),
        "flow_min_sell_sol": _f("EXIT_FLOW_MIN_SELL_SOL", 0.5),
        "drain_pct": _f("EXIT_DRAIN_PCT", 0.25),
        "drain_min_peak_sol": _f("EXIT_DRAIN_MIN_PEAK_SOL", 1.0),
        "max_hold_s": _f("EXIT_MAX_HOLD_S", 600),
        "trail_pct": _f("PLAN_TRAIL_PCT", 0.25),
        "trail_arm_pct": _f("PLAN_TRAIL_ARM_PCT", 0.10),
        "hard_stop_pct": _f("PLAN_HARD_STOP_PCT", 0.35),
        "creator_sell_min_sol": _f("EXIT_CREATOR_SELL_MIN_SOL", 0.0),
    }


def _card(kind: str, fired: bool, action: str, detail: str, severity: str = "warn", **extra: Any) -> Dict[str, Any]:
    return {"kind": kind, "fired": fired, "action": action if fired else "hold", "detail": detail,
            "severity": severity if fired else "info", "recommend_only": True, **extra}


def evaluate(
    *,
    trades: List[Dict[str, Any]],
    creator: Optional[str],
    price: Optional[float],
    high: Optional[float],
    entry_price: Optional[float],
    entry_ms: Optional[int],
    real_sol: Optional[float],
    peak_real_sol: Optional[float],
    migrated: bool,
    tp1_hit: bool = False,
    now_ms: Optional[int] = None,
    cfg: Optional[Dict[str, float]] = None,
) -> List[Dict[str, Any]]:
    """Return one card per trigger. `real_sol`/`peak_real_sol` in SOL; prices in SOL/token."""
    c = cfg or config()
    now = now_ms or int(time.time() * 1000)
    since = entry_ms or 0
    cards: List[Dict[str, Any]] = []

    # 1. creator sells (any sell by the coin creator after entry)
    dev_sells = [t for t in trades if creator and t.get("user") == creator and not t.get("is_buy")
                 and t.get("recv_ms", now) >= since and t.get("sol_amount", 0) / 1e9 >= c["creator_sell_min_sol"]]
    dev_sol = sum(t.get("sol_amount", 0) for t in dev_sells) / 1e9
    cards.append(_card("creator_sell", bool(dev_sells), "exit_all",
                       f"creator sold {len(dev_sells)}× ({dev_sol:.3f} SOL)" if dev_sells else
                       ("watching creator " + (creator[:6] + "…" if creator else "unknown")),
                       severity="critical", creator=creator))

    # 2. flow flip: sells > ratio×buys over window AND price below high by X
    w = c["flow_window_s"] * 1000
    recent = [t for t in trades if now - t.get("recv_ms", now) <= w]
    buy_sol = sum(t["sol_amount"] for t in recent if t.get("is_buy")) / 1e9
    sell_sol = sum(t["sol_amount"] for t in recent if not t.get("is_buy")) / 1e9
    below = (high and price) and price < high * (1 - c["flow_below_high_pct"])
    flip = bool(below and sell_sol >= c["flow_min_sell_sol"] and sell_sol > buy_sol * c["flow_sell_buy_ratio"])
    runner = bool(tp1_hit and entry_price and price and price >= entry_price)   # v1.14.1: runner → trail only
    flip = flip and not runner
    cards.append(_card("flow_flip", flip, "exit_remainder",
                       f"{c['flow_window_s']:.0f}s flow: sells {sell_sol:.2f} vs buys {buy_sol:.2f} SOL; "
                       f"{'below' if below else 'near'} high", buy_sol=buy_sol, sell_sol=sell_sol))

    # 3. curve SOL draining from peak
    drain = bool(peak_real_sol and real_sol is not None and peak_real_sol >= c["drain_min_peak_sol"]
                 and real_sol <= peak_real_sol * (1 - c["drain_pct"])) and not runner
    cards.append(_card("curve_draining", drain, "exit_remainder",
                       f"curve SOL {real_sol if real_sol is not None else '?'} vs peak {peak_real_sol or '?'}"
                       if real_sol is None or peak_real_sol is None else
                       f"curve SOL {real_sol:.2f} vs peak {peak_real_sol:.2f} (−{(1 - real_sol / peak_real_sol) * 100 if peak_real_sol else 0:.0f}%)"))

    # 4. max hold timer — price independent (fires even if price feed is dead)
    held = (now - entry_ms) / 1000 if entry_ms else None
    mh = bool(held is not None and held >= c["max_hold_s"] and not tp1_hit)
    cards.append(_card("max_hold", mh, "exit_all",
                       f"held {held:.0f}s / max {c['max_hold_s']:.0f}s" if held is not None else "no entry",
                       held_s=held))

    # 5. migration detected
    cards.append(_card("migration", bool(migrated), "switch_venue_review",
                       "curve complete → PumpSwap; curve sells stop working — re-quote via Jupiter"
                       if migrated else "pre-migration (bonding curve)"))

    # 6. trailing stop — ARMS ONLY IN PROFIT; never fires below entry (hard stop owns losses)
    armed = bool(entry_price and high and high >= entry_price * (1 + c["trail_arm_pct"]))
    trail_hit = bool(armed and price and price <= high * (1 - c["trail_pct"]) and (price > entry_price or tp1_hit))
    cards.append(_card("trailing_stop", trail_hit, "exit_remainder",
                       (f"armed (high {high / entry_price:.2f}× entry); trail {c['trail_pct']:.0%}"
                        if armed else f"not armed — arms at +{c['trail_arm_pct']:.0%} profit")
                       if entry_price and high else "no entry", armed=armed))

    # 7. hard stop
    hs = bool(entry_price and price and price <= entry_price * (1 - c["hard_stop_pct"]))
    cards.append(_card("hard_stop", hs, "exit_all",
                       f"price {price / entry_price:.2f}× entry (stop at {1 - c['hard_stop_pct']:.2f}×)"
                       if entry_price and price else "no price", severity="critical"))
    return cards


def fired(cards: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [c for c in cards if c.get("fired")]


# ---------------------------------------------------------------------------------------------
# v1.12 — `fomo_top_trader` profile exits ("buy low, sell high"; protect gains, cap losses).
# Stop RATCHETS (never lowers): hard stop → breakeven(+buffer) once up +BE_ARM → trailing once up
# +TRAIL_ARM. NOT a no-loss guarantee: fills happen at the next observed mark, so gaps/rugs/slippage
# can land below the stop.
# ---------------------------------------------------------------------------------------------
def fomo_config() -> Dict[str, Any]:
    ladder_raw = os.getenv("FOMO_TP_LADDER", "1.5:0.30,2.0:0.30,3.0:0.25")
    ladder = []
    for i, part in enumerate(x for x in ladder_raw.split(",") if x.strip()):
        try:
            m, f = part.split(":")
            ladder.append((float(m), float(f), f"TP{i + 1}"))
        except ValueError:
            continue
    return {
        "hard_stop_pct": _f("FOMO_HARD_STOP_PCT", 0.20),
        "be_arm_pct": _f("FOMO_BE_ARM_PCT", 0.15),
        "be_buffer_pct": _f("FOMO_BE_BUFFER_PCT", 0.03),
        "trail_arm_pct": _f("FOMO_TRAIL_ARM_PCT", 0.25),
        "trail_pct": _f("FOMO_TRAIL_PCT", 0.15),
        "max_hold_s": _f("FOMO_MAX_HOLD_S", 4 * 3600),
        "stale_mark_s": _f("FOMO_STALE_MARK_S", 120),
        "follow_full_frac": _f("FOMO_FOLLOW_FULL_EXIT_FRAC", 0.5),
        "ladder": ladder,
    }


def fomo_stop(entry: float, high: Optional[float], prev_stop: Optional[float],
              cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    c = cfg or fomo_config()
    hi = max(high or entry, entry)
    cands = [(entry * (1 - c["hard_stop_pct"]), "hard_stop")]
    if hi >= entry * (1 + c["be_arm_pct"]):
        cands.append((entry * (1 + c["be_buffer_pct"]), "breakeven_stop"))
    if hi >= entry * (1 + c["trail_arm_pct"]):
        cands.append((hi * (1 - c["trail_pct"]), "trailing_stop"))
    px, kind = max(cands, key=lambda t: t[0])
    if prev_stop and prev_stop > px:      # ratchet: never loosen
        return {"stop_price": prev_stop, "ratcheted": False}
    return {"stop_price": px, "stop_kind": kind, "ratcheted": True}


def evaluate_fomo(*, entry: float, price: Optional[float], high: Optional[float], stop_price: Optional[float],
                  stop_kind: Optional[str], entry_ms: int, trader_sells: List[Dict[str, Any]],
                  mark_age_s: Optional[float] = None, now_ms: Optional[int] = None,
                  cfg: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    c = cfg or fomo_config()
    now = now_ms or int(time.time() * 1000)
    cards: List[Dict[str, Any]] = []
    hit = bool(price and stop_price and price <= stop_price)
    kind = stop_kind or "hard_stop"
    cards.append(_card(kind, hit, "exit_all",
                       f"mark {price:.3g} ≤ stop {stop_price:.3g} ({kind}); fills at next mark — gaps can be worse"
                       if hit and price and stop_price else f"stop {stop_price:.3g} ({kind})" if stop_price else "no stop",
                       "high", stop_price=stop_price))
    full = [s for s in trader_sells if (s.get("sold_fraction") or 0) >= c["follow_full_frac"]]
    part = [s for s in trader_sells if s not in full]
    if full:
        who = ", ".join(sorted({s.get("handle") or "?" for s in full}))
        cards.append(_card("follow_trader", True, "exit_all", f"source trader(s) sold ≥{c['follow_full_frac']:.0%}: {who}", "high"))
    elif part:
        frac = min(0.9, max(s.get("sold_fraction") or 0 for s in part))
        cards.append(_card("follow_trader", True, "exit_fraction",
                           f"source trader trimmed {frac:.0%} — mirror proportionally", "warn", fraction=frac))
    else:
        cards.append(_card("follow_trader", False, "exit_all", "no source-trader sell yet"))
    held = (now - entry_ms) / 1000
    trailing = (stop_kind == "trailing_stop") and bool(price and price >= entry)   # v1.14.1: runner → trail only
    if trailing:
        cards = [x if x["kind"] != "follow_trader" else _card("follow_trader", False, "exit_all",
                                                               "runner: trail armed — trader sells ignored")
                 for x in cards]
    cards.append(_card("max_hold", held >= c["max_hold_s"] and not trailing, "exit_all",
                       f"held {held / 60:.0f}m / {c['max_hold_s'] / 60:.0f}m" + (" (runner: disabled)" if trailing else "")))
    stale = mark_age_s is not None and mark_age_s > c["stale_mark_s"]
    cards.append(_card("stale_mark", stale, "review", f"last mark {mark_age_s:.0f}s old — stop can't fire on time"
                       if mark_age_s is not None else "no mark yet", "warn"))
    return cards
