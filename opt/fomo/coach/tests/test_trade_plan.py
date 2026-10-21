"""Recommend-only Fluffy plan — offline."""
from app.scoring import Signal
from app.trade_plan import build_trade_plan, is_fluffy_signal


def test_fluffy_plan_0_40_and_ladder():
    sig = Signal(
        token_address="REDACTED",
        symbol="X",
        chain="solana",
        score=80,
        unique_traders=3,
        buy_count=3,
        trader_handles=["Fluffy Alpha"],
        reasons=[],
        source="fluffy/telegram",
        mesh={"status": "ok", "price_usd": 0.00001},
        gate={"action": "show", "blocked": False},
    )
    assert is_fluffy_signal(sig)
    p = build_trade_plan(sig)
    assert p["entry"]["sol_suggest"] == 0.4
    assert p["entry"]["sol_suggest_lamports"] == "400000000"
    assert p["live_exec"] is False
    assert p["actionable"] is True
    ladder = p["exits"]["ladder"]
    assert ladder[0]["multiple"] == 2 and ladder[0]["sell_frac_of_entry"] == 0.5
    assert ladder[1]["multiple"] == 3 and ladder[2]["multiple"] == 5


def test_blocked_not_actionable():
    sig = Signal(
        token_address="x",
        symbol="Y",
        chain="solana",
        score=1,
        unique_traders=1,
        buy_count=1,
        trader_handles=[],
        reasons=[],
        source="fluffy/telegram",
        gate={"action": "skip", "blocked": True},
    )
    p = build_trade_plan(sig)
    assert p["blocked"] and not p["actionable"]
