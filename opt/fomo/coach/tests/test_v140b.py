import asyncio


def test_gtfa_then_cache(monkeypatch):
    from app.scout import source as srcmod
    s = srcmod.source
    s.age_cache.clear()
    monkeypatch.setattr(s, "hot_url", "https://example.invalid/?api-key=x")
    s.hot_cool_until = 0
    n = []

    async def fake_call(method, params, client=None, url=None, timeout=4.0):
        n.append(method)
        assert method == "getTransactionsForAddress" and params[1]["sortOrder"] == "asc"
        return {"data": [{"signature": "S1", "blockTime": 1234}, {"signature": "S2", "blockTime": 1240}]}
    monkeypatch.setattr(srcmod.rpc, "call", fake_call)
    a = asyncio.run(s.token_age("7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"))
    b = asyncio.run(s.token_age("7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"))
    assert a["created_s"] == 1234 and a["first_sigs"] == ["S1", "S2"] and b["created_s"] == 1234 and n == [n[0]]


def test_gtfa_error_falls_back(monkeypatch):
    from app.scout import source as srcmod
    s = srcmod.source
    s.age_cache.clear()
    monkeypatch.setattr(s, "hot_url", "https://example.invalid/?api-key=x")

    async def boom(*a, **k):
        raise RuntimeError("rpc error method not found")

    async def pager(pda, max_pages=3):
        return {"ok": True, "created_s": 99, "tx_count": 3}
    monkeypatch.setattr(srcmod.rpc, "call", boom)
    monkeypatch.setattr(s, "_token_age_pager", pager)
    r = asyncio.run(s.token_age("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"))
    assert r["created_s"] == 99 and all("api-key" not in e for e in s.last_errors)


def test_pda_cached():
    from app.pump.solana_pda import bonding_curve_pda, _fpa_cached
    a = bonding_curve_pda("So11111111111111111111111111111111111111112")
    b = bonding_curve_pda("So11111111111111111111111111111111111111112")
    assert a == b and _fpa_cached.cache_info().hits >= 1
