"""v1.19: profit-gated daily trade cap (10 → 20 when realized P&L >= 0) + fomo_top_paper (PAPER ONLY)."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

import pytest

from app.live import engine as eng_mod, jupiter
from app.live.engine import LiveEngine, LivePos

MINT = "MintAAAA1111111111111111111111111111111pump"
MINT2 = "MintBBBB1111111111111111111111111111111pump"
ZAKUM = "BXLEfLN8bHsQCn81rFZebcavP1JSory1XbSDoeYQxJPc"
NARU = "2L17Sw85Lh2Ca7ejEzWqyJsZ3yS4DxQhYr52X5H5xRns"
OTHER = "9EhcDGGJ6NpetxNumZKwD1o1kuHBwvfNGKrmS6zekP7C"


@pytest.fixture
def live_env(monkeypatch, tmp_path):
    env = {"JUPITER_EXEC_ENABLED": "true", "LIVE_AUTOTRADE_ENABLED": "true",
           "LIVE_ARMS": "scout_solo,scout_confirmed,fluffy_pump_dump,zyrox",
           "LIVE_SIZE_SOL_SCOUT_SOLO": "0.20", "LIVE_SIZE_SOL_SCOUT_CONFIRMED": "0.20",
           "LIVE_MAX_SOL_PER_TRADE": "0.40", "LIVE_MAX_OPEN_POSITIONS": "3", "LIVE_MAX_TRADES_PER_DAY": "10",
           "LIVE_MAX_TRADES_PER_DAY_PROFIT": "20", "LIVE_PROFIT_EXTEND_ENABLED": "true",
           "LIVE_DAILY_LOSS_LIMIT_SOL": "0.60", "LIVE_MIN_SOL_RESERVE": "0.50",
           "LIVE_STATE_PATH": str(tmp_path / "state.json"), "LIVE_LEDGER_PATH": str(tmp_path / "ledger.jsonl"),
           "LIVE_KILL_FILE": str(tmp_path / "KILL")}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(jupiter, "pubkey", lambda: "FakePubkey111")
    return tmp_path


def _pos(**kw) -> LivePos:
    base = dict(mint=MINT, arm="scout_solo", size_sol=0.2, spent_lamports=200_000_000, tokens_initial=1_000_000_000,
                tokens_left=1_000_000_000, entry_price_sol=0.0002, entry_ms=int(time.time() * 1000))
    base.update(kw)
    return LivePos(**base)


# ---------------- profit-gated trade cap ----------------
def test_at_base_cap_with_breakeven_or_profit_allows(live_env):
    e = LiveEngine()
    e.day()["trades"] = 10
    e.day()["realized_pnl_sol"] = 0.0                 # break-even counts
    ok, why, _ = e.check_caps("scout_solo", MINT)
    assert ok, why
    tc = e.trade_cap()
    assert tc["effective"] == 20 and tc["extension"] == "unlocked" and tc["display"] == "10/20"
    e.day()["realized_pnl_sol"] = 0.05
    assert e.check_caps("scout_solo", MINT)[0]


def test_at_base_cap_with_loss_blocks(live_env):
    e = LiveEngine()
    e.day()["trades"] = 10
    e.day()["realized_pnl_sol"] = -0.0001
    ok, why, _ = e.check_caps("scout_solo", MINT)
    assert not ok and any("max trades/day 10" in w for w in why)
    assert e.trade_cap()["extension"] == "blocked"


def test_at_extended_cap_blocks(live_env):
    e = LiveEngine()
    e.day()["trades"] = 20
    e.day()["realized_pnl_sol"] = 1.0
    ok, why, _ = e.check_caps("scout_solo", MINT)
    assert not ok and any("max trades/day 20" in w for w in why)
    assert e.trade_cap()["extension"] == "max_reached"


def test_disabled_flag_is_old_behavior(live_env, monkeypatch):
    monkeypatch.setenv("LIVE_PROFIT_EXTEND_ENABLED", "false")
    e = LiveEngine()
    e.day()["trades"] = 10
    e.day()["realized_pnl_sol"] = 0.5
    ok, why, _ = e.check_caps("scout_solo", MINT)
    assert not ok and "max trades/day 10" in why
    assert e.trade_cap()["extension"] == "off" and e.trade_cap()["effective"] == 10
    e.day()["trades"] = 9
    assert e.check_caps("scout_solo", MINT)[0]


def test_flag_absent_defaults_off(live_env, monkeypatch):
    monkeypatch.delenv("LIVE_PROFIT_EXTEND_ENABLED", raising=False)
    e = LiveEngine()
    e.day()["trades"] = 10
    assert not e.check_caps("scout_solo", MINT)[0]


def test_turns_negative_above_base_blocks_then_recovers(live_env):
    e = LiveEngine()
    e.day()["trades"] = 13
    e.day()["realized_pnl_sol"] = -0.02
    assert not e.check_caps("scout_solo", MINT)[0]
    e.day()["realized_pnl_sol"] = 0.0
    assert e.check_caps("scout_solo", MINT)[0]


def test_unrealized_is_ignored_for_extension(live_env):
    e = LiveEngine()
    e.day()["trades"] = 10
    e.day()["realized_pnl_sol"] = 0.01
    p = _pos(last_price_sol=0.0001)                   # −0.10 unrealized (below the 0.60 loss limit)
    e.positions[p.id] = p
    ok, why, _ = e.check_caps("scout_solo", MINT2)
    assert ok, why


def test_loss_limit_and_max_open_still_apply_when_extended(live_env):
    e = LiveEngine()
    e.day()["trades"] = 12
    e.day()["realized_pnl_sol"] = 0.0
    for i in range(3):
        q = _pos(mint=f"M{i}", last_price_sol=0.0002)
        e.positions[q.id] = q
    ok, why, _ = e.check_caps("scout_solo", MINT)
    assert not ok and any("max open positions" in w for w in why)


def test_below_base_reports_base_and_eligible(live_env):
    e = LiveEngine()
    e.day()["trades"] = 8
    e.day()["realized_pnl_sol"] = 0.0086
    tc = e.summary()["trade_cap"]
    assert tc["effective"] == 10 and tc["display"] == "8/10" and tc["extension"] == "eligible"
    e.day()["realized_pnl_sol"] = -0.01
    assert e.trade_cap()["extension"] == "not_eligible"


def test_logs_unlock_and_block_once(live_env, caplog):
    e = LiveEngine()
    e.day()["trades"] = 10
    with caplog.at_level(logging.INFO, logger="coach.live"):
        e.check_caps("scout_solo", MINT)
        e.check_caps("scout_solo", MINT)
        e.day()["realized_pnl_sol"] = -0.01
        e.check_caps("scout_solo", MINT)
    msgs = [r.getMessage() for r in caplog.records if "profit extension" in r.getMessage()]
    assert len(msgs) == 2 and "UNLOCKED" in msgs[0] and "BLOCKED" in msgs[1]


# ---------------- fomo_top_paper (PAPER ONLY) ----------------
@pytest.fixture
def paper_env(monkeypatch, tmp_path, live_env):
    import app.paper_fomo_top as ftp
    monkeypatch.setenv("PAPER_FOMO_TOP_ENABLED", "true")
    monkeypatch.setenv("PAPER_FOMO_TOP_LOG_PATH", str(tmp_path / "paper_fomo_top.jsonl"))
    monkeypatch.setenv("PAPER_FOMO_TOP_STATE_PATH", str(tmp_path / "paper_fomo_top_state.json"))
    monkeypatch.delenv("PAPER_FOMO_TOP_WALLETS", raising=False)

    def boom(*a, **k):
        raise AssertionError("paper arm must never reach the live executor / signer / Jupiter")
    for name in ("order", "execute", "swap", "sign_partial", "keypair", "simulate_order", "simulate_signed"):
        monkeypatch.setattr(jupiter, name, boom)
    monkeypatch.setattr(LiveEngine, "on_signal", boom)
    monkeypatch.setattr(LiveEngine, "_buy", boom)
    monkeypatch.setattr(LiveEngine, "_sell", boom)
    import app.jupiter_exec as jx
    monkeypatch.setattr(jx, "fetch_order", boom)

    p = ftp.PaperFomoTop()
    prices = {"px": 0.0001}

    async def price_sol(mint, force=True):
        return prices["px"], {"symbol": "TST", "price_usd": 0.02}

    class Rug:
        async def prebuy(self, mint, arm, creator=None):
            return {"ok": True, "refuse": [], "unknown": [], "na": [], "elapsed_ms": 5}

    async def mesh_ok(mint, timeout_s):
        return {"ok": True, "why": "mesh ok"}
    monkeypatch.setattr(p, "_price_sol", price_sol)
    monkeypatch.setattr(p, "_rugchecker", lambda: Rug())
    monkeypatch.setattr(ftp, "mesh_verdict", mesh_ok)
    monkeypatch.setattr(p, "ensure_loop", lambda: None)    # no network mark loop in tests
    return p, prices, tmp_path


def _rows(path: Path):
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


async def _drain(p):
    for _ in range(5):
        await asyncio.sleep(0)
        if p._tasks:
            await asyncio.gather(*list(p._tasks), return_exceptions=False)


def test_paper_buy_never_touches_live(paper_env):
    p, prices, tmp = paper_env
    live = LiveEngine()

    async def go():
        st = p.on_wallet_buy(ZAKUM, "zakum", {"mint": MINT, "usd": 100.0, "fill_price_usd": 0.02}, signature="S1",
                             block_ms=int(time.time() * 1000), detect_lag_ms=1500)
        await _drain(p)
        return st
    assert asyncio.run(go()) == "entering"
    assert len(p.book) == 1
    b = next(iter(p.book.values()))
    assert b["meta"]["would_live_buy"] is True and b["meta"]["entry_src"] == "dexscreener"
    lt = b["tracks"]["live_exit"]
    assert lt.size_sol == pytest.approx(0.20) and lt.arm == "fomo_top_paper"
    # live engine untouched: no positions, no trades counted, caps unchanged
    assert live.positions == {} and live.day()["trades"] == 0 and live.check_caps("scout_solo", MINT2)[0]
    rows = _rows(tmp / "paper_fomo_top.jsonl")
    assert any(r["kind"] == "start" for r in rows) and any(r["kind"] == "open" and r["paper"] for r in rows)


def test_paper_ignores_non_targets_and_respects_off_switch(paper_env, monkeypatch):
    p, _, _ = paper_env
    assert p.on_wallet_buy(OTHER, "lordarbiter", {"mint": MINT}) is None
    monkeypatch.setenv("PAPER_FOMO_TOP_ENABLED", "false")
    assert p.on_wallet_buy(ZAKUM, "zakum", {"mint": MINT}) == "inactive"
    assert not p.book


def test_paper_window_seven_days(paper_env):
    p, _, _ = paper_env
    assert p.active()
    assert p.end_ms - p.start_ms == 7 * 86400 * 1000
    assert not p.active(now_ms=p.end_ms + 1)


def test_paper_live_exit_tp_trail_and_follow_wallet(paper_env):
    p, prices, tmp = paper_env

    async def go():
        p.on_wallet_buy(NARU, "NotARandomUser", {"mint": MINT, "usd": 50.0}, signature="S2")
        await _drain(p)
        pid = next(iter(p.book))
        b = p.book[pid]
        e = b["tracks"]["live_exit"].entry_price_sol

        def mark(px):
            for t in b["tracks"].values():
                if t.status == "open":
                    t.last_price_sol, t.last_mark_ms = px, int(time.time() * 1000)
                    t.high_price_sol = max(t.high_price_sol or px, px)
        mark(e * 1.2)
        await p._manage(pid)
        assert p.on_wallet_sell(NARU, "NotARandomUser", MINT, sold_fraction=1.0) == "noted"
        await _drain(p)
        assert b["tracks"]["follow_wallet"].status == "closed"
        assert b["tracks"]["follow_wallet"].exit_reason == "trader_sold"
        assert b["tracks"]["live_exit"].status == "open"               # live_exit ignores wallet sells
        mark(e * 2.1)
        await p._manage(pid)                                            # 50% at 2x
        lt = b["tracks"]["live_exit"]
        assert lt.tp_done and lt.tokens_left == lt.tokens_initial - int(lt.tokens_initial * 0.5)
        mark(e * 4.0)
        await p._manage(pid)
        mark(e * 4.0 * 0.69)                                            # > 30% off the high → trail
        await p._manage(pid)
        return pid
    pid = asyncio.run(go())
    assert pid not in p.book                                            # both tracks closed → off the book
    rows = _rows(tmp / "paper_fomo_top.jsonl")
    closes = {r["track"]: r for r in rows if r["kind"] == "close"}
    assert closes["live_exit"]["reason"] == "trail" and closes["live_exit"]["pnl_sol"] > 0
    assert closes["follow_wallet"]["reason"] == "trader_sold"
    ws = [r for r in rows if r["kind"] == "wallet_sell"][0]
    assert ws["live_exit_pnl_if_exit_all_now_sol"] is not None
    s = p.summary()
    assert s["legacy_tracks"]["live_exit"]["closed"] == 1 and s["legacy_tracks"]["follow_wallet"]["closed"] == 1   # v1.37: legacy kept; headline = realistic
    assert s["paper_only"] is True and s["arm"] == "fomo_top_paper"


def test_paper_hard_stop_and_dedupe(paper_env):
    p, prices, tmp = paper_env

    async def go():
        p.on_wallet_buy(ZAKUM, "zakum", {"mint": MINT})
        await _drain(p)
        assert p.on_wallet_buy(NARU, "NotARandomUser", {"mint": MINT}) == "repeat"   # one entry per mint
        pid = next(iter(p.book))
        b = p.book[pid]
        assert b["meta"]["handles"] == ["zakum", "NotARandomUser"]
        e = b["tracks"]["live_exit"].entry_price_sol
        for t in b["tracks"].values():
            t.last_price_sol, t.last_mark_ms = e * 0.6, int(time.time() * 1000)
        await p._manage(pid)
    asyncio.run(go())
    closes = [r for r in _rows(tmp / "paper_fomo_top.jsonl") if r["kind"] == "close"]
    assert {c["reason"] for c in closes} == {"hard_stop"} and len(closes) == 2
    assert all(c["pnl_sol"] < 0 for c in closes)


def test_paper_records_refused_checks_but_still_simulates(paper_env, monkeypatch):
    import app.paper_fomo_top as ftp
    p, _, _ = paper_env

    async def mesh_block(mint, timeout_s):
        return {"ok": False, "why": "mesh hard block: honeypot"}
    monkeypatch.setattr(ftp, "mesh_verdict", mesh_block)

    async def go():
        p.on_wallet_buy(ZAKUM, "zakum", {"mint": MINT})
        await _drain(p)
    asyncio.run(go())
    b = next(iter(p.book.values()))
    assert b["meta"]["would_live_buy"] is False and b["meta"]["mesh"]["ok"] is False


def test_paper_state_survives_restart(paper_env):
    import app.paper_fomo_top as ftp
    p, _, _ = paper_env

    async def go():
        p.on_wallet_buy(ZAKUM, "zakum", {"mint": MINT})
        await _drain(p)
    asyncio.run(go())
    q = ftp.PaperFomoTop()
    q.ensure_loop = lambda: None
    q.load()
    assert q.start_ms == p.start_ms and len(q.book) == 1 and MINT in q.seen_mints
    assert isinstance(next(iter(q.book.values()))["tracks"]["live_exit"], LivePos)


def test_paper_summary_endpoint(paper_env, monkeypatch):
    import app.paper_fomo_top as ftp
    from fastapi.testclient import TestClient
    from app.main import app
    p, _, _ = paper_env
    monkeypatch.setattr(ftp, "paper", p)
    r = TestClient(app).get("/api/paper/fomo-top/summary")
    assert r.status_code == 200
    j = r.json()
    assert j["arm"] == "fomo_top_paper" and j["paper_only"] and j["enabled"]
    assert {t["handle"] for t in j["targets"]} == {"zakum", "NotARandomUser"}
    assert all("…" in t["wallet"] for t in j["targets"])


def test_source_sync_subs_keeps_paper_targets(paper_env):
    from app.fomo_onchain.source import Manager
    from app.fomo_onchain.wallets import watchlist
    watchlist.load_seed()
    w = watchlist.get(ZAKUM)
    if w is None:
        pytest.skip("seed file not present")
    w.status = "dropped"
    m = Manager()
    m._sync_subs()
    assert any(ZAKUM in sh["want"] for sh in m.shards)


def test_watchlist_seed_excludes_inactive_wallets():
    from app.fomo_onchain.wallets import SEED_PATH
    real = Path(__file__).resolve().parents[1] / "data" / "fomo_top100_wallets.json"
    for path in (real, SEED_PATH):
        if not path.exists():
            continue
        handles = {r.get("handle") for r in json.loads(path.read_text())}
        assert "LP1111" not in handles and "lordarbiter" not in handles
        assert {"zakum", "NotARandomUser"} <= handles
