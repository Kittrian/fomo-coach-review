"""v1.40: chase before signing (no signer call on refusal) + scout hot-path RPC/age cache."""
import asyncio

import pytest


def test_pumpdirect_chase_refusal_never_signs(monkeypatch):
    from app.live import pumpdirect
    signed = []

    async def prep(side, mint, amount, cl, slippage_pct=0):
        return {"ok": True, "expected_out": 1000, "fetch_ms": 1, "build_ms": 1}

    async def sign(*a, **k):
        signed.append(1)
        raise AssertionError("signer must not be called")

    monkeypatch.setattr(pumpdirect, "prepare", prep)
    monkeypatch.setattr(pumpdirect, "sign_compiled", sign)
    seen = []

    def pre(q):
        seen.append(q)
        return False, "chase filter: test"
    r = asyncio.run(pumpdirect.swap(side="buy", mint="M", amount=10_000_000, client=object(), pre_sign_quote=pre))
    assert r["stage"] == "gate" and r.get("pre_sign") and not r.get("fallback")
    assert seen == [{"in_amount": 10_000_000, "out_amount": 1000, "path": "pump_direct"}]
    assert not signed


def test_jupiter_chase_refusal_never_signs(monkeypatch):
    from app.live import jupiter

    async def order(**k):
        return {"ok": True, "transaction": "x", "in_amount": 5, "out_amount": 7, "router": "metis"}

    async def sign_tx(*a, **k):
        raise AssertionError("signer must not be called")
    monkeypatch.setattr(jupiter, "order", order)
    monkeypatch.setattr(jupiter, "sign_tx", sign_tx)
    r = asyncio.run(jupiter.swap(input_mint="So11111111111111111111111111111111111111112", output_mint="M", amount=5,
                                 slippage_bps=100, max_priority_lamports=1, pre_sign_quote=lambda q: (False, "chase")))
    assert r["stage"] == "gate" and r["error"] == "chase" and r.get("pre_sign")


def test_age_cache_and_hot_fallback(monkeypatch):
    from app.scout import source as srcmod
    s = srcmod.source
    calls = []

    async def fake_age(mint, max_pages=3):
        calls.append(mint)
        return {"ok": True, "created_s": 1000.0, "tx_count": 5}
    monkeypatch.setattr(s, "_token_age", fake_age)
    s.age_cache.clear()
    a = asyncio.run(s.token_age("MINT1"))
    b = asyncio.run(s.token_age("MINT1"))
    assert a["created_s"] == b["created_s"] == 1000.0 and calls == ["MINT1"]

    async def boom(*a, **k):
        raise RuntimeError("rpc http 429")

    async def normal(method, params, *, bg=False, timeout=6.0):
        return {"fallback": method}
    monkeypatch.setattr(s, "hot_url", "https://example.invalid/?api-key=x")
    monkeypatch.setattr(srcmod.rpc, "call", boom)
    monkeypatch.setattr(s, "call", normal)
    s.hot_cool_until = 0
    r = asyncio.run(s.call_hot("getAccountInfo", []))
    assert r == {"fallback": "getAccountInfo"} and s.hot_cool_until > 0
    assert all("api-key" not in e for e in s.last_errors)
