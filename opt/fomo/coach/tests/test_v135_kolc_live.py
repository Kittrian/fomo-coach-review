"""v1.35 kol_confluence LIVE lane: routing, dedupe, flag, strong cap bypass (Jev-gated), exit config."""
from __future__ import annotations

import asyncio

import pytest

from tests.test_v119 import live_env, MINT, _pos  # noqa: F401
from app.live import engine as eng_mod
from app.live.engine import LiveEngine, trail_for
from app.live.config import config

W = [f"W{i}" + "x" * 38 for i in range(7)]


@pytest.fixture
def env(live_env, monkeypatch):
    monkeypatch.setenv("LIVE_ARMS", "scout_solo,scout_confirmed,fluffy_pump_dump,zyrox,kol_confluence")
    monkeypatch.setenv("LIVE_SIZE_SOL_KOL_CONFLUENCE", "0.10")
    monkeypatch.setenv("LIVE_TRADE_CAP_EXEMPT_ARMS", "fluffy_pump_dump")
    monkeypatch.delenv("LIVE_TRADE_CAP_UNCOUNTED_ARMS", raising=False)
    monkeypatch.delenv("LIVE_OWN_POOL_ARMS", raising=False)
    monkeypatch.delenv("LIVE_LOSS_LIMIT_EXEMPT_ARMS", raising=False)
    monkeypatch.setenv("LIVE_PROFIT_EXTEND_ENABLED", "false")
    monkeypatch.setenv("LIVE_NIGHT_PAUSE_ENABLED", "false")
    monkeypatch.setenv("LIVE_LIMIT_WINDOW", "calendar")
    monkeypatch.setenv("KOL_CONFLUENCE_LIVE_ENABLED", "true")
    monkeypatch.setenv("KOL_CONFLUENCE_CAP_BYPASS_MIN_WALLETS", "5")
    monkeypatch.delenv("LIVE_EXIT_RULES", raising=False)
    monkeypatch.setenv("LIVE_EXIT_RULES_KOL_CONFLUENCE", "tp2x_trail,min10_check,hard_stop,max_hold")
    monkeypatch.delenv("LIVE_TRAIL_PCT_KOL_CONFLUENCE", raising=False)
    monkeypatch.setenv("KOL_CONFLUENCE_CAP_BYPASS_COUNT", "all")
    monkeypatch.delenv("KOL_CONFLUENCE_CAP_BYPASS_REQUIRE_JEV", raising=False)
    return live_env


def test_size_rules_trail(env):
    c = config()
    assert c["sizes_sol"]["kol_confluence"] == 0.10
    assert "trader_sells" not in c["rules"]["kol_confluence"] and "tp2x_trail" in c["rules"]["kol_confluence"]
    assert trail_for(_pos(arm="kol_confluence"), c) == c["trail_pct"]
    assert trail_for(_pos(arm="scout_solo"), c) == c["trail_pct"]          # other lanes unchanged
    assert c["sizes_sol"]["scout_solo"] == 0.20


def test_cap_inside_and_bypass(env):
    e = LiveEngine(); e.day()["trades"] = 10
    ok, why, size = e.check_caps("kol_confluence", MINT)
    assert not ok and any("max trades" in w for w in why)
    ok, why, size = e.check_caps("kol_confluence", MINT, cap_bypass=True)
    assert ok, why; assert size == 0.10 and e._cap_bypass_applied
    ok, why, _ = e.check_caps("scout_solo", MINT, cap_bypass=True)          # bypass only for this lane
    assert not ok
    (env / "KILL").write_text("1")
    ok, why, _ = e.check_caps("kol_confluence", MINT, cap_bypass=True)
    assert not ok and "kill switch active" in why
    (env / "KILL").unlink()
    e.bought_mints[MINT] = {"arm": "scout_solo"}
    ok, why, _ = e.check_caps("kol_confluence", MINT, cap_bypass=True)
    assert not ok and any("dedupe" in w for w in why)
    e.bought_mints.clear(); e.day()["realized_pnl_sol"] = -1.0
    ok, why, _ = e.check_caps("kol_confluence", MINT, cap_bypass=True)
    assert not ok and any("loss limit" in w for w in why)


def _kc(monkeypatch, tmp_path):
    import app.paper_kol_confluence as m
    monkeypatch.setenv("KOL_CONFLUENCE_ENABLED", "true")
    monkeypatch.setenv("KOL_CONFLUENCE_LOG_PATH", str(tmp_path / "kc.jsonl"))
    monkeypatch.setenv("KOL_CONFLUENCE_STATE_PATH", str(tmp_path / "kc_state.json"))
    p = m.KolConfluence()
    monkeypatch.setattr(p, "active", lambda now_ms=None: True)
    monkeypatch.setattr(p, "on_wallet_buy", lambda *a, **k: "entering")
    from app.fomo_onchain import market
    monkeypatch.setattr(market, "sol_usd", lambda: 200.0)
    calls = []
    fake = LiveEngine()
    monkeypatch.setattr(eng_mod, "engine", fake)

    def on_signal(arm, mint, **kw):
        calls.append((arm, mint, kw))
        return {"decision": "buy", "reasons": [], "size_sol": 0.10, "cap_bypass": (kw.get("meta") or {}).get("cap_bypass")}
    monkeypatch.setattr(fake, "on_signal", on_signal)
    return p, calls, fake


def test_route_once_per_event(env, monkeypatch):
    p, calls, _ = _kc(monkeypatch, env)
    for i in range(4):
        p.tracker(W[i], f"w{i}", "scout", True, MINT, price_sol=1e-4, sol=1, sig=f"s{i}", recv_ms=1_000_000 + i)
    assert len(calls) == 1 and calls[0][0] == "kol_confluence"
    assert set(calls[0][2]["trader_wallets"]) == set(W[:3]) and calls[0][2]["meta"]["cap_bypass"] is None


def test_flag_off_no_live(env, monkeypatch):
    monkeypatch.setenv("KOL_CONFLUENCE_LIVE_ENABLED", "false")
    p, calls, _ = _kc(monkeypatch, env)
    for i in range(6):
        p.tracker(W[i], f"w{i}", "cryptohub", True, MINT, price_sol=1e-4, sol=1, sig=f"s{i}", recv_ms=1_000_000 + i)
    assert calls == []


@pytest.mark.parametrize("jev_ok,req", [(True, True), (False, True), (False, False)])
def test_strong_bypass_jev(env, monkeypatch, jev_ok, req):
    monkeypatch.setenv("KOL_CONFLUENCE_CAP_BYPASS_REQUIRE_JEV", "true" if req else "false")
    p, calls, fake = _kc(monkeypatch, env)
    monkeypatch.setattr(p, "_live_signal", type(p)._live_signal.__get__(p))
    monkeypatch.setattr(p, "_jev", lambda *a: {"jev_status": "ok", "action": "show" if jev_ok else "skip",
                                                "safe_to_surface": jev_ok})

    def first_refused(arm, mint, **kw):
        calls.append((arm, mint, kw))
        byp = (kw.get("meta") or {}).get("cap_bypass")
        return {"decision": "buy" if byp else "refused", "reasons": [] if byp else ["max trades/day 10"],
                "cap_bypass": byp}
    monkeypatch.setattr(fake, "on_signal", first_refused)

    async def run():
        for i in range(7):
            p.tracker(W[i], f"w{i}", "cryptohub", True, MINT, price_sol=1e-4, sol=1, sig=f"s{i}", recv_ms=1_000_000 + i)
            await asyncio.sleep(0)
        await asyncio.sleep(0.05)
    asyncio.run(run())
    byp = [c for c in calls if c[2]["meta"]["cap_bypass"]]
    assert calls[0][2]["meta"]["cap_bypass"] is None                       # 3 wallets: inside the cap
    if jev_ok or not req:
        assert len(byp) == 1 and "KOL_CONFLUENCE_CAP_BYPASS_MIN_WALLETS=5" in byp[0][2]["meta"]["cap_bypass"]["reason"]
    else:
        assert byp == []
    assert len(p.strong_tried) == 1


def test_strong_counts_cryptohub_only(env, monkeypatch):
    monkeypatch.setenv("KOL_CONFLUENCE_CAP_BYPASS_COUNT", "cryptohub")
    p, calls, _ = _kc(monkeypatch, env)
    tried = []
    monkeypatch.setattr(p, "_strong", lambda *a, **k: tried.append(a))
    for i in range(7):
        p.tracker(W[i], f"w{i}", "scout", True, MINT, price_sol=1e-4, sol=1, sig=f"s{i}", recv_ms=1_000_000 + i)
    assert tried == [] and len(calls) == 1


def test_strong_counts_all_roles(env, monkeypatch):
    p, calls, fake = _kc(monkeypatch, env)
    monkeypatch.setattr(fake, "on_signal", lambda arm, mint, **kw: {"decision": "refused", "reasons": ["max trades/day 10"]})
    tried = []
    monkeypatch.setattr(p, "_strong", lambda *a, **k: tried.append(a) or asyncio.sleep(0))

    async def run():
        for i in range(7):
            p.tracker(W[i], f"w{i}", "scout", True, MINT, price_sol=1e-4, sol=1, sig=f"s{i}", recv_ms=1_000_000 + i)
    asyncio.run(run())
    assert len(tried) == 1


def test_on_signal_logs_bypass(env, monkeypatch):
    import json
    e = LiveEngine(); e.day()["trades"] = 10
    monkeypatch.setattr(e, "_spawn", lambda coro: coro.close())

    async def run():
        return e.on_signal("kol_confluence", MINT, trader_wallets=W[:5],
                           meta={"lane": "kol_confluence", "cap_bypass": {"reason": "strong test"}})
    d = asyncio.run(run())
    assert d["decision"] == "buy" and d["cap_bypass"]["applied"] is True and d["size_sol"] == 0.10
    assert e.pending_buys[MINT]["meta"]["cap_bypass"]["reason"] == "strong test"
    rows = [json.loads(x) for x in (env / "ledger.jsonl").read_text().splitlines()]
    assert any(r["kind"] == "cap_bypass" and r["reason"] == "strong test" for r in rows)
    d2 = asyncio.run(run())
    assert d2["decision"] == "refused"                                       # never twice


def test_uncounted_own_pool(env, monkeypatch):
    monkeypatch.setenv("LIVE_TRADE_CAP_UNCOUNTED_ARMS", "kol_confluence")
    monkeypatch.setenv("LIVE_OWN_POOL_ARMS", "kol_confluence")
    monkeypatch.setenv("LIVE_MAX_OPEN_POSITIONS_KOL_CONFLUENCE", "7")
    e = LiveEngine(); e.day()["trades"] = 10
    monkeypatch.setattr(e, "_spawn", lambda coro: coro.close())
    for i in range(3):
        e.positions[f"s{i}"] = _pos(mint=f"S{i}", arm="scout_solo", last_price_sol=0.0002)
    ok, why, size = e.check_caps("kol_confluence", MINT)
    assert ok, why
    assert size == 0.10
    ok, why, _ = e.check_caps("scout_solo", "Other1")
    assert not ok and any("max trades" in w for w in why) and any("max open positions 3" in w for w in why)

    async def buy(m):
        return e.on_signal("kol_confluence", m, trader_wallets=W[:3], meta={"lane": "kol_confluence"})
    for i in range(7):
        assert asyncio.run(buy(f"K{i}"))["decision"] == "buy"
    assert e.day()["trades"] == 10
    d = asyncio.run(buy("K7"))
    assert d["decision"] == "refused" and any("for kol_confluence 7" in w for w in d["reasons"])
    e.positions.clear(); e.day()["trades"] = 0
    ok, why, _ = e.check_caps("scout_solo", "Other2")
    assert ok, why
    e._refund("K0", "test", arm="kol_confluence")
    assert e.day()["trades"] == 0
    (env / "KILL").write_text("1")
    ok, why, _ = e.check_caps("kol_confluence", "K9")
    assert not ok and "kill switch active" in why
    (env / "KILL").unlink()
    e.day()["realized_pnl_sol"] = -1.0
    ok, why, _ = e.check_caps("kol_confluence", "K9")
    assert not ok and any("loss limit" in w for w in why)              # (exemption off in this test)


def test_loss_limit_exempt(env, monkeypatch):
    monkeypatch.setenv("LIVE_LOSS_LIMIT_EXEMPT_ARMS", "kol_confluence")
    e = LiveEngine(); e.day()["trades"] = 0
    k = _pos(mint="K1", arm="kol_confluence", last_price_sol=0.00001)    # deep unrealized loss
    e.positions[k.id] = k
    assert e.unrealized_today_sol() == 0.0
    ok, why, _ = e.check_caps("scout_solo", "Other")
    assert ok, why                                                     # its loss never halts other lanes
    e._record_sell(k, k.tokens_left, 0, "hard_stop", 0, None, {})
    assert e.day()["realized_pnl_sol"] == 0.0 and e.limit_window()["realized_pnl_sol"] == 0.0
    e.day()["realized_pnl_sol"] = -1.0                                 # shared limit hit by other lanes
    ok, why, _ = e.check_caps("scout_solo", "Other")
    assert not ok and any("loss limit" in w for w in why)
    ok, why, _ = e.check_caps("kol_confluence", "K2")
    assert ok, why
    (env / "KILL").write_text("1")
    ok, why, _ = e.check_caps("kol_confluence", "K2")
    assert not ok and "kill switch active" in why
