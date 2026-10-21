"""Unit tests for QP Data Mesh rule gate + combiner — offline, no network."""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import mesh as mesh_mod  # noqa: E402
from app.mesh import _risk_summary, enrich_token, is_solana_mint  # noqa: E402
from app.risk_gate import combine, mesh_rule_decision  # noqa: E402


def _risk_env(flags=None, verdict="low", gmgn=None, rugcheck=None, sources=("rugcheck", "jupiter")):
    return {
        "ok": True,
        "body": {
            "source": "rugcheck+jupiter",
            "data": {
                "verdict": verdict,
                "coverage": "full",
                "sources_answered": list(sources),
                "flags": flags or [],
                "details": {"gmgn": gmgn or {}, "rugcheck": rugcheck or {"score_normalised": 3}},
            },
        },
    }


def _mesh(risk):
    return {"status": "ok", "liquidity_usd": 50_000.0, "risk": risk, "stale": False}


def test_mint_validation():
    assert is_solana_mint("DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263")
    assert not is_solana_mint("DEMOA")
    assert not is_solana_mint("DemoToken1111111111111111111111111111111O")  # 'O' not base58


def test_clean_token_shows_when_jev_unavailable():
    r = _risk_summary(_risk_env(gmgn={"renounced_mint": True, "renounced_freeze_account": True}))
    assert r["mint_authority_live"] is False and r["freeze_authority_live"] is False
    dec = mesh_rule_decision(_mesh(r))
    assert dec["action"] == "show" and not dec["blocked"]
    g = combine({"jev_status": "error", "action": "escalate_human"}, dec)
    assert g["action"] == "show" and g["decided_by"] == "mesh_rules"
    assert g["auto_trade"] is False and g["dry_run"] is True


def test_mint_authority_live_blocks_even_if_jev_says_show():
    r = _risk_summary(_risk_env(gmgn={"renounced_mint": False, "renounced_freeze_account": True}))
    dec = mesh_rule_decision(_mesh(r))
    assert dec["blocked"] and "mint authority live" in dec["reasons"]
    g = combine({"jev_status": "ok", "action": "show"}, dec)
    assert g["action"] == "skip" and g["decided_by"] == "mesh_block"


def test_rugcheck_text_flags_block():
    r = _risk_summary(_risk_env(flags=[{"flag": "rugcheck: Freeze Authority still enabled", "source": "rugcheck", "severity": "danger"}], verdict="high"))
    assert r["freeze_authority_live"] is True
    dec = mesh_rule_decision(_mesh(r))
    assert dec["blocked"]


def test_honeypot_and_sell_tax_block():
    r = _risk_summary(_risk_env(gmgn={"is_honeypot": 1}))
    assert mesh_rule_decision(_mesh(r))["blocked"]
    r = _risk_summary(_risk_env(gmgn={"sell_tax": 0.9}))
    assert mesh_rule_decision(_mesh(r))["blocked"]


def test_medium_flags_warn_only():
    r = _risk_summary(_risk_env(flags=[{"flag": "top5 holders ~57%", "source": "rugcheck", "severity": "medium"}], verdict="medium"))
    dec = mesh_rule_decision(_mesh(r))
    assert dec["action"] == "show" and dec["warnings"]


def test_jev_ok_overrides_mesh_pass():
    r = _risk_summary(_risk_env())
    dec = mesh_rule_decision(_mesh(r))
    assert combine({"jev_status": "ok", "action": "escalate_human"}, dec)["action"] == "escalate_human"
    assert combine({"jev_status": "ok", "action": "skip"}, dec)["decided_by"] == "jev"


def test_mesh_down_fails_safe_to_escalate():
    dec = mesh_rule_decision({"status": "unavailable", "error": "ConnectError"})
    g = combine({"jev_status": "error", "action": "escalate_human"}, dec)
    assert g["action"] == "escalate_human" and g["decided_by"] == "fail_safe_mesh_unavailable"
    # Jev 'show' can NOT upgrade a mesh-down token to show
    assert combine({"jev_status": "ok", "action": "show"}, dec)["action"] == "escalate_human"
    assert mesh_rule_decision(None)["action"] == "escalate_human"
    assert mesh_rule_decision({"status": "pending"})["action"] == "escalate_human"


def test_invalid_mint_makes_no_network_call():
    out = asyncio.run(enrich_token("DEMOA", "solana"))
    assert out["status"] == "invalid_mint"


def test_unreachable_mesh_returns_unavailable():
    from dataclasses import replace

    orig = mesh_mod.settings
    mesh_mod.settings = replace(orig, mesh_base_url="http://127.0.0.1:9/api/mesh", mesh_timeout_s=1.0)
    try:
        mesh_mod.clear_mesh_cache()
        out = asyncio.run(enrich_token("DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263", "solana"))
        assert out["status"] == "unavailable", out
        g = combine({"jev_status": "error"}, mesh_rule_decision(out))
        assert g["action"] == "escalate_human"
    finally:
        mesh_mod.settings = orig
        mesh_mod.clear_mesh_cache()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("ok", fn.__name__)
    print(f"{len(fns)} passed")
