"""Phase 0 pump lane tests: PDA, curve/pool decode, TradeEvent, exit cards, paper ladder, gate, latency.
All offline (no RPC, no keys, no signing)."""
import asyncio
import base64
import struct
import time

import pytest

from app.pump import curve as C
from app.pump import events as E
from app.pump import exit_signals as X
from app.pump import fast_lane as F
from app.pump import paper as P
from app.pump.solana_pda import b58decode, b58encode, bonding_curve_pda, is_on_curve, is_pubkey
from app.pump.trade_stream import MintStream
from app.risk_gate import combine

MINT = "pf8M5XrxXFifn5vtWAssifi9VMf3wUyDGfyuXHQpump"
CREATOR = "A2Hg3ApCtjR1YKLHng8RsKsgT3ww1FdfUPJyEvy2KY6p"
USER = "11111111111111111111111111111112"


def test_pda_live_vector():
    # verified against mainnet getAccountInfo on 2026-10-06
    assert bonding_curve_pda(MINT) == "8xnnTeP1ZZ3gd1w81jHb8M8LjpCMT416NrkipsAAG83o"
    assert not is_on_curve(b58decode("8xnnTeP1ZZ3gd1w81jHb8M8LjpCMT416NrkipsAAG83o"))
    assert b58encode(b58decode(MINT)) == MINT
    assert is_pubkey(MINT) and not is_pubkey("nope")


def _curve_bytes(n, complete=False, vtr=1_000_000_000 * 10**6, vqr=40 * 10**9, rtr=700_000_000 * 10**6,
                 rqr=10 * 10**9, fee_bps=None):
    b = bytearray(C.CURVE_DISCRIMINATOR)
    b += struct.pack("<QQQQQ", vtr, vqr, rtr, rqr, 10**15)
    b += bytes([1 if complete else 0]) + b58decode(CREATOR) + b"\x00\x00" + bytes(32)
    if fee_bps is not None:
        b += struct.pack("<Q", fee_bps) + b"\x00\x01"
    b += bytes(max(0, n - len(b)))
    return bytes(b[:n]) if n >= len(b) else bytes(b)


@pytest.mark.parametrize("n", [125, 151])
def test_decode_curve_layouts(n):
    d = C.decode_curve(_curve_bytes(n, fee_bps=95 if n == 125 else None))
    assert d["creator"] == CREATOR and d["quote_is_sol"] and not d["complete"]
    assert d["account_len"] == n
    assert C.curve_price_sol(d) == pytest.approx(4e-8)
    if n == 125:
        assert d["creator_fee_bps"] == 95 and d["is_holder_reward"]


def test_decode_curve_rejects_other():
    with pytest.raises(ValueError):
        C.decode_curve(b"\x00" * 151)


def test_curve_math_roundtrip_loses_fees():
    d = C.decode_curve(_curve_bytes(151))
    toks = C.buy_tokens_out(d, int(0.4e9))
    assert toks > 0
    after = dict(d, virtual_token_reserves=d["virtual_token_reserves"] - toks,
                 virtual_quote_reserves=d["virtual_quote_reserves"] + int(0.4e9 * 0.9875))
    back = C.sell_sol_out(after, toks)
    assert 0.38e9 < back < 0.4e9


def test_pool_negative_virtual_quote():
    data = bytearray(C.POOL_VIRTUAL_QUOTE_OFFSET + 16 + 8)
    data[C.POOL_VIRTUAL_QUOTE_OFFSET:C.POOL_VIRTUAL_QUOTE_OFFSET + 16] = (-12345).to_bytes(16, "little", signed=True)
    assert C.decode_pool(bytes(data))["virtual_quote_reserves"] == -12345


def test_trade_event_roundtrip_and_logs():
    raw = E.encode_trade_event_for_tests(mint=MINT, user=USER, creator=CREATOR, sol_amount=5 * 10**8,
                                         is_buy=False, vsr=40 * 10**9, vtr=10**15)
    ev = E.decode_trade_event(raw)
    assert ev["mint"] == MINT and ev["user"] == USER and ev["creator"] == CREATOR and not ev["is_buy"]
    logs = ["Program log: Instruction: Sell", "Program data: " + base64.b64encode(raw).decode()]
    assert len(E.parse_logs(logs, MINT)) == 1 and E.parse_logs(logs, USER) == []
    assert E.logs_show_migration(["Program log: Instruction: Migrate"])


def _cards(**kw):
    base = dict(trades=[], creator=CREATOR, price=1.0, high=1.0, entry_price=1.0, entry_ms=int(time.time() * 1000),
                real_sol=10.0, peak_real_sol=10.0, migrated=False)
    base.update(kw)
    return {c["kind"]: c for c in X.evaluate(**base)}


def test_exit_cards():
    now = int(time.time() * 1000)
    assert _cards(trades=[{"user": CREATOR, "is_buy": False, "sol_amount": 10**9, "recv_ms": now}])["creator_sell"]["fired"]
    flow = [{"user": USER, "is_buy": False, "sol_amount": 3 * 10**9, "recv_ms": now},
            {"user": USER, "is_buy": True, "sol_amount": 10**9, "recv_ms": now}]
    assert _cards(trades=flow, price=0.8, high=1.0)["flow_flip"]["fired"]
    assert _cards(real_sol=5.0, peak_real_sol=10.0)["curve_draining"]["fired"]
    assert _cards(entry_ms=now - 700_000)["max_hold"]["fired"]
    assert _cards(migrated=True)["migration"]["fired"]
    assert not any(c["fired"] for c in _cards().values())


def test_trail_arms_only_in_profit():
    t = _cards(price=1.05, high=1.08)["trailing_stop"]  # high below +10% → not armed
    assert not t.get("armed") and not t["fired"]
    t = _cards(price=1.4, high=2.0)["trailing_stop"]
    assert t["armed"] and t["fired"]
    t = _cards(price=0.9, high=2.0)["trailing_stop"]  # below entry: trail never fires (hard stop's job)
    assert not t["fired"]


def test_paper_ladder(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "LOG_PATH", tmp_path / "paper.jsonl")
    monkeypatch.setattr(P.PaperEngine, "_ensure_ticker", lambda self: None)
    eng = P.PaperEngine()
    cv = dict(C.decode_curve(_curve_bytes(151)), status="ok")
    cv["price_sol"] = C.curve_price_sol(cv)

    async def go():
        pos = await eng.open_from_curve(MINT, cv, source="fluffy")
        assert pos and pos.entry_sol == pytest.approx(P._plan()["entry_sol"])
        s = MintStream(mint=MINT, creator=CREATOR)
        # pump price to 3× via a buy event (vsr up, vtr down) → TP1/TP2 ladder fills on the curve sim
        vsr, vtr = cv["virtual_quote_reserves"] * 2, cv["virtual_token_reserves"] * 2 // 3
        raw = E.encode_trade_event_for_tests(mint=MINT, user=USER, creator=CREATOR, sol_amount=10**9,
                                             is_buy=True, vsr=vsr, vtr=vtr, rsr=30 * 10**9, rtr=600_000_000 * 10**6)
        s.apply_trade(E.decode_trade_event(raw))
        await eng.on_stream(s)
        return pos

    pos = asyncio.run(go())
    assert "TP1" in pos.ladder_done
    assert pos.sol_out_lamports > 0 and pos.pnl()["pnl_sol"] > 0
    assert (tmp_path / "paper.jsonl").read_text().count('"kind": "fill"') >= 1


def test_gate_curve_lane_jev_advisory():
    mesh_pending = {"mesh_status": "pending", "blocked": False, "action": "escalate_human"}
    jev_skip = {"jev_status": "ok", "action": "skip"}
    g = combine(jev_skip, mesh_pending, fast_gate={"lane": "curve", "fast_ok": True, "reason": "on curve"})
    assert g["action"] == "show" and g["jev_role"] == "advisory" and g["size_hint"] == "reduce"
    # mesh hard block still wins on curve lane
    g = combine(jev_skip, {"mesh_status": "ok", "blocked": True, "action": "skip"}, fast_gate={"lane": "curve", "fast_ok": True})
    assert g["action"] == "skip" and g["decided_by"] == "mesh_block"
    # migrated keeps v1.10 rules: Jev skip blocks
    g = combine(jev_skip, {"mesh_status": "ok", "blocked": False, "action": "show"}, fast_gate={"lane": "migrated"})
    assert g["action"] == "skip" and g["jev_role"] == "gate"


def test_latency_timeline():
    now = int(time.time() * 1000)
    t = F.new_timeline("Mint111", source="fluffy", event_id="e1", meta={"tg_recv_ms": now - 100}, ingest_ms=now)
    F.mark(t, "curve_read", took_ms=90)
    v = F.view(t)
    stages = [r["stage"] for r in v["stages"]]
    assert stages[:3] == ["tg_recv", "ingest", "curve_read"]
    assert v["stages"][1]["since_start_ms"] == 100
    assert v["stages"][2]["took_ms"] == 90 and v["total_ms"] >= 100


def test_exec_still_disabled():
    from app.config import settings
    assert settings.jupiter_exec_enabled is False


def test_non_sol_quote_curve_not_simulated(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "LOG_PATH", tmp_path / "p.jsonl")
    cv = {"status": "ok", "complete": False, "quote_is_sol": False, "price_sol": None,
          "virtual_token_reserves": 10**15, "virtual_quote_reserves": 10**9}
    assert F.fast_gate(cv)["fast_ok"] is False
    assert asyncio.run(P.PaperEngine().open_from_curve(MINT, cv, source="fluffy")) is None
