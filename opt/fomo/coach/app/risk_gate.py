"""Mesh rule gate + final gate combiner (recommend-only, dry-run, fail-safe).

Order of authority:
  1. Mesh hard block (clear rug flags)            → action=skip, blocked=True  (always wins, even over Jev)
  2. Mesh unavailable / pending / unknown mint     → escalate_human (fail-safe; Jev may only downgrade to skip)
  3. Jev ok                                        → Jev's action (show | skip | escalate_human)
  4. Jev unavailable/error (e.g. 402 no credits)   → mesh rule decision (show = recommend-only card)

Nothing here can trade: recommend_only=True, auto_trade=False, dry_run=True on every decision.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

BLOCK_SEVERITIES = {"high", "critical", "danger"}
BLOCK_VERDICTS = {"high", "critical", "danger"}
SELL_TAX_BLOCK = 0.5  # ≥50% sell tax is honeypot-like
THIN_LIQUIDITY_USD = 5_000.0


def _base(**kw: Any) -> Dict[str, Any]:
    d = {"recommend_only": True, "auto_trade": False, "dry_run": True}
    d.update(kw)
    return d


def mesh_rule_decision(mesh: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Rule gate from QP Data Mesh enrichment. Returns {mesh_status, action, blocked, reasons, warnings}."""
    if not mesh:
        return _base(mesh_status="missing", action="escalate_human", blocked=False,
                     reasons=["no mesh enrichment attached"], warnings=[])
    status = mesh.get("status")
    if status != "ok":
        return _base(mesh_status=status or "unknown", action="escalate_human", blocked=False,
                     reasons=[f"mesh {status}: {mesh.get('note') or mesh.get('error') or ''}".strip()], warnings=[])

    risk = mesh.get("risk") or {}
    if risk.get("status") != "ok":
        return _base(mesh_status="risk_unavailable", action="escalate_human", blocked=False,
                     reasons=[f"token_risk unavailable: {risk.get('error') or 'no data'}"], warnings=[])
    if not risk.get("sources_answered"):
        return _base(mesh_status="risk_no_coverage", action="escalate_human", blocked=False,
                     reasons=["token_risk: no risk source answered"], warnings=[])

    block: List[str] = []
    warn: List[str] = []
    if risk.get("mint_authority_live") is True:
        block.append("mint authority live")
    if risk.get("freeze_authority_live") is True:
        block.append("freeze authority live")
    if risk.get("honeypot") is True:
        block.append("honeypot flag")
    if risk.get("rugcheck_rugged") is True:
        block.append("RugCheck: rugged")
    st = risk.get("sell_tax")
    if isinstance(st, (int, float)) and st >= SELL_TAX_BLOCK:
        block.append(f"sell tax {st:.0%} (honeypot-like)")
    verdict = (risk.get("verdict") or "").lower()
    for f in risk.get("flags") or []:
        sev = (f.get("severity") or "").lower()
        if sev in BLOCK_SEVERITIES:
            block.append(f"{f.get('flag')} [{sev}]")
        elif f.get("flag"):
            warn.append(f"{f.get('flag')} [{sev or '?'}]")
    if verdict in BLOCK_VERDICTS and not any("[" in b for b in block):
        block.append(f"mesh risk verdict {verdict}")

    unknown = [n for n, k in (("mint", "mint_authority_live"), ("freeze", "freeze_authority_live")) if risk.get(k) is None]
    if unknown:
        warn.append(f"{'/'.join(unknown)} authority status unknown (no source reported it)")

    liq = mesh.get("liquidity_usd")
    if isinstance(liq, (int, float)) and liq < THIN_LIQUIDITY_USD:
        warn.append(f"thin liquidity ~${liq:,.0f}")
    if risk.get("coverage") not in (None, "full"):
        warn.append(f"risk coverage {risk.get('coverage')}")
    if mesh.get("stale"):
        warn.append("some mesh data stale")

    if block:
        return _base(mesh_status="ok", action="skip", blocked=True, verdict=verdict or None,
                     reasons=block, warnings=warn)
    return _base(mesh_status="ok", action="show", blocked=False, verdict=verdict or None,
                 reasons=[f"no hard rug flags (verdict {verdict or 'n/a'}, sources {','.join(risk.get('sources_answered') or [])})"],
                 warnings=warn)


def combine(jev: Optional[Dict[str, Any]], mesh_dec: Dict[str, Any],
            fast_gate: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Final recommend-only gate decision for a signal card.

    v1.11: when `fast_gate.lane == "curve"` (pre-migration pump.fun bonding curve read on-chain),
    the curve fast gate decides and Jev is ADVISORY (shown, never blocks; skip → size/exit hint).
    Mesh hard block still wins for every token. Migrated / non-pump tokens keep the v1.10 rules.
    """
    jev = jev or {}
    jev_status = jev.get("jev_status") or "missing"
    jev_action = jev.get("action")
    jev_ok = jev_status == "ok" and jev_action in {"show", "skip", "escalate_human"}
    curve_lane = bool(fast_gate and fast_gate.get("lane") == "curve")
    extra_warn: List[str] = []
    jev_role = "advisory" if curve_lane else "gate"

    if mesh_dec.get("blocked"):
        action, by = "skip", "mesh_block"
    elif curve_lane:
        if fast_gate.get("fast_ok"):
            action, by = "show", "curve_fast_gate"
        else:
            action, by = "escalate_human", "curve_fast_gate_no_price"
        extra_warn.extend(fast_gate.get("warnings") or [])
        if jev_ok and jev_action != "show":
            extra_warn.append(f"JEV advisory: {jev_action} → consider smaller clip / tighter exits")
        if mesh_dec.get("mesh_status") != "ok":
            extra_warn.append(f"mesh {mesh_dec.get('mesh_status')} — not blocking curve lane")
    elif mesh_dec.get("mesh_status") != "ok":
        if jev_ok and jev_action == "skip":
            action, by = "skip", "jev"
        else:
            action, by = "escalate_human", "fail_safe_mesh_unavailable"
    elif jev_ok:
        action, by = jev_action, "jev"
    else:
        action, by = mesh_dec.get("action") or "escalate_human", "mesh_rules"

    return _base(
        action=action,
        decided_by=by,
        blocked=bool(mesh_dec.get("blocked")),
        safe_to_surface=action == "show",
        mesh_status=mesh_dec.get("mesh_status"),
        mesh_action=mesh_dec.get("action"),
        jev_status=jev_status,
        jev_action=jev_action,
        reasons=list(mesh_dec.get("reasons") or []) + ([fast_gate.get("reason")] if curve_lane and not mesh_dec.get("blocked") else []),
        warnings=list(mesh_dec.get("warnings") or []) + extra_warn,
        lane=(fast_gate or {}).get("lane") or "standard",
        jev_role=jev_role,
        size_hint=("reduce" if curve_lane and jev_ok and jev_action == "skip" else None),
        note=(
            "Recommend-only. Human decides any wallet buy; Jupiter stays quote/dry-run "
            "(JUPITER_EXEC_ENABLED=false)."
        ),
    )
