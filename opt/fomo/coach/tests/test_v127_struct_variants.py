"""v1.27 PAPER structure exit variants: CANDLE_15s/30s/1m, DIAG, HALF2X_C30 (35% hard stop + rug kept)."""
import importlib

import pytest

from app.scout import exit_variants as ev

E = 1.0
T0 = 1_800_000_000_000          # aligned to 60 s


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("EXIT_VARIANTS_LOG_PATH", str(tmp_path / "xv.jsonl"))
    monkeypatch.setenv("EXIT_VARIANTS_STATE_PATH", str(tmp_path / "xv_state.json"))
    for k in ("EXIT_VARIANTS_ENABLED", "EXIT_VAR_CANDLE_TFS", "EXIT_VAR_CANDLE_BUFFER_PCT", "EXIT_VAR_DIAG_TF",
              "EXIT_VAR_DIAG_BUFFER_PCT", "LIVE_HARD_STOP_PCT"):
        monkeypatch.delenv(k, raising=False)


def P():
    p = ev.params()
    p.update(hard_stop_pct=0.35, tp_mult=2.0, tp_frac=0.5, gap_pct=0.10, haircut=0.04, max_age_s=86400)
    return p


def run(variant, path, step_ms=1000, start=T0):
    """path: list of prices, one mark every step_ms. Returns (tracker, closed_at_index)."""
    t = ev.Tracker(signal_id="s", arm="scout_solo", mint="M", variant=variant, lat_s=2, entry_ms=start,
                   entry_price_sol=E, high=E)
    cd = ev.Candles()
    tf = ev.STRUCT[variant]["tf"]
    for i, px in enumerate(path):
        now = start + i * step_ms
        cd.update(px, now, [tf])
        if ev.evaluate(t, px, now, P(), cd):
            return t, i
    return t, None


def hold(px, n):
    return [px] * n                       # one mark per second


def test_variants_registered_and_opened_for_every_signal(tmp_path):
    names = {"CANDLE_15s", "CANDLE_30s", "CANDLE_1m", "DIAG", "HALF2X_C30", "HALF2X_C30_NOSCOUT", "CANDLE15_ARM30"}
    assert names <= set(ev.VARIANTS) and names <= set(ev.LABELS)
    x = ev.ExitVariants()
    assert x.open_signal(arm="scout_confirmed", mint="M", signal_id="1", lat_s=2, entry_ms=T0, entry_price_sol=1.0,
                         scout_wallet="W", chase_pct=1.0) == len(ev.VARIANTS) == 21   # v1.33: +T15/T25/T35/T50; v1.32: +CHASE5, +T60_CANDLE15


def test_candles_from_marks():
    cd = ev.Candles()
    for i, px in enumerate([1.0, 1.2, 0.9, 1.1, 1.3, 1.25, 1.4, 1.0, 2.0]):     # 2 s marks; last one at 16 s
        cd.update(px, T0 + i * 2000, [15])
    c = cd.completed(15)[0]
    assert (c["o"], c["h"], c["l"], c["c"]) == (1.0, 1.4, 0.9, 1.0) and c["t1"] - c["t0"] == 15_000


def test_candle_stop_trails_prev_low_minus_3pct_and_never_lowers():
    path = hold(1.0, 15) + hold(1.5, 15) + hold(1.6, 15) + [1.6] + [1.40]
    t, i = run("CANDLE_15s", path)
    # after the 1.5 candle completes the stop is 1.5*0.97=1.455; then 1.6 candle → 1.552; 1.40 breaks it
    assert i is not None and t.exits[-1]["reason"] == "candle_15s_stop"
    assert t.stop_line == pytest.approx(1.6 * 0.97)


def test_candle_stop_not_hit_inside_buffer():
    path = hold(1.0, 15) + hold(1.5, 15) + [1.46] * 3
    t, i = run("CANDLE_15s", path)
    assert i is None and t.stop_line == pytest.approx(1.455)


def test_candle_30s_and_1m_use_their_own_timeframe():
    path = hold(1.0, 30) + hold(1.5, 30) + [1.5] + [1.44]
    t30, i30 = run("CANDLE_30s", path)
    t60, i60 = run("CANDLE_1m", path)
    assert i30 is not None and t30.exits[-1]["reason"] == "candle_30s_stop"
    assert i60 is None                       # first 1 m candle (low 1.0) → stop 0.97 only


@pytest.mark.parametrize("v", ["CANDLE_15s", "CANDLE_30s", "CANDLE_1m", "DIAG", "HALF2X_C30", "HALF2X_C30_NOSCOUT",
                               "CANDLE15_ARM30"])
def test_hard_stop_35_kept(v):
    t, i = run(v, [1.0, 0.9, 0.64])
    assert i == 2 and t.exits[-1]["reason"] == "hard_stop" and t.exits[-1]["fill_mult"] == pytest.approx(0.65 * 0.96)


@pytest.mark.parametrize("v", ["CANDLE_15s", "DIAG", "HALF2X_C30", "HALF2X_C30_NOSCOUT", "CANDLE15_ARM30"])
def test_rug_flag_exits_struct_only(v, tmp_path):
    x = ev.ExitVariants()
    x.open_signal(arm="scout_solo", mint="M", signal_id="1", lat_s=2, entry_ms=T0, entry_price_sol=1.0, scout_wallet="W")
    assert x.on_rug("M", "rug_flag_creator_dump") == 8   # v1.32: +T60_CANDLE15
    x.on_mark("M", 1.05, T0 + 2000)
    rows = x.rows()
    closed = {r["variant"]: r["exit_reason"] for r in rows}
    assert closed.get(v) == "rug_rug_flag_creator_dump"
    assert "A" not in closed and "T30" not in closed           # original variants untouched


def test_half2x_then_candle30_on_rest():
    path = hold(1.0, 32) + hold(1.6, 32) + [2.1] + hold(2.2, 62) + [2.0]
    t, i = run("HALF2X_C30", path)
    reasons = [x["reason"] for x in t.exits]
    assert reasons[0] == "tp_2x" and t.exits[0]["frac"] == 0.5
    assert i is not None and reasons[-1] == "candle_30s_stop"


def _diag_path():
    # 30 s candles (15 marks each): lows 1.0, 0.9(swing), 1.2, 1.1(swing, higher), 1.4, 1.5 ...
    out = []
    for lo, hi in [(1.0, 1.1), (0.9, 1.05), (1.2, 1.3), (1.1, 1.35), (1.4, 1.5), (1.5, 1.6)]:
        out += [lo] + [hi] * 28 + [lo]    # 30 one-second marks per 30 s candle, close at the low
    return out


def test_diag_arms_after_two_higher_lows_and_exits_on_close_below_line():
    t, i = run("DIAG", _diag_path())
    assert i is None and t.diag and t.diag["l2"] > t.diag["l1"]
    # line rises 0.2 per minute from 1.1; a candle closing far below → diag_break
    t2, i2 = run("DIAG", _diag_path() + [1.2] * 30 + [1.21])
    assert i2 is not None and t2.exits[-1]["reason"] == "diag_break"


def test_diag_no_exit_within_buffer():
    base = _diag_path()
    t, _ = run("DIAG", base)
    line_next = ev.line_at(t.diag, T0 + (len(base) // 30 + 1) * 30_000)
    t2, i2 = run("DIAG", base + [line_next * 0.99] * 30 + [line_next * 0.99])
    assert i2 is None


def test_diag_uses_30pct_trail_before_armed():
    t, i = run("DIAG", [1.0, 1.5, 1.6, 1.11])
    assert i == 3 and t.exits[-1]["reason"] == "diag_pre_trail_30" and t.diag is None


def test_env_tunables(monkeypatch):
    monkeypatch.setenv("EXIT_VAR_CANDLE_TFS", "10, 2m")
    monkeypatch.setenv("EXIT_VAR_CANDLE_BUFFER_PCT", "5")
    monkeypatch.setenv("EXIT_VAR_DIAG_TF", "60")
    monkeypatch.setenv("EXIT_VAR_DIAG_BUFFER_PCT", "1")
    m = importlib.reload(ev)
    try:
        assert {"CANDLE_10s", "CANDLE_2m"} <= set(m.VARIANTS) and "CANDLE_15s" not in m.STRUCT
        assert m.STRUCT["DIAG"]["tf"] == 60 and m.struct_cfg()["buf"] == 0.05 and m.struct_cfg()["diag_buf"] == 0.01
    finally:
        for k in ("EXIT_VAR_CANDLE_TFS", "EXIT_VAR_CANDLE_BUFFER_PCT", "EXIT_VAR_DIAG_TF", "EXIT_VAR_DIAG_BUFFER_PCT"):
            monkeypatch.delenv(k)
        importlib.reload(ev)


def test_summary_lists_new_variants_and_params(tmp_path):
    x = ev.ExitVariants()
    x.open_signal(arm="scout_solo", mint="M", signal_id="1", lat_s=2, entry_ms=T0, entry_price_sol=1.0, scout_wallet="W")
    x.on_mark("M", 0.5, T0 + 2000)                              # everything hard-stops → rows for all
    s = x.summary()
    assert {"CANDLE_15s", "DIAG", "HALF2X_C30"} <= set(s["variants"]) <= set(s["all_arms"]["variants"]) | set(s["variants"])
    assert s["all_arms"]["variants"]["DIAG"]["n"] == 1
    assert s["rules"]["structure_v127"]["buf"] == 0.03 and s["rules"]["structure_v127"]["diag_tf"] == 30


def test_restored_old_state_without_new_fields_loads(tmp_path):
    import json
    p = ev.state_path()
    p.write_text(json.dumps({"open": [{"signal_id": "9", "arm": "scout_solo", "mint": "Q", "variant": "A", "lat_s": 2,
                                       "entry_ms": T0, "entry_price_sol": 1.0}]}))
    x = ev.ExitVariants()
    x.load()
    assert x.stats["restored"] == 1


def test_half2x_pre2x_scout_sell_kept_vs_noscout_ignores_it():
    t = ev.Tracker(signal_id="s", arm="scout_solo", mint="M", variant="HALF2X_C30", lat_s=2, entry_ms=T0,
                   entry_price_sol=E, high=E, trader_sold=True)
    assert ev.evaluate(t, 1.1, T0 + 1000, P(), ev.Candles()) and t.exits[-1]["reason"] == "trader_sold"
    n = ev.Tracker(signal_id="s", arm="scout_solo", mint="M", variant="HALF2X_C30_NOSCOUT", lat_s=2, entry_ms=T0,
                   entry_price_sol=E, high=E, trader_sold=True)
    assert not ev.evaluate(n, 1.1, T0 + 1000, P(), ev.Candles())


def test_half2x_no_candle_stop_before_2x_but_min10_applies():
    # 1.5 then drop to 1.3: a candle stop would have fired; pre-2x live rules don't. At minute 10 below entry → exit.
    path = hold(1.0, 15) + hold(1.5, 30) + [1.3] * 10
    t, i = run("HALF2X_C30_NOSCOUT", path)
    assert i is None and t.stop_line is None
    t2, i2 = run("HALF2X_C30_NOSCOUT", [1.0] + [0.9] * 601)
    assert i2 is not None and t2.exits[-1]["reason"] == "below_entry_min10"


def test_arm30_waits_for_1_3x_then_15s_low_minus_5pct_ratchets():
    # below 1.3x: dips do nothing (no candle stop), only hard stop / min-10
    t, i = run("CANDLE15_ARM30", hold(1.0, 15) + hold(1.25, 15) + [1.05] * 5)
    assert i is None and t.armed_ms is None and t.stop_line is None
    path = hold(1.0, 15) + hold(1.35, 15) + hold(1.6, 15) + [1.6] + [1.50]
    t, i = run("CANDLE15_ARM30", path)
    assert t.armed_ms == T0 + 15_000
    assert t.stop_line == pytest.approx(1.6 * 0.95) and i is not None and t.exits[-1]["reason"] == "candle_15s_stop"
    assert t.exits[-1]["frac"] == 1.0


def test_arm30_ignores_scout_sell_and_uses_min10():
    t = ev.Tracker(signal_id="s", arm="scout_solo", mint="M", variant="CANDLE15_ARM30", lat_s=2, entry_ms=T0,
                   entry_price_sol=E, high=E, trader_sold=True)
    assert not ev.evaluate(t, 1.1, T0 + 1000, P(), ev.Candles())
    assert ev.evaluate(t, 0.95, T0 + 601_000, P(), ev.Candles()) and t.exits[-1]["reason"] == "below_entry_min10"


def test_stop_never_lowers():
    path = hold(1.0, 15) + hold(1.5, 15) + hold(1.47, 15) + [1.47]
    t, i = run("CANDLE_15s", path)
    assert i is None and t.stop_line == pytest.approx(1.5 * 0.97)      # 1.47 candle → 1.426 < 1.455, kept
