"""v1.32 PAPER rules + daily self-learn: CHASE5 gate, T60_CANDLE15, absurd-mark guard, paper_top4, live peak logging
(logging only), learner bounds / min-n / auto-undo, and that the live engine never reads learner output."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.scout import exit_variants as ev

ROOT = Path(__file__).resolve().parents[1]
T0 = 1_800_000_000_000


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("EXIT_VARIANTS_LOG_PATH", str(tmp_path / "xv.jsonl"))
    monkeypatch.setenv("EXIT_VARIANTS_STATE_PATH", str(tmp_path / "xv_state.json"))
    monkeypatch.setenv("PAPER_ANOMALY_LOG_PATH", str(tmp_path / "anom.jsonl"))
    from app import learn_overrides
    learn_overrides.reset_cache()
    yield
    learn_overrides.reset_cache()


def _rows(p):
    return [json.loads(x) for x in Path(p).read_text().splitlines()] if Path(p).exists() else []


def test_chase5_gate_opens_only_at_or_below_cap(tmp_path):
    x = ev.ExitVariants()
    x.open_signal(arm="scout_solo", mint="M1", signal_id="a", lat_s=2, entry_ms=T0, entry_price_sol=1.0,
                  scout_wallet="W", chase_pct=4.9)
    x.open_signal(arm="scout_solo", mint="M2", signal_id="b", lat_s=2, entry_ms=T0, entry_price_sol=1.0,
                  scout_wallet="W", chase_pct=8.0)
    x.open_signal(arm="scout_solo", mint="M3", signal_id="c", lat_s=2, entry_ms=T0, entry_price_sol=1.0,
                  scout_wallet="W")
    assert "a:CHASE5" in x.trackers and "b:CHASE5" not in x.trackers and "c:CHASE5" not in x.trackers
    assert "b:A" in x.trackers and "b:T60_CANDLE15" in x.trackers
    g = [r for r in _rows(tmp_path / "xv.jsonl") if r["kind"] == "variant_gate"]
    assert [(r["signal_id"], r["passed"], r["cap_pct"]) for r in g] == [("a", True, 5.0), ("b", False, 5.0), ("c", False, 5.0)]


def test_chase_cap_from_overrides_is_clamped(tmp_path, monkeypatch):
    from app import learn_overrides
    monkeypatch.setattr(learn_overrides, "path", lambda: tmp_path / "ov.json")
    (tmp_path / "ov.json").write_text(json.dumps({"values": {"scout_chase_cap_pct": 99, "top4_handles": ["Evil", "Zef"]}}))
    learn_overrides.reset_cache()
    v = learn_overrides.get()
    assert v["scout_chase_cap_pct"] == 15.0 and v["top4_handles"] == ["Zef"]
    (tmp_path / "ov.json").write_text(json.dumps({"values": {"scout_chase_cap_pct": -4}}))
    learn_overrides.reset_cache()
    assert learn_overrides.get()["scout_chase_cap_pct"] == 3.0
    (tmp_path / "ov.json").write_text("{not json")
    learn_overrides.reset_cache()
    assert learn_overrides.get() == learn_overrides.DEFAULTS


def _P():
    p = ev.params()
    p.update(hard_stop_pct=0.35, tp_mult=2.0, tp_frac=0.5, gap_pct=0.10, haircut=0.04, max_age_s=86400)
    return p


def test_t60_candle15_ignores_scout_sell_for_60s():
    t = ev.Tracker(signal_id="s", arm="scout_solo", mint="M", variant="T60_CANDLE15", lat_s=2, entry_ms=T0,
                   entry_price_sol=1.0, high=1.0)
    cd = ev.Candles()
    t.trader_sold = True
    for i in range(0, 60):                             # flat-to-rising, scout already sold → still held
        px = 1.0 + i * 0.002
        cd.update(px, T0 + i * 1000, [15])
        assert not ev.evaluate(t, px, T0 + i * 1000, _P(), cd)
    assert ev.evaluate(t, 1.12, T0 + 61_000, _P(), cd)
    assert t.exits[-1]["reason"] == "trader_sold_after_60s"


def test_t60_candle15_candle_stop_still_applies_inside_60s():
    t = ev.Tracker(signal_id="s", arm="scout_solo", mint="M", variant="T60_CANDLE15", lat_s=2, entry_ms=T0,
                   entry_price_sol=1.0, high=1.0)
    cd = ev.Candles()
    closed = None
    for i, px in enumerate([1.3] * 20 + [1.0] * 5):
        cd.update(px, T0 + i * 1000, [15])
        if ev.evaluate(t, px, T0 + i * 1000, _P(), cd):
            closed = i
            break
    assert closed is not None and t.exits[-1]["reason"] == "candle_15s_stop"


def test_absurd_mark_rejected_and_logged(tmp_path):
    assert ev.mark_anomaly(1.0, [1.0], 25.0)
    assert ev.mark_anomaly(None, [1.0], 500.0)
    assert ev.mark_anomaly(1.0, [1.0], 3.0) is None
    x = ev.ExitVariants()
    x.open_signal(arm="scout_solo", mint="M", signal_id="a", lat_s=2, entry_ms=T0, entry_price_sol=1.0,
                  scout_wallet="W", chase_pct=1.0)
    x.on_mark("M", 1.1, T0 + 1000)
    x.on_mark("M", 4000.0, T0 + 2000)
    assert all(t.high < 2 for t in x.trackers.values())
    assert _rows(tmp_path / "anom.jsonl")[0]["kind"] == "mark_rejected"


def test_live_engine_never_reads_learner_or_paper_top4():
    src = (ROOT / "app/live/engine.py").read_text() + (ROOT / "app/live/config.py").read_text()
    for bad in ("learn_overrides", "paper_top4", "paper_overrides", "data/learn"):
        assert bad not in src


def test_peak_info_is_logging_only():
    from app.live.engine import LivePos, _peak_info
    p = LivePos(mint="M", arm="scout_solo", size_sol=0.2, spent_lamports=1, tokens_initial=1, tokens_left=0,
                entry_price_sol=1.0, entry_ms=T0, high_price_sol=2.5)
    p.closed_ms = T0 + 90_000
    assert _peak_info(p) == {"high_price_sol": 2.5, "entry_price_sol": 1.0, "peak_multiple": 2.5, "hold_s": 90.0,
                             "mark_source": p.mark_source}
    assert _peak_info(object()) == {}


def test_paper_top4_targets_are_the_four_and_inherit_paper_v3(monkeypatch):
    monkeypatch.setenv("PAPER_V3_ENABLED", "true")
    for k in ("PAPER_V3_WALLETS", "PAPER_V3_WHALE_WALLETS"):
        monkeypatch.delenv(k, raising=False)
    from app import paper_top4
    c = paper_top4.cfg()
    assert sorted(c["targets"].values()) == ["Inquixit", "Lynk", "Yenni", "Zef"]
    assert c["enabled"] and c["whales"] == {} and c["watch"] == {} and c["log_path"].name == "paper_top4.jsonl"
    monkeypatch.setenv("PAPER_V3_ENABLED", "false")
    assert not paper_top4.cfg()["enabled"]


def _seed(d: Path, now_ms: int):
    d.mkdir(parents=True, exist_ok=True)
    live = []
    for i in range(20):
        e = now_ms - (20 - i) * 3600_000
        live.append({"ts_ms": e, "kind": "buy", "id": f"L{i}", "arm": "scout_solo", "mint": f"M{i}", "size_sol": 0.2,
                     "entry_ms": e, "trader_wallets": ["W"], "meta": {"chase": {"premium_pct": i - 5}}})
        live.append({"ts_ms": e + 60_000, "kind": "close", "id": f"L{i}", "arm": "scout_solo", "mint": f"M{i}",
                     "reason": "trader_sold", "pnl": {"pnl_sol": -0.02 if i % 3 else 0.05},
                     "peak": {"peak_multiple": 1.3}})
    (d / "live_trades.jsonl").write_text("\n".join(json.dumps(r) for r in live) + "\n")
    xv = []
    for i in range(80):
        ch = round(i * 0.1, 2)
        sid = f"s{i}"
        xv.append({"ts_ms": now_ms - 3600_000, "kind": "variant_gate", "signal_id": sid, "chase_pct": ch, "passed": ch <= 5})
        xv.append({"ts_ms": now_ms - 3000_000, "kind": "variant_closed", "arm": "scout_solo", "variant": "A",
                   "signal_id": sid, "pnl_sol": 0.02 if ch <= 3 else -0.03})
        if ch <= 5:
            xv.append({"ts_ms": now_ms - 3000_000, "kind": "variant_closed", "arm": "scout_solo", "variant": "CHASE5",
                       "signal_id": sid, "pnl_sol": 0.02 if ch <= 3 else -0.03})
    (d / "exit_variants.jsonl").write_text("\n".join(json.dumps(r) for r in xv) + "\n")
    v3 = [{"ts_ms": now_ms - 7200_000, "kind": "close", "track": "half2x_c30", "handles": ["Gucci"], "pnl_sol": 500.0,
           "max_multiple": 4000}]
    (d / "paper_v3.jsonl").write_text("\n".join(json.dumps(r) for r in v3) + "\n")


def _run(d: Path):
    env = {**os.environ, "FOMO_DATA_DIR": str(d), "FOMO_COACH_DIR": str(d.parent)}
    r = subprocess.run([sys.executable, str(ROOT / "scripts/learn_daily.py")], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r


def test_learner_moves_cap_one_step_within_bounds_and_writes_note(tmp_path):
    d = tmp_path / "data"
    now = int(time.time() * 1000)
    _seed(d, now)
    _run(d)
    ov = json.loads((d / "learn/paper_overrides.json").read_text())
    assert ov["paper_only"] and ov["values"]["scout_chase_cap_pct"] == 3.0          # 5 → 3 (one step), never < 3
    assert ov["applied"][0]["key"] == "scout_chase_cap_pct"
    notes = list((d / "learn/notes").glob("* Fomo self-learn daily.md"))
    assert notes and "Paper changes applied" in notes[0].read_text() and "artifacts" in notes[0].read_text()
    _run(d)                                                                           # same day: no second change
    st = json.loads((d / "learn/state.json").read_text())
    assert len(st["changes"]) == 1 and st["runs"] == 2
    props = _rows(d / "learn/live_proposals.jsonl")
    assert all(p["status"] == "awaiting_andrew" for p in props)


def test_learner_auto_undo_after_7_days(tmp_path):
    d = tmp_path / "data"
    now = int(time.time() * 1000)
    _seed(d, now)
    (d / "learn").mkdir(parents=True)
    (d / "learn/state.json").write_text(json.dumps({"last_run_ms": 0, "runs": 1, "changes": [
        {"ts_ms": now - 8 * 86400_000, "key": "scout_chase_cap_pct", "old": 5.0, "new": 7.5, "status": "active",
         "baseline_mean": 0.5}]}))
    (d / "learn/paper_overrides.json").write_text(json.dumps({"values": {"scout_chase_cap_pct": 7.5}}))
    _run(d)
    st = json.loads((d / "learn/state.json").read_text())
    assert st["changes"][0]["status"] == "undone"
    ov = json.loads((d / "learn/paper_overrides.json").read_text())
    assert ov["undone"][0]["back_to"] == 5.0


def test_learner_respects_min_n(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    (d / "exit_variants.jsonl").write_text("")
    _run(d)
    ov = json.loads((d / "learn/paper_overrides.json").read_text())
    assert ov["applied"] == [] and ov["values"]["scout_chase_cap_pct"] == 5.0


def test_learner_source_never_touches_live_controls():
    src = (ROOT / "scripts/learn_daily.py").read_text()
    imports = [l.strip() for l in src.splitlines() if l.strip().startswith(("import ", "from "))]
    assert not [l for l in imports if "app" in l.split()[1] or "signer" in l]
    for bad in ("unlink(", "os.remove", "rmtree", "/etc/fomo-signer", "credstore", "open(\"/opt/fomo/coach/.env"):
        assert bad not in src
    assert "write" not in "".join(l for l in src.splitlines() if "KILL" in l or "HALT" in l)
