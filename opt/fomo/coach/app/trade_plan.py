"""Recommend-only entry/exit plan for fast Fluffy-style pump→dump signals.

Never executes trades. Mesh/JEV hard-block still wins in the gate combiner.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.config import settings

FLUFFY_SOURCES = {"fluffy/telegram", "telegram"}


def _f(name: str, default: float) -> float:
    try:
        return float(getattr(settings, name))
    except Exception:
        return default


def is_fluffy_signal(sig: Any) -> bool:
    src = (getattr(sig, "source", None) or "").strip().lower()
    if src in FLUFFY_SOURCES or "fluff" in src:
        return True
    meta = getattr(sig, "meta", None) or {}
    if isinstance(meta, dict):
        g = str(meta.get("source_group") or meta.get("telegram_channel") or "").lower()
        if "fluff" in g:
            return True
        for s in meta.get("sources") or []:
            if isinstance(s, str) and ("fluff" in s.lower() or s.lower() in FLUFFY_SOURCES):
                return True
    # any buy handle/source on signal meta
    for h in getattr(sig, "trader_handles", None) or []:
        if isinstance(h, str) and "fluff" in h.lower():
            return True
    return False


def build_trade_plan(
    sig: Any,
    *,
    entry_price_usd: Optional[float] = None,
) -> Dict[str, Any]:
    """Structured recommend-only plan attached to signal cards."""
    entry_min = _f("plan_entry_sol_min", 0.40)
    entry_max = _f("plan_entry_sol_max", 0.40)
    entry_default = _f("plan_entry_sol_default", 0.40)
    # clamp default into band
    entry_default = max(entry_min, min(entry_max, entry_default))

    tp1_m = _f("plan_tp1_mult", 2.0)
    tp1_f = _f("plan_tp1_frac", 0.50)
    tp2_m = _f("plan_tp2_mult", 3.0)
    tp2_f = _f("plan_tp2_frac", 0.25)
    tp3_m = _f("plan_tp3_mult", 5.0)
    tp3_f = _f("plan_tp3_frac", 0.125)
    trail = _f("plan_trail_pct", 0.25)
    hard = _f("plan_hard_stop_pct", 0.35)

    mesh = getattr(sig, "mesh", None) or {}
    if entry_price_usd is None:
        entry_price_usd = mesh.get("price_usd") if isinstance(mesh, dict) else None

    gate = getattr(sig, "gate", None) or {}
    blocked = bool(gate.get("blocked")) or gate.get("action") == "skip"
    actionable = (not blocked) and gate.get("action") == "show"

    def level(mult: float, frac: float, label: str) -> Dict[str, Any]:
        px = (float(entry_price_usd) * mult) if isinstance(entry_price_usd, (int, float)) else None
        return {
            "label": label,
            "multiple": mult,
            "sell_frac_of_entry": frac,
            "price_usd": px,
            "note": f"Sell {frac:.0%} of original size at {mult:g}× entry",
        }

    ladder: List[Dict[str, Any]] = [
        level(tp1_m, tp1_f, "TP1"),
        level(tp2_m, tp2_f, "TP2"),
        level(tp3_m, tp3_f, "TP3"),
    ]
    remainder = max(0.0, 1.0 - (tp1_f + tp2_f + tp3_f))

    profile = "fluffy_pump_dump" if is_fluffy_signal(sig) else "generic_meme"
    return {
        "profile": profile,
        "recommend_only": True,
        "auto_trade": False,
        "live_exec": False,
        "jupiter_exec_enabled": False,
        "actionable": actionable,
        "blocked": blocked,
        "gate_action": gate.get("action"),
        "entry": {
            "rule": "CA announce only (not dip)",
            "sol_min": entry_min,
            "sol_max": entry_max,
            "sol_suggest": entry_default,
            "sol_suggest_lamports": str(int(entry_default * 1_000_000_000)),
            "wallet_note": "Size for ~1 SOL bag — 0.40 SOL clip until live enabled",
            "entry_price_usd": entry_price_usd,
        },
        "exits": {
            "ladder": ladder,
            "trailing_stop_pct_off_high": trail,
            "hard_stop_pct_from_entry": hard,
            "remainder_frac_after_ladder": round(remainder, 4),
            "summary": (
                f"Bank {tp1_f:.0%} @ {tp1_m:g}×, {tp2_f:.0%} @ {tp2_m:g}×, "
                f"{tp3_f:.0%} @ {tp3_m:g}×; trail rest (~{trail:.0%} off high); "
                f"hard stop −{hard:.0%} from entry"
            ),
        },
        "fail_closed": (
            "Mesh/JEV hard-block always wins — no entry. "
            "JUPITER_EXEC_ENABLED stays false (quote/unsigned only)."
        ),
        "note": (
            "Recommend-only playbook for fast pump→dump. Human confirms every step. "
            "See PLAN.md."
        ),
    }


def attach_trade_plan(sig: Any) -> Any:
    """Attach .plan when Fluffy (or always attach a muted generic — Fluffy gets full profile)."""
    try:
        if is_fluffy_signal(sig) or True:
            # Always attach so UI has defaults; profile distinguishes Fluffy
            sig.plan = build_trade_plan(sig)
            if getattr(sig, "meta", None) is None:
                sig.meta = {}
            if isinstance(sig.meta, dict):
                sig.meta["plan_profile"] = sig.plan.get("profile")
    except Exception:
        sig.plan = {
            "profile": "error",
            "recommend_only": True,
            "auto_trade": False,
            "live_exec": False,
            "note": "trade plan soft-fail",
        }
    return sig
