"""v1.33 live exit/entry changes (Oct 15, 2026)."""
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from app.live import engine as E
from app.live.config import config

CT = ZoneInfo("America/Chicago")


def cfg(**kw):
    c = dict(config())
    c.update(trail_pct=0.20, tg_trail_pct=0.30, hard_stop_pct=0.35, candle_stop_enabled=True, candle_stop_s=15.0,
             candle_stop_buf_pct=0.03, candle_stop_arms=["scout_solo", "scout_confirmed"], trader_sell_window_s=30.0,
             night_pause_enabled=True, night_pause_start_h=22, night_pause_end_h=6,
             night_pause_exempt_arms=["fluffy_pump_dump", "zyrox"], fast_cut_enabled=False, fast_tp_enabled=False,
             runner_max_hold_s=0)
    c.update(kw)
    return c


def pos(arm="scout_solo", entry=1.0, t0=1_000_000):
    return E.LivePos(mint="M", arm=arm, size_sol=0.2, spent_lamports=1, tokens_initial=1000, tokens_left=1000,
                     entry_price_sol=entry, entry_ms=t0)


RULES = ["tp2x_trail", "min10_check", "hard_stop", "trader_sells", "max_hold"]


def test_trail_20_for_scout_runner():
    c = cfg(); p = pos(); p.tp_done = True; p.ladder_done = ["tp_2x"]; p.high_price_sol = 3.0
    assert E.evaluate_exit(p, 2.35, p.entry_ms + 60_000, c, RULES) == [("sell_all", 1.0, "trail")]   # -21.7%
    p.high_price_sol = 3.0
    assert E.evaluate_exit(p, 2.5, p.entry_ms + 60_000, c, RULES) == []                             # -16.7%


def test_tg_arm_keeps_30_trail():
    c = cfg(); p = pos(arm="fluffy_pump_dump"); p.tp_done = True; p.ladder_done = ["tp1_2x"]; p.high_price_sol = 2.9
    rules = ["ladder", "hard_stop", "max_hold"]
    assert E.trail_for(p, c) == 0.30
    assert E.evaluate_exit(p, 2.25, p.entry_ms + 60_000, c, rules) == []          # -22% < 30%: hold
    assert not E.candle_arm(p, c)


def test_candle_stop_prev_low_minus_3():
    c = cfg(); p = pos(); t = p.entry_ms
    for i, px in enumerate([1.0, 1.05, 1.1, 1.08]):                       # candle 0: low 1.0
        assert E.evaluate_exit(p, px, t + i * 3000, c, RULES) == []
    assert E.evaluate_exit(p, 1.06, t + 16_000, c, RULES) == []           # candle 1 opens, prev low 1.0 → line 0.97
    assert E.evaluate_exit(p, 0.98, t + 18_000, c, RULES) == []
    out = E.evaluate_exit(p, 0.96, t + 20_000, c, RULES)
    assert out == [("sell_all", 1.0, "candle_stop")]


def test_candle_stop_off_after_2x_and_hard_stop_backstop():
    c = cfg(); p = pos(); p.tp_done = True; p.ladder_done = ["tp_2x"]; p.high_price_sol = 2.0
    assert E.evaluate_exit(p, 1.7, p.entry_ms + 20_000, c, RULES) == []
    q = pos()
    assert E.evaluate_exit(q, 0.6, q.entry_ms + 1000, c, RULES) == [("sell_all", 1.0, "hard_stop")]


def test_trader_sell_window():
    c = cfg(); p = pos(); p.trader_sold = True; p.meta["trader_sold_ms"] = p.entry_ms + 10_000
    assert E.evaluate_exit(p, 1.0, p.entry_ms + 11_000, c, RULES) == [("sell_all", 1.0, "trader_sold")]
    q = pos(); q.trader_sold = True; q.meta["trader_sold_ms"] = q.entry_ms + 45_000
    assert E.evaluate_exit(q, 1.0, q.entry_ms + 46_000, c, RULES) == []
    r = pos(); r.trader_sold = True                                   # unknown time → honored (safe)
    assert E.evaluate_exit(r, 1.0, r.entry_ms + 46_000, c, RULES) == [("sell_all", 1.0, "trader_sold")]
    s = pos(); s.trader_sold = True; s.meta["trader_sold_ms"] = s.entry_ms + 45_000
    assert E.evaluate_exit(s, 1.0, s.entry_ms + 46_000, cfg(trader_sell_window_s=0), RULES)[0][2] == "trader_sold"


def ms(h):
    return int(datetime(2026, 10, 15, h, 30, tzinfo=CT).timestamp() * 1000)


def test_night_pause_exempts_tg():
    c = cfg()
    assert E.night_paused("scout_solo", c, ms(23)) and E.night_paused("scout_solo", c, ms(3))
    assert not E.night_paused("scout_solo", c, ms(6)) and not E.night_paused("scout_solo", c, ms(21))
    assert not E.night_paused("fluffy_pump_dump", c, ms(23)) and not E.night_paused("zyrox", c, ms(2))
    assert not E.night_paused("scout_solo", cfg(night_pause_enabled=False), ms(23))
