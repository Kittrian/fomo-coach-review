"""v1.21: rolling 24h limit window (+ cutover floor), chase filter, scout benching, rug curve floor / creator cap,
fast cut (ships OFF) and the T30 / T30F paper exit variants. No network, no keypair, no live state."""
from __future__ import annotations

import asyncio
import json
import logging
import time

import httpx
import pytest

from app.live import bench as bench_mod, jupiter, pumpdirect as pd, rugcheck
from app.live.config import config
from app.live.engine import LiveEngine, LivePos, evaluate_exit
from app.scout import exit_variants as ev_mod

MINT = "MintAAAA1111111111111111111111111111111pump"
MINT2 = "MintBBBB1111111111111111111111111111111pump"
SCOUT = "4BdKaxN8G6ka4GYtQQWk4G4dZRUTX2vQH9GcXdBREFUk"
CONF = "9EhcDGGJ6NpetxNumZKwD1o1kuHBwvfNGKrmS6zekP7C"
H = 3600_000


@pytest.fixture
def live_env(monkeypatch, tmp_path):
    env = {"JUPITER_EXEC_ENABLED": "true", "LIVE_AUTOTRADE_ENABLED": "true",
           "LIVE_ARMS": "scout_solo,scout_confirmed,fluffy_pump_dump,zyrox",
           "LIVE_SIZE_SOL_SCOUT_SOLO": "0.20", "LIVE_SIZE_SOL_SCOUT_CONFIRMED": "0.20",
           "LIVE_MAX_SOL_PER_TRADE": "0.40", "LIVE_MAX_OPEN_POSITIONS": "3", "LIVE_MAX_TRADES_PER_DAY": "10",
           "LIVE_MAX_TRADES_PER_DAY_PROFIT": "20", "LIVE_PROFIT_EXTEND_ENABLED": "true",
           "LIVE_DAILY_LOSS_LIMIT_SOL": "0.60", "LIVE_MIN_SOL_RESERVE": "0.50",
           "LIVE_STATE_PATH": str(tmp_path / "state.json"), "LIVE_LEDGER_PATH": str(tmp_path / "ledger.jsonl"),
           "LIVE_KILL_FILE": str(tmp_path / "KILL"), "CANDIDATES_PATH": str(tmp_path / "candidates.jsonl"),
           "LIVE_BENCH_STATE_PATH": str(tmp_path / "bench.json"), "LIVE_BENCH_LOG_PATH": str(tmp_path / "bench.jsonl")}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    for k in ("LIVE_LIMIT_WINDOW", "LIVE_ROLLING_START", "LIVE_CHASE_FILTER_ENABLED", "LIVE_BENCH_ENABLED",
              "LIVE_FAST_CUT_ENABLED", "LIVE_RUG_CHECK_CURVE_FLOOR", "LIVE_RUG_CHECK_CREATOR_CAP"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(jupiter, "pubkey", lambda: "FakePubkey111")
    bench_mod.bench.__init__()                      # fresh singleton per test
    return tmp_path


def _rolling(monkeypatch, start="2020-01-01T00:00:00-05:00"):
    monkeypatch.setenv("LIVE_LIMIT_WINDOW", "rolling24h")
    monkeypatch.setenv("LIVE_ROLLING_START", start)


def _pos(**kw) -> LivePos:
    base = dict(mint=MINT, arm="scout_solo", size_sol=0.2, spent_lamports=200_000_000, tokens_initial=1_000_000_000,
                tokens_left=1_000_000_000, entry_price_sol=0.0002, entry_ms=int(time.time() * 1000))
    base.update(kw)
    return LivePos(**base)


def _trades(e, n, ago_h, pnl=None):
    now = int(time.time() * 1000)
    for i in range(n):
        e.window_events.append({"k": "t", "ts": now - int(ago_h * H) - i, "m": f"M{ago_h}-{i}"})
    if pnl is not None:
        e.window_events.append({"k": "p", "ts": now - int(ago_h * H), "m": "x", "pnl": pnl})


# ================= rolling 24h window =================
def test_config_defaults_calendar_and_off(live_env):
    c = config()
    assert c["limit_window"] == "calendar" and c["rolling_start_ms"] == 0
    assert c["chase_filter_enabled"] is False and c["fast_cut_enabled"] is False
    assert c["max_chase_pct"] == 5 and c["fast_cut_pct"] == 0.15 and c["fast_cut_window_s"] == 30
    assert bench_mod.cfg()["enabled"] is False and bench_mod.cfg()["loss_streak"] == 5
    rc = rugcheck.cfg()
    assert rc["checks"]["curve_floor"] is False and rc["checks"]["creator_cap"] is False
    assert rc["min_curve_sol"] == 5 and rc["creator_max_pct"] == 10


def test_rolling_start_parses_ct_offset(live_env, monkeypatch):
    _rolling(monkeypatch, "2026-10-09T00:00:00-05:00")
    assert config()["rolling_start_ms"] == 1791522000000          # 2026-10-09 05:00 UTC


def test_calendar_mode_unchanged(live_env):
    e = LiveEngine()
    _trades(e, 15, 1)                                # rolling events must be ignored in calendar mode
    e.day()["trades"] = 9
    assert e.check_caps("scout_solo", MINT)[0]
    tc = e.trade_cap()
    assert tc["window"] == "calendar" and tc["display"] == "9/10" and "next_slot_frees" not in tc
    e.day()["trades"] = 10
    e.day()["realized_pnl_sol"] = -0.01
    ok, why, _ = e.check_caps("scout_solo", MINT)
    assert not ok and any(w.startswith("max trades/day 10") and "rolling" not in w for w in why)
    e.day()["realized_pnl_sol"] = -0.7
    assert any("halted for today" in w for w in e.check_caps("scout_solo", MINT)[1])


def test_rolling_counts_last_24h_only(live_env, monkeypatch):
    _rolling(monkeypatch)
    e = LiveEngine()
    _trades(e, 6, 30)                                # older than 24 h → aged out
    _trades(e, 9, 2)
    w = e.limit_window()
    assert w["window"] == "rolling24h" and w["trades"] == 9
    assert e.check_caps("scout_solo", MINT)[0]
    monkeypatch.setenv("LIVE_PROFIT_EXTEND_ENABLED", "false")
    e.day()["trades"] = 0                            # calendar counter irrelevant in rolling mode
    _trades(e, 1, 1)
    ok, why, _ = e.check_caps("scout_solo", MINT)
    assert not ok and any(x.startswith("max trades/day 10 (rolling 24h)") for x in why)


def test_rolling_cap_blocks_and_next_slot_frees(live_env, monkeypatch):
    _rolling(monkeypatch)
    monkeypatch.setenv("LIVE_PROFIT_EXTEND_ENABLED", "false")
    e = LiveEngine()
    _trades(e, 4, 20)
    _trades(e, 6, 3)
    ok, why, _ = e.check_caps("scout_solo", MINT)
    assert not ok and any("(rolling 24h)" in x and "next slot frees" in x for x in why)
    tc = e.trade_cap()
    oldest = min(ev["ts"] for ev in e.window_events)
    assert tc["window"] == "rolling24h" and tc["next_slot_frees_ms"] == oldest + 24 * H
    assert tc["next_slot_frees"].endswith("CT") and tc["window_start"].endswith("CT")


def test_rolling_trades_age_out(live_env, monkeypatch):
    _rolling(monkeypatch)
    monkeypatch.setenv("LIVE_PROFIT_EXTEND_ENABLED", "false")
    e = LiveEngine()
    _trades(e, 10, 23.9)
    assert not e.check_caps("scout_solo", MINT)[0]
    later = int(time.time() * 1000) + int(0.2 * H)
    assert e.limit_window(now_ms=later)["trades"] == 0


def test_cutover_floor(live_env, monkeypatch):
    now = int(time.time() * 1000)
    from datetime import datetime, timezone
    floor = datetime.fromtimestamp((now - 2 * H) / 1000, timezone.utc).isoformat()
    _rolling(monkeypatch, floor)
    e = LiveEngine()
    _trades(e, 8, 5, pnl=-0.5)                       # before the cutover → ignored
    _trades(e, 2, 1)
    w = e.limit_window()
    assert w["trades"] == 2 and w["realized_pnl_sol"] == 0 and w["start_ms"] == now - 2 * H or abs(w["start_ms"] - (now - 2 * H)) < 2000


def test_loss_limit_rolling_and_recovers(live_env, monkeypatch):
    _rolling(monkeypatch)
    e = LiveEngine()
    _trades(e, 1, 23.5, pnl=-0.65)
    ok, why, _ = e.check_caps("scout_solo", MINT)
    assert not ok and any("daily loss limit hit" in x and "rolling 24h" in x for x in why)
    e.window_events.append({"k": "p", "ts": int(time.time() * 1000), "m": "y", "pnl": 0.10})  # P&L recovers
    assert e.check_caps("scout_solo", MINT)[0]
    e.window_events.pop()
    assert e.limit_window(now_ms=int(time.time() * 1000) + H)["realized_pnl_sol"] == 0       # loss ages out


def test_loss_limit_still_counts_unrealized(live_env, monkeypatch):
    _rolling(monkeypatch)
    e = LiveEngine()
    _trades(e, 1, 1, pnl=-0.55)
    p = _pos(mint=MINT2, last_price_sol=0.0001)      # −0.10 unrealized
    e.positions[p.id] = p
    assert any("daily loss limit" in x for x in e.check_caps("scout_solo", MINT)[1])


def test_profit_extension_rolling(live_env, monkeypatch):
    _rolling(monkeypatch)
    e = LiveEngine()
    _trades(e, 10, 2, pnl=0.01)
    ok, why, _ = e.check_caps("scout_solo", MINT)
    assert ok, why
    assert e.trade_cap()["extension"] == "unlocked" and e.trade_cap()["display"] == "10/20"
    e.window_events.append({"k": "p", "ts": int(time.time() * 1000), "m": "z", "pnl": -0.02})
    ok, why, _ = e.check_caps("scout_solo", MINT)
    assert not ok and e.trade_cap()["extension"] == "blocked"
    e.day()["realized_pnl_sol"] = 5.0                # calendar P&L must not unlock in rolling mode
    assert not e.check_caps("scout_solo", MINT)[0]


def _sig(e, monkeypatch, *a, **kw):
    async def nobuy(*x, **y):
        return None
    monkeypatch.setattr(e, "_buy", nobuy)

    async def go():
        return e.on_signal(*a, **kw)
    return asyncio.run(go())


def test_on_signal_refund_and_sell_feed_window(live_env, monkeypatch):
    _rolling(monkeypatch)
    e = LiveEngine()
    d = _sig(e, monkeypatch, "scout_solo", MINT, meta={})
    assert d["decision"] == "buy", d
    assert e.limit_window()["trades"] == 1
    e._refund(MINT, "test")
    assert e.limit_window()["trades"] == 0
    p = _pos()
    e.positions[p.id] = p
    e._record_sell(p, p.tokens_left, 150_000_000, "hard_stop", 0, "SIG", {})
    assert e.limit_window()["realized_pnl_sol"] == pytest.approx(-0.05)


def test_window_events_persist_and_backfill(live_env, monkeypatch, tmp_path):
    _rolling(monkeypatch)
    e = LiveEngine()
    e.loaded = True
    _trades(e, 3, 1, pnl=-0.1)
    e.save()
    st = json.loads((tmp_path / "state.json").read_text())
    assert len(st["window_events"]) == 4
    e2 = LiveEngine()
    e2.load()
    assert e2.limit_window()["trades"] == 3
    st.pop("window_events")                          # pre-v1.21 state: backfill from bought_mints + sells
    now = int(time.time() * 1000)
    st["bought_mints"] = {MINT: {"arm": "scout_solo", "ts_ms": now - H, "size_sol": 0.2}}
    (tmp_path / "state.json").write_text(json.dumps(st))
    e3 = LiveEngine()
    e3.load()
    assert e3.limit_window()["trades"] == 1


def test_summary_and_trade_cap_log_mention_window(live_env, monkeypatch, caplog):
    _rolling(monkeypatch)
    e = LiveEngine()
    _trades(e, 10, 1)
    with caplog.at_level(logging.INFO, logger="coach.live"):
        e.check_caps("scout_solo", MINT)
    assert any("live trade cap [rolling24h]" in r.getMessage() for r in caplog.records)
    s = e.summary()
    assert s["trade_cap"]["window"] == "rolling24h" and s["limit_window"]["trades"] == 10
    assert "scout_bench" in s and "chase_filter" in s and s["fast_cut"]["enabled"] is False


# ================= chase filter =================
def _chase_on(monkeypatch):
    monkeypatch.setenv("LIVE_CHASE_FILTER_ENABLED", "true")
    monkeypatch.setenv("LIVE_MAX_CHASE_PCT", "15")


def test_chase_check_math(live_env, monkeypatch, tmp_path):
    _chase_on(monkeypatch)
    e = LiveEngine()
    c = config()
    q = {"in_amount": 200_000_000, "out_amount": 1_000_000_000_000}   # 0.2 SOL / 1e6 tokens = 2e-7
    assert e._chase_check("scout_solo", MINT, 2e-7, q, c)["ok"]
    assert e._chase_check("scout_solo", MINT, 1.8e-7, q, c)["ok"]          # +11.1 %
    r = e._chase_check("scout_solo", MINT, 1.7e-7, q, c)                   # +17.6 %
    assert not r["ok"] and "chase filter" in r["why"] and r["premium_pct"] == pytest.approx(17.65, abs=0.05)
    assert e._chase_check("fluffy_pump_dump", MINT, 1e-7, q, c) is None    # not a chase arm
    assert e._chase_check("scout_solo", MINT, None, q, c) is None
    assert e._chase_check("scout_solo", MINT, 2e-9, q, c)["ok"]            # nonsense ratio → never blocks
    rows = [json.loads(x) for x in (tmp_path / "chase_skips.jsonl").read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["mint"] == MINT and rows[0]["premium_pct"] > 15
    monkeypatch.setenv("LIVE_CHASE_FILTER_ENABLED", "false")
    assert e._chase_check("scout_solo", MINT, 1e-7, q, config()) is None


class Jup:
    def __init__(self, monkeypatch, out_amount):
        self.exec_calls = 0

        async def order(**kw):
            return {"ok": True, "transaction": "TX", "request_id": "RID", "router": "metis",
                    "in_amount": kw.get("amount"), "out_amount": out_amount}

        async def simulate(signed, router, *, rpc_url="", client=None):
            return {"ok": True, "err": None}

        async def execute(signed, rid, client=None):
            self.exec_calls += 1
            return {"ok": True, "status": "Success", "signature": "SIG", "total_in": 200_000_000,
                    "total_out": out_amount, "ambiguous": False}
        monkeypatch.setattr(jupiter, "order", order)
        monkeypatch.setattr(jupiter, "sign_partial", lambda tx: "SIGNED")
        monkeypatch.setattr(jupiter, "simulate_signed", simulate)
        monkeypatch.setattr(jupiter, "execute", execute)
        monkeypatch.setattr(jupiter, "signature_of", lambda s: "SIG")


def _engine(monkeypatch):
    e = LiveEngine()

    async def mesh(self, mint):
        return True, "mesh ok"
    monkeypatch.setattr(LiveEngine, "_mesh_check", mesh)

    async def prebuy(mint, arm, *, creator=None):
        return {"ok": True, "refuse": [], "unknown": [], "checks": {}, "elapsed_ms": 1}
    monkeypatch.setattr(rugcheck.checker, "prebuy", prebuy)

    async def balance(**kw):
        return 2.5

    async def tok(mint):
        return 1_000_000_000_000, 6
    monkeypatch.setattr(e, "sol_balance", balance)
    monkeypatch.setattr(e, "token_balance", tok)
    monkeypatch.setattr(e, "_start_stream", lambda m: None)
    monkeypatch.setattr(e, "ensure_loop", lambda: None)
    return e


def _run(e, arm="scout_solo", ref=None, meta=None):
    async def go():
        e.on_signal(arm, MINT, trader_wallets=[SCOUT], ref_price_sol=ref, meta=meta or {})
        for _ in range(10):
            if not e._tasks:
                break
            await asyncio.gather(*list(e._tasks), return_exceptions=True)
    asyncio.run(go())


def test_chase_skip_on_jupiter_path_never_executes_and_refunds(live_env, monkeypatch):
    _chase_on(monkeypatch)
    j = Jup(monkeypatch, 1_000_000_000_000)                   # 2e-7 SOL/token
    e = _engine(monkeypatch)
    _run(e, ref=1.5e-7)                                       # +33 %
    assert j.exec_calls == 0 and not e.open_positions() and MINT not in e.bought_mints
    assert e.stats["chase_skipped"] == 1 and e.day()["trades"] == 0
    d = e.decisions[0]
    assert any("chase filter" in r for r in d["reasons"])


def test_chase_pass_on_jupiter_path_buys_and_records_meta(live_env, monkeypatch):
    _chase_on(monkeypatch)
    j = Jup(monkeypatch, 1_000_000_000_000)
    e = _engine(monkeypatch)
    _run(e, ref=1.9e-7)                                       # +5 %
    assert j.exec_calls == 1
    p = e.open_positions()[0]
    assert p.meta["chase"]["ok"] and p.meta["timing"]["chase_premium_pct"] == pytest.approx(5.26, abs=0.05)


def test_chase_off_buys_regardless(live_env, monkeypatch):
    j = Jup(monkeypatch, 1_000_000_000_000)
    e = _engine(monkeypatch)
    _run(e, ref=1e-7)
    assert j.exec_calls == 1 and "chase" not in e.open_positions()[0].meta


def test_chase_gate_receives_direct_curve_quote(monkeypatch, tmp_path):
    from test_v120_direct_pump import FakeRpc, _curve, _patch_state
    from solders.keypair import Keypair
    kp = Keypair()
    monkeypatch.setattr(jupiter, "keypair", lambda: kp)
    monkeypatch.setattr(jupiter, "pubkey", lambda: str(kp.pubkey()))
    monkeypatch.setenv("LIVE_DIRECT_LOG_PATH", str(tmp_path / "direct.jsonl"))
    rpc = FakeRpc()
    monkeypatch.setattr(pd, "_rpc", rpc)
    _patch_state(monkeypatch, _curve())
    seen = {}

    async def gate(age, quote=None):
        seen.update(quote or {})
        return False, "chase filter: test", False
    gate.wants_quote = True
    r = asyncio.run(pd.swap(side="buy", mint=MINT, amount=200_000_000, client=httpx.AsyncClient(), pre_execute=gate))
    assert r["stage"] == "gate" and not r.get("fallback") and "sendTransaction" not in [m for m, _ in rpc.calls]
    assert seen["in_amount"] == 200_000_000 and seen["out_amount"] > 0 and seen["path"] == "pump_direct"


def test_old_style_gate_still_called_without_quote(monkeypatch):
    calls = []

    async def gate(age):
        calls.append(age)
        return False, "no", False
    from test_v120_direct_pump import FakeRpc, _curve, _patch_state  # noqa: F401
    assert not getattr(gate, "wants_quote", False)


# ================= scout benching =================
def _bench_on(monkeypatch):
    monkeypatch.setenv("LIVE_BENCH_ENABLED", "true")


def _closed(pnl_sol, wallets=(SCOUT,), arm="scout_solo", meta=None, entry_ms=None):
    p = _pos(arm=arm, trader_wallets=list(wallets), meta=meta or {}, entry_ms=entry_ms or int(time.time() * 1000) + 5)
    p.tokens_left = 0
    p.received_lamports = int(200_000_000 + pnl_sol * 1e9)
    p.status = "closed"
    return p


def test_bench_after_five_losses_and_win_resets(live_env, monkeypatch):
    _bench_on(monkeypatch)
    b = bench_mod.bench
    b.load()
    for _ in range(4):
        b.record_live(_closed(-0.02))
    b.record_live(_closed(0.01))                               # win resets the streak
    assert not b.benched_of([SCOUT])
    for _ in range(4):
        b.record_live(_closed(-0.02))
    assert not b.benched_of([SCOUT])
    b.record_live(_closed(-0.02, wallets=(SCOUT,), meta={"confirmer_wallet": CONF}))
    assert b.benched_of([SCOUT]) == [SCOUT] and not b.benched_of([CONF])
    s = b.summary(short=True)
    assert s["benched_count"] == 1 and s["benched"][0]["wallet_full"] == SCOUT


def test_bench_only_counts_trades_after_start(live_env, monkeypatch):
    _bench_on(monkeypatch)
    b = bench_mod.bench
    b.load()
    for _ in range(6):
        assert b.record_live(_closed(-0.02, entry_ms=b.started_ms - 60_000)) is None
    assert not b.benched_of([SCOUT])


def test_bench_ignores_non_scout_arms_and_disabled(live_env, monkeypatch):
    b = bench_mod.bench
    assert b.record_live(_closed(-0.02)) is None              # disabled
    _bench_on(monkeypatch)
    b.load()
    for _ in range(6):
        b.record_live(_closed(-0.02, arm="fluffy_pump_dump"))
    assert not b.benched_of([SCOUT])


def test_benched_signal_refused_live_and_paper_copied(live_env, monkeypatch):
    _bench_on(monkeypatch)
    b = bench_mod.bench
    b.load()
    for _ in range(5):
        b.record_live(_closed(-0.02))
    e = LiveEngine()
    d = e.on_signal("scout_solo", MINT, trader_wallets=[SCOUT], ref_price_sol=1e-7, meta={})
    assert d["decision"] != "buy" and any("scout bench" in r for r in d["reasons"])
    assert e.day()["trades"] == 0 and e.stats["bench_refused"] == 1
    assert len(b.paper) == 1                                   # no loop → synchronous ref-price paper entry
    p = next(iter(b.paper.values()))
    assert "paper" in p.flags and p.entry_price_sol == pytest.approx(1e-7 * 1.077 * 1.015)
    # other scouts are unaffected
    d2 = _sig(e, monkeypatch, "scout_solo", MINT2, trader_wallets=[CONF], ref_price_sol=1e-7, meta={})
    assert d2["decision"] == "buy", d2


def test_confirmer_wallet_benched_blocks_confirmed_signal(live_env, monkeypatch):
    _bench_on(monkeypatch)
    b = bench_mod.bench
    b.load()
    for _ in range(5):
        b.record_live(_closed(-0.02, wallets=(CONF,)))
    e = LiveEngine()
    d = e.on_signal("scout_confirmed", MINT, trader_wallets=[SCOUT], ref_price_sol=1e-7,
                    meta={"confirmer_wallet": CONF})
    assert any("scout bench" in r for r in d["reasons"])


def _paper_close(b, mult):
    pid = next(iter(b.paper))
    p = b.paper[pid]
    now = p.entry_ms + 5_000
    p.last_price_sol = p.entry_price_sol * mult
    p.last_mark_ms = now
    p.high_price_sol = max(p.high_price_sol, p.last_price_sol)
    return b.manage(pid, now)


def test_unbench_after_two_paper_wins(live_env, monkeypatch):
    _bench_on(monkeypatch)
    b = bench_mod.bench
    b.load()
    for _ in range(5):
        b.record_live(_closed(-0.02))
    for i, mult in enumerate((0.5, )):                        # a paper loss (hard stop) keeps the wallet benched
        b.paper_open("scout_solo", f"M{i}", [SCOUT], 1e-7)
        assert _paper_close(b, mult) == "hard_stop"
    assert b.benched_of([SCOUT])
    b.paper_open("scout_solo", "W1", [SCOUT], 1e-7)
    p = next(iter(b.paper.values()))
    p.trader_sold = True                                       # forwarded scout sell exits the paper copy
    p.last_price_sol = p.entry_price_sol * 1.3
    p.last_mark_ms = p.entry_ms + 5_000
    assert b.manage(p.id, p.entry_ms + 5_000) == "trader_sold"
    assert b.benched_of([SCOUT])                               # one win so far
    b.paper_open("scout_solo", "W2", [SCOUT], 1e-7)
    b.note_trader_sell(SCOUT, "W2")
    p = next(iter(b.paper.values()))
    p.last_price_sol, p.last_mark_ms = p.entry_price_sol * 1.2, p.entry_ms + 5_000
    b.manage(p.id, p.entry_ms + 5_000)
    assert not b.benched_of([SCOUT])
    rows = [json.loads(x)["kind"] for x in (live_env / "bench.jsonl").read_text().splitlines()]
    assert "bench" in rows and "unbench" in rows and rows.count("paper_close") == 3


def test_manual_unbench_api_and_env(live_env, monkeypatch):
    _bench_on(monkeypatch)
    b = bench_mod.bench
    b.load()
    for _ in range(5):
        b.record_live(_closed(-0.02))
    assert b.unbench(SCOUT, "manual") and not b.benched_of([SCOUT])
    for _ in range(5):
        b.record_live(_closed(-0.02))
    assert b.benched_of([SCOUT])
    monkeypatch.setenv("LIVE_BENCH_UNBENCH", SCOUT)
    b2 = bench_mod.ScoutBench()
    b2.load()
    assert not b2.benched_of([SCOUT])


def test_bench_endpoints(live_env, monkeypatch):
    _bench_on(monkeypatch)
    from fastapi.testclient import TestClient
    from app.live import auth
    from app.main import app
    b = bench_mod.bench
    b.load()
    for _ in range(5):
        b.record_live(_closed(-0.02))
    c = TestClient(app)
    assert c.get("/api/live/bench").status_code == 403                 # loopback + token required
    app.dependency_overrides[auth.require_localhost] = lambda: None
    try:
        assert c.get("/api/live/bench").status_code == 401
        h = {"X-Live-Token": auth.ensure_token()}
        assert c.get("/api/live/bench", headers=h).json()["benched_count"] == 1
        assert c.post("/api/live/bench/unbench", json={"wallet": SCOUT}, headers=h).json()["unbenched"] is True
        assert c.get("/api/live/bench", headers=h).json()["benched_count"] == 0
    finally:
        app.dependency_overrides.pop(auth.require_localhost, None)


def test_exempt_wallet_never_benched(live_env, monkeypatch):
    _bench_on(monkeypatch)
    monkeypatch.setenv("LIVE_BENCH_EXEMPT", SCOUT)
    b = bench_mod.bench
    b.load()
    for _ in range(6):
        b.record_live(_closed(-0.02))
    assert not b.benched_of([SCOUT])


# ================= rug curve floor / creator cap =================
def _rc(**checks):
    c = rugcheck.cfg()
    c["checks"].update(checks)
    return c


def test_curve_floor_and_creator_cap_evaluate():
    base = {"launch": None, "holdings": None, "jup": None}
    c = _rc(curve_floor=True, creator_cap=True)
    r = rugcheck.evaluate({**base, "curve": {"on_curve": True, "complete": False, "quote_is_sol": True,
                                             "quote_sol": 1.54, "creator_holdings": {"Dev": 5 * 10**13}}}, c)
    assert r["curve_floor"]["status"] == "fail" and "1.54 SOL" in r["curve_floor"]["why"]
    assert r["creator_cap"]["status"] == "pass"                                   # 5 %
    r = rugcheck.evaluate({**base, "curve": {"on_curve": True, "complete": False, "quote_is_sol": True,
                                             "quote_sol": 13.0, "creator_holdings": {"Dev": 12 * 10**13}}}, c)
    assert r["curve_floor"]["status"] == "pass" and r["creator_cap"]["status"] == "fail"   # 12 %
    r = rugcheck.evaluate({**base, "curve": {"on_curve": False}}, c)
    assert r["curve_floor"]["status"] == "na" and r["creator_cap"]["status"] == "na"
    r = rugcheck.evaluate({**base, "curve": {"on_curve": True, "complete": True}}, c)
    assert r["curve_floor"]["status"] == "na"
    r = rugcheck.evaluate(base, c)
    assert r["curve_floor"]["status"] == "unknown" and r["creator_cap"]["status"] == "unknown"
    r = rugcheck.evaluate(base, rugcheck.cfg())                                   # off → not in the result
    assert "curve_floor" not in r and "creator_cap" not in r


def test_curve_check_gathers_and_refuses(monkeypatch):
    from app.pump.curve import CURVE_DISCRIMINATOR
    import base64
    import struct
    from app.pump.solana_pda import PUMP_PROGRAM_ID
    monkeypatch.setenv("LIVE_RUG_CHECK_CURVE_FLOOR", "true")
    monkeypatch.setenv("LIVE_RUG_CHECK_CREATOR_CAP", "true")
    for k in ("BUNDLE", "HOLDINGS", "TOP10", "DEV", "RUGGER"):
        monkeypatch.setenv(f"LIVE_RUG_CHECK_{k}", "false")
    data = bytearray(CURVE_DISCRIMINATOR) + bytearray(150)
    struct.pack_into("<QQQQQ", data, 8, 10**15, 31 * 10**9, 7 * 10**14, 1_540_000_000, 10**15)
    acc = {"owner": PUMP_PROGRAM_ID, "data": [base64.b64encode(bytes(data)).decode(), "base64"], "lamports": 1,
           "executable": False}
    rc = rugcheck.RugChecker()

    async def rpc(method, params):
        if method == "getAccountInfo":
            return {"value": acc}
        if method == "getMultipleAccounts":
            return {"value": [None, None]}
        raise AssertionError(method)
    monkeypatch.setattr(rc, "rpc", rpc)
    out = asyncio.run(rc.prebuy(MINT, "scout_solo"))
    assert not out["ok"] and any("curve_floor" in r for r in out["refuse"])
    assert out["checks"]["creator_cap"]["status"] == "pass"


def test_new_checks_unknown_never_fail_closed_tg(monkeypatch):
    monkeypatch.setenv("LIVE_RUG_CHECK_CURVE_FLOOR", "true")
    for k in ("BUNDLE", "HOLDINGS", "TOP10", "DEV", "RUGGER"):
        monkeypatch.setenv(f"LIVE_RUG_CHECK_{k}", "false")
    rc = rugcheck.RugChecker()

    async def rpc(method, params):
        return None
    monkeypatch.setattr(rc, "rpc", rpc)
    out = asyncio.run(rc.prebuy(MINT, "zyrox"))
    assert out["ok"] and "curve_floor" in out["unknown"]


# ================= fast cut =================
def _fc_pos(age_s, mult, high_mult=None, tp_done=False):
    now = int(time.time() * 1000)
    p = _pos(entry_ms=now - int(age_s * 1000), entry_price_sol=1.0, last_price_sol=mult,
             high_price_sol=high_mult or max(1.0, mult), tp_done=tp_done)
    return p, now


def test_fast_cut_off_by_default(live_env):
    c = config()
    p, now = _fc_pos(10, 0.80)
    assert evaluate_exit(p, 0.80, now, c, ["hard_stop", "trail", "tp_2x"]) == []


def test_fast_cut_on(live_env, monkeypatch):
    monkeypatch.setenv("LIVE_FAST_CUT_ENABLED", "true")
    c = config()
    rules = ["hard_stop", "trail", "tp_2x"]
    p, now = _fc_pos(10, 0.84)
    assert evaluate_exit(p, 0.84, now, c, rules) == [("sell_all", 1.0, "fast_cut")] and p.meta["fast_cut"]
    p, now = _fc_pos(10, 0.86)
    assert evaluate_exit(p, 0.86, now, c, rules) == []                    # only −14 %
    p, now = _fc_pos(40, 0.80)
    assert evaluate_exit(p, 0.80, now, c, rules) == []                    # outside the 30 s window
    p, now = _fc_pos(10, 0.60)
    assert evaluate_exit(p, 0.60, now, c, rules)[0][2] == "hard_stop"      # hard stop unchanged
    p, now = _fc_pos(10, 0.80, high_mult=2.1)                              # past 2x → never fast cut
    assert ("sell_all", 1.0, "fast_cut") not in evaluate_exit(p, 0.80, now, c, rules)


# ================= T30 / T30F paper variants =================
def _tr(v, **kw):
    return ev_mod.Tracker(signal_id="s", arm="scout_solo", mint=MINT, variant=v, lat_s=2, entry_ms=0,
                          entry_price_sol=1.0, high=1.0, **kw)


def _P():
    P = ev_mod.params()
    P.update(haircut=0.0, gap_pct=0.10)
    return P


def test_t30_ignores_pre2x_scout_sell_and_trails_from_peak():
    P = _P()
    a, t = _tr("A", trader_sold=True), _tr("T30", trader_sold=True)
    assert ev_mod.evaluate(a, 1.1, 1000, P) and a.exits[0]["reason"] == "trader_sold"
    assert not ev_mod.evaluate(t, 1.1, 1000, P)
    assert not ev_mod.evaluate(t, 1.6, 2000, P)                  # peak 1.6
    assert ev_mod.evaluate(t, 1.10, 3000, P)                      # below 1.6 × 0.70 = 1.12
    assert t.exits[0]["reason"] == "pre2x_trail_30" and t.exits[0]["mark_mult"] == 1.1


def test_t30_from_entry_and_post2x_normal():
    P = _P()
    t = _tr("T30")
    assert ev_mod.evaluate(t, 0.69, 1000, P) and t.exits[0]["reason"] == "pre2x_trail_30"   # 30 % from entry
    t = _tr("T30")
    ev_mod.evaluate(t, 2.0, 1000, P)                              # half at 2x
    assert t.tp_done and t.rem == pytest.approx(0.5)
    assert ev_mod.evaluate(t, 1.39, 2000, P) and t.exits[-1]["reason"] == f"trail_{round(P['trail_pct'] * 100)}"


def test_t30f_fast_cut_and_window():
    P = _P()
    f = _tr("T30F")
    assert ev_mod.evaluate(f, 0.84, 10_000, P) and f.exits[0]["reason"] == "fast_cut"
    t = _tr("T30")
    assert not ev_mod.evaluate(t, 0.84, 10_000, P)                # T30 has no fast cut
    f = _tr("T30F")
    assert not ev_mod.evaluate(f, 0.84, 40_000, P)                # outside 30 s → only the trail (0.70)


def test_t30_keeps_hard_stop():
    P = _P()
    t = _tr("T30")
    ev_mod.evaluate(t, 0.60, 1000, P)
    assert t.exits[0]["reason"] == "hard_stop"


def test_t30_variants_in_summary_and_paper_only():
    assert {"T30", "T30F"} <= set(ev_mod.VARIANTS) and "T30" in ev_mod.LABELS and "T30F" in ev_mod.LABELS
    import inspect
    src = inspect.getsource(ev_mod)
    assert "import jupiter" not in src and "pumpdirect" not in src and "sign" not in src.replace("signal", "")
