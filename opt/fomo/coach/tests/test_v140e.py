"""v1.40e: direct curve sells with Jupiter fallback, sell fee plan + confirm window, prefetched Jupiter sell route."""
import asyncio

from tests.test_v120_direct_pump import (MINT, FakeRpc, _curve, _drain, _engine, _patch_state, env, keys)  # noqa: F401
from app.live import jupiter
from app.live import pumpdirect as pd


def test_sell_confirm_window_env(monkeypatch):
    monkeypatch.setenv("LIVE_DIRECT_SELL_CONFIRM_TIMEOUT_S", "2.5")
    assert pd.cfg()["sell_confirm_timeout_s"] == 2.5 and pd.cfg()["confirm_timeout_s"] == 30


def test_jupiter_prefetched_skips_order(monkeypatch):
    async def order(**k):
        raise AssertionError("order must not be fetched")

    async def sign_tx(*a, **k):
        raise RuntimeError("stop here")
    monkeypatch.setattr(jupiter, "order", order)
    monkeypatch.setattr(jupiter, "sign_tx", sign_tx)
    pf = {"ok": True, "transaction": "x", "in_amount": 5, "out_amount": 7, "router": "metis", "_fetched_at": 1.0}
    r = asyncio.run(jupiter.swap(input_mint=MINT, output_mint="So11111111111111111111111111111111111111112", amount=5,
                                 slippage_bps=100, max_priority_lamports=1, prefetched=pf))
    assert r["stage"] == "sign"


def _direct_live_env(monkeypatch):
    monkeypatch.setenv("LIVE_DIRECT_PUMP_MODE", "shadow")
    monkeypatch.setenv("LIVE_DIRECT_PUMP_LIVE_ARMS", "scout_solo")
    monkeypatch.setenv("LIVE_DIRECT_PUMP_LIVE_SIDES", "buy,sell")


def test_direct_sell_not_confirmed_falls_back_to_jupiter(env, monkeypatch):
    _direct_live_env(monkeypatch)
    monkeypatch.setenv("LIVE_DIRECT_SELL_AMBIG_WAIT_S", "0")
    _patch_state(monkeypatch, _curve(), user_tokens=120_000)
    monkeypatch.setattr(pd, "_rpc", FakeRpc())
    jc = []
    e, bal = _engine(monkeypatch, jc, tokens_after=120_000)
    calls = []
    orig = pd.swap

    async def dswap(**kw):
        if kw["side"] == "buy":
            return {"ok": True, "stage": "execute", "signature": "DBUY", "total_in": 200_000_000, "total_out": 120_000,
                    "order": {"router": "pump_direct"}, "timing": {}}
        calls.append(kw)
        return {"ok": False, "ambiguous": True, "stage": "execute", "signature": "DSELL",
                "order": {"router": "pump_direct", "out_amount": 1}, "error": "pump direct: not confirmed in time"}
    monkeypatch.setattr(pd, "swap", dswap)

    async def go():
        e.on_signal("scout_solo", MINT, trader_wallets=["W"], ref_price_sol=1.5e-6, meta={})
        await _drain(e)
        (p,) = e.open_positions()
        await e._sell(p, 1.0, "hard_stop")
        await _drain(e)
        return p
    p = asyncio.run(go())
    sells = [k for k in jc if k.get("output_mint") != MINT]
    assert len(calls) == 1 and len(sells) == 1                     # one direct try, then Jupiter, same step
    assert p.status == "closed" and e.stats["direct_sell_fallback_jup"] == 1


def test_direct_sell_late_land_is_reconciled_not_double_sold(env, monkeypatch):
    _direct_live_env(monkeypatch)
    monkeypatch.setenv("LIVE_DIRECT_SELL_AMBIG_WAIT_S", "0")
    monkeypatch.setenv("LIVE_SELL_SKIP_BALANCE", "false")
    _patch_state(monkeypatch, _curve(), user_tokens=120_000)
    monkeypatch.setattr(pd, "_rpc", FakeRpc())
    jc = []
    e, bal = _engine(monkeypatch, jc, tokens_after=120_000)

    async def dswap(**kw):
        if kw["side"] == "buy":
            return {"ok": True, "stage": "execute", "signature": "DBUY", "total_in": 200_000_000, "total_out": 120_000,
                    "order": {"router": "pump_direct"}, "timing": {}}
        bal["v"] = 0                                               # it landed after the window
        return {"ok": False, "ambiguous": True, "stage": "execute", "signature": "DSELL",
                "order": {"router": "pump_direct", "out_amount": 1}, "error": "pump direct: not confirmed in time"}
    monkeypatch.setattr(pd, "swap", dswap)

    async def go():
        e.on_signal("scout_solo", MINT, trader_wallets=["W"], ref_price_sol=1.5e-6, meta={})
        await _drain(e)
        (p,) = e.open_positions()
        await e._sell(p, 1.0, "hard_stop")
        await _drain(e)
        return p
    p = asyncio.run(go())
    assert not [k for k in jc if k.get("output_mint") != MINT]   # no Jupiter sell on top
    assert p.status == "closed"
