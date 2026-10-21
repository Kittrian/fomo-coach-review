"""v1.37 paper realism (PAPER ONLY): live-calibrated entries / exits / fees / gates; legacy tracks kept."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from app import paper_realism as pr
from app.live import jupiter
from app.live.engine import LiveEngine

MINT = "MintRRRR1111111111111111111111111111111pump"
NUMER0 = "A3W8psibkTUvjxs4LRscbnjux6TFDXdvD4m4GsGpQ2KJ"


def P(arm="kol_confluence", **kw):
    p = pr.params(arm)
    p.update(kw)
    return p


def test_calibration_defaults_are_the_measured_live_numbers():
    k = pr.params("kol_confluence")
    s = pr.params("paper_top4")
    assert k["group"] == "kol_confluence" and k["path"] == "jupiter"
    assert k["lat_ms"] == pytest.approx(2628) and k["slip_pct"] == pytest.approx(12.89)
    assert s["group"] == "scout" and s["slip_pct"] == pytest.approx(4.37) and s["live_arm"] == "scout_solo"
    assert 0.9 < k["hard_stop_ratio"] <= 1.0


def test_calibration_file_overrides(tmp_path, monkeypatch):
    f = tmp_path / "c.json"
    f.write_text(json.dumps({"routes": {"kol_confluence": {"jupiter": {"lat_ms_p50": 1000, "slip_pct_p50": 30}}}}))
    monkeypatch.setenv("PAPER_REALISM_CALIB_PATH", str(f))
    k = pr.params("kol_confluence")
    assert k["lat_ms"] == 1000 and k["slip_pct"] == 30


def test_entry_never_below_trader_and_rejects_lagging_mark():
    p = P()
    # lagging DexScreener mark 30% below the trader's fill → rejected, fallback trader × (1 + live median slip)
    e = pr.entry_fill("kol_confluence", 1e-7, None, {"px": 0.7e-7, "ms": int(time.time() * 1000)}, 0.1, p)
    assert e["ok"] and e["src"] == "trader_fill+live_slip"
    assert e["px"] == pytest.approx(1e-7 * (1 + p["slip_pct"] / 100))
    assert e["rejected"] and "lagging" in e["rejected"][0]
    # stale mark rejected
    e = pr.entry_fill("kol_confluence", 1e-7, None, {"px": 2e-7, "ms": int(time.time() * 1000) - 60_000}, 0.1, p)
    assert e["px"] == pytest.approx(1e-7 * (1 + p["slip_pct"] / 100)) and "stale" in e["rejected"][0]
    # fresh mark above the floor wins
    e = pr.entry_fill("kol_confluence", 1e-7, None, {"px": 2e-7, "ms": int(time.time() * 1000)}, 0.1, p)
    assert e["px"] == pytest.approx(2e-7) and e["src"] == "mark>trader_floor"


def test_entry_onchain_floored_at_trader_and_skip_without_data():
    p = P()
    curve = {"virtual_quote_reserves": 30 * 10**9, "virtual_token_reserves": 1_000_000_000 * 10**6,
             "real_token_reserves": 800_000_000 * 10**6, "complete": False, "quote_is_sol": True}
    from app.pump.curve import curve_price_sol
    spot = curve_price_sol(curve)
    e = pr.entry_fill("kol_confluence", spot * 0.5, {"px": spot, "ms": 1, "src": "curve", "curve": curve}, None, 0.1, p)
    assert e["ok"] and e["src"] == "onchain_curve" and e["px"] > spot          # our size + curve fee on top of spot
    e = pr.entry_fill("kol_confluence", spot * 2, {"px": spot, "ms": 1, "src": "curve", "curve": curve}, None, 0.1, p)
    assert e["px"] == pytest.approx(spot * 2) and e["floored_to_trader"]      # never below the trader's fill
    e = pr.entry_fill("momentum_paper", None, None, {"px": spot, "ms": int(time.time() * 1000)}, 0.1, P("momentum_paper"))
    assert not e["ok"] and "skipped" in e["reason"]


def test_exit_fills_and_fees():
    p = P(hard_stop_ratio=0.95, generic_ratio=0.98)
    lc = {"hard_stop_pct": 0.35}
    assert pr.exit_px(p, "hard_stop", 0.64, 1.0, lc) == pytest.approx(0.65 * 0.95)
    assert pr.exit_px(p, "hard_stop", 0.30, 1.0, lc) == pytest.approx(0.65 * p["hard_stop_ratio_p10"])  # gap floored at live p10
    assert pr.exit_px(p, "trail", 2.0, 1.0, lc) == pytest.approx(1.96)
    lam = pr.proceeds_lamports(p, 1_000_000, 0.1)                               # 1 token @ 0.1 SOL
    assert lam == int(0.1e9 * (1 - p["fee_bps"] / 1e4)) - p["priority_lamports"] - p["tip_lamports"]
    spent, toks = pr.entry_position(p, 1e-6, 0.1)
    assert spent == int(0.1e9) + p["priority_lamports"] + p["tip_lamports"] and toks == int(0.1 / 1e-6 * 1e6)


def test_drain_proxy_and_gate_ledger():
    p = P(drain_frac=0.5, drain_window_s=30)
    d = pr.DrainWatch()
    assert d.check("x", 1.0, 0, p) is None
    assert d.check("x", 0.8, 5_000, p) is None
    assert d.check("x", 0.24, 10_000, p)                                        # −76% < (1−0.5)² line
    g = pr.GateLedger()
    sp = P("paper_top4", live_cfg={"chase_filter_enabled": True, "chase_arms": ["scout_solo"], "max_chase_pct": 5,
                                   "max_trades_per_day": 2, "daily_loss_limit_sol": 0.1, "max_open_positions": 1,
                                   "night_pause_enabled": False})
    now = int(time.time() * 1000)
    assert g.refusals("paper_top4", now, would_live_buy=True, trader_px=1.0, entry_px=1.04, P=sp) == []
    assert "chase>5%" in g.refusals("paper_top4", now, would_live_buy=True, trader_px=1.0, entry_px=1.10, P=sp)
    assert "rugcheck/mesh" in g.refusals("paper_top4", now, would_live_buy=False, trader_px=1.0, entry_px=1.0, P=sp)
    g.opened("a", now)
    assert any(x.startswith("max_open") for x in g.refusals("paper_top4", now, would_live_buy=True, trader_px=1.0,
                                                           entry_px=1.0, P=sp))
    g.closed("a", now, -0.2)
    r = g.refusals("paper_top4", now, would_live_buy=True, trader_px=1.0, entry_px=1.0, P=sp)
    assert any(x.startswith("loss_limit") for x in r)


@pytest.fixture
def v3(monkeypatch, tmp_path):
    import app.paper_fomo_top as ftp
    import app.paper_v3 as pv
    monkeypatch.setenv("PAPER_V3_ENABLED", "true")
    monkeypatch.setenv("PAPER_V3_LOG_PATH", str(tmp_path / "paper_v3.jsonl"))
    monkeypatch.setenv("PAPER_V3_STATE_PATH", str(tmp_path / "paper_v3_state.json"))
    monkeypatch.setenv("LIVE_NIGHT_PAUSE_ENABLED", "false")

    def boom(*a, **k):
        raise AssertionError("paper arm must never reach the live executor / signer / Jupiter")
    for name in ("order", "execute", "swap", "sign_partial", "keypair", "simulate_order", "simulate_signed"):
        monkeypatch.setattr(jupiter, name, boom)
    monkeypatch.setattr(LiveEngine, "on_signal", boom)
    monkeypatch.setattr(LiveEngine, "_buy", boom)
    monkeypatch.setattr(LiveEngine, "_sell", boom)
    p = pv.PaperV3()
    prices = {"px": 0.00007}                                     # DexScreener mark 30% BELOW the trader (lagging)

    async def price_sol(mint, force=True):
        return prices["px"], {"symbol": "TST", "price_usd": 0.014, "at_ms": int(time.time() * 1000)}

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


def test_paper_v3_realistic_twin_vs_legacy(v3):
    p, prices, tmp = v3

    async def go():
        p.on_scout_trade(NUMER0, "v3:Numer0", True, MINT, price_sol=0.0001, sol=1.0, sig="S1",
                         recv_ms=int(time.time() * 1000), venue="pump_curve")
        await _drain(p)
        pid = next(iter(p.book))
        b = p.book[pid]
        legacy, real = b["tracks"]["live_exit"], b["tracks"]["real_live_exit"]
        assert legacy.entry_price_sol == pytest.approx(0.00007 * 1.015)       # legacy: lagging mark + 150 bps
        sp = pr.params("paper_v3_new")
        assert real.entry_price_sol == pytest.approx(0.0001 * (1 + sp["slip_pct"] / 100))   # realistic: trader + live slip
        assert real.arm == "scout_solo" and real.size_sol == pytest.approx(sp["size_sol"])
        assert set(t for t in b["tracks"] if t.startswith("real_")) == {"real_live_exit", "real_follow_wallet",
                                                                       "real_half2x_c30"}
        assert b["meta"]["real"]["gated"] is True and b["meta"]["real"]["fill_vs_legacy_pct"] > 30
        # price back to the trader's fill: legacy shows +40% paper gain, realistic is under water
        for t in b["tracks"].values():
            t.last_price_sol, t.last_mark_ms = 0.0001, int(time.time() * 1000)
            t.high_price_sol = max(t.high_price_sol or 0, 0.0001)
        assert legacy.pnl()["pnl_sol"] > 0 and real.pnl()["pnl_sol"] < 0
        # hard stop on the realistic track fills at the live ratio of the stop line, with live fees
        px = real.entry_price_sol * 0.6
        for t in b["tracks"].values():
            t.last_price_sol, t.last_mark_ms = px, int(time.time() * 1000)
        await p._manage(pid)
        assert real.status == "closed" and real.exit_reason == "hard_stop"
        return pid
    asyncio.run(go())
    rows = _rows(tmp / "paper_v3.jsonl")
    rc = [r for r in rows if r["kind"] == "close" and r.get("realistic") and r["track"] == "real_live_exit"]
    assert rc and rc[0]["gated"] is True
    s = p.summary()
    assert s["headline"]["basis"] == "realistic" and s["headline"]["track"] == "real_follow_wallet"
    assert "legacy_tracks" in s and "realism" in s and s["tracks"]["live_exit"]["closed"] == 1
    assert LiveEngine().day()["trades"] == 0


def test_realism_off_switch_keeps_legacy(v3, monkeypatch):
    p, prices, tmp = v3
    monkeypatch.setenv("PAPER_REALISM_ENABLED", "false")

    async def go():
        p.on_scout_trade(NUMER0, "v3:Numer0", True, MINT, price_sol=0.0001, sol=1.0, sig="S1",
                         recv_ms=int(time.time() * 1000), venue="pump_curve")
        await _drain(p)
    asyncio.run(go())
    b = next(iter(p.book.values()))
    assert not any(t.startswith("real_") for t in b["tracks"])
    assert p.summary()["headline"]["basis"] == "legacy"


def test_realism_module_never_touches_live_execution():
    src = Path(pr.__file__).read_text()
    for bad in ("signer_client", "jupiter.order", "jupiter.execute", "_buy(", "_sell(", "kill_file", "KILL"):
        assert bad not in src
    src2 = (Path(pr.__file__).parent / "scout" / "real.py").read_text()
    assert "signer" not in src2.replace("Never imports the signer", "") and "on_signal" not in src2


def test_trader_fill_unit_mismatch_is_ignored():
    p = P()
    now = int(time.time() * 1000)
    # trader "fill" 1000x below the market (non-SOL quote / decimals) must not set the price floor
    e = pr.entry_fill("kol_confluence", 1e-10, None, {"px": 1e-7, "ms": now}, 0.1, p)
    assert not e["ok"] and e.get("trader_px_suspect") == pytest.approx(1e-10)


def test_hard_stop_fill_bounded_by_live_distribution():
    p = P()
    lc = {"hard_stop_pct": 0.35}
    line = 0.65
    # lagging mark gapped to -80%: fill floored at the live p10 stop fill, not the stale print
    assert pr.exit_px(p, "hard_stop", 0.2, 1.0, lc) == pytest.approx(line * p["hard_stop_ratio_p10"])
    # mark right at the line: live median stop ratio
    assert pr.exit_px(p, "hard_stop", 0.65, 1.0, lc) == pytest.approx(line * p["hard_stop_ratio"])
    # drain: never better than mark, floored at live p10 drain multiple
    assert pr.exit_px(p, "rug_flag_liquidity_drain", 0.1, 1.0, lc) == pytest.approx(p["drain_mult_p10"])


def test_scout_real_opens_realistic_and_closes_on_hard_stop(tmp_path, monkeypatch):
    from app.scout.real import ScoutReal
    monkeypatch.setenv("SCOUT_REAL_LOG_PATH", str(tmp_path / "sr.jsonl"))
    monkeypatch.setenv("SCOUT_REAL_STATE_PATH", str(tmp_path / "sr.json"))
    sr = ScoutReal()
    pid = asyncio.run(sr.open("scout_solo", MINT, "sig1", int(time.time() * 1000), 1e-7, NUMER0))
    assert pid
    p = sr.pos[pid]
    sp = pr.params("scout_solo")
    assert p.entry_price_sol == pytest.approx(1e-7 * (1 + sp["slip_pct"] / 100))
    assert p.size_sol == pytest.approx(sp["size_sol"]) and p.arm == "scout_solo"
    # duplicate signal on the same mint is ignored
    assert asyncio.run(sr.open("scout_solo", MINT, "sig2", int(time.time() * 1000), 1e-7, NUMER0)) is None
    now = int(time.time() * 1000)
    sr.on_mark(MINT, p.entry_price_sol * 0.5, now + 3000)
    assert pid not in sr.pos
    rows = [json.loads(l) for l in (tmp_path / "sr.jsonl").read_text().splitlines()]
    close = [r for r in rows if r["kind"] == "real_close"][0]
    assert close["pnl_pct"] < -30 and close["realistic"] and close["paper"]
    rep = sr.report()
    assert rep["by_arm"]["scout_solo"]["all"]["closed"] == 1
