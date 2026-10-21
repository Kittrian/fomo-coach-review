"""v1.13 scout-wallet paper test (offline)."""
import asyncio
import base64
import time

import pytest

from app.pump.events import encode_trade_event_for_tests
from app.scout import ARM_A, ARM_B, ARM_C, SMUG_SOURCE
from app.scout import engine as eng_mod, filters as filt_mod, roster as roster_mod, source as src_mod

SCOUT = "4BdKaxN8G6ka4GYtQQWk4G4dZRUTX2vQH9GcXdBREFUk"
CONF = "CyaE1VxvBrahnPWkqm5VsdCvyS2QmNht2UFrKJHga54o"
MINT = "DwcXyhEcSvzWgakDKpetZLFvbAHutpGpzhbU4iempump"
CREATOR = "862TYSvRYoiHAK3F3WwTRYAfuGiQaGdxedN9AGvRGWo2"
VTR0, VSR0 = 1_073_000_000 * 10**6, 30 * 10**9


def curve(vsr=VSR0, vtr=VTR0, complete=False):
    return {"virtual_token_reserves": vtr, "virtual_quote_reserves": vsr, "real_token_reserves": 0 if complete else 793_100_000 * 10**6,
            "real_quote_reserves": max(0, vsr - VSR0), "complete": complete, "quote_is_sol": True,
            "creator": CREATOR, "read_at_ms": int(time.time() * 1000)}


def ev(user, *, sol=1.0, is_buy=True, vsr=VSR0 + 10**9, vtr=VTR0 - 34_000_000 * 10**6, rtr=700_000_000 * 10**6):
    return {"mint": MINT, "sol_amount": int(sol * 1e9), "token_amount": 34_000_000 * 10**6, "is_buy": is_buy,
            "user": user, "timestamp": int(time.time()), "virtual_sol_reserves": vsr, "virtual_token_reserves": vtr,
            "real_sol_reserves": vsr - VSR0, "real_token_reserves": rtr, "creator": CREATOR}


@pytest.fixture(autouse=True)
def iso(tmp_path, monkeypatch):
    monkeypatch.setattr(eng_mod, "LOG_PATH", tmp_path / "scout.jsonl")
    monkeypatch.setattr(filt_mod, "PATH", tmp_path / "f.json")
    monkeypatch.setenv("SCOUT_LATENCIES", "2,5")
    eng_mod.engine.__init__()
    filt_mod.filters.__init__()
    roster_mod.roster.__init__()
    roster_mod.roster.add(SCOUT, "Jijo", "scout")
    roster_mod.roster.add(CONF, "Cented", "confirmer")
    from app.fomo_onchain import market
    monkeypatch.setattr(market, "sol_usd", lambda: 150.0)

    async def age(mint, max_pages=3):
        return {"ok": True, "created_s": time.time() - 120, "first_sigs": ["c"] + [f"s{i}" for i in range(25)]}
    state = {"curve": curve()}

    async def rc(mint):
        return dict(state["curve"], read_at_ms=int(time.time() * 1000))

    async def rcs(mints):
        return {m: dict(state["curve"], read_at_ms=int(time.time() * 1000)) for m in mints}
    monkeypatch.setattr(src_mod.source, "token_age", age)
    monkeypatch.setattr(src_mod.source, "read_curve", rc)
    monkeypatch.setattr(src_mod.source, "read_curves", rcs)
    monkeypatch.setattr(eng_mod.engine, "scan_first_buyers", lambda mint: asyncio.sleep(0))
    yield state


def w(addr):
    return roster_mod.roster.by_wallet[addr]


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def test_roster_uses_volume_accumulator_and_skips_truncated(tmp_path):
    r = roster_mod.Roster()
    assert r.add(SCOUT, "Jijo", "scout").key == roster_mod.uva_pda(SCOUT) != SCOUT
    assert r.add("CyaE…a54o", "Cented", "confirmer") is None


def test_logs_decode_only_own_trades():
    got = []
    src = src_mod.ScoutSource()
    src.listeners.append(lambda w_, e, s, ms: got.append((w_.label, e["is_buy"])))
    key = w(SCOUT).key
    mine = base64.b64encode(encode_trade_event_for_tests(mint=MINT, user=SCOUT, sol_amount=10**9, token_amount=1,
                                                         creator=CREATOR)).decode()
    other = base64.b64encode(encode_trade_event_for_tests(mint=MINT, user=CONF, sol_amount=1, token_amount=1)).decode()
    src._on_logs(key, "sig1", ["Program data: " + other, "Program data: " + mine], 1)
    src._on_logs(key, "sig1", ["Program data: " + mine], 1)          # dup signature ignored
    assert got == [("Jijo", True)]


async def _signal_a(e_):
    E = eng_mod.engine
    rec_ms = int(time.time() * 1000) - 10_000            # detection in the past → fills run immediately
    E.on_trade(w(SCOUT), e_, "sigA", rec_ms)
    for _ in range(50):
        await asyncio.sleep(0.01)
        if not E._tasks:
            break
    return E


def test_arm_a_enters_two_latencies_four_rules_with_edge_gap():
    E = run(_signal_a(ev(SCOUT)))
    s = E.signals[0]
    assert s["arm"] == ARM_A and s["decision"] == "enter" and s["mcap_usd"] < 20_000
    ps = [p for p in E.positions.values() if p.arm == ARM_A]
    assert len(ps) == 8 and {p.lat_s for p in ps} == {2, 5} and {p.rule for p in ps} == set(eng_mod.RULES)
    assert all(p.edge_gap_pct is not None and p.entry_sol == 0.10 for p in ps)


@pytest.mark.parametrize("kw,reason", [({"sol": 0.2}, "scout buy"), ({"rtr": 0}, "migrated"),
                                        ({"vsr": 200 * 10**9}, "mcap")])
def test_arm_a_skip_filters(kw, reason):
    E = run(_signal_a(ev(SCOUT, **kw)))
    assert E.signals[0]["decision"] == "skip" and any(reason in r for r in E.signals[0]["reasons"])
    assert not E.positions


def test_scheduled_and_rugged_deployer_skip():
    filt_mod.filters.note_telegram("fluffy/telegram", MINT, {})
    E = run(_signal_a(ev(SCOUT)))
    assert any("scheduled-launch" in r for r in E.signals[0]["reasons"])
    eng_mod.engine.__init__()
    filt_mod.filters.__init__()
    now = int(time.time() * 1000)
    assert filt_mod.filters.note_outcome(creator=CREATOR, mint="Other111111111111111111111111111111111111111",
                                         peak_price=1.0, price=0.1, peak_ms=now, created_ms=now - 60_000,
                                         now_ms=now) == "dump"
    E = run(_signal_a(ev(SCOUT)))
    assert any("rug" in r for r in E.signals[0]["reasons"])


def test_arm_b_needs_second_wallet_within_60s(monkeypatch):
    monkeypatch.setenv("SCOUT_LATENCIES", "0,1")      # B detects "now" → fills at +0 s/+1 s in this test

    async def go():
        E = await _signal_a(ev(SCOUT))
        assert not [p for p in E.positions.values() if p.arm == ARM_B]
        E.on_trade(w(CONF), ev(CONF, sol=0.3), "sigB", int(time.time() * 1000) - 9_000)
        for _ in range(300):
            await asyncio.sleep(0.01)
            if not E._tasks:
                break
        return E
    E = run(go())
    assert len([p for p in E.positions.values() if p.arm == ARM_B]) == 8 and E.alerts and E.alerts[0]["confirmer"] == "Cented"


def test_exit_rules(iso):
    E = run(_signal_a(ev(SCOUT)))
    cfg = eng_mod.config()
    by = {(p.rule, p.lat_s): p for p in E.positions.values()}
    entry = by[("tp2x_trail", 2)].entry_price_sol
    # pump to ~2.2x → tp2x sells half; then dump 40% from high → trail; hard stop + min10 + scout_sells
    iso["curve"] = curve(vsr=int(VSR0 * 1.55), vtr=int(VTR0 / 1.55))
    run(E.mark_mints([MINT]))
    p = by[("tp2x_trail", 2)]
    assert p.tp_done and p.fills[0]["reason"] == "tp_2x" and p.status == "open"
    iso["curve"] = curve(vsr=int(VSR0 * 1.1), vtr=int(VTR0 / 1.1))
    run(E.mark_mints([MINT]))
    assert p.status == "closed" and p.exit_reason == "trail" and p.pnl()["pnl_sol"] > 0
    iso["curve"] = curve(vsr=int(VSR0 * 0.75), vtr=int(VTR0 / 0.75))
    run(E.mark_mints([MINT]))
    hs = by[("hard_stop_35", 2)]
    assert hs.status == "closed" and hs.exit_reason == "hard_stop" and hs.pnl()["pnl_pct"] < -35
    m10 = by[("min10_check", 2)]
    m10.entry_ms -= int(cfg["min10_s"] * 1000) + 1000
    run(E.mark_mints([MINT]))
    assert m10.status == "closed" and m10.exit_reason == "below_entry_min10"
    E.on_trade(w(SCOUT), ev(SCOUT, is_buy=False), "sigS", int(time.time() * 1000))
    run(E.mark_mints([MINT]))
    assert by[("scout_sells", 2)].exit_reason == "scout_sold"
    rows = {(r["arm"], r["rule"], r["lat_s"]): r for r in E.report()["rows"]}
    r = rows[(ARM_A, "hard_stop_35", 2)]
    assert r["closed"] == 1 and r["win_rate"] == 0 and r["avg_pnl_pct"] < -35 and r["median_edge_gap_pct"] is not None


def test_smugcalls_ingest_is_arm_c_only(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.pump import fast_lane

    def boom(*a, **k):
        raise AssertionError("fast lane must not run for SmugCalls")
    monkeypatch.setattr(fast_lane, "schedule", boom)
    monkeypatch.setattr("app.ingest.buy.schedule_enrichment", boom)
    called = {}
    monkeypatch.setattr(eng_mod.engine, "on_smugcall", lambda mint, **kw: called.setdefault("c", (mint, kw)) and
                        {"queued": True})
    r = TestClient(app).post("/api/ingest/buy", json={"trader_id": "tg:smugcalls", "token_address": MINT,
                                                       "source": SMUG_SOURCE, "meta": {"tg_recv_ms": 123}})
    assert r.status_code == 200 and r.json()["fast_lane_scheduled"] is False and called["c"][0] == MINT
    assert called["c"][1]["recv_ms"] == 123


def test_arm_c_simulates_from_curve():
    async def go():
        E = eng_mod.engine
        E.on_smugcall(MINT, recv_ms=int(time.time() * 1000) - 10_000, message_ms=None, meta={})
        for _ in range(50):
            await asyncio.sleep(0.01)
            if not E._tasks:
                break
        return E
    E = run(go())
    ps = [p for p in E.positions.values() if p.arm == ARM_C]
    assert len(ps) == 6 and "scout_sells" not in {p.rule for p in ps}       # no scout → 3 rules × 2 latencies


def test_gap_poller_recovers_missed_ws_trade(monkeypatch):
    src = src_mod.ScoutSource()
    got = []
    src.listeners.append(lambda w_, e, s, ms: got.append(s))
    raw = base64.b64encode(encode_trade_event_for_tests(mint=MINT, user=SCOUT, sol_amount=10**9, token_amount=1,
                                                        creator=CREATOR)).decode()
    calls = {"n": 0}

    async def fake(method, params):
        if method == "getSignaturesForAddress":
            calls["n"] += 1
            if params[0] != w(SCOUT).key:
                return []
            if calls["n"] <= 2:
                return [{"signature": "old", "blockTime": int(time.time())}]
            if calls["n"] >= 7:
                raise asyncio.CancelledError
            return [{"signature": "new", "blockTime": int(time.time())}]
        return {"meta": {"logMessages": ["Program data: " + raw]}}
    monkeypatch.setattr(src, "_poll_call", fake)
    with pytest.raises(asyncio.CancelledError):
        run(src._gap_poll())
    assert got == ["new"] and src.stats["poll_recovered"] == 1
