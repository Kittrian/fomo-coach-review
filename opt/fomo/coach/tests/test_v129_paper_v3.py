"""v1.29 paper_v3_new (PAPER ONLY): 10 vetted wallets via the scout websocket, 3 exit tracks, fill vs trader, watch-only."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from app.live import jupiter
from app.live.engine import LiveEngine

MINT = "MintAAAA1111111111111111111111111111111pump"
NUMER0 = "A3W8psibkTUvjxs4LRscbnjux6TFDXdvD4m4GsGpQ2KJ"
RAMBO = "2net6etAtTe3Rbq2gKECmQwnzcKVXRaLcHy2Zy1iCiWz"
OTHER = "9EhcDGGJ6NpetxNumZKwD1o1kuHBwvfNGKrmS6zekP7C"


@pytest.fixture
def v3(monkeypatch, tmp_path):
    import app.paper_fomo_top as ftp
    import app.paper_v3 as pv
    monkeypatch.setenv("PAPER_V3_ENABLED", "true")
    monkeypatch.setenv("PAPER_V3_LOG_PATH", str(tmp_path / "paper_v3.jsonl"))
    monkeypatch.setenv("PAPER_V3_STATE_PATH", str(tmp_path / "paper_v3_state.json"))
    for k in ("PAPER_V3_WALLETS", "PAPER_V3_WATCH_WALLETS", "PAPER_V3_WHALE_WALLETS", "PAPER_V3_WHALE_MAX_CHASE_PCT"):
        monkeypatch.delenv(k, raising=False)

    def boom(*a, **k):
        raise AssertionError("paper arm must never reach the live executor / signer / Jupiter")
    for name in ("order", "execute", "swap", "sign_partial", "keypair", "simulate_order", "simulate_signed"):
        monkeypatch.setattr(jupiter, name, boom)
    monkeypatch.setattr(LiveEngine, "on_signal", boom)
    monkeypatch.setattr(LiveEngine, "_buy", boom)
    monkeypatch.setattr(LiveEngine, "_sell", boom)
    p = pv.PaperV3()
    prices = {"px": 0.000105}

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
    monkeypatch.setattr(p, "ensure_loop", lambda: None)
    from app.fomo_onchain import market
    monkeypatch.setattr(market, "sol_usd", lambda: 200.0)
    return p, prices, tmp_path


def _rows(path: Path):
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


async def _drain(p):
    for _ in range(5):
        await asyncio.sleep(0)
        if p._tasks:
            await asyncio.gather(*list(p._tasks))


def test_defaults_ten_wallets_seven_near_misses():
    import app.paper_v3 as pv
    c = pv.cfg()
    assert len(c["targets"]) == 11 and len(c["watch"]) == 7   # v1.30: 10 + Mitch (peacefuldestroy already in)
    assert "Cv5GgkpXvtXcsrMcKCJvx46BpMjiM61mpPU1j8RbbbVL" not in c["targets"]
    src = Path(pv.__file__).read_text()
    assert "signer" not in src.replace("never imports the signer", "") and "jupiter" not in src.lower().replace(
        "never calls jupiter", "")


def test_entry_records_fill_vs_trader_and_three_tracks(v3):
    p, prices, tmp = v3

    async def go():
        st = p.on_scout_trade(NUMER0, "v3:Numer0", True, MINT, price_sol=0.0001, sol=1.0, sig="S1",
                              recv_ms=int(time.time() * 1000), venue="pump_curve")
        await _drain(p)
        return st
    assert asyncio.run(go()) == "entering"
    b = next(iter(p.book.values()))
    assert {t for t in b["tracks"] if not t.startswith("real_")} == {"live_exit", "follow_wallet", "half2x_c30"}   # v1.37: + realistic twins
    assert b["tracks"]["live_exit"].arm == "paper_v3_new" and b["tracks"]["live_exit"].size_sol == pytest.approx(0.2)
    # our entry = mark 0.000105 + 150 bps vs trader 0.0001 → +6.575%
    assert b["meta"]["fill_vs_trader_pct"] == pytest.approx(6.58, abs=0.01)
    assert any(r["kind"] == "fill_vs_trader" for r in _rows(tmp / "paper_v3.jsonl"))
    assert LiveEngine().day()["trades"] == 0


def test_half2x_track_sells_half_at_2x_then_candle_stop(v3):
    p, prices, tmp = v3

    async def go():
        p.on_scout_trade(NUMER0, "v3:Numer0", True, MINT, price_sol=0.0001, sol=1.0, sig="S1",
                         recv_ms=int(time.time() * 1000))
        await _drain(p)
        pid = next(iter(p.book))
        b = p.book[pid]
        h = b["tracks"]["half2x_c30"]
        e = h.entry_price_sol
        t0 = int(time.time() * 1000)

        def step(px, ms):
            h.last_price_sol, h.last_mark_ms = px, ms
            h.high_price_sol = max(h.high_price_sol or px, px)
            p._custom_track(pid, b, "half2x_c30", h, ms)
        step(e * 2.1, t0 + 1000)
        assert h.tokens_left == h.tokens_initial - int(h.tokens_initial * 0.5)
        for i in range(2, 95):
            step(e * 2.5, t0 + i * 1000)
        step(e * 2.0, t0 + 96_000)
        assert h.status == "closed" and h.exit_reason.startswith("candle_30s")
        return b
    asyncio.run(go())
    closes = [r for r in _rows(tmp / "paper_v3.jsonl") if r["kind"] == "close" and r["track"] == "half2x_c30"]
    assert closes and closes[0]["pnl_sol"] > 0


def test_wallet_sell_closes_follow_and_flags_half2x_pre2x(v3):
    p, prices, tmp = v3

    async def go():
        p.on_scout_trade(NUMER0, "v3:Numer0", True, MINT, price_sol=0.0001, sol=1.0, sig="S1",
                         recv_ms=int(time.time() * 1000))
        await _drain(p)
        b = next(iter(p.book.values()))
        assert p.on_scout_trade(NUMER0, "v3:Numer0", False, MINT, price_sol=0.0001, sol=1.0, sig="S2",
                                recv_ms=int(time.time() * 1000)) == "noted"
        await _drain(p)
        return b
    b = asyncio.run(go())
    assert b["tracks"]["follow_wallet"].status == "closed"
    assert b["meta"]["xv"]["trader_sold"] is True


def test_watch_only_logs_never_trades_and_others_ignored(v3):
    p, _, tmp = v3
    assert p.on_scout_trade(RAMBO, "watch:rambo", True, MINT, price_sol=0.0001, sol=2.0, sig="W1",
                            recv_ms=int(time.time() * 1000)) == "watch"
    assert p.on_scout_trade(OTHER, "x", True, MINT, price_sol=0.0001, sol=2.0, sig="W2", recv_ms=1) is None
    assert not p.book and any(r["kind"] == "watch_trade" for r in _rows(tmp / "paper_v3.jsonl"))


def test_roster_roles_and_subscription_cost(monkeypatch):
    from app.scout.roster import Roster
    monkeypatch.setenv("PAPER_V3_ENABLED", "true")
    r = Roster()
    assert r.add_paper_v3_wallets() == 18   # v1.30: + Mitch
    kinds = {w.role: [k for _, _, k in r.sub_keys(w)] for w in r.by_wallet.values()}
    assert kinds["paper_v3"] == ["pump_uva", "amm_uva"] and kinds["watch"] == ["pump_uva"]
    monkeypatch.setenv("PAPER_V3_ENABLED", "false")
    assert Roster().add_paper_v3_wallets() == 0


def test_scout_engine_routes_v3_roles_away_from_scout_arms(monkeypatch):
    from app.scout.engine import ScoutEngine
    import app.paper_v3 as pv
    calls = []
    monkeypatch.setattr(pv.paper, "on_scout_trade", lambda *a, **k: calls.append((a, k)))
    e = ScoutEngine()

    class W:
        wallet, label, role = NUMER0, "v3:Numer0", "paper_v3"
    ev = {"mint": MINT, "is_buy": True, "sol_amount": 1_000_000_000, "token_amount": 10_000_000_000}
    e.on_trade(W(), ev, "SIG", int(time.time() * 1000))
    W.role = "watch"
    e.on_trade(W(), ev, "SIG2", int(time.time() * 1000))
    assert len(calls) == 2 and not e.buys.get(MINT) and not e.decided and e.confirmer_buys_seen == 0
