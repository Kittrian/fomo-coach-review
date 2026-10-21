"""v1.16: scout detection fix, data hygiene, learning log (offline; no network, temp data dir only)."""
import asyncio
import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.pump import events as E
from app.pump.solana_pda import b58encode
from app.scout import ARM_A, ARM_C
from app.scout import engine as eng_mod, filters as filt_mod, roster as roster_mod, source as src_mod
from app.scout.txparse import parse_wallet_trades

FIX = json.loads((Path(__file__).parent / "fixtures" / "v116_real_scout_txs.json").read_text())
DOC = "DYAn4XpAkN5mhiXkRB7dGq4Jadnx6XYgu8L5b3WGhbrt"
FOX = "DAEdBmTPEKM6xkwfzC3d411QUe6coKpkND6UURa4CvHC"
SCOUT = "4BdKaxN8G6ka4GYtQQWk4G4dZRUTX2vQH9GcXdBREFUk"
CONF = "CyaE1VxvBrahnPWkqm5VsdCvyS2QmNht2UFrKJHga54o"
MINT = "DwcXyhEcSvzWgakDKpetZLFvbAHutpGpzhbU4iempump"
CREATOR = "862TYSvRYoiHAK3F3WwTRYAfuGiQaGdxedN9AGvRGWo2"
QUOTE = "pumpCmXqMfrsAkQ5r49WcJnRayYRqmXz6ae8H7H9Dfn"
VTR0, VSR0 = 1_073_000_000 * 10**6, 30 * 10**9


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def curve(**kw):
    c = {"virtual_token_reserves": VTR0, "virtual_quote_reserves": VSR0, "real_token_reserves": 793_100_000 * 10**6,
         "real_quote_reserves": 0, "complete": False, "quote_is_sol": True, "creator": CREATOR,
         "read_at_ms": int(time.time() * 1000)}
    c.update(kw)
    return c


@pytest.fixture(autouse=True)
def iso(monkeypatch):
    eng_mod.engine.__init__()
    filt_mod.filters.__init__()
    roster_mod.roster.__init__()
    roster_mod.roster.add(SCOUT, "Jijo", "scout")
    roster_mod.roster.add(CONF, "Cented", "confirmer")
    roster_mod.roster.add(DOC, "The Doc", "scout")
    from app.fomo_onchain import market
    monkeypatch.setitem(market._cache, market.WSOL_MINT, {"price_usd": 150.0, "_t": time.time()})
    state = {"curve": curve()}

    async def age(mint, max_pages=3):
        return {"ok": True, "created_s": time.time() - 120, "tx_count": 40, "first_sigs": ["c"]}

    async def rc(mint):
        return None if state["curve"] is None else dict(state["curve"], read_at_ms=int(time.time() * 1000))

    async def rcs(mints):
        return {m: await rc(m) for m in mints}
    monkeypatch.setattr(src_mod.source, "token_age", age)
    monkeypatch.setattr(src_mod.source, "read_curve", rc)
    monkeypatch.setattr(src_mod.source, "read_curves", rcs)
    monkeypatch.setattr(eng_mod.engine, "scan_first_buyers", lambda mint: asyncio.sleep(0))
    for p in (os.environ["CANDIDATES_PATH"], os.environ["SCOUT_DETECTIONS_PATH"], os.environ["SCOUT_LOG_PATH"]):
        Path(p).unlink(missing_ok=True)
    monkeypatch.setattr(eng_mod, "LOG_PATH", Path(os.environ["SCOUT_LOG_PATH"]))
    yield state


def _cands():
    p = Path(os.environ["CANDIDATES_PATH"])
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


# ---------------- 1. decoder: every TradeEvent version ----------------
def test_decode_v1_v2_sol_and_non_sol_quote():
    v1 = E.decode_trade_event(E.encode_trade_event_for_tests(mint=MINT, user=SCOUT, sol_amount=10**9, creator=CREATOR))
    assert v1["event_version"] == "v1" and v1["quote_is_sol"] and v1["creator"] == CREATOR
    v2 = E.decode_trade_event(E.encode_trade_event_for_tests(mint=MINT, user=SCOUT, sol_amount=10**9, v2=True,
                                                             shareholders=2, ix_name="buy_v3"))
    assert v2["event_version"] == "v2" and v2["ix_name"] == "buy_v3" and v2["quote_is_sol"] and E.event_price_sol(v2)
    nq = E.decode_trade_event(E.encode_trade_event_for_tests(mint=MINT, user=SCOUT, sol_amount=0, vsr=0, v2=True,
                                                             quote_mint=QUOTE, quote_amount=5 * 10**9, vqr=8 * 10**11))
    assert nq["quote_is_sol"] is False and nq["quote_mint"] == QUOTE and nq["quote_amount"] == 5 * 10**9
    assert E.event_price_sol(nq) is None
    short = E.encode_trade_event_for_tests(mint=MINT, user=SCOUT, sol_amount=7)[:129]
    assert E.decode_trade_event(short)["sol_amount"] == 7          # oldest prefix still decodes


def test_decode_amm_and_emit_cpi_inner_events():
    raw = E.encode_amm_event_for_tests(is_buy=True, user=SCOUT, pool=CONF, base_amount=5, quote_amount=2 * 10**9)
    a = E.decode_amm_event(raw)
    assert a["kind"] == "amm_buy" and a["user"] == SCOUT and a["pool"] == CONF and a["quote_amount"] == 2 * 10**9
    tr = E.encode_trade_event_for_tests(mint=MINT, user=SCOUT, sol_amount=3, v2=True)
    meta = {"innerInstructions": [{"index": 0, "instructions": [{"data": b58encode(E.EMIT_CPI_TAG + tr)},
                                                                 {"data": b58encode(E.EMIT_CPI_TAG + raw)}]}]}
    kinds = [e["kind"] for e in E.parse_inner_events(meta)]
    assert kinds == ["pump_trade", "amm_buy"]
    logs = ["Program data: " + base64.b64encode(raw).decode(), "Program data: " + base64.b64encode(tr).decode()]
    assert len(E.parse_logs(logs)) == 1 and len(E.parse_all_logs(logs)) == 2


# ---------------- 2. real transactions: the zero-SOL decodes ----------------
def test_real_doc_8_sol_buy_of_non_sol_quote_coin():
    tx = FIX["doc_8sol_nonsol_quote_buy"]
    old = [e for e in E.parse_logs(tx["meta"]["logMessages"]) if e["user"] == DOC]
    assert old and old[0]["sol_amount"] == 0                       # what v1.15 saw: "0.00 SOL"
    t = parse_wallet_trades(tx, DOC)
    assert len(t) == 1 and t[0]["is_buy"] and t[0]["mint"].startswith("4wqefE")
    assert 7.9 < t[0]["sol_amount"] / 1e9 < 8.1 and t[0]["quote_is_sol"] is False and t[0]["quote_mint"] == QUOTE


def test_real_fox_2_sol_jupiter_routed_buy():
    t = parse_wallet_trades(FIX["fox_2sol_jupiter_buy"], FOX)
    assert len(t) == 1 and t[0]["venue"] == "jupiter" and 1.95 < t[0]["sol_amount"] / 1e9 < 2.05
    assert t[0]["quote_is_sol"] is False and t[0]["ix_name"] == "buy_exact_quote_in"


def _tx(wallet, *, sol_delta, tok_delta, mint=MINT, logs=None, err=None, fee=5000, programs=("JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",)):
    pre = 10 * 10**9
    return {"transaction": {"message": {"accountKeys": [wallet, "11111111111111111111111111111111"]}},
            "meta": {"err": err, "fee": fee, "preBalances": [pre, 1], "postBalances": [pre + sol_delta - fee, 1],
                     "preTokenBalances": [], "postTokenBalances": [{"owner": wallet, "mint": mint, "accountIndex": 1,
                                                                     "uiTokenAmount": {"amount": str(tok_delta), "decimals": 6}}],
                     "logMessages": (logs or []) + [f"Program {p} invoke [1]" for p in programs], "innerInstructions": []}}


def test_txparse_router_buy_transfer_and_failed():
    t = parse_wallet_trades(_tx(SCOUT, sol_delta=-int(1.5e9), tok_delta=10**12), SCOUT)
    assert t[0]["is_buy"] and t[0]["sol_amount"] == int(1.5e9) and t[0]["venue"] == "jupiter" and t[0]["spot_from_fill"]
    assert parse_wallet_trades(_tx(SCOUT, sol_delta=0, tok_delta=10**12), SCOUT) == []          # airdrop/transfer
    assert parse_wallet_trades(_tx(SCOUT, sol_delta=-10**9, tok_delta=1, err={"x": 1}), SCOUT) == []


# ---------------- 3. source: triggers + tx fallback ----------------
def _src(monkeypatch, txs):
    src = src_mod.ScoutSource()
    got = []
    src.listeners.append(lambda w_, e, s, ms: got.append((w_.label, e["is_buy"], round(e["sol_amount"] / 1e9, 3), s, e.get("decode_path"))))
    calls = []

    async def call(method, params, **kw):
        calls.append(method)
        if method == "getTransaction":
            return txs.get(params[0])
        if method == "getSignaturesForAddress":
            return [{"signature": s, "err": None, "slot": 100} for s in txs]
        return None
    monkeypatch.setattr(src, "call", call)
    monkeypatch.setenv("SCOUT_LAMPORT_RESOLVE_DELAY_S", "0")
    return src, got, calls


def _drain(src):
    async def go():
        for _ in range(100):
            await asyncio.sleep(0.005)
            if not src._spawned:
                break
    return go()


def test_non_sol_quote_event_goes_through_tx_and_is_sized(monkeypatch):
    tx = FIX["doc_8sol_nonsol_quote_buy"]
    src, got, calls = _src(monkeypatch, {"sigD": tx})
    w = roster_mod.roster.by_wallet[DOC]

    async def go():
        src._on_logs(w.key, "sigD", tx["meta"]["logMessages"], 1, path="pump_uva")
        await _drain(src)
    run(go())
    assert got and got[0][0] == "The Doc" and 7.9 < got[0][2] < 8.1 and got[0][4] == "tx"
    assert calls == ["getTransaction"]
    rows = [json.loads(l) for l in Path(os.environ["SCOUT_DETECTIONS_PATH"]).read_text().splitlines()]
    assert rows[-1]["sig"] == "sigD" and rows[-1]["path"] == "pump_uva" and rows[-1]["quote_is_sol"] is False


def test_sol_quoted_event_decodes_from_logs_without_rpc(monkeypatch):
    src, got, calls = _src(monkeypatch, {})
    raw = base64.b64encode(E.encode_trade_event_for_tests(mint=MINT, user=SCOUT, sol_amount=10**9, token_amount=5,
                                                          v2=True, ix_name="buy_v3")).decode()
    src._on_logs(roster_mod.roster.by_wallet[SCOUT].key, "s1", ["Program data: " + raw], 1, path="pump_uva")
    assert got == [("Jijo", True, 1.0, "s1", "logs")] and calls == []


def test_lamport_trigger_catches_router_buy_and_dedupes(monkeypatch):
    txs = {"sigR": _tx(SCOUT, sol_delta=-2 * 10**9, tok_delta=10**12)}
    src, got, calls = _src(monkeypatch, txs)
    w = roster_mod.roster.by_wallet[SCOUT]

    async def go():
        src._on_account(w.wallet, 10 * 10**9, 99, 1)              # baseline
        src._on_account(w.wallet, 10 * 10**9 - 10_000, 99, 1)     # fee-sized change: ignored
        src._on_account(w.wallet, 8 * 10**9, 100, 2)              # 2 SOL out → resolve
        await _drain(src)
        src._on_account(w.wallet, 6 * 10**9, 100, 3)              # same sig again → already seen
        await _drain(src)
    run(go())
    assert got == [("Jijo", True, 2.0, "sigR", "tx")]
    assert src.stats["lamport_small"] == 1 and src.stats["lamport_triggers"] == 2 and src.stats["lamport_already_seen"] == 1


def test_roster_sub_keys_and_sharding():
    r = roster_mod.roster
    ks = r.sub_keys(r.by_wallet[SCOUT])
    assert [k[2] for k in ks] == ["pump_uva", "amm_uva", "wallet"] and ks[1][0] == roster_mod.amm_uva_pda(SCOUT)
    fomo = r.add("2fg5QD1eD7rzNNCsvnhmXFm5hqNgwTTG8p7kQ6f3rx6f", "fomo:x", "fomo")
    assert [k[2] for k in r.sub_keys(fomo)] == ["pump_uva"]
    src = src_mod.ScoutSource()
    src._sync_subs()
    assert src._sub_msg(1, SCOUT)["method"] == "accountSubscribe"
    assert src._sub_msg(1, r.by_wallet[SCOUT].amm_key)["method"] == "logsSubscribe"


# ---------------- 4. engine: no permanent lock on decode/transient gaps ----------------
async def _on(ev_, w_addr=SCOUT, sig="sigA"):
    Eng = eng_mod.engine
    Eng.on_trade(roster_mod.roster.by_wallet[w_addr], ev_, sig, int(time.time() * 1000) - 10_000)
    for _ in range(80):
        await asyncio.sleep(0.01)
        if not Eng._tasks:
            break
    return Eng


def _ev(**kw):
    e = {"kind": "pump_trade", "mint": MINT, "sol_amount": 10**9, "token_amount": 34_000_000 * 10**6, "is_buy": True,
         "user": SCOUT, "virtual_sol_reserves": VSR0 + 10**9, "virtual_token_reserves": VTR0 - 34_000_000 * 10**6,
         "real_sol_reserves": 10**9, "real_token_reserves": 700_000_000 * 10**6, "creator": CREATOR, "quote_is_sol": True}
    e.update(kw)
    return e


def test_undecodable_first_event_does_not_lock_mint():
    Eng = run(_on(_ev(sol_amount=0)))                      # unsized buy: ignored, mint not decided
    assert (ARM_A, MINT) not in Eng.decided and Eng.stats["decode_incomplete"] == 1
    Eng = run(_on(_ev(), sig="sigB"))
    assert Eng.signals[0]["decision"] == "enter"


def test_retryable_no_sol_usd_then_enters(monkeypatch):
    from app.fomo_onchain import market
    monkeypatch.setitem(market._cache, market.WSOL_MINT, {})
    monkeypatch.setattr(eng_mod, "_mcap_usd", lambda spot: asyncio.sleep(0, result=None))
    Eng = run(_on(_ev()))
    assert Eng.signals[0]["decision"] == "skip" and Eng.signals[0]["reasons"] == ["no SOL/USD price yet"]
    assert (ARM_A, MINT) not in Eng.decided and Eng.retries[MINT] == 1
    monkeypatch.setitem(market._cache, market.WSOL_MINT, {"price_usd": 150.0, "_t": time.time()})
    Eng = run(_on(_ev(), sig="sigC"))
    assert Eng.signals[0]["decision"] == "enter"


def test_non_sol_quote_buy_detected_sized_and_skipped_with_clear_reason():
    Eng = run(_on({"kind": "wallet_trade", "mint": MINT, "sol_amount": 8 * 10**9, "token_amount": 10**12, "is_buy": True,
                   "user": DOC, "quote_is_sol": False, "quote_mint": QUOTE, "price_sol": 8e-6, "spot_sol": 8e-6,
                   "spot_from_fill": True, "decode_path": "tx", "venue": "pump_curve"}, w_addr=DOC))
    s = Eng.signals[0]
    # v1.16.1: the non-SOL path now runs real gates; this fixture's curve is SOL-quoted → quote mismatch → skip
    assert s["decision"] == "skip" and s["scout_sol"] == 8.0 and any("non-SOL" in r for r in s["reasons"])
    c = _cands()[-1]
    assert c["arm"] == ARM_A and c["features"]["quote_is_sol"] is False and c["features"]["scout"] == "The Doc"


def test_router_buy_reads_curve_and_enters(iso):
    Eng = run(_on({"kind": "wallet_trade", "mint": MINT, "sol_amount": 10**9, "token_amount": 34 * 10**12,
                   "is_buy": True, "user": SCOUT, "quote_is_sol": None, "price_sol": 3e-8, "spot_sol": 3e-8,
                   "spot_from_fill": True, "decode_path": "tx", "venue": "jupiter"}))
    assert Eng.signals[0]["decision"] == "enter" and Eng.signals[0]["venue"] == "jupiter"
    iso["curve"] = None
    eng_mod.engine.__init__()
    Eng = run(_on({"kind": "wallet_trade", "mint": MINT, "sol_amount": 10**9, "token_amount": 1, "is_buy": True,
                   "user": SCOUT, "quote_is_sol": None, "price_sol": 1e-6, "decode_path": "tx", "venue": "pumpswap"}))
    assert any("not on a pump.fun curve" in r for r in Eng.signals[0]["reasons"])


# ---------------- 5. data hygiene ----------------
def test_recap_filter():
    from app.ingest.recap import recap_reason
    assert recap_reason("🚀 $Babem did 3.8X from our call 🚀\n📈 MC $11K → $42K\n💰 1 SOL → 3.8 SOL (~$443)")
    assert recap_reason("$X up 5x from our call") and recap_reason("Huge: did 12x")
    for real in ("😏 SMUG CALL\n\n$Lens\nGhY5\n\n💊 MC ~$24K  ·  Liq $12K", "🚀 LAUNCH\n\n$LOOT\n4X8b\n\nproven deployer"):
        assert recap_reason(real) is None


def test_ingest_drops_smugcalls_recap(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    called = []
    monkeypatch.setattr(eng_mod.engine, "on_smugcall", lambda *a, **k: called.append(1) or {"queued": True})
    r = TestClient(app).post("/api/ingest/buy", json={"trader_id": "smug", "token_address": MINT,
                                                      "source": "smugcalls/telegram",
                                                      "meta": {"snippet": "🚀 $Babem did 3.8X from our call 🚀"}})
    assert r.json()["filtered"] == "recap_post" and not called
    assert _cands()[-1]["excluded"] == "recap_post"


def test_excluded_rows_and_restart_gap_are_left_out_of_stats():
    p = Path(os.environ["SCOUT_LOG_PATH"])
    rows = [{"ts_ms": 1, "kind": "signal", "arm": ARM_A, "mint": "MintAAAA1111111111111111111111111111111pump",
             "decision": "enter", "excluded": "test_fixture"},
            {"ts_ms": 2, "kind": "signal", "arm": ARM_A, "mint": MINT, "decision": "skip"}]
    base = dict(arm=ARM_A, rule="hard_stop_35", lat_s=2, mint=MINT, signal_id="x", entry_ms=1, entry_sol=0.1, tokens=1,
                entry_price_sol=1.0, ref_price_sol=1.0, scout_wallet=None, status="closed", sol_out=0.2)
    rows.append({"ts_ms": 3, "kind": "pos", "pos": {**base, "id": "gap1", "flags": ["restart_gap"]}})
    rows.append({"ts_ms": 4, "kind": "pos", "pos": {**base, "id": "ok1", "sol_out": 0.05}})
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))
    eng_mod.engine.__init__()
    eng_mod.engine.load()
    rep = eng_mod.engine.report()
    assert rep["arms"][ARM_A].get("enter", 0) == 0 and rep["hygiene"]["excluded_rows_on_load"] == {"test_fixture": 1}
    row = next(r for r in rep["rows"] if r["arm"] == ARM_A and r["rule"] == "hard_stop_35" and r["lat_s"] == 2)
    assert row["closed"] == 1 and row["restart_gap_excluded"] == 1 and row["win_rate"] == 0.0


def test_latency_timelines_survive_restart():
    from app.pump import fast_lane as F
    F._timelines.clear()
    t = F.new_timeline(MINT, source="fluffy/telegram", event_id="e1", meta={"tg_message_ms": 1_700_000_000_000})
    F.mark(t, "curve_read", took_ms=80)
    t["status"] = "done"
    F._persist(t)
    F._timelines.clear()
    F._persist_loaded = False
    got = F.recent(5)
    assert got and got[0]["mint"] == MINT and got[0]["restored"] is True and got[0]["ingest_to_curve_ms"] is not None


def test_arm_c_blocked_without_sol_usd(monkeypatch):
    from app.fomo_onchain import market
    monkeypatch.setitem(market._cache, market.WSOL_MINT, {})
    monkeypatch.setattr(market, "marks", lambda mints, **k: asyncio.sleep(0, result={}))

    async def go():
        eng_mod.engine.on_smugcall(MINT, recv_ms=int(time.time() * 1000), message_ms=None, meta={"snippet": "😏 SMUG CALL"})
        for _ in range(50):
            await asyncio.sleep(0.01)
            if not eng_mod.engine._tasks:
                break
    run(go())
    s = eng_mod.engine.signals[0]
    assert s["arm"] == ARM_C and s["decision"] == "skip" and "no SOL/USD price yet" in s["reasons"]


def test_tests_never_write_real_data():
    import conftest
    from app.paths import data_dir
    from app.pump import paper
    from app.learning import candidates, outcomes
    from app.scout import detections, audit
    assert data_dir() == conftest.TEST_DATA_DIR
    for p in (paper.LOG_PATH, eng_mod.LOG_PATH, filt_mod.PATH, candidates.path(), outcomes.path(), detections.path(),
              audit.result_path()):
        assert str(p).startswith(str(conftest.TEST_DATA_DIR)), p
    with pytest.raises(PermissionError):
        conftest._guard("open", (str(conftest.REAL_DATA_DIR / "scout_trades.jsonl"), "a", 0))
    with pytest.raises(PermissionError):
        conftest._guard("os.replace", (str(conftest.TEST_DATA_DIR / "x"), str(conftest.REAL_DATA_DIR / "x")))
    conftest._guard("open", (str(conftest.REAL_DATA_DIR / "scout_wallets.json"), "r", 0))   # reads are fine


# ---------------- 6. learning log ----------------
def test_candidate_log_normalizes_reason_and_tracks():
    from app.learning import candidates as C
    from app.learning.outcomes import tracker
    tracker.items.clear()
    cid = C.log_candidate(arm="zyrox", source="zyrox/telegram", mint=MINT, decision="skip",
                          reasons=["token age 900s > 600s"], features={"real_sol": 1.234567891234}, ref_price_sol=1e-8,
                          detect_ms=1000, decision_ms=4000)
    r = _cands()[-1]
    assert r["cid"] == cid and r["skip_reason"] == "token age #s > #s" and r["latency_ms"] == 3000
    from app import __version__; assert r["version"] == __version__ and cid in tracker.items and tracker.items[cid]["due"] == [0, 60, 300, 900, 3600]


def test_outcome_tracker_records_prices_and_missed(monkeypatch):
    from app.learning.outcomes import OutcomeTracker, path
    Path(path()).unlink(missing_ok=True)
    t = OutcomeTracker()

    px = {"v": 2e-8}

    async def prices(mints):
        return {m: {"price_sol": px["v"], "src": "curve"} for m in mints}
    monkeypatch.setattr(t, "prices", prices)
    monkeypatch.setattr(t, "_holders", lambda *a: asyncio.sleep(0))
    t.items["c1"] = {"mint": MINT, "t0_ms": 1_000_000, "ref": 1e-8, "due": [0, 60, 3600], "quote_is_sol": True, "p0": None}
    run(t.step(now_ms=1_000_000 + 1_000))
    px["v"] = 3e-8
    run(t.step(now_ms=1_000_000 + 61_000))
    run(t.step(now_ms=1_000_000 + 3600_000 + 2_000_000))
    rows = [json.loads(l) for l in Path(path()).read_text().splitlines()]
    # ret is vs the +0 price seen by the same job (not the possibly-lagging decision-time ref price)
    assert [(r["horizon_s"], r["src"], r.get("ret"), r.get("ret_vs_ref")) for r in rows] == \
        [(0, "curve", 0.0, 1.0), (60, "curve", 0.5, 2.0), (3600, "missed", None, None)]
    assert "c1" not in t.items


def test_scorecard_wilson_and_newer_data_only(monkeypatch):
    from app.learning import scorecard as S
    from app.learning.outcomes import path as opath
    assert S.wilson(0, 0) is None and S.wilson(5, 10) == [0.2366, 0.7634]
    now = int(time.time() * 1000)
    cp, op = Path(os.environ["CANDIDATES_PATH"]), Path(opath())
    cands, outs = [], []
    for i in range(25):
        cid = f"k{i}"
        cands.append({"ts_ms": now - 1000 * i, "cid": cid, "version": "1.16.0-demo", "arm": "scout_solo", "mint": MINT,
                      "decision": "skip", "entered": False, "skip_reason": "mcap $# ≥ $#"})
        outs.append({"cid": cid, "kind": "outcome", "horizon_s": 900, "ret": 0.5 if i % 2 else -0.2, "src": "curve",
                     "price_sol": 1e-8, "ret_base": "p0"})
    cands.append({"ts_ms": now, "cid": "old", "version": "1.15.0-demo", "arm": "scout_solo", "mint": MINT,
                  "decision": "enter", "entered": True})
    cp.write_text("".join(json.dumps(r) + "\n" for r in cands))
    op.write_text("".join(json.dumps(r) + "\n" for r in outs))
    s = S.summarize(days=7)
    assert s["auto_tune"] is False and s["candidates"] == 25
    g = s["by_skip_reason"]["scout_solo|mcap $# ≥ $#"]
    assert g["n"] == 25 and not g["insufficient"] and g["pos_rate_15m"] == 0.48 and g["pos_rate_15m_ci95"]
    out = subprocess.run([sys.executable, "scripts/scorecard.py", "--json"], capture_output=True, text=True,
                         cwd=Path(__file__).resolve().parents[1], env=dict(os.environ), timeout=60)
    assert out.returncode == 0 and json.loads(out.stdout)["candidates"] == 25


# ---------------- 7. audit + endpoints ----------------
def test_audit_compare_and_coverage(monkeypatch):
    from app.scout import audit as A, detections as D
    truth = [{"sig": "a", "label": "Fox", "sol": 2.0}, {"sig": "b", "label": "Fox", "sol": 8.0},
             {"sig": "c", "label": "R4", "sol": 1.0}]
    r = A.compare(truth, {"a": {"sol": 1.99}, "b": {"sol": 0.0}})
    assert r["detection_rate"] == round(2 / 3, 4) and r["sized_ok_rate"] == round(1 / 3, 4)
    assert r["missed"][0]["sig"] == "c" and r["mis_sized"][0]["sig"] == "b"
    Path(D.path()).unlink(missing_ok=True)
    monkeypatch.setenv("SCOUT_HEARTBEAT_S", "100")
    for ts in (1_000_000, 1_100_000, 1_200_000, 5_000_000):
        Path(D.path()).open("a").write(json.dumps({"ts_ms": ts, "kind": "heartbeat"}) + "\n")
    cov = D.coverage(0, 6_000_000)
    assert cov == [(1_000_000, 1_200_000), (5_000_000, 5_000_000)] and D.covered(1_150_000, cov)
    assert not D.covered(3_000_000, cov)


def test_audit_health_warning(monkeypatch):
    from app.scout.audit import Auditor
    a = Auditor()
    a.loaded = True
    a.last = {"at_ms": int(time.time() * 1000), "truth_buys": 10, "detected": 5, "sized_ok": 4,
              "detection_rate": 0.5, "sized_ok_rate": 0.4}
    h = a.health()
    assert any("detection rate 50%" in w for w in h["warnings"]) and len(h["warnings"]) == 2
    a.last.update(truth_buys=2)
    assert a.health()["warnings"] == []                     # below the minimum sample: no alarm


def test_endpoints_health_audit_learning():
    from fastapi.testclient import TestClient
    from app.main import app
    c = TestClient(app)
    h = c.get("/health").json()
    from app import __version__; assert h["version"] == __version__ and isinstance(h["warnings"], list) and "detection" in h["scout"]
    a = c.get("/api/scout/audit").json()
    assert a["ok"] and a["read_only"] and a["run_request"] is None
    s = c.get("/api/learning/summary").json()
    assert s["ok"] and s["scorecard"]["auto_tune"] is False
    assert c.get("/api/learning/candidates?limit=5").json()["ok"]


def test_scorecard_rebases_legacy_rows():
    from app.learning import candidates, scorecard
    from app.learning.outcomes import path as out_path
    now = int(time.time() * 1000)
    cid = candidates.log_candidate(arm="legacy_arm", source="t", mint=MINT, decision="skip", reasons=["x"],
                                   ref_price_sol=1e-8, track=False)
    with Path(out_path()).open("a") as fh:
        for h, px, ret in ((0, 4e-8, 3.0), (60, 6e-8, 5.0)):
            fh.write(json.dumps({"ts_ms": now, "cid": cid, "kind": "outcome", "horizon_s": h, "price_sol": px,
                                 "src": "jupiter", "ret": ret}) + "\n")
    c = [x for x in scorecard.load(now - 60_000, "1.16.0-demo") if x["cid"] == cid][0]
    assert c["out"] == {0: 0.0, 60: 0.5}


def test_audit_on_demand_run_starts(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.scout.audit import auditor
    async def fake_run(*, window_h=None, trigger="manual"):
        return {}
    monkeypatch.setattr(auditor, "run", fake_run)
    monkeypatch.setattr(auditor, "load", lambda: None)
    monkeypatch.setattr(auditor, "last", None)
    r = TestClient(app).get("/api/scout/audit?run=true&window_h=6").json()   # no lifespan: no live/scout tasks
    assert r["run_request"] == {"started": True}
    # (v1.16.0 shipped this endpoint as a sync def → request() had no running loop: "no event loop")


def test_mark_excluded_script_is_non_destructive(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("mx", Path(__file__).resolve().parents[1] / "scripts" / "v116_mark_excluded.py")
    mx = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mx)
    rows = [{"kind": "signal", "arm": ARM_A, "mint": "MintAAAA1111111111111111111111111111111pump", "id": "t"},
            {"kind": "signal", "arm": ARM_C, "mint": MINT, "id": "r", "snippet": "$Babem did 3.8X from our call"},
            {"kind": "pos", "pos": {"id": "p1", "signal_id": "r", "mint": MINT, "flags": []}},
            {"kind": "pos", "pos": {"id": "p2", "signal_id": "z", "mint": MINT, "flags": []}},
            {"kind": "pos", "pos": {"id": "p2", "signal_id": "z", "mint": MINT, "flags": ["restart_gap"]}},
            {"kind": "signal", "arm": ARM_C, "mint": MINT, "id": "ok", "snippet": "😏 SMUG CALL"}]
    (tmp_path / "scout_trades.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (tmp_path / "paper_trades.jsonl").write_text(json.dumps({"ts_ms": int(time.time() * 1000) - 5 * 3600_000,
                                                             "kind": "open", "id": "o1", "mint": MINT}) + "\n")
    (tmp_path / "live_trades.jsonl").write_text("x\n")
    mx.main(["--data-dir", str(tmp_path), "--apply"])
    got = [json.loads(l) for l in (tmp_path / "scout_trades.jsonl").read_text().splitlines()]
    assert len(got) == len(rows)
    assert [g.get("excluded") for g in got] == ["test_fixture", "recap_post", "recap_post", "restart_gap", "restart_gap", None]
    assert (tmp_path / "archive" / "20261007-fomoant" / "scout_trades.jsonl").read_text().count("excluded") == 0
    paper = [json.loads(l) for l in (tmp_path / "paper_trades.jsonl").read_text().splitlines()]
    assert paper[0]["kind"] == "open" and paper[-1]["excluded"] == "restart_gap" and paper[-1]["id"] == "o1"
    assert (tmp_path / "live_trades.jsonl").read_text() == "x\n"
    with pytest.raises(SystemExit):
        mx.archive(tmp_path, "live_trades.jsonl", "t")
