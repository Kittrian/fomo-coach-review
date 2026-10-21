"""v1.14 live auto-trade: caps, kill switch, dedupe, exit engine, sell escalation, persistence, auth, refusals."""
from __future__ import annotations

import asyncio
import os
import stat
import time

import pytest
from fastapi.testclient import TestClient

from app.live import auth, config as live_config, engine as eng_mod, jupiter
from app.live.engine import LiveEngine, LivePos, arm_for_telegram, evaluate_exit

MINT = "MintAAAA1111111111111111111111111111111pump"
MINT2 = "MintBBBB1111111111111111111111111111111pump"


@pytest.fixture
def live_env(monkeypatch, tmp_path):
    """Both flags on, steering config, private tmp state, fake keypair."""
    env = {"JUPITER_EXEC_ENABLED": "true", "LIVE_AUTOTRADE_ENABLED": "true",
           "LIVE_ARMS": "scout_solo,scout_confirmed,fluffy_pump_dump,zyrox",
           "LIVE_SIZE_SOL_SCOUT_SOLO": "0.10", "LIVE_SIZE_SOL_SCOUT_CONFIRMED": "0.10",
           "LIVE_SIZE_SOL_FLUFFY_PUMP_DUMP": "0.40", "LIVE_SIZE_SOL_ZYROX": "0.40",
           "LIVE_MAX_SOL_PER_TRADE": "0.40", "LIVE_MAX_OPEN_POSITIONS": "3", "LIVE_MAX_TRADES_PER_DAY": "10",
           "LIVE_DAILY_LOSS_LIMIT_SOL": "0.60", "LIVE_MIN_SOL_RESERVE": "0.50",
           "LIVE_STATE_PATH": str(tmp_path / "state.json"), "LIVE_LEDGER_PATH": str(tmp_path / "ledger.jsonl"),
           "LIVE_KILL_FILE": str(tmp_path / "KILL"), "LIVE_SELL_RETRY_S": "0",
           "LIVE_SELL_STEP_BACKOFF_S": "0"}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(jupiter, "pubkey", lambda: "FakePubkey111")

    async def mesh_ok(self, mint):                     # v1.15: buys wait for the mesh — tests answer "ok"
        return True, "mesh ok"
    monkeypatch.setattr(LiveEngine, "_mesh_check", mesh_ok)
    return tmp_path


def _pos(**kw) -> LivePos:
    base = dict(mint=MINT, arm="scout_solo", size_sol=0.1, spent_lamports=100_000_000, tokens_initial=1_000_000_000,
                tokens_left=1_000_000_000, entry_price_sol=0.0001, entry_ms=int(time.time() * 1000))
    base.update(kw)
    return LivePos(**base)


# ---------------- refusal when flags off ----------------
def test_refuses_when_flags_off(monkeypatch, tmp_path):
    monkeypatch.setenv("LIVE_STATE_PATH", str(tmp_path / "s.json"))
    e = LiveEngine()
    spawned = []
    monkeypatch.setattr(e, "_spawn", lambda c: (spawned.append(c), c.close()))

    async def go():
        return e.on_signal("scout_solo", MINT)
    d = asyncio.run(go())
    assert d["decision"] == "refused"
    assert "JUPITER_EXEC_ENABLED=false" in d["reasons"] and "LIVE_AUTOTRADE_ENABLED=false" in d["reasons"]
    assert not spawned and MINT not in e.bought_mints
    assert e.summary()["buys_enabled"] is False


def test_refuses_with_only_exec_flag(live_env, monkeypatch):
    monkeypatch.setenv("LIVE_AUTOTRADE_ENABLED", "false")
    ok, why, _ = LiveEngine().check_caps("scout_solo", MINT)
    assert not ok and why == ["LIVE_AUTOTRADE_ENABLED=false"]


# ---------------- caps ----------------
def test_caps_arms_and_sizes(live_env):
    e = LiveEngine()
    ok, why, size = e.check_caps("scout_solo", MINT)
    assert ok and size == pytest.approx(0.10)
    assert e.check_caps("zyrox", MINT)[2] == pytest.approx(0.40)
    assert e.check_caps("fluffy_pump_dump", MINT)[2] == pytest.approx(0.40)
    for paper_only in ("smugcalls_control", "fomo_top_trader"):
        ok, why, _ = e.check_caps(paper_only, MINT)
        assert not ok and any("not in LIVE_ARMS" in w for w in why)


def test_cap_max_sol_per_trade(live_env, monkeypatch):
    monkeypatch.setenv("LIVE_SIZE_SOL_ZYROX", "2.0")
    assert LiveEngine().check_caps("zyrox", MINT)[2] == pytest.approx(0.40)


def test_cap_max_open_positions(live_env):
    e = LiveEngine()
    for i in range(3):
        p = _pos(mint=f"M{i}")
        e.positions[p.id] = p
    ok, why, _ = e.check_caps("scout_solo", MINT)
    assert not ok and any("max open positions" in w for w in why)
    e.positions.clear()
    e.inflight.update({"a", "b", "c"})               # in-flight buys count too
    assert not e.check_caps("scout_solo", MINT)[0]


def test_cap_trades_per_day(live_env):
    e = LiveEngine()
    e.day()["trades"] = 10
    ok, why, _ = e.check_caps("scout_solo", MINT)
    assert not ok and any("max trades/day" in w for w in why)


def test_cap_daily_loss_limit_realized_and_unrealized(live_env):
    e = LiveEngine()
    e.day()["realized_pnl_sol"] = -0.61
    ok, why, _ = e.check_caps("scout_solo", MINT)
    assert not ok and any("daily loss limit" in w for w in why)
    e.day()["realized_pnl_sol"] = -0.30
    p = _pos(size_sol=0.4, spent_lamports=400_000_000, last_price_sol=0.000025)   # −0.30 unrealized
    e.positions[p.id] = p
    assert not e.check_caps("scout_solo", MINT2)[0]
    e.day()["realized_pnl_sol"] = -0.20
    assert e.check_caps("scout_solo", MINT2)[0]


def test_dedupe_forever_and_inflight(live_env, monkeypatch):
    e = LiveEngine()
    monkeypatch.setattr(e, "_spawn", lambda c: c.close())

    async def go():
        a = e.on_signal("scout_solo", MINT)
        b = e.on_signal("scout_confirmed", MINT)
        return a, b
    a, b = asyncio.run(go())
    assert a["decision"] == "buy" and b["decision"] == "refused"
    assert any("dedupe" in w for w in b["reasons"])
    e.inflight.clear()                               # even after the buy finishes, the mint stays burned
    assert not e.check_caps("zyrox", MINT)[0]
    assert e.day()["trades"] == 1


def test_confirmer_annotates_held_position(live_env):
    e = LiveEngine()
    e.bought_mints[MINT] = {"arm": "scout_solo"}
    p = _pos(trader_wallets=["ScoutW"])
    e.positions[p.id] = p
    e.on_signal("scout_confirmed", MINT, trader_wallets=["CentedW"])
    assert "also:scout_confirmed" in p.flags and "CentedW" in p.trader_wallets


# ---------------- kill switch ----------------
def test_kill_file_blocks_buys(live_env):
    e = LiveEngine()
    (live_env / "KILL").write_text("")
    ok, why, _ = e.check_caps("scout_solo", MINT)
    assert not ok and "kill switch active" in why
    assert e.resume()["note"].startswith("KILL file still present")
    os.remove(live_env / "KILL")
    assert e.check_caps("scout_solo", MINT)[0]


def test_kill_api_and_resume(live_env):
    e = LiveEngine()
    e.kill()
    assert not e.check_caps("scout_solo", MINT)[0]
    e2 = LiveEngine(); e2.load()                      # kill survives restart
    assert e2.killed()["api"] is True
    e.resume()
    assert e.check_caps("scout_solo", MINT)[0]


def test_kill_sell_all_exit():
    p = _pos()
    cfg = live_config.config()
    assert evaluate_exit(p, 0.0001, p.entry_ms + 1000, cfg, live_config.arm_rules("scout_solo"), kill_sell_all=True) \
        == [("sell_all", 1.0, "kill_switch_sell_all")]


# ---------------- exit engine ----------------
def _cfg():
    return {**live_config.config(), "tp_mult": 2.0, "tp_frac": 0.5, "trail_pct": 0.30, "min10_s": 600,
            "hard_stop_pct": 0.35, "max_hold_s": 3600}


def test_exit_rules():
    rules = live_config.arm_rules("scout_solo")
    t0 = int(time.time() * 1000)
    p = _pos(entry_ms=t0)
    c = _cfg()
    c["candle_stop_enabled"] = False           # v1.33: legacy rule set (candle stop covered in test_v133_live)
    assert evaluate_exit(p, 0.00015, t0 + 5_000, c, rules) == []
    assert evaluate_exit(p, 0.00020, t0 + 5_000, c, rules) == [("sell_frac", 0.5, "tp_2x")]
    assert evaluate_exit(p, 0.000065, t0 + 5_000, c, rules) == [("sell_all", 1.0, "hard_stop")]
    p.tp_done, p.high_price_sol = True, 0.0004
    assert evaluate_exit(p, 0.00027, t0 + 5_000, c, rules) == [("sell_all", 1.0, "trail")]
    assert evaluate_exit(p, 0.00035, t0 + 5_000, c, rules) == []
    q = _pos(entry_ms=t0)
    assert evaluate_exit(q, 0.00009, t0 + 601_000, c, rules) == [("sell_all", 1.0, "below_entry_min10")]
    r = _pos(entry_ms=t0)
    assert evaluate_exit(r, 0.00011, t0 + 601_000, c, rules) == [] and r.min10_checked
    assert evaluate_exit(r, 0.00009, t0 + 700_000, c, rules) == []          # min-10 check is one-shot
    assert evaluate_exit(r, None, t0 + 3_600_000, c, rules) == [("sell_all", 1.0, "max_hold")]
    s = _pos(entry_ms=t0, trader_sold=True)
    assert evaluate_exit(s, None, t0 + 1000, c, rules) == [("sell_all", 1.0, "trader_sold")]


def test_hard_stop_and_max_hold_cannot_be_removed(monkeypatch):
    monkeypatch.setenv("LIVE_EXIT_RULES_ZYROX", "tp2x_trail")
    r = live_config.arm_rules("zyrox")
    assert "hard_stop" in r and "max_hold" in r and "trader_sells" not in r


def test_trader_sell_marks_only_tracked_wallet(live_env, monkeypatch):
    e = LiveEngine()
    monkeypatch.setattr(e, "_spawn", lambda c: c.close())
    p = _pos(trader_wallets=["ScoutW"])
    e.positions[p.id] = p
    e.note_trader_sell("OtherW", MINT)
    assert not p.trader_sold
    e.note_trader_sell("ScoutW", MINT)
    assert p.trader_sold


def _fake_swap(results, calls):
    async def swap(**kw):
        calls.append(kw)
        return results.pop(0)
    return swap


def test_sell_escalates_slippage_then_closes(live_env, monkeypatch):
    e = LiveEngine()
    p = _pos()
    e.positions[p.id] = p
    calls = []
    fail = {"ok": False, "stage": "execute", "error": "slippage", "ambiguous": False}
    ok = {"ok": True, "stage": "execute", "signature": "SIG123", "total_in": 1_000_000_000, "total_out": 150_000_000}
    monkeypatch.setattr(jupiter, "swap", _fake_swap([dict(fail), dict(fail), ok], calls))

    async def bal(mint):
        return 1_000_000_000, 6
    monkeypatch.setattr(e, "token_balance", bal)
    asyncio.run(e._sell(p, 1.0, "hard_stop"))
    assert [c["slippage_bps"] for c in calls] == [2500, 4000, 5000]      # v1.15: capped at 5000 bps
    assert p.status == "closed" and p.exit_reason == "hard_stop"
    assert e.day()["realized_pnl_sol"] == pytest.approx(0.05)
    a = e.alerts[0]
    assert a["kind"] == "live_sell" and a["solscan"] == "https://solscan.io/tx/SIG123"


def test_sell_all_fail_keeps_exit_pending_and_retries(live_env, monkeypatch):
    e = LiveEngine()
    p = _pos()
    e.positions[p.id] = p
    calls = []
    monkeypatch.setattr(jupiter, "swap", _fake_swap([{"ok": False, "error": "x"}] * 3 +
                                                     [{"ok": True, "signature": "S2", "total_in": 10**9, "total_out": 5 * 10**7}],
                                                     calls))

    async def bal(mint):
        return 10**9, 6
    monkeypatch.setattr(e, "token_balance", bal)
    asyncio.run(e._sell(p, 1.0, "trader_sold"))
    assert p.status == "open" and p.exit_pending == "trader_sold" and p.sell_failures == 1
    assert e.alerts[0]["kind"] == "live_sell_failed"
    asyncio.run(e.manage(p))                          # retry path (LIVE_SELL_RETRY_S=0)
    assert p.status == "closed" and len(calls) == 4


def test_tp_partial_failure_does_not_become_full_exit(live_env, monkeypatch):
    e = LiveEngine()
    p = _pos(last_price_sol=0.00021)
    e.positions[p.id] = p
    monkeypatch.setattr(jupiter, "swap", _fake_swap([{"ok": False, "error": "x"}] * 4, []))

    async def bal(mint):
        return 10**9, 6
    monkeypatch.setattr(e, "token_balance", bal)
    asyncio.run(e.manage(p))
    assert p.exit_pending is None and p.tp_done is False and p.status == "open"


def test_tp_partial_then_trail(live_env, monkeypatch):
    e = LiveEngine()
    p = _pos(last_price_sol=0.00021, high_price_sol=0.00021)
    e.positions[p.id] = p
    calls = []
    monkeypatch.setattr(jupiter, "swap", _fake_swap([
        {"ok": True, "signature": "A", "total_in": 500_000_000, "total_out": 105_000_000},
        {"ok": True, "signature": "B", "total_in": 500_000_000, "total_out": 70_000_000}], calls))

    async def bal(mint):
        return p.tokens_left, 6
    monkeypatch.setattr(e, "token_balance", bal)
    asyncio.run(e.manage(p))
    assert p.tp_done and p.tokens_left == 500_000_000 and p.status == "open"
    p.high_price_sol, p.last_price_sol = 0.0002, 0.00013
    asyncio.run(e.manage(p))
    assert p.status == "closed" and p.exit_reason == "trail" and calls[1]["amount"] == 500_000_000


def test_exec_off_sell_goes_pending_not_dropped(live_env, monkeypatch):
    monkeypatch.setenv("JUPITER_EXEC_ENABLED", "false")
    e = LiveEngine()
    p = _pos()
    e.positions[p.id] = p
    asyncio.run(e._sell(p, 1.0, "hard_stop"))
    assert p.status == "open" and p.exit_pending == "hard_stop"
    assert any("cannot sign" in w for w in e.summary()["warnings"])


# ---------------- buy path ----------------
def test_buy_creates_position_with_alert(live_env, monkeypatch):
    e = LiveEngine()
    calls = []
    monkeypatch.setattr(jupiter, "swap", _fake_swap([{"ok": True, "stage": "execute", "signature": "BUYSIG",
                                                       "total_in": 100_000_000, "total_out": 2_000_000_000_000,
                                                       "order": {"router": "metis"}}], calls))

    async def balance(**kw):
        return 2.5

    async def tok(mint):
        return 2_000_000_000_000, 6
    monkeypatch.setattr(e, "sol_balance", balance)
    monkeypatch.setattr(e, "token_balance", tok)
    monkeypatch.setattr(e, "_start_stream", lambda m: None)
    monkeypatch.setattr(e, "ensure_loop", lambda: None)

    async def go():
        d = e.on_signal("scout_solo", MINT, trader_wallets=["ScoutW"], ref_price_sol=4.5e-8)
        await asyncio.gather(*list(e._tasks))
        return d
    d = asyncio.run(go())
    assert d["decision"] == "buy" and calls[0]["amount"] == 100_000_000 and calls[0]["slippage_bps"] == 1500
    (p,) = e.open_positions()
    assert p.entry_price_sol == pytest.approx(5e-8) and p.trader_wallets == ["ScoutW"]
    assert p.meta["edge_gap_pct"] == pytest.approx(11.11, abs=0.01)
    assert e.alerts[0]["kind"] == "live_buy" and e.alerts[0]["solscan"].endswith("BUYSIG")
    assert MINT not in e.inflight


def test_buy_refunded_when_reserve_would_break(live_env, monkeypatch):
    e = LiveEngine()
    calls = []
    monkeypatch.setattr(jupiter, "swap", _fake_swap([], calls))

    async def balance(**kw):
        return 0.55                                   # 0.55 − 0.40 − fee < 0.50 reserve
    monkeypatch.setattr(e, "sol_balance", balance)

    async def go():
        e.on_signal("zyrox", MINT, meta={"tg_gate_passed": True})
        await asyncio.gather(*list(e._tasks))
    asyncio.run(go())
    assert not calls and MINT not in e.bought_mints and e.day()["trades"] == 0
    assert e.decisions[0]["decision"] == "aborted"


def test_buy_order_failure_refunds(live_env, monkeypatch):
    e = LiveEngine()
    monkeypatch.setattr(jupiter, "swap", _fake_swap([{"ok": False, "stage": "order", "error": "no route"}], []))

    async def balance(**kw):
        return 2.5
    monkeypatch.setattr(e, "sol_balance", balance)

    async def go():
        e.on_signal("scout_solo", MINT)
        await asyncio.gather(*list(e._tasks))
    asyncio.run(go())
    assert MINT not in e.bought_mints and not e.open_positions()


# ---------------- persistence ----------------
def test_state_persist_and_resume(live_env):
    e = LiveEngine()
    p = _pos(exit_pending="hard_stop")
    e.positions[p.id] = p
    e.bought_mints[MINT] = {"arm": "scout_solo"}
    e.day()["trades"] = 2
    e.save()
    e2 = LiveEngine(); e2.load()
    (q,) = e2.open_positions()
    assert q.mint == MINT and q.exit_pending == "hard_stop" and "resumed_after_restart" in q.flags
    assert MINT in e2.bought_mints and e2.day()["trades"] == 2


# ---------------- routing ----------------
def test_arm_for_telegram():
    assert arm_for_telegram("telegram", {"source_group": "Zyrox Insider Calls"}) == "zyrox"
    assert arm_for_telegram("telegram", {"source_group": "SolanaInsiderTerminaal"}) == "zyrox"
    assert arm_for_telegram("fluffy/telegram", {"source_group": "Fluffy's Pump & Dump"}) == "fluffy_pump_dump"
    assert arm_for_telegram("smugcalls/telegram", {"source_group": "SmugCalls"}) is None
    assert arm_for_telegram("telegram", {"source_group": "Some Other Group"}) is None


# ---------------- auth / endpoints ----------------
def test_token_file_is_0600():
    tok = auth.ensure_token()
    p = live_config.config()["token_path"]
    assert len(tok) >= 32 and stat.S_IMODE(os.stat(p).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(os.path.dirname(p)).st_mode) == 0o700


def test_live_endpoints_auth(monkeypatch):
    from app.main import app
    c = TestClient(app)
    assert c.get("/api/live/status").status_code == 403            # TestClient host is not loopback
    assert c.post("/api/exec/swap", json={"mint": MINT, "side": "buy"}).status_code == 403
    app.dependency_overrides[auth.require_localhost] = lambda: None
    try:
        assert c.get("/api/live/status").status_code == 401
        assert c.get("/api/live/status", headers={"X-Live-Token": "wrong"}).status_code == 401
        assert c.post("/api/exec/swap", json={"mint": MINT, "side": "buy"}).status_code == 401
        h = {"X-Live-Token": auth.ensure_token()}
        s = c.get("/api/live/status", headers=h).json()
        assert s["ok"] and s["buys_enabled"] is False and "caps" in s
        assert c.post("/api/live/kill", json={"sell_all": False}, headers=h).json()["kill"]["api"] is True
        assert c.post("/api/live/resume", headers=h).json()["kill"]["api"] is False
        assert c.get("/api/live/alerts", headers=h).json()["ok"]
        body = c.get("/health").json()["live"]
        assert body["buys_enabled"] is False and "token" not in str(body).lower()
    finally:
        app.dependency_overrides.clear()


def test_scout_enter_calls_live_router(monkeypatch):
    from app.scout import engine as scout_mod
    seen = []
    monkeypatch.setattr(eng_mod.engine, "on_signal", lambda arm, mint, **kw: seen.append((arm, mint, kw)) or {"decision": "refused"})
    se = scout_mod.ScoutEngine() if hasattr(scout_mod, "ScoutEngine") else scout_mod.engine
    se._record_signal("scout_solo", MINT, "enter", [], scout_wallet="ScoutW", scout_price=1e-7, detect_ms=1)
    assert seen and seen[0][0] == "scout_solo" and seen[0][2]["trader_wallets"] == ["ScoutW"]


# ---------------- v1.14.1 runner: only the trail exits a position that keeps going up ----------------
H = 3_600_000


def test_runner_ignores_max_hold_min10_and_trader_sells():
    rules = live_config.arm_rules("scout_solo")
    t0 = int(time.time() * 1000) - 5 * H
    p = _pos(entry_ms=t0, tp_done=True, high_price_sol=0.0003, trader_sold=True, tokens_left=500_000_000)
    now = t0 + 5 * H                                   # 5 h old, way past the 60-min max hold
    assert evaluate_exit(p, 0.00025, now, _cfg(), rules) == []          # 2.5x, 17% off high → hold
    assert evaluate_exit(p, None, now, _cfg(), rules) == []             # no price → keep monitoring, no timeout
    assert not p.min10_checked


def test_trail_exits_runner():
    rules = live_config.arm_rules("zyrox")
    t0 = int(time.time() * 1000) - 9 * H
    p = _pos(arm="zyrox", entry_ms=t0, tp_done=True, high_price_sol=0.001, tokens_left=125_000_000,
             ladder_done=["tp1_2x", "tp2_3x", "tp3_5x"])          # v1.15: ladder fully taken → trail only
    assert evaluate_exit(p, 0.00071, t0 + 9 * H, _cfg(), rules) == []
    assert evaluate_exit(p, 0.00069, t0 + 9 * H, _cfg(), rules) == [("sell_all", 1.0, "trail")]


def test_trail_armed_before_tp_fill_counts_as_runner():
    rules = live_config.arm_rules("scout_solo")
    t0 = int(time.time() * 1000) - 2 * H
    p = _pos(entry_ms=t0, high_price_sol=0.00025)       # high hit 2x but the TP partial hasn't filled yet
    assert evaluate_exit(p, 0.00021, t0 + 2 * H, _cfg(), rules) == [("sell_frac", 0.5, "tp_2x")]


def test_loser_and_flat_still_hit_max_hold_and_hard_stop():
    rules = live_config.arm_rules("scout_solo")
    t0 = int(time.time() * 1000) - H
    flat = _pos(entry_ms=t0, high_price_sol=0.00012)
    assert evaluate_exit(flat, 0.000105, t0 + H + 1000, _cfg(), rules) == [("sell_all", 1.0, "max_hold")]
    loser = _pos(entry_ms=t0)
    assert evaluate_exit(loser, 0.00006, t0 + 1000, _cfg(), rules) == [("sell_all", 1.0, "hard_stop")]
    # a former runner that fell below entry is no longer a runner → max hold applies again
    fell = _pos(entry_ms=t0, tp_done=True, high_price_sol=0.00011, tokens_left=500_000_000)
    assert evaluate_exit(fell, 0.000095, t0 + H + 1000, _cfg(), rules) == [("sell_all", 1.0, "max_hold")]
    # trader sells still exit a non-runner
    ts = _pos(entry_ms=int(time.time() * 1000), trader_sold=True)
    assert evaluate_exit(ts, 0.00012, ts.entry_ms + 1000, _cfg(), rules) == [("sell_all", 1.0, "trader_sold")]


def test_runner_optional_max_hold(monkeypatch):
    monkeypatch.setenv("LIVE_RUNNER_MAX_HOLD_MIN", "240")
    cfg = live_config.config()
    assert cfg["runner_max_hold_s"] == 240 * 60
    rules = live_config.arm_rules("scout_solo")
    t0 = int(time.time() * 1000) - 5 * H
    p = _pos(entry_ms=t0, tp_done=True, high_price_sol=0.0003)
    assert evaluate_exit(p, 0.00028, t0 + 3 * H, cfg, rules) == []
    assert evaluate_exit(p, 0.00028, t0 + 4 * H + 1, cfg, rules) == [("sell_all", 1.0, "runner_max_hold")]
    monkeypatch.delenv("LIVE_RUNNER_MAX_HOLD_MIN")
    assert live_config.config()["runner_max_hold_s"] == 0


def test_mesh_force_exit_spares_runner_unless_rug(live_env, monkeypatch):
    e = LiveEngine()
    sold = []
    monkeypatch.setattr(e, "_spawn", lambda c: (sold.append(1), c.close()))
    p = _pos(tp_done=True, high_price_sol=0.0003, last_price_sol=0.00028)
    e.positions[p.id] = p
    e.force_exit(MINT, "mesh_hard_block_after_entry", signals=["low liquidity"])
    assert not sold and any(f.startswith("ignored:mesh_hard_block") for f in p.flags)
    e.force_exit(MINT, "mesh_hard_block_after_entry", signals=["mint authority live"])   # v1.15: severe
    assert sold == [1]
    q = _pos(mint=MINT2)                                 # non-runner: any hard block exits
    e.positions[q.id] = q
    e.force_exit(MINT2, "mesh_hard_block_after_entry", signals=["mint authority live"])
    assert sold == [1, 1]


def test_runner_persists_and_resumes(live_env):
    e = LiveEngine()
    p = _pos(tp_done=True, high_price_sol=0.0004, tokens_left=500_000_000)
    e.positions[p.id] = p
    e.save()
    e2 = LiveEngine(); e2.load()
    (q,) = e2.open_positions()
    assert q.tp_done and q.high_price_sol == 0.0004 and q.tokens_left == 500_000_000
    assert e2.status()["positions"][0]["runner"] is True


def test_pinned_stream_skips_ttl_and_eviction(monkeypatch):
    from app.pump import trade_stream as ts
    mgr = ts.StreamManager() if hasattr(ts, "StreamManager") else type(ts.streams)()
    old = ts.MintStream(mint="RUNNER", started_ms=int(time.time() * 1000) - 10 * H, status="subscribed")
    mgr.streams["RUNNER"] = old
    mgr.pinned.add("RUNNER")
    assert mgr._alive(old) is True                     # past the 30-min TTL but pinned
    mgr.pinned.discard("RUNNER")
    assert mgr._alive(old) is False


def test_live_close_unpins_stream(live_env):
    from app.pump.trade_stream import streams
    e = LiveEngine()
    p = _pos()
    e.positions[p.id] = p
    streams.pinned.add(MINT)
    e._close(p, "trail")
    assert MINT not in streams.pinned


def test_scout_paper_runner_mirror(monkeypatch):
    from app.scout import engine as se
    eng = se.ScoutEngine()
    sells = []
    monkeypatch.setattr(eng, "_sell", lambda p, tokens, reason, *a: sells.append(reason))
    cfg = {**se.config(), "max_hold_s": 3600, "runner_max_hold_s": 0, "trail_pct": 0.30, "tp_mult": 2.0}
    now = int(time.time() * 1000)
    mk = lambda **kw: se.VPos(arm="scout_solo", rule="tp2x_trail", lat_s=2, mint=MINT, signal_id="s", entry_ms=now - 5 * H,
                             entry_sol=0.1, tokens=10**9, entry_price_sol=1e-7, ref_price_sol=None, scout_wallet="W",
                             tokens_left=5 * 10**8, **kw)
    runner = mk(tp_done=True, high_price=3e-7)
    eng._evaluate(runner, None, 2.5e-7, now, cfg)
    assert sells == []                                  # 5 h old runner: max hold ignored
    eng._evaluate(runner, None, 2.0e-7, now, cfg)
    assert sells == ["trail"]
    loser = mk(high_price=1.1e-7)
    eng._evaluate(loser, None, 0.9e-7, now, cfg)
    assert sells[-1] == "max_hold"


def test_paper_exit_signal_mirrors():
    from app.pump import exit_signals as xs
    now = int(time.time() * 1000)
    # fomo paper: once the trailing stop is the active stop, max hold + follow-trader are disabled
    cards = xs.evaluate_fomo(entry=1.0, price=1.5, high=1.6, stop_price=1.36, stop_kind="trailing_stop",
                             entry_ms=now - 10 * H, trader_sells=[{"sold_fraction": 1.0, "handle": "x"}], now_ms=now)
    assert not xs.fired(cards)
    cards = xs.evaluate_fomo(entry=1.0, price=0.95, high=1.05, stop_price=0.8, stop_kind="hard_stop",
                             entry_ms=now - 10 * H, trader_sells=[], now_ms=now)
    assert [c["kind"] for c in xs.fired(cards)] == ["max_hold"]
    # fluffy paper: after TP1 the runner exits on the trail (even below entry), not on max hold
    cards = xs.evaluate(trades=[], creator=None, price=1.6, high=2.2, entry_price=1.0, entry_ms=now - 5 * H,
                        real_sol=None, peak_real_sol=None, migrated=False, tp1_hit=True, now_ms=now)
    assert [c["kind"] for c in xs.fired(cards)] == ["trailing_stop"]
    cards = xs.evaluate(trades=[], creator=None, price=1.9, high=2.2, entry_price=1.0, entry_ms=now - 5 * H,
                        real_sol=None, peak_real_sol=None, migrated=False, tp1_hit=True, now_ms=now)
    assert not xs.fired(cards)
