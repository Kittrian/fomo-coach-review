"""v1.38 (Sky approved Oct 18, 2026 6:58 PM CT): realism follow-ups — exit_variants twins, bench + fluffy realistic
twins (legacy fields / live-affecting decisions unchanged), learner on the realistic basis, safe coach snapshot."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app import paper_realism as pr

ROOT = Path(__file__).resolve().parents[1]
MINT = "MintRRRR1111111111111111111111111111111pump"
W = "A3W8psibkTUvjxs4LRscbnjux6TFDXdvD4m4GsGpQ2KJ"


def test_exit_variants_realistic_twin_separate_rows(tmp_path, monkeypatch):
    from app.scout import exit_variants as ev
    monkeypatch.setenv("EXIT_VARIANTS_LOG_PATH", str(tmp_path / "xv.jsonl"))
    monkeypatch.setenv("EXIT_VARIANTS_STATE_PATH", str(tmp_path / "xv.json"))
    monkeypatch.setattr(ev, "log_path", lambda: tmp_path / "xv.jsonl")
    monkeypatch.setattr(ev, "state_path", lambda: tmp_path / "xv.json")
    x = ev.ExitVariants()
    now = int(time.time() * 1000)
    n = x.open_signal(arm="scout_solo", mint=MINT, signal_id="s1", lat_s=2, entry_ms=now, entry_price_sol=1e-7,
                      scout_wallet=W, chase_pct=1.0, ref_price_sol=1.1e-7)
    assert n >= 1
    reals = [t for t in x.trackers.values() if t.realistic]
    legs = [t for t in x.trackers.values() if not t.realistic]
    assert reals and len(reals) == len(legs)
    assert all(t.entry_price_sol == pytest.approx(1.1e-7) for t in reals)        # never below the scout's fill
    assert all(t.entry_price_sol == pytest.approx(1e-7) for t in legs)           # legacy unchanged
    x.on_mark(MINT, 0.3e-7, now + 5000)                                           # gap through the hard stop
    rows = [json.loads(l) for l in (tmp_path / "xv.jsonl").read_text().splitlines()]
    leg = [r for r in rows if r["kind"] == "variant_closed" and r["variant"] == "A"][0]
    real = [r for r in rows if r["kind"] == "variant_closed_real" and r["variant"] == "A"][0]
    assert not leg.get("realistic") and real["realistic"] and real["legacy_entry_price_sol"] == pytest.approx(1e-7)
    P = pr.params("scout_solo")
    line = 1.1e-7 * 0.65
    assert real["exits"][-1]["fill_mult"] == pytest.approx(line * P["hard_stop_ratio_p10"] * (1 - P["fee_bps"] / 1e4)
                                                           / 1.1e-7, rel=1e-3)
    assert ev.ExitVariants.rows(x) and all(r["kind"] == "variant_closed" for r in x.rows())   # legacy readers unchanged
    s = x.summary()
    assert s["headline_basis"] == "realistic" and s["realistic"]["all_arms"]["variants"]["A"]["n"] == 1


def test_bench_realistic_twin_does_not_change_unbench_by_default(tmp_path, monkeypatch):
    from app.live import bench as bm
    monkeypatch.setenv("LIVE_BENCH_ENABLED", "true")
    monkeypatch.setenv("LIVE_BENCH_STATE_PATH", str(tmp_path / "b.json"))
    monkeypatch.setenv("LIVE_BENCH_LOG_PATH", str(tmp_path / "b.jsonl"))
    monkeypatch.setenv("LIVE_UNBENCH_PAPER_WINS", "1")
    b = bm.ScoutBench()
    b.load()
    b.wallets[W] = {"streak": 8, "benched": True, "paper_wins": 0, "live_wins": 0, "live_losses": 8}
    b._mark_src[MINT] = "curve"
    pid = "bench-x"
    b.paper_meta[pid] = {"arm": "scout_solo", "mint": MINT, "benched": [W], "ref_price_sol": 1e-7}
    b._enter_sync(pid, 1e-7, 1e-7)
    assert pid in b.paper and pid in b.paper_real
    leg, real = b.paper[pid], b.paper_real[pid]
    assert leg.entry_price_sol == pytest.approx(1e-7 * 1.015)                    # legacy model untouched
    assert real.entry_price_sol >= 1e-7 and "realistic" in real.flags
    now = int(time.time() * 1000)
    # realistic twin wins (+3x), legacy loses → legacy basis decides: still benched, realistic only logged
    real.last_price_sol, real.last_mark_ms = real.entry_price_sol * 0.5, now
    leg.last_price_sol, leg.last_mark_ms = leg.entry_price_sol * 0.5, now
    b.manage(pid, now)
    assert b.wallets[W]["benched"]
    rows = [json.loads(l) for l in (tmp_path / "b.jsonl").read_text().splitlines()]
    assert any(r["kind"] == "paper_open" and r.get("real_entry_price_sol") for r in rows)
    assert b.summary()["unbench_basis"] == "legacy"


def test_bench_realistic_close_logged_and_would_unbench_flag(tmp_path, monkeypatch):
    from app.live import bench as bm
    monkeypatch.setenv("LIVE_BENCH_ENABLED", "true")
    monkeypatch.setenv("LIVE_BENCH_STATE_PATH", str(tmp_path / "b.json"))
    monkeypatch.setenv("LIVE_BENCH_LOG_PATH", str(tmp_path / "b.jsonl"))
    monkeypatch.setenv("LIVE_UNBENCH_PAPER_WINS", "1")
    b = bm.ScoutBench()
    b.load()
    b.wallets[W] = {"streak": 8, "benched": True, "paper_wins": 0}
    b._mark_src[MINT] = "curve"
    pid = "bench-y"
    b.paper_meta[pid] = {"arm": "scout_solo", "mint": MINT, "benched": [W], "ref_price_sol": 1e-7}
    b._enter_sync(pid, 1e-7, 1e-7)
    real = b.paper_real[pid]
    now = int(time.time() * 1000)
    real.trader_sold = True
    real.meta["trader_sold_ms"] = real.entry_ms
    real.last_price_sol, real.last_mark_ms = real.entry_price_sol * 1.5, now
    b.manage_real(pid, now)
    rows = [json.loads(l) for l in (tmp_path / "b.jsonl").read_text().splitlines()]
    assert any(r["kind"] == "paper_close_real" and r["win"] for r in rows)
    assert b.wallets[W]["benched"] and b.wallets[W].get("would_unbench_realistic_ms")   # flagged, NOT unbenched


def test_fluffy_realistic_twin_writes_own_log(tmp_path, monkeypatch):
    from app.pump import paper as pp
    monkeypatch.setattr(pp, "LOG_PATH", tmp_path / "paper_trades.jsonl")
    monkeypatch.setattr(pp, "REAL_LOG_PATH", tmp_path / "paper_trades_real.jsonl")
    e = pp.PaperEngine()
    pos = pp.PaperPosition(mint=MINT, source="t", entry_sol=0.4, entry_lamports=400_000_000, tokens_initial=4_000_000,
                           tokens_left=4_000_000, entry_price_sol=1e-7, entry_venue="pump_curve_sim",
                           entry_ms=int(time.time() * 1000))
    pos.last_price_sol = 1e-7
    e.positions[MINT] = pos
    asyncio.run(e._open_real(pos))
    r = pos.meta["real"]
    P = pr.params("fluffy_pump_dump")
    assert r["entry_px"] == pytest.approx(1e-7 * (1 + P["slip_pct"] / 100))      # never better than the signal fill
    e._real_ladder(pos, r["entry_px"] * 2.1)                                      # TP1 vs the twin's own entry
    assert "TP1" in r["ladder_done"]
    pos.last_price_sol = 0.5e-7
    e._close(pos, "max_hold")
    real_rows = [json.loads(l) for l in (tmp_path / "paper_trades_real.jsonl").read_text().splitlines()]
    assert real_rows[0]["kind"] == "real_open" and real_rows[-1]["kind"] == "real_close"
    legacy_rows = [json.loads(l) for l in (tmp_path / "paper_trades.jsonl").read_text().splitlines()]
    assert all(not x.get("realistic") for x in legacy_rows)                       # rugcheck / recap input unchanged
    assert e.summary()["headline"]["closed"] == 1


def test_snapshot_has_no_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("PAPER_REALISM_SNAPSHOT_PATH", str(tmp_path / "s.json"))
    monkeypatch.setenv("LIVE_SIM_RPC_URL", "https://example.invalid/?api-key=SECRET123")
    assert pr.write_snapshot(force=True)
    raw = (tmp_path / "s.json").read_text()
    d = json.loads(raw)
    assert "SECRET123" not in raw and "api-key" not in raw
    assert "rules" in d["live_cfg"] and "kol_confluence" in d["params"]
    assert not [k for k in d["live_cfg"] if k.endswith(("_url", "_path", "_file")) or "token" in k]


def _seed_kol(d: Path, now: int, n=60):
    lines = []
    for i in range(n):
        pid = f"k{i}"
        t = now - 3 * 86400_000 + i * 600_000
        tp = 1e-7
        lines += [{"ts_ms": t, "kind": "open", "id": pid, "mint": f"M{i}", "handles": ["zeropnl"], "wallets": [W],
                   "entry_price_sol": tp * 0.8, "entry_src": "dexscreener", "mark_price_sol": tp * 0.8},
                  {"ts_ms": t, "kind": "fill_vs_trader", "id": pid, "trader_price_sol": tp, "entry_price_sol": tp * 0.8},
                  {"ts_ms": t + 60_000, "kind": "sell", "id": pid, "price_sol": tp * (3.0 if i % 2 else 0.5)},
                  {"ts_ms": t + 120_000, "kind": "sell", "id": pid, "price_sol": tp * (2.0 if i % 2 else 0.4)},
                  {"ts_ms": t + 120_000, "kind": "close", "id": pid, "track": "half2x_c30", "handles": ["zeropnl"],
                   "pnl_sol": 0.1, "max_multiple": 3.0 if i % 2 else 1.0, "reason": "trail", "mint": f"M{i}"}]
    (d / "kol_confluence.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n")


def test_learner_decides_on_realistic_basis_and_keeps_legacy(tmp_path):
    d = tmp_path / "data"
    (d / "learn").mkdir(parents=True)
    (d / "cryptohub_wallets.json").write_text(json.dumps({"wallets": [{"address": W, "label": "zeropnl"}]}))
    now = int(time.time() * 1000)
    _seed_kol(d, now)
    env = {**os.environ, "FOMO_DATA_DIR": str(d), "FOMO_COACH_DIR": str(d.parent)}
    r = subprocess.run([sys.executable, str(ROOT / "scripts/learn_daily.py")], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    book = json.loads((d / "learn/realistic_book.json").read_text())
    rows = book["books"]["kol_confluence"]
    assert len(rows) == 60 and all(x["basis"] == "replayed" for x in rows)
    sc = json.loads(next((d / "learn").glob("scores_*.json")).read_text())
    assert sc["basis"] == "realistic"
    real = sc["kol_confluence"]["by_wallet"]["zeropnl"]
    legacy = sc["legacy"]["kol_confluence"]["by_wallet"]["zeropnl"]
    assert real["n"] == 60 and legacy["n"] == 60
    assert real["sum"] < legacy["sum"]                       # realistic entries are worse than the lagging marks
    note = next((d / "learn/notes").glob("* Fomo self-learn daily.md")).read_text()
    assert "Basis: realistic" in note and "Realistic vs legacy" in note
    assert "snapshot missing" in note                       # no coach snapshot in this temp dir → said so


def test_learn_realism_never_reads_env_file(tmp_path):
    """fomo-learn makes .env inaccessible; app/config.py's load_dotenv must not run inside learn_realism."""
    fake = tmp_path / "fake"
    (fake / "dotenv").mkdir(parents=True)
    (fake / "dotenv" / "__init__.py").write_text(
        "def load_dotenv(*a, **k):\n    raise PermissionError('.env is inaccessible in the learner sandbox')\n"
        "def dotenv_values(*a, **k):\n    raise PermissionError('.env is inaccessible in the learner sandbox')\n")
    d = tmp_path / "data"
    (d / "learn").mkdir(parents=True)
    env = {**os.environ, "FOMO_DATA_DIR": str(d), "PYTHONPATH": str(fake)}
    r = subprocess.run([sys.executable, str(ROOT / "scripts/learn_realism.py")], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-2000:]
    assert (d / "learn/realistic_book.json").exists()
