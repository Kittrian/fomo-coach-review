"""v1.16.1: live buys of non-SOL-quote coins in the scout arms. Offline: price/route sources, RPC, Jupiter and the
mesh are all faked. No network, no keypair, no live state, no swaps."""
from __future__ import annotations

import asyncio
import base64
import struct
import time

import pytest

from app.live import jupiter, nonsol
from app.live.engine import LiveEngine, LivePos
from app.pump.curve import CURVE_DISCRIMINATOR, decode_curve
from app.pump.solana_pda import PUMP_PROGRAM_ID, pubkey_bytes

WSOL = nonsol.WSOL
PUMPQ = "pumpCmXqMfrsAkQ5r49WcJnRayYRqmXz6ae8H7H9Dfn"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
WLD = "WLDTE73PPdfFFjhpBz8Yuhvx5N45gvgEgyMXqwa3rFn"
MINT = "4wqefEvwWVHcuV8gJpXCm6SWRUcLpxn6gsr2FdbEMcbr"
CREATOR = "862TYSvRYoiHAK3F3WwTRYAfuGiQaGdxedN9AGvRGWo2"
PUMP_USD, SOL_USD = 0.00625, 116.5
SPQ = PUMP_USD / SOL_USD                                   # SOL per PUMP


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def curve_dict(vqr=803_924_938_774, vtr=680_110_528_032_885, complete=False, quote=PUMPQ):
    """The Doc's real 8 SOL buy (4wqefEvw, PUMP-quoted): post-trade reserves from the decoded v2 TradeEvent."""
    return {"virtual_token_reserves": vtr, "virtual_quote_reserves": vqr, "real_token_reserves": 400_000_000 * 10**6,
            "real_quote_reserves": 294_365_000_044, "token_total_supply": 10**15, "complete": complete,
            "quote_mint": quote, "quote_is_sol": quote == WSOL, "creator": CREATOR}


def curve_bytes(c):
    b = CURVE_DISCRIMINATOR + struct.pack("<QQQQQ", c["virtual_token_reserves"], c["virtual_quote_reserves"],
                                          c["real_token_reserves"], c["real_quote_reserves"], c["token_total_supply"])
    b += bytes([1 if c["complete"] else 0]) + pubkey_bytes(CREATOR) + b"\x00\x00" + pubkey_bytes(c["quote_mint"])
    return b + struct.pack("<Q", 0) + b"\x00\x00"


def prices(quote_usd=PUMP_USD, sol_usd=SOL_USD, decimals=6, route_dev=0.0, mint=PUMPQ):
    async def fetch(mints):
        return {mint: {"usdPrice": quote_usd, "decimals": decimals}, WSOL: {"usdPrice": sol_usd}}

    async def route(qm, lam):
        q = nonsol.KNOWN_QUOTES[qm]
        spq = (quote_usd / sol_usd) * (1 + route_dev)
        return int((lam / 1e9) / spq * 10 ** q["decimals"])
    return fetch, route


@pytest.fixture
def px(monkeypatch):
    from app.fomo_onchain import market
    monkeypatch.setitem(market._cache, market.WSOL_MINT, {})
    f, r = prices()
    monkeypatch.setattr(nonsol, "_fetch_prices", f)
    monkeypatch.setattr(nonsol, "_route_rate", r)
    return monkeypatch


@pytest.fixture
def env(monkeypatch, tmp_path):
    for k, v in {"JUPITER_EXEC_ENABLED": "true", "LIVE_AUTOTRADE_ENABLED": "true",
                 "LIVE_ARMS": "scout_solo,scout_confirmed,fluffy_pump_dump,zyrox",
                 "LIVE_SIZE_SOL_SCOUT_SOLO": "0.10", "LIVE_SIZE_SOL_SCOUT_CONFIRMED": "0.10",
                 "LIVE_SIZE_SOL_FLUFFY_PUMP_DUMP": "0.40", "LIVE_SIZE_SOL_ZYROX": "0.40",
                 "LIVE_MAX_SOL_PER_TRADE": "0.40", "LIVE_MIN_SOL_RESERVE": "0.50",
                 "LIVE_STATE_PATH": str(tmp_path / "state.json"), "LIVE_LEDGER_PATH": str(tmp_path / "ledger.jsonl"),
                 "LIVE_KILL_FILE": str(tmp_path / "KILL"), "LIVE_SELL_RETRY_S": "0", "LIVE_SELL_STEP_BACKOFF_S": "0"
                 }.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(jupiter, "pubkey", lambda: "FakePubkey111")

    async def ok(self, mint):
        return True, "mesh ok"
    monkeypatch.setattr(LiveEngine, "_mesh_check", ok)
    return tmp_path


async def _drain(e):
    for _ in range(6):
        if not e._tasks:
            return
        await asyncio.gather(*list(e._tasks), return_exceptions=True)


NS_META = {"quote_mint": PUMPQ, "scout_sol": 2.0, "nonsol_gates_passed": True}


# ======================= sizing =======================
@pytest.mark.parametrize("scout,size", [(5.0, 0.40), (2.0, 0.20), (0.8, 0.08), (8.010891, 0.40), (0.5, 0.05),
                                        (1.570219449, 0.157), (0.7, 0.07)])
def test_size_one_tenth_capped(scout, size):
    got, note = nonsol.size_for(scout, max_sol_per_trade=0.40)
    assert got == pytest.approx(size) and "non-SOL size" in note


def test_size_refusals_and_trade_cap(monkeypatch):
    assert nonsol.size_for(None, max_sol_per_trade=0.4)[0] is None
    assert nonsol.size_for(0.0, max_sol_per_trade=0.4)[0] is None
    assert nonsol.size_for(0.49, max_sol_per_trade=0.4)[0] is None           # scout rule: ≥ 0.5 SOL
    assert nonsol.size_for(5.0, max_sol_per_trade=0.25)[0] == pytest.approx(0.25)   # LIVE_MAX_SOL_PER_TRADE wins
    monkeypatch.setenv("LIVE_SCOUT_NONSOL_FRACTION", "0.05")
    assert nonsol.size_for(2.0, max_sol_per_trade=0.4)[0] == pytest.approx(0.10)
    monkeypatch.setenv("LIVE_SCOUT_NONSOL_FRACTION", "3")
    assert nonsol.size_for(2.0, max_sol_per_trade=0.4)[0] is None


# ======================= quote-asset price (two sources) =======================
def test_quote_price_verified(px):
    info, why = run(nonsol.refresh(PUMPQ))
    assert why == "ok" and info["sol_per_quote"] == pytest.approx(SPQ) and abs(info["route_dev_pct"]) < 0.01


@pytest.mark.parametrize("kw,frag", [({"route_dev": 0.10}, "disagree"), ({"quote_usd": 0}, "unavailable"),
                                     ({"decimals": 9}, "decimals mismatch"), ({"sol_usd": 0}, "unavailable")])
def test_quote_price_refusals(px, kw, frag):
    f, r = prices(**kw)
    px.setattr(nonsol, "_fetch_prices", f)
    px.setattr(nonsol, "_route_rate", r)
    info, why = run(nonsol.refresh(PUMPQ))
    assert info is None and frag in why


def test_stable_off_peg_and_no_route_and_down(px):
    f, r = prices(quote_usd=0.95, mint=USDC)
    px.setattr(nonsol, "_fetch_prices", f)
    px.setattr(nonsol, "_route_rate", r)
    assert "off peg" in run(nonsol.refresh(USDC))[1]

    async def none(*a):
        return None
    f, _ = prices()
    px.setattr(nonsol, "_fetch_prices", f)
    px.setattr(nonsol, "_route_rate", none)
    assert "no Jupiter SOL→PUMP route" in run(nonsol.refresh(PUMPQ))[1]

    async def boom(*a):
        raise RuntimeError("x")
    px.setattr(nonsol, "_fetch_prices", boom)
    nonsol._cache.clear()
    info, why = run(nonsol.refresh(PUMPQ))
    assert info is None and why.startswith("quote price unavailable")
    assert "unknown" in run(nonsol.refresh("So1idUnknown11111111111111111111111111111"))[1]


def test_sol_usd_cross_check(px):
    from app.fomo_onchain import market
    px.setitem(market._cache, market.WSOL_MINT, {"price_usd": 150.0, "_t": time.time()})
    assert "SOL/USD sources disagree" in run(nonsol.refresh(PUMPQ))[1]


def test_stale_price_not_used_for_marks(px):
    run(nonsol.refresh(PUMPQ))
    assert nonsol.mark_price_sol(curve_dict(), PUMPQ)
    nonsol._cache[PUMPQ]["ts"] -= 120
    assert nonsol.mark_price_sol(curve_dict(), PUMPQ) is None


# ======================= gate math =======================
def test_curve_math_matches_the_docs_real_buy(px):
    info, _ = run(nonsol.refresh(PUMPQ))
    m = nonsol.curve_metrics(curve_dict(), info)
    spot_quote = (803_924_938_774 / 1e6) / (680_110_528_032_885 / 1e6)
    assert m["spot_quote"] == pytest.approx(spot_quote)                     # ≈ 0.001182 PUMP / token
    assert m["mcap_usd"] == pytest.approx(spot_quote * PUMP_USD * 1e9)      # ≈ $7,388
    assert 7_000 < m["mcap_usd"] < 7_800
    assert m["spot_sol"] == pytest.approx(spot_quote * SPQ)
    # WLD has 8 decimals: the same raw reserves mean 100× fewer quote units
    run(_set_price(px, WLD, 0.515, 8))
    w = nonsol.curve_metrics(curve_dict(quote=WLD), nonsol._cache[WLD])
    assert w["spot_quote"] == pytest.approx(spot_quote / 100)


async def _set_price(px, mint, usd, dec):
    f, r = prices(quote_usd=usd, decimals=dec, mint=mint)
    px.setattr(nonsol, "_fetch_prices", f)
    px.setattr(nonsol, "_route_rate", r)
    await nonsol.refresh(mint)


def test_decision_gates(px, monkeypatch):
    why, facts = run(nonsol.decision_gates(curve_dict(), PUMPQ, max_mcap_usd=20_000))
    assert why == [] and 7_000 < facts["mcap_usd"] < 7_800
    assert run(nonsol.decision_gates(curve_dict(vqr=803_924_938_774 * 3), PUMPQ, max_mcap_usd=20_000))[0][0].startswith("mcap $")
    assert "not whitelisted" in run(nonsol.decision_gates(curve_dict(quote=WLD), WLD, max_mcap_usd=20_000))[0][0]
    assert "not whitelisted" in run(nonsol.decision_gates(curve_dict(), "Unknown1111111111111111111111111111111111",
                                                          max_mcap_usd=20_000))[0][0]
    assert run(nonsol.decision_gates(curve_dict(complete=True), PUMPQ, max_mcap_usd=20_000))[0] == ["migrated: curve complete"]
    assert "≠ signal quote" in run(nonsol.decision_gates(curve_dict(quote=USDC), PUMPQ, max_mcap_usd=20_000))[0][0]
    assert "no pump.fun curve" in run(nonsol.decision_gates(None, PUMPQ, max_mcap_usd=20_000))[0][0]
    monkeypatch.setenv("LIVE_SCOUT_NONSOL_ENABLED", "false")
    assert "LIVE_SCOUT_NONSOL_ENABLED=false" in run(nonsol.decision_gates(curve_dict(), PUMPQ, max_mcap_usd=20_000))[0][0]
    monkeypatch.setenv("LIVE_SCOUT_NONSOL_ENABLED", "true")
    monkeypatch.setenv("LIVE_SCOUT_NONSOL_QUOTES", "USDC,USDT,PUMP,WLD")    # opt-in by symbol from the known table
    assert nonsol.allowed(WLD)


def test_decision_gates_no_price_fails_closed(monkeypatch):
    why, _ = run(nonsol.decision_gates(curve_dict(), PUMPQ, max_mcap_usd=20_000))   # conftest: network down
    assert why and why[0].startswith("quote price unavailable")


def _quote_fn(buy_tokens=None, sell_lamports=None, raise_=False):
    async def q(inp, out, amount):
        if raise_:
            raise RuntimeError("down")
        return buy_tokens if inp == WSOL else sell_lamports
    return q


def _rc(c):
    async def rc(mint):
        return c
    return rc


def _spot_tokens(size):
    info = nonsol._cache[PUMPQ]
    spot = nonsol.curve_metrics(curve_dict(), info)["spot_sol"]
    return int(size / spot * 1e6)


def test_prebuy_ok_and_refusals(px):
    run(nonsol.refresh(PUMPQ))
    toks = int(_spot_tokens(0.2) * 0.96)                    # 4% worse than spot (fees + impact)
    ok, why, facts = run(nonsol.prebuy(MINT, PUMPQ, 0.2, max_mcap_usd=20_000, read_curve=_rc(curve_dict()),
                                       quote=_quote_fn(toks, int(0.2e9 * 0.92))))
    assert ok and facts["roundtrip"] == pytest.approx(0.92) and 7_000 < facts["mcap_usd"] < 7_800
    cases = [
        (dict(read_curve=_rc(None)), "no pump.fun curve"),
        (dict(read_curve=_rc(curve_dict(complete=True))), "migrated"),
        (dict(quote=_quote_fn(None, None)), "no Jupiter SOL→token route"),
        (dict(quote=_quote_fn(toks, None)), "SELL route"),
        (dict(quote=_quote_fn(toks, int(0.2e9 * 0.5))), "round trip"),
        (dict(quote=_quote_fn(int(toks * 0.6), int(0.2e9 * 0.9))), "vs curve spot"),
        (dict(quote=_quote_fn(raise_=True)), "no Jupiter SOL→token route"),
    ]
    for over, frag in cases:
        kw = dict(max_mcap_usd=20_000, read_curve=_rc(curve_dict()), quote=_quote_fn(toks, int(0.2e9 * 0.92)))
        kw.update(over)
        ok, why, _ = run(nonsol.prebuy(MINT, PUMPQ, 0.2, **kw))
        assert not ok and frag in why, (frag, why)

    async def boom(m):
        raise RuntimeError("rpc")
    assert "curve read error" in run(nonsol.prebuy(MINT, PUMPQ, 0.2, max_mcap_usd=20_000, read_curve=boom,
                                                   quote=_quote_fn(toks, 1)))[1]
    assert "mcap" in run(nonsol.prebuy(MINT, PUMPQ, 0.2, max_mcap_usd=5_000, read_curve=_rc(curve_dict()),
                                       quote=_quote_fn(toks, int(0.2e9 * 0.92))))[1]
    assert "not whitelisted" in run(nonsol.prebuy(MINT, WLD, 0.2, max_mcap_usd=20_000, read_curve=_rc(curve_dict()),
                                                  quote=_quote_fn(toks, 1)))[1]


# ======================= live engine: entry =======================
def test_engine_sizes_non_sol_from_scout_and_buys_via_jupiter_sol_in(env, px):
    run(nonsol.refresh(PUMPQ))
    e = LiveEngine()
    calls, toks = [], int(_spot_tokens(0.2) * 0.96)
    px.setattr(e, "_read_curve_raw", _rc(curve_dict()))
    px.setattr(e, "_jup_quote", _quote_fn(toks, int(0.2e9 * 0.92)))

    async def bal(**kw):
        return 2.5
    px.setattr(e, "sol_balance", bal)

    async def tb(mint):
        return toks, 6
    px.setattr(e, "token_balance", tb)

    async def swap(**kw):
        calls.append(kw)
        return {"ok": True, "stage": "execute", "total_in": kw["amount"], "total_out": toks, "signature": "SIG1",
                "order": {"router": "aggregator"}}
    px.setattr(jupiter, "swap", swap)

    async def go():
        d = e.on_signal("scout_solo", MINT, trader_wallets=["DocW"], meta=dict(NS_META))
        assert d["decision"] == "buy" and d["size_sol"] == pytest.approx(0.20) and d["nonsol"]
        assert e.pending_buys[MINT]["meta"]["quote_mint"] == PUMPQ and e.pending_buys[MINT]["meta"]["nonsol"]
        await _drain(e)
    run(go())
    assert calls and calls[0]["input_mint"] == WSOL and calls[0]["output_mint"] == MINT and calls[0]["amount"] == 200_000_000
    p = e.open_positions()[0]
    assert e.is_nonsol(p) and p.meta["quote_mint"] == PUMPQ and p.meta["nonsol_prebuy"]["roundtrip"] == pytest.approx(0.92)
    assert p.entry_price_sol == pytest.approx(0.2 / (toks / 1e6))
    assert e.day()["trades"] == 1 and e.summary()["nonsol"]["open"] == 1


def test_engine_sol_coin_size_unchanged(env):
    e = LiveEngine()
    ok, why, size = e.check_caps("scout_solo", MINT)
    assert size == pytest.approx(0.10)
    d = e.on_signal("scout_solo", MINT, meta={"scout_sol": 5.0, "quote_mint": None})   # no loop → refused, size 0.10
    assert d["size_sol"] == pytest.approx(0.10) and "nonsol" not in d


@pytest.mark.parametrize("arm,meta,frag", [
    ("fluffy_pump_dump", dict(NS_META, tg_gate_passed=True), "only for scout arms"),
    ("scout_solo", dict(NS_META, nonsol_gates_passed=False), "decision gates not passed"),
    ("scout_solo", dict(NS_META, quote_mint=WLD), "not whitelisted"),
    ("scout_solo", dict(NS_META, scout_sol=None), "scout buy size unknown"),
    ("scout_solo", dict(NS_META, scout_sol=0.3), "< 0.5"),
])
def test_engine_non_sol_refusals(env, arm, meta, frag):
    e = LiveEngine()

    async def go():
        return e.on_signal(arm, MINT, meta=meta)
    d = run(go())
    assert d["decision"] == "refused" and any(frag in r for r in d["reasons"]) and MINT not in e.bought_mints
    assert not e.pending_buys and e.day()["trades"] == 0


def test_engine_non_sol_flag_off(env, monkeypatch):
    monkeypatch.setenv("LIVE_SCOUT_NONSOL_ENABLED", "false")
    e = LiveEngine()

    async def go():
        return e.on_signal("scout_solo", MINT, meta=dict(NS_META))
    d = run(go())
    assert d["decision"] == "refused" and any("LIVE_SCOUT_NONSOL_ENABLED=false" in r for r in d["reasons"])


def test_engine_non_sol_counts_toward_caps(env):
    e = LiveEngine()
    for i in range(3):
        p = LivePos(mint=f"M{i}", arm="scout_solo", size_sol=0.1, spent_lamports=10**8, tokens_initial=10**9,
                    tokens_left=10**9, entry_price_sol=1e-7, entry_ms=int(time.time() * 1000))
        e.positions[p.id] = p

    async def go():
        return e.on_signal("scout_confirmed", MINT, meta=dict(NS_META))
    d = run(go())
    assert d["decision"] == "refused" and any("max open positions" in r for r in d["reasons"])
    e.positions.clear()
    e.day()["trades"] = 10
    assert any("max trades/day" in r for r in run(go())["reasons"])
    e.day()["trades"] = 0
    e.day()["realized_pnl_sol"] = -0.6
    assert any("daily loss" in r for r in run(go())["reasons"])


def test_engine_prebuy_refusal_refunds_without_swap(env, px):
    e = LiveEngine()
    calls = []

    async def swap(**kw):
        calls.append(kw)
        return {"ok": True}
    px.setattr(jupiter, "swap", swap)
    px.setattr(e, "_read_curve_raw", _rc(curve_dict(complete=True)))
    px.setattr(e, "_jup_quote", _quote_fn(10**12, 10**8))

    async def go():
        e.on_signal("scout_solo", MINT, meta=dict(NS_META))
        await _drain(e)
    run(go())
    assert not calls and e.decisions[0]["decision"] == "aborted" and "migrated" in e.decisions[0]["reasons"][0]
    assert MINT not in e.bought_mints and not e.pending_buys and e.day()["trades"] == 0


def test_engine_reserve_and_simulate_still_apply(env, px):
    run(nonsol.refresh(PUMPQ))
    e = LiveEngine()
    toks = int(_spot_tokens(0.4) * 0.96)
    px.setattr(e, "_read_curve_raw", _rc(curve_dict()))
    px.setattr(e, "_jup_quote", _quote_fn(toks, int(0.4e9 * 0.9)))
    calls = []

    async def bal(**kw):
        return 0.8                                          # 0.8 − 0.40 − fee < 0.50 reserve
    px.setattr(e, "sol_balance", bal)

    async def swap(**kw):
        calls.append(kw)
        return {"ok": False, "stage": "simulate", "error": "simulation failed"}
    px.setattr(jupiter, "swap", swap)

    async def go():
        e.on_signal("scout_solo", MINT, meta=dict(NS_META, scout_sol=5.0))
        await _drain(e)
    run(go())
    assert not calls and "SOL reserve" in e.decisions[0]["reasons"][0]

    async def bal2(**kw):
        return 2.5
    px.setattr(e, "sol_balance", bal2)
    nonsol._cache[PUMPQ]["ts"] = time.time()

    async def go2():
        e.on_signal("scout_solo", MINT, meta=dict(NS_META, scout_sol=5.0))
        await _drain(e)
    run(go2())
    assert calls and e.decisions[0]["decision"] == "aborted" and "simulate" in e.decisions[0]["reasons"][0]
    assert not e.open_positions() and not e.pending_buys


def test_engine_corrupt_state_blocks_non_sol(env):
    (env / "state.json").write_text("{not json")
    e = LiveEngine()
    e.load()

    async def go():
        return e.on_signal("scout_solo", MINT, meta=dict(NS_META))
    d = run(go())
    assert d["decision"] == "refused" and any("live state file problem" in r for r in d["reasons"])


# ======================= live engine: marks / trail / sell =======================
def _ns_pos(**kw):
    base = dict(mint=MINT, arm="scout_solo", size_sol=0.2, spent_lamports=200_000_000, tokens_initial=10**12,
                tokens_left=10**12, entry_price_sol=2e-7, entry_ms=int(time.time() * 1000) - 5000,
                meta={"nonsol": True, "quote_mint": PUMPQ})
    base.update(kw)
    return LivePos(**base)


def _rpc_curve(c):
    async def rpc(method, params):
        raw = curve_bytes(c)
        return {"value": [{"owner": PUMP_PROGRAM_ID, "data": [base64.b64encode(raw).decode(), "base64"]}
                          for _ in params[0]]}
    return rpc


def test_curve_bytes_roundtrip():
    c = decode_curve(curve_bytes(curve_dict()))
    assert c["quote_mint"] == PUMPQ and not c["quote_is_sol"] and c["virtual_quote_reserves"] == 803_924_938_774


def test_mark_non_sol_from_curve_times_quote_price(env, px):
    run(nonsol.refresh(PUMPQ))
    e = LiveEngine()
    p = _ns_pos(last_mark_ms=int(time.time() * 1000) - 5000)
    e.positions[p.id] = p
    px.setattr(e, "_rpc", _rpc_curve(curve_dict()))

    async def no_order(**kw):
        raise AssertionError("Jupiter not needed while the curve mark is fresh")
    px.setattr(jupiter, "order", no_order)
    run(e.mark([p]))
    want = nonsol.curve_metrics(curve_dict(), nonsol._cache[PUMPQ])["spot_sol"]
    assert p.mark_source == "curve_quote" and p.last_price_sol == pytest.approx(want)
    # a SOL-curve stream price (quote units) is never applied to a non-SOL position
    class S:
        mint, last_price_sol, migrated, price_ms = MINT, 123.0, False, int(time.time() * 1000) + 10

    run(e.on_stream(S()))
    assert p.last_price_sol == pytest.approx(want)


def test_mark_non_sol_falls_back_to_jupiter_sell_quote(env, px):
    e = LiveEngine()
    p = _ns_pos(last_mark_ms=int(time.time() * 1000) - 10_000)
    e.positions[p.id] = p
    px.setattr(e, "_rpc", _rpc_curve(curve_dict()))           # curve ok but NO verified quote price (network down)

    async def boom(*a, **k):
        raise RuntimeError("down")
    px.setattr(nonsol, "_fetch_prices", boom)
    seen = []

    async def order(**kw):
        seen.append(kw)
        return {"ok": True, "out_amount": 150_000_000}
    px.setattr(jupiter, "order", order)
    run(e.mark([p]))
    assert seen and seen[0]["input_mint"] == MINT and seen[0]["output_mint"] == WSOL and seen[0]["amount"] == p.tokens_left
    assert p.mark_source == "jupiter_quote" and p.last_price_sol == pytest.approx(0.15 / (10**12 / 1e6))


def test_non_sol_stale_then_sell_never_stranded(env, px):
    e = LiveEngine()
    old = int(time.time() * 1000) - 120_000
    p = _ns_pos(last_mark_ms=old, entry_ms=old)
    e.positions[p.id] = p

    async def rpc_fail(*a, **k):
        raise RuntimeError("rpc")
    px.setattr(e, "_rpc", rpc_fail)

    async def no_quote(**kw):
        return {"ok": False}
    px.setattr(jupiter, "order", no_quote)
    run(e.mark([p]))
    assert p.quote_fail_since_ms
    p.quote_fail_since_ms -= 181_000
    sold = []

    async def fake_sell(pos, frac, reason):
        sold.append(reason)
    px.setattr(e, "_sell", fake_sell)
    run(e.manage(p))
    assert sold == ["stale_mark_no_quote"]


def test_non_sol_trail_and_hard_stop_on_sol_marks(env, px):
    e = LiveEngine()
    sold = []

    async def fake_sell(pos, frac, reason):
        sold.append((reason, frac))
    px.setattr(e, "_sell", fake_sell)
    now = int(time.time() * 1000)
    p = _ns_pos(tp_done=True, ladder_done=["tp_2x"], high_price_sol=6e-7, last_price_sol=4.1e-7, last_mark_ms=now)
    e.positions[p.id] = p
    run(e.manage(p))                                          # 4.1e-7 ≤ 6e-7 × (1 − 0.30) = 4.2e-7 → trail
    assert sold == [("trail", 1.0)]
    sold.clear()
    q = _ns_pos(last_price_sol=1.2e-7, high_price_sol=2e-7, last_mark_ms=now)   # −40% from 2e-7 entry
    e.positions[q.id] = q
    run(e.manage(q))
    assert sold == [("hard_stop", 1.0)]


def test_non_sol_sell_goes_token_to_sol(env, px):
    e = LiveEngine()
    p = _ns_pos()
    e.positions[p.id] = p
    calls = []

    async def tb(mint):
        return p.tokens_left, 6
    px.setattr(e, "token_balance", tb)

    async def swap(**kw):
        calls.append(kw)
        return {"ok": True, "total_in": kw["amount"], "total_out": 260_000_000, "signature": "S", "order": {}}
    px.setattr(jupiter, "swap", swap)
    run(e._sell(p, 1.0, "trail"))
    assert calls[0]["input_mint"] == MINT and calls[0]["output_mint"] == WSOL and p.status == "closed"
    assert p.sells[0]["pnl_sol"] == pytest.approx(0.06)


def test_reconciled_non_sol_position_keeps_quote_meta(env, px):
    e = LiveEngine()
    e.pending_buys[MINT] = {"arm": "scout_solo", "ts_ms": int(time.time() * 1000), "size_sol": 0.2, "stage": "sending",
                            "signature": "S", "traders": [], "meta": {"quote_mint": PUMPQ, "nonsol": True, "scout_sol": 2.0}}

    async def tb(mint):
        return 10**12, 6
    px.setattr(e, "token_balance", tb)
    run(e.reconcile())
    p = e.open_positions()[0]
    assert e.is_nonsol(p) and p.meta["quote_mint"] == PUMPQ


def test_read_curve_never_prices_non_sol_pool_in_sol(monkeypatch):
    from app.pump import curve as C

    async def acct(pubkey, **kw):
        return {"owner": PUMP_PROGRAM_ID, "data": curve_bytes(curve_dict(complete=True))}

    async def pool(mint, qm, c):
        return {"status": "ok", "price_sol": 0.5}
    monkeypatch.setattr(C.rpc, "get_account", acct)
    monkeypatch.setattr(C, "read_pumpswap_pool", pool)

    async def irr(c):
        return 793_100_000 * 10**6
    monkeypatch.setattr(C, "initial_real_token_reserves", irr)
    res = run(C.read_curve(MINT, use_cache=False))
    assert res["status"] == "ok" and res["price_sol"] is None and res["pumpswap"]["price_sol"] is None


# ======================= scout engine → live hand-off =======================
def test_scout_arm_a_and_b_hand_non_sol_to_live_with_scout_size(px, monkeypatch):
    from tests import test_v116 as T
    from app.scout import engine as eng_mod, roster as roster_mod, source as src_mod
    from app.live import engine as live_mod
    from app.fomo_onchain import market
    eng_mod.engine.__init__()
    roster_mod.roster.__init__()
    roster_mod.roster.add(T.DOC, "The Doc", "scout")
    roster_mod.roster.add(T.CONF, "Cented", "confirmer")
    monkeypatch.setitem(market._cache, market.WSOL_MINT, {"price_usd": SOL_USD, "_t": time.time()})

    async def rc(mint):
        return dict(curve_dict(), read_at_ms=int(time.time() * 1000))

    async def age(mint, max_pages=3):
        return {"ok": True, "created_s": time.time() - 300, "tx_count": 40, "first_sigs": ["c"]}
    monkeypatch.setattr(src_mod.source, "read_curve", rc)
    monkeypatch.setattr(src_mod.source, "token_age", age)
    monkeypatch.setattr(eng_mod.engine, "scan_first_buyers", lambda mint: asyncio.sleep(0))
    monkeypatch.setattr(eng_mod.engine, "enter", lambda *a, **k: asyncio.sleep(0))
    got = []
    monkeypatch.setattr(live_mod.engine, "on_signal", lambda arm, mint, **kw: got.append((arm, kw)) or {"decision": "buy"})
    ev = {"kind": "wallet_trade", "mint": MINT, "sol_amount": int(8.010891e9), "token_amount": 144_470_883_256_231,
          "is_buy": True, "user": T.DOC, "quote_is_sol": False, "quote_mint": PUMPQ, "price_sol": 5.5e-8,
          "decode_path": "tx", "venue": "pump_curve"}
    run(T._on(ev, w_addr=T.DOC))
    s = eng_mod.engine.signals[0]
    assert s["decision"] == "enter", s["reasons"]
    arm, kw = got[0]
    assert arm == "scout_solo" and kw["meta"]["quote_mint"] == PUMPQ and kw["meta"]["nonsol_gates_passed"]
    assert kw["meta"]["scout_sol"] == pytest.approx(8.010891)
    assert nonsol.size_for(kw["meta"]["scout_sol"], max_sol_per_trade=0.40)[0] == pytest.approx(0.40)
    # confirmer within 60 s → arm B gets the TRIGGERING scout's size, not the confirmer's
    conf = dict(ev, user=T.CONF, sol_amount=int(0.7e9))
    run(T._on(conf, w_addr=T.CONF, sig="sigConf"))
    arm_b, kw_b = got[1]
    assert arm_b == "scout_confirmed" and kw_b["meta"]["scout_sol"] == pytest.approx(8.010891)
    assert kw_b["meta"]["quote_mint"] == PUMPQ and kw_b["meta"]["nonsol_gates_passed"]


def test_scout_non_sol_over_mcap_or_no_price_skips(px, monkeypatch):
    from tests import test_v116 as T
    from app.scout import engine as eng_mod, roster as roster_mod, source as src_mod
    from app.fomo_onchain import market
    eng_mod.engine.__init__()
    roster_mod.roster.__init__()
    roster_mod.roster.add(T.DOC, "The Doc", "scout")
    monkeypatch.setitem(market._cache, market.WSOL_MINT, {"price_usd": SOL_USD, "_t": time.time()})

    async def rc(mint):
        return dict(curve_dict(vqr=803_924_938_774 * 4), read_at_ms=int(time.time() * 1000))
    monkeypatch.setattr(src_mod.source, "read_curve", rc)
    monkeypatch.setattr(eng_mod.engine, "scan_first_buyers", lambda mint: asyncio.sleep(0))
    ev = {"kind": "wallet_trade", "mint": MINT, "sol_amount": int(2e9), "token_amount": 10**12, "is_buy": True,
          "user": T.DOC, "quote_is_sol": False, "quote_mint": PUMPQ, "price_sol": 2e-6, "decode_path": "tx"}
    run(T._on(ev, w_addr=T.DOC))
    s = eng_mod.engine.signals[0]
    assert s["decision"] == "skip" and any(r.startswith("mcap $") for r in s["reasons"])
    eng_mod.engine.__init__()

    async def boom(*a, **k):
        raise RuntimeError("down")
    monkeypatch.setattr(nonsol, "_fetch_prices", boom)
    nonsol._cache.clear()
    run(T._on(ev, w_addr=T.DOC, sig="sigZ"))
    s = eng_mod.engine.signals[0]
    assert s["decision"] == "skip" and s["reasons"][0].startswith("quote price unavailable")
    assert eng_mod.engine.retries[MINT] == 1                # transient: the next scout buy re-evaluates
