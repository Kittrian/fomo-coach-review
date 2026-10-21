"""v1.20 direct pump.fun path. Instruction layout vs REAL mainnet txs (tests/fixtures/pump_v3_mainnet.json), fee /
slippage math, graduated-coin fallback, shadow never signs or sends, no double-buy on an ambiguous send.
No network, no keypair, no live state."""
from __future__ import annotations

import asyncio
import base64
import json
import struct
from pathlib import Path

import httpx
import pytest

from app.live import jupiter, pumpdirect as pd, rugcheck
from app.live.engine import LiveEngine

FIX = json.loads((Path(__file__).parent / "fixtures" / "pump_v3_mainnet.json").read_text())
MINT = "MintAAAA1111111111111111111111111111111pump"


def _curve(**kw):
    c = {"virtual_token_reserves": 1_000_000_000_000_000, "virtual_quote_reserves": 30_000_000_000,
         "real_token_reserves": 793_100_000_000_000, "real_quote_reserves": 1_000_000_000,
         "token_total_supply": 1_000_000_000_000_000, "complete": False, "quote_is_sol": True,
         "is_cashback_coin": False, "is_mayhem_mode": False, "creator_fee_bps": 0}
    c.update(kw)
    return c


# ---------------- instruction layout vs mainnet ----------------
def test_buy_accounts_match_mainnet_buy_exact_quote_in_v3():
    f = FIX["buy_exact_quote_in_v3_ata"]                                # standard-ATA buyer (5DF9…)
    acc = f["accounts"]
    got = pd.v3_accounts(acc[1], acc[8], acc[3], acc[13])
    assert [k for k, _s, _w in got] == acc and len(acc) == 17


def test_buy_accounts_match_second_mainnet_buy_except_custom_token_account():
    f = FIX["buy_exact_quote_in_v3"]                                    # bot tx (24Tw…): custom token account at 9
    acc = f["accounts"]
    got = [k for k, _s, _w in pd.v3_accounts(acc[1], acc[8], acc[3], acc[13])]
    assert got[:9] == acc[:9] and got[10:] == acc[10:]


def test_sell_accounts_match_mainnet_sell_v3():
    f = FIX["sell_v3"]
    acc = f["accounts"]
    got = pd.v3_accounts(acc[1], acc[8], acc[3], acc[13])
    assert [k for k, _s, _w in got] == acc


def test_signer_writable_flags_match_idl():
    for name in ("buy_exact_quote_in_v3", "sell_v3"):
        idl = FIX["idl"][name]
        got = pd.v3_accounts(*(FIX[name]["accounts"][i] for i in (1, 8, 3, 13)))
        assert [(s, w) for _k, s, w in got] == [(a["signer"], a["writable"]) for a in idl["accounts"]]
        assert list(pd.DISC[name]) == idl["discriminator"]


def test_instruction_data_bytes_match_mainnet():
    buy_hex = FIX["buy_exact_quote_in_v3"]["data_hex"]
    raw = bytes.fromhex(buy_hex)
    spend, min_tok = struct.unpack_from("<QQ", raw, 8)
    assert pd.buy_data(spend, min_tok).hex() == buy_hex and len(raw) == 25 and spend == 600_000_000
    sell_hex = FIX["sell_v3"]["data_hex"]
    amt, min_sol = struct.unpack_from("<QQ", bytes.fromhex(sell_hex), 8)
    assert pd.sell_data(amt, min_sol).hex() == sell_hex


def test_pdas_and_constants_derive():
    assert pd.pda([b"fee_config", bytes(pd._pk(pd.PUMP_PROGRAM))], pd.PUMP_FEES_PROGRAM) == pd.FEE_CONFIG
    assert pd.pda([b"__event_authority"], pd.PUMP_PROGRAM) == pd.EVENT_AUTHORITY
    assert pd.pda([b"global"], pd.PUMP_PROGRAM) == pd.PUMP_GLOBAL
    g = base64.b64decode(FIX["global_b64"])
    from solders.pubkey import Pubkey
    onchain = [str(Pubkey.from_bytes(g[741 + 32 * i:773 + 32 * i])) for i in range(8)]
    assert tuple(onchain) == pd.BUYBACK_FEE_RECIPIENTS


def test_full_tx_compiles_with_budget_ata_and_pump_ix(monkeypatch):
    from solders.keypair import Keypair
    kp = Keypair()
    monkeypatch.setattr(jupiter, "keypair", lambda: kp)
    user = str(kp.pubkey())
    st = {"curve": _curve(), "token_program": pd.TOKEN_2022_PROGRAM, "fee_config": None}
    b = pd.build("buy", MINT, user, 200_000_000, st, slippage_pct=0.15, cu_limit=200_000, priority_lamports=50_000)
    tx = pd.compile_tx(b["ixs"], user, "11111111111111111111111111111111", sign=True)
    msg = tx.message
    keys = [str(k) for k in msg.account_keys]
    progs = [keys[ix.program_id_index] for ix in msg.instructions]
    assert progs == ["ComputeBudget111111111111111111111111111111", "ComputeBudget111111111111111111111111111111",
                     pd.ATA_PROGRAM, pd.PUMP_PROGRAM]
    pump_ix = msg.instructions[3]
    assert [keys[i] for i in pump_ix.accounts] == [k for k, _s, _w in pd.v3_accounts(MINT, user, pd.TOKEN_2022_PROGRAM,
                                                                                       b["buyback"])]
    spend, mn = struct.unpack_from("<QQ", bytes(pump_ix.data), 8)
    assert spend == 200_000_000 and mn == b["min_out"]
    assert str(tx.signatures[0]) != str(type(tx.signatures[0]).default())
    ata_ix = msg.instructions[2]
    assert bytes(ata_ix.data) == b"\x01" and keys[ata_ix.accounts[5]] == pd.TOKEN_2022_PROGRAM
    unsigned = pd.compile_tx(b["ixs"], user, None, sign=False)
    assert str(unsigned.signatures[0]) == str(type(unsigned.signatures[0]).default())


# ---------------- fees / quote / slippage math ----------------
def test_fee_config_decodes_real_account():
    fc = pd.decode_fee_config(base64.b64decode(FIX["fee_config_b64"]))
    assert fc["tiers"] and fc["tiers"][0]["threshold"] == 0
    f = pd.fee_bps(_curve(), fc)
    assert f == {"protocol": 95, "creator": 30, "total": 125}


def test_fee_tier_selection_by_market_cap():
    fc = {"tiers": [{"threshold": 100 * 10**9, "protocol": 90, "creator": 30},
                    {"threshold": 400 * 10**9, "protocol": 50, "creator": 20}]}
    assert pd.fee_bps(_curve(), fc)["total"] == 120                     # mcap 30 SOL < first threshold → first tier
    big = _curve(virtual_quote_reserves=500 * 10**9)
    assert pd.fee_bps(big, fc)["total"] == 70


def test_buy_quote_and_min_out():
    c = _curve()
    out = pd.quote_buy(c, 200_000_000, 125)
    net = 200_000_000 * 10_000 // 10_125
    assert out == net * c["virtual_token_reserves"] // (c["virtual_quote_reserves"] + net)
    assert pd.min_out(out, 0.15) == int(out * 0.85)
    assert pd.quote_buy(_curve(real_token_reserves=5), 200_000_000, 125) == 5      # capped at real reserves


def test_sell_quote_ceil_fee():
    c = _curve()
    gross = 10**12 * c["virtual_quote_reserves"] // (c["virtual_token_reserves"] + 10**12)
    assert pd.quote_sell(c, 10**12, 125) == gross - (-(-gross * 125 // 10_000))


def test_slippage_default_and_env(monkeypatch):
    monkeypatch.delenv("LIVE_DIRECT_SLIPPAGE_PCT", raising=False)
    assert pd.cfg()["slippage_pct"] == pytest.approx(0.15)
    monkeypatch.setenv("LIVE_DIRECT_SLIPPAGE_PCT", "8")
    assert pd.cfg()["slippage_pct"] == pytest.approx(0.08)
    monkeypatch.setenv("LIVE_DIRECT_SLIPPAGE_PCT", "90")
    assert pd.cfg()["slippage_pct"] == pytest.approx(0.5)               # hard ceiling


def test_mode_default_off_and_shadow(monkeypatch):
    monkeypatch.delenv("LIVE_DIRECT_PUMP_ENABLED", raising=False)
    assert pd.mode() == "off"
    monkeypatch.setenv("LIVE_DIRECT_PUMP_ENABLED", "true")
    monkeypatch.delenv("LIVE_DIRECT_PUMP_MODE", raising=False)
    assert pd.mode() == "shadow"
    monkeypatch.setenv("LIVE_DIRECT_PUMP_MODE", "bogus")
    assert pd.mode() == "shadow"
    monkeypatch.setenv("LIVE_DIRECT_PUMP_MODE", "live")
    assert pd.mode() == "live"


@pytest.mark.parametrize("curve,tp,why", [
    (None, pd.TOKEN_PROGRAM, "no pump bonding curve"),
    (_curve(complete=True), pd.TOKEN_PROGRAM, "graduated"),
    (_curve(is_cashback_coin=True), pd.TOKEN_PROGRAM, "cashback"),
    (_curve(quote_is_sol=False), pd.TOKEN_PROGRAM, "non-SOL"),
])
def test_ineligible_coins(curve, tp, why):
    ok, reason = pd.eligibility({"curve": curve, "token_program": tp}, "buy", 1)
    assert not ok and why in reason


# ---------------- fake RPC ----------------
class FakeRpc:
    def __init__(self, curve=None, *, send_exc=None, statuses=None, sim_ok=True):
        self.calls = []
        self.curve = curve
        self.send_exc = send_exc
        self.statuses = list(statuses or [])
        self.sim_ok = sim_ok

    async def __call__(self, method, params, client, timeout=6.0):
        self.calls.append((method, params))
        if method == "getMultipleAccounts":
            return {"context": {"slot": 1}, "value": [None] * 5}
        if method == "getLatestBlockhash":
            return {"value": {"blockhash": "11111111111111111111111111111111"}}
        if method == "simulateTransaction":
            data = bytearray(165)
            struct.pack_into("<Q", data, 64, 123_456)
            return {"value": {"err": None if self.sim_ok else {"InstructionError": [3, {"Custom": 6002}]},
                              "unitsConsumed": 90_000, "logs": [],
                              "accounts": [{"lamports": 2039280, "data": [base64.b64encode(bytes(data)).decode(), "base64"]},
                                           {"lamports": 1_000_000_000}]}}
        if method == "sendTransaction":
            if self.send_exc:
                raise self.send_exc
            return "SIG"
        if method == "getSignatureStatuses":
            v = self.statuses.pop(0) if self.statuses else None
            return {"value": [v]}
        if method == "getTransaction":
            return None
        raise AssertionError(method)


def _patch_state(monkeypatch, curve, tp=pd.TOKEN_PROGRAM, user_tokens=0):
    async def fetch_state(mint, user, client, *, blockhash=True):
        return {"curve": curve, "token_program": tp, "fee_config": None, "blockhash": "11111111111111111111111111111111",
                "user_lamports": 1_000_000_000, "user_tokens": user_tokens, "user_ata_exists": bool(user_tokens),
                "slot": 1}
    monkeypatch.setattr(pd, "fetch_state", fetch_state)


@pytest.fixture
def keys(monkeypatch, tmp_path):
    from solders.keypair import Keypair
    kp = Keypair()
    monkeypatch.setattr(jupiter, "keypair", lambda: kp)
    monkeypatch.setattr(jupiter, "pubkey", lambda: str(kp.pubkey()))
    monkeypatch.setenv("LIVE_DIRECT_LOG_PATH", str(tmp_path / "direct.jsonl"))
    monkeypatch.setenv("LIVE_DIRECT_CONFIRM_TIMEOUT_S", "0.3")
    return kp


def test_shadow_never_signs_or_sends(monkeypatch, keys):
    rpc = FakeRpc()
    monkeypatch.setattr(pd, "_rpc", rpc)
    _patch_state(monkeypatch, _curve())
    rec = asyncio.run(pd.shadow("buy", MINT, 200_000_000, client=httpx.AsyncClient()))
    methods = [m for m, _ in rpc.calls]
    assert "sendTransaction" not in methods and methods == ["simulateTransaction"]
    tx_b64, opts = rpc.calls[0][1]
    from solders.transaction import VersionedTransaction
    from solders.signature import Signature
    tx = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
    assert tx.signatures[0] == Signature.default()                     # unsigned: cannot execute anywhere
    assert opts["sigVerify"] is False and opts["replaceRecentBlockhash"] is True
    assert rec["eligible"] and rec["sim_ok"] and rec["sim_out"] == 123_456 and rec["ready_ms"] is not None


def test_shadow_on_graduated_coin_only_logs(monkeypatch, keys):
    rpc = FakeRpc()
    monkeypatch.setattr(pd, "_rpc", rpc)
    _patch_state(monkeypatch, _curve(complete=True))
    rec = asyncio.run(pd.shadow("buy", MINT, 200_000_000, client=httpx.AsyncClient()))
    assert rec["eligible"] is False and "graduated" in rec["why"] and rpc.calls == []


def test_live_swap_graduated_falls_back_without_sending(monkeypatch, keys):
    rpc = FakeRpc()
    monkeypatch.setattr(pd, "_rpc", rpc)
    _patch_state(monkeypatch, _curve(complete=True))
    signed = []
    r = asyncio.run(pd.swap(side="buy", mint=MINT, amount=200_000_000, client=httpx.AsyncClient(),
                            on_signed=signed.append))
    assert r["fallback"] is True and r["stage"] == "ineligible" and not signed and rpc.calls == []


def test_live_swap_sim_failure_falls_back_without_sending(monkeypatch, keys):
    rpc = FakeRpc(sim_ok=False)
    monkeypatch.setattr(pd, "_rpc", rpc)
    _patch_state(monkeypatch, _curve())
    signed = []
    r = asyncio.run(pd.swap(side="buy", mint=MINT, amount=200_000_000, client=httpx.AsyncClient(),
                            on_signed=signed.append))
    assert r["fallback"] is True and r["stage"] == "simulate" and not signed
    assert "sendTransaction" not in [m for m, _ in rpc.calls]


def test_live_swap_send_timeout_is_ambiguous_not_fallback(monkeypatch, keys):
    rpc = FakeRpc(send_exc=httpx.ReadTimeout("t"))
    monkeypatch.setattr(pd, "_rpc", rpc)
    _patch_state(monkeypatch, _curve())
    r = asyncio.run(pd.swap(side="buy", mint=MINT, amount=200_000_000, client=httpx.AsyncClient()))
    assert not r.get("fallback") and r["ambiguous"] is True and r["signature"]


def test_live_swap_refused_but_seen_on_chain_is_not_fallback(monkeypatch, keys):
    rpc = FakeRpc(send_exc=pd.RpcRefused("x"), statuses=[{"confirmationStatus": "processed", "err": None}])
    monkeypatch.setattr(pd, "_rpc", rpc)
    _patch_state(monkeypatch, _curve())
    r = asyncio.run(pd.swap(side="buy", mint=MINT, amount=200_000_000, client=httpx.AsyncClient()))
    assert not r.get("fallback")


def test_live_swap_confirmed_fill(monkeypatch, keys):
    monkeypatch.setenv("LIVE_DIRECT_CONFIRM_TIMEOUT_S", "3")
    rpc = FakeRpc(statuses=[None, {"confirmationStatus": "confirmed", "err": None}])
    monkeypatch.setattr(pd, "_rpc", rpc)
    _patch_state(monkeypatch, _curve())
    r = asyncio.run(pd.swap(side="buy", mint=MINT, amount=200_000_000, client=httpx.AsyncClient()))
    assert r["ok"] and r["sender"] == "pump_direct" and r["total_in"] == 200_000_000
    assert {"fetch_ms", "build_ms", "sign_ms", "sim_ms", "send_ms", "confirm_ms"} <= set(r["timing"])
    assert [m for m, _ in rpc.calls].count("sendTransaction") >= 1


def test_live_swap_gate_refusal_never_sends(monkeypatch, keys):
    rpc = FakeRpc()
    monkeypatch.setattr(pd, "_rpc", rpc)
    _patch_state(monkeypatch, _curve())

    async def gate(age):
        return False, "mesh says no", False
    r = asyncio.run(pd.swap(side="buy", mint=MINT, amount=200_000_000, client=httpx.AsyncClient(), pre_execute=gate))
    assert r["stage"] == "gate" and not r.get("fallback") and "sendTransaction" not in [m for m, _ in rpc.calls]


# ---------------- engine integration ----------------
@pytest.fixture
def env(monkeypatch, tmp_path, keys):
    for k, v in {"JUPITER_EXEC_ENABLED": "true", "LIVE_AUTOTRADE_ENABLED": "true",
                 "LIVE_ARMS": "scout_solo,scout_confirmed,fluffy_pump_dump,zyrox",
                 "LIVE_SIZE_SOL_SCOUT_SOLO": "0.20", "LIVE_MAX_SOL_PER_TRADE": "0.40", "LIVE_MIN_SOL_RESERVE": "0.50",
                 "LIVE_STATE_PATH": str(tmp_path / "state.json"), "LIVE_LEDGER_PATH": str(tmp_path / "ledger.jsonl"),
                 "LIVE_KILL_FILE": str(tmp_path / "KILL"), "CANDIDATES_PATH": str(tmp_path / "candidates.jsonl"),
                 "LIVE_DIRECT_PUMP_ENABLED": "true"}.items():
        monkeypatch.setenv(k, v)

    async def prebuy(mint, arm, *, creator=None):
        return {"ok": True, "refuse": [], "unknown": [], "checks": {}, "elapsed_ms": 1}
    monkeypatch.setattr(rugcheck.checker, "prebuy", prebuy)
    return tmp_path


def _engine(monkeypatch, jup_calls, tokens_after=0):
    e = LiveEngine()

    async def mesh(self, mint):
        return True, "ok"
    monkeypatch.setattr(LiveEngine, "_mesh_check", mesh)

    async def balance(**kw):
        return 2.5
    bal = {"v": tokens_after}

    async def tok(mint):
        return bal["v"], 6

    async def jswap(**kw):
        jup_calls.append(kw)
        if kw.get("pre_execute"):
            await kw["pre_execute"](0.1)
        if kw.get("on_signed"):
            kw["on_signed"]("JUPSIG")
        if kw.get("output_mint") != MINT:                                # a sell: sells exactly the amount asked
            return {"ok": True, "stage": "execute", "signature": "JUPSELL", "total_in": kw["amount"],
                    "total_out": 100_000_000, "order": {"router": "metis"}}
        return {"ok": True, "stage": "execute", "signature": "JUPSIG", "total_in": 200_000_000,
                "total_out": 120_000, "order": {"router": "metis"}}
    monkeypatch.setattr(e, "sol_balance", balance)
    monkeypatch.setattr(e, "token_balance", tok)
    monkeypatch.setattr(e, "_start_stream", lambda m: None)
    monkeypatch.setattr(e, "ensure_loop", lambda: None)
    monkeypatch.setattr(jupiter, "swap", jswap)
    return e, bal


async def _drain(e):
    for _ in range(10):
        pend = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if not pend:
            return
        await asyncio.wait(pend, timeout=5)


def test_engine_live_mode_graduated_coin_uses_jupiter(env, monkeypatch):
    monkeypatch.setenv("LIVE_DIRECT_PUMP_MODE", "live")
    rpc = FakeRpc()
    monkeypatch.setattr(pd, "_rpc", rpc)
    _patch_state(monkeypatch, _curve(complete=True))
    jc = []
    e, _ = _engine(monkeypatch, jc)

    async def go():
        e.on_signal("scout_solo", MINT, trader_wallets=["W"], ref_price_sol=1.5e-6, meta={})
        await _drain(e)
    asyncio.run(go())
    (p,) = e.open_positions()
    assert len(jc) == 1 and p.meta["timing"]["path"] == "jupiter"
    assert p.meta["timing"]["direct"]["fallback"] is True and "sendTransaction" not in [m for m, _ in rpc.calls]
    assert "fill_vs_scout_pct" in p.meta["timing"]


def test_engine_live_mode_ambiguous_direct_never_double_buys(env, monkeypatch):
    monkeypatch.setenv("LIVE_DIRECT_PUMP_MODE", "live")
    jc = []
    e, bal = _engine(monkeypatch, jc, tokens_after=150_000)

    async def dswap(**kw):
        kw["on_signed"]("DIRSIG")
        return {"ok": False, "ambiguous": True, "stage": "execute", "signature": "DIRSIG", "error": "timeout",
                "timing": {}, "order": {"router": "pump_direct", "out_amount": 150_000}}
    monkeypatch.setattr(pd, "swap", dswap)
    real_sleep = asyncio.sleep

    async def fast_sleep(s, *a, **k):
        await real_sleep(0)
    monkeypatch.setattr("app.live.engine.asyncio.sleep", fast_sleep)

    async def go():
        e.on_signal("scout_solo", MINT, trader_wallets=["W"], ref_price_sol=1.5e-6, meta={})
        await _drain(e)
    asyncio.run(go())
    assert jc == []                                                     # Jupiter never tried after a send
    (p,) = e.open_positions()
    assert p.buy_sig == "DIRSIG" and "reconciled_from_chain" in p.flags and p.tokens_initial == 150_000


def test_engine_shadow_mode_trades_via_jupiter_and_logs(env, monkeypatch, tmp_path):
    monkeypatch.setenv("LIVE_DIRECT_PUMP_MODE", "shadow")
    rpc = FakeRpc()
    monkeypatch.setattr(pd, "_rpc", rpc)
    _patch_state(monkeypatch, _curve())
    called = []

    async def dswap(**kw):
        called.append(kw)
        raise AssertionError("live direct swap must not run in shadow mode")
    monkeypatch.setattr(pd, "swap", dswap)
    jc = []
    e, _ = _engine(monkeypatch, jc)

    async def go():
        e.on_signal("scout_solo", MINT, trader_wallets=["W"], ref_price_sol=1.5e-6, meta={})
        await _drain(e)
    asyncio.run(go())
    assert len(jc) == 1 and not called
    assert "sendTransaction" not in [m for m, _ in rpc.calls]
    rows = [json.loads(x) for x in (tmp_path / "direct.jsonl").read_text().splitlines()]
    assert rows[-1]["kind"] == "shadow" and rows[-1]["side"] == "buy" and rows[-1]["sim_ok"]
    assert rows[-1]["direct_vs_jup_pct"] == pytest.approx((123_456 / 120_000 - 1) * 100, abs=1e-3)
    (p,) = e.open_positions()
    assert p.meta["timing"]["path"] == "jupiter" and p.meta["direct_shadow"]["sim_ok"] is True
    s = pd.summary()
    assert s["mode"] == "shadow" and s["buys"]["sim_ok"] == 1 and s["go_live_ready"] is False


def test_engine_off_mode_untouched(env, monkeypatch, tmp_path):
    monkeypatch.setenv("LIVE_DIRECT_PUMP_ENABLED", "false")

    async def boom(*a, **k):
        raise AssertionError("direct path must not run when disabled")
    monkeypatch.setattr(pd, "swap", boom)
    monkeypatch.setattr(pd, "shadow", boom)
    jc = []
    e, _ = _engine(monkeypatch, jc)

    async def go():
        e.on_signal("scout_solo", MINT, trader_wallets=["W"], ref_price_sol=1.5e-6, meta={})
        await _drain(e)
    asyncio.run(go())
    assert len(jc) == 1 and not (tmp_path / "direct.jsonl").exists()


def test_summary_go_live_checks_pass_with_good_evidence(monkeypatch, tmp_path):
    monkeypatch.setenv("LIVE_DIRECT_LOG_PATH", str(tmp_path / "d.jsonl"))
    rows = []
    for i in range(20):
        rows.append({"kind": "shadow", "side": "buy", "eligible": True, "sim_ok": True, "ready_ms": 150, "jup_ms": 1250,
                     "sim_out": 1000, "expected_out": 1000, "direct_vs_jup_pct": 2.0, "ts_ms": i})
    for i in range(10):
        rows.append({"kind": "shadow", "side": "sell", "eligible": True, "sim_ok": True, "ready_ms": 150,
                     "direct_vs_jup_pct": 0.5, "ts_ms": 100 + i})
    (tmp_path / "d.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    s = pd.summary()
    assert s["go_live_ready"] is True and s["buys"]["sim_ok_rate"] == 1.0


def test_engine_sells_shadow_then_live_fallback_on_graduated(env, monkeypatch, tmp_path):
    monkeypatch.setenv("LIVE_DIRECT_PUMP_MODE", "shadow")
    rpc = FakeRpc()
    monkeypatch.setattr(pd, "_rpc", rpc)
    _patch_state(monkeypatch, _curve(), user_tokens=120_000)
    jc = []
    e, bal = _engine(monkeypatch, jc, tokens_after=120_000)

    async def go():
        e.on_signal("scout_solo", MINT, trader_wallets=["W"], ref_price_sol=1.5e-6, meta={})
        await _drain(e)
        (p,) = e.open_positions()
        await e._sell(p, 0.5, "tp2x_half")
        await _drain(e)
        monkeypatch.setenv("LIVE_DIRECT_PUMP_MODE", "live")
        _patch_state(monkeypatch, _curve(complete=True), user_tokens=60_000)
        await e._sell(p, 1.0, "hard_stop")
        await _drain(e)
    asyncio.run(go())
    rows = [json.loads(x) for x in (tmp_path / "direct.jsonl").read_text().splitlines()]
    sh = [r for r in rows if r["kind"] == "shadow" and r["side"] == "sell"]
    lv = [r for r in rows if r["kind"] == "live" and r["side"] == "sell"]
    assert len(sh) == 1 and sh[0]["sim_ok"] and sh[0]["reason"] == "tp2x_half"
    assert lv and lv[0]["fallback"] is True and lv[0]["stage"] == "ineligible"
    assert len(jc) == 3                                                 # buy + 2 sells all via Jupiter
    assert "sendTransaction" not in [m for m, _ in rpc.calls]
