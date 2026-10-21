"""v1.18 exit-variants PAPER add-on: variant math, live pre-2x / runner rules, off switch, persistence, summary,
and proof that live exits and the scout paper positions are unchanged."""
import asyncio
import json
import time

import pytest

from app.scout import ARM_A, ARM_C
from app.scout import engine as eng_mod, exit_variants as ev_mod, filters as filt_mod, roster as roster_mod
from app.scout import source as src_mod

SCOUT = "4BdKaxN8G6ka4GYtQQWk4G4dZRUTX2vQH9GcXdBREFUk"
MINT = "DwcXyhEcSvzWgakDKpetZLFvbAHutpGpzhbU4iempump"
CREATOR = "862TYSvRYoiHAK3F3WwTRYAfuGiQaGdxedN9AGvRGWo2"
VTR0, VSR0 = 1_073_000_000 * 10**6, 30 * 10**9
T0 = 1_791_400_000_000


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("EXIT_VARIANTS_LOG_PATH", str(tmp_path / "exit_variants.jsonl"))
    monkeypatch.setenv("EXIT_VARIANTS_STATE_PATH", str(tmp_path / "exit_variants_state.json"))
    monkeypatch.delenv("EXIT_VARIANTS_ENABLED", raising=False)
    for k in ("LIVE_TRAIL_PCT", "LIVE_TP_MULT", "LIVE_TP_FRAC", "LIVE_HARD_STOP_PCT", "LIVE_MIN10_S", "LIVE_MAX_HOLD_S",
              "LIVE_RUNNER_MAX_HOLD_MIN", "LIVE_GAP_PCT"):
        monkeypatch.delenv(k, raising=False)
    ev_mod.variants.__init__()
    yield tmp_path
    ev_mod.variants.__init__()          # never leak open trackers (they would keep later tests' mark loops alive)
    eng_mod.engine.__init__()
    filt_mod.filters.__init__()          # the integration flow records a dump for CREATOR — don't leak it


def P(**kw):
    p = ev_mod.params()
    p.update(kw)
    return p


def trk(variant="A", entry=1.0, wallet=None):
    return ev_mod.Tracker(signal_id="s1", arm=ARM_A, mint=MINT, variant=variant, lat_s=2, entry_ms=T0,
                          entry_price_sol=entry, scout_wallet=wallet, high=entry)


def feed(t, prices, step_s=5, p=None):
    p = p or P()
    for i, px in enumerate(prices):
        if ev_mod.evaluate(t, px, T0 + (i + 1) * step_s * 1000, p):
            break
    return t


def exp(*fills, overhead=0.015):
    return round(0.10 * (sum(f * px * 0.96 for f, px in fills) - 1 - overhead), 6)


def test_params_mirror_live_defaults():
    p = ev_mod.params()
    assert (p["tp_mult"], p["tp_frac"], p["trail_pct"], p["hard_stop_pct"]) == (2.0, 0.5, 0.30, 0.35)
    assert (p["min10_s"], p["max_hold_s"], p["gap_pct"], p["haircut"], p["overhead"]) == (600, 3600, 0.10, 0.04, 0.015)
    assert set(ev_mod.VARIANTS) - set(ev_mod.STRUCT) == {"A", "B", "C", "C2", "E", "F", "T30", "T30F", "CHASE5", "T15", "T25", "T35", "T50"}   # v1.21 + T30/T30F + v1.32 CHASE5


def test_variant_a_tp_then_trail_line_fill():
    t = feed(trk("A"), [1.5, 2.1, 3.0, 2.05])          # line 2.1; 2.05 is <10% past → fills at the line
    assert t.status == "closed" and [x["reason"] for x in t.exits] == ["tp_2x", "trail_30"]
    assert t.pnl_sol(0.015) == exp((0.5, 2.1), (0.5, 2.1))


def test_gap_past_line_fills_at_mark():
    t = feed(trk("A"), [2.1, 3.0, 1.5])                # line 2.1, 1.5 is >10% past → fills at 1.5
    assert t.exits[-1]["fill_mult"] == round(1.5 * 0.96, 4)
    assert t.pnl_sol(0.015) == exp((0.5, 2.1), (0.5, 1.5))


def test_variant_b_two_quarters_fire_separately():
    t = feed(trk("B"), [2.0, 3.0, 2.38])               # 20% line 2.4 → quarter at 2.4; 40% line 1.8 not hit
    assert t.status == "open" and t.rem == pytest.approx(0.25) and t.exits[-1]["reason"] == "trail_20"
    feed(t, [4.0, 2.3])                                # new high 4.0 → 40% line 2.4; 2.3 is <10% past → 2.4
    assert t.status == "closed" and [x["reason"] for x in t.exits] == ["tp_2x", "trail_20", "trail_40"]
    assert t.pnl_sol(0.015) == exp((0.5, 2.0), (0.25, 2.4), (0.25, 2.4))


def test_both_quarters_same_mark_and_c_c2_e_f():
    t = feed(trk("C2"), [2.0, 4.0, 1.9])               # 25% line 3.0 and 50% line 2.0 both crossed by one mark
    assert t.status == "closed" and {x["reason"] for x in t.exits[1:]} == {"trail_25", "trail_50"}
    for v, reason in (("E", "trail_20"), ("F", "trail_40")):
        t = feed(trk(v), [2.0, 3.0, 1.6])
        assert t.status == "closed" and t.exits[-1]["reason"] == reason and t.exits[-1]["frac"] == 0.5


def test_pre_2x_rules_hard_stop_min10_max_hold_trader_sold():
    t = feed(trk("A"), [0.9, 0.6])                     # line 0.65, crossed by <10% → fills at the line
    assert t.exits[-1]["reason"] == "hard_stop" and t.exits[-1]["fill_mult"] == round(0.65 * 0.96, 4)
    t = feed(trk("A"), [0.9, 0.5])                     # gap past the line → fills at the mark
    assert t.exits[-1]["fill_mult"] == round(0.5 * 0.96, 4)
    t = trk("A")
    ev_mod.evaluate(t, 0.95, T0 + 601_000, P())
    assert t.status == "closed" and t.exits[-1]["reason"] == "below_entry_min10"
    t = trk("A")
    ev_mod.evaluate(t, 1.2, T0 + 601_000, P())          # above entry at minute 10 → keeps going
    assert t.status == "open" and t.min10_done
    ev_mod.evaluate(t, 1.3, T0 + 3_601_000, P())
    assert t.exits[-1]["reason"] == "max_hold"
    t = trk("A", wallet=SCOUT)
    t.trader_sold = True
    ev_mod.evaluate(t, 1.4, T0 + 5_000, P())
    assert t.exits[-1]["reason"] == "trader_sold"


def test_runner_ignores_trader_sold_and_max_hold_until_below_entry():
    t = feed(trk("C", wallet=SCOUT), [2.0])
    t.trader_sold = True
    ev_mod.evaluate(t, 1.9, T0 + 4_000_000, P())         # runner (≥ entry): no trader_sold, no max hold
    assert t.status == "open" and [x["reason"] for x in t.exits] == ["tp_2x"]
    ev_mod.evaluate(t, 1.5, T0 + 4_005_000, P())         # 30% line 1.4 not hit
    assert t.status == "open"
    ev_mod.evaluate(t, 0.98, T0 + 4_010_000, P())        # below entry → no longer a runner → max hold first
    assert t.status == "closed" and t.exits[-1]["reason"] in {"max_hold", "trail_30"}


def test_below_entry_after_2x_trader_sold_applies():
    t = feed(trk("F", wallet=SCOUT), [2.0])
    t.trader_sold = True
    ev_mod.evaluate(t, 1.05, T0 + 20_000, P(trail_pct=0.30))   # 40% line 1.2 → trail fires first
    assert t.exits[-1]["reason"] == "trail_40"
    t = feed(trk("C", wallet=SCOUT), [2.0, 2.0])
    ev_mod.evaluate(t, 1.45, T0 + 20_000, P())                  # 30% line 1.4 not hit
    t.trader_sold = True
    ev_mod.evaluate(t, 0.99, T0 + 25_000, P())                  # below entry: trader_sold (as live) — sells all
    assert t.status == "closed" and t.exits[-1]["reason"] in {"trader_sold", "trail_30"}


def test_off_switch_opens_nothing(monkeypatch, _iso):
    monkeypatch.setenv("EXIT_VARIANTS_ENABLED", "false")
    V = ev_mod.variants
    assert V.open_signal(arm=ARM_A, mint=MINT, signal_id="x", lat_s=2, entry_ms=T0, entry_price_sol=1.0,
                         scout_wallet=None) == 0
    assert V.open_mints() == set() and V.on_mark(MINT, 2.0, T0) == 0
    assert not (_iso / "exit_variants.jsonl").exists() and not (_iso / "exit_variants_state.json").exists()
    assert V.summary()["enabled"] is False


def test_rows_summary_and_restart_restore(_iso):
    V = ev_mod.variants
    now = int(time.time() * 1000)
    assert V.open_signal(arm=ARM_A, mint=MINT, signal_id="sigA", lat_s=2, entry_ms=now, entry_price_sol=1.0,
                         scout_wallet=SCOUT, chase_pct=1.0) == len(ev_mod.VARIANTS)
    assert V.open_signal(arm=ARM_C, mint="M2", signal_id="sigC", lat_s=2, entry_ms=now, entry_price_sol=1.0,
                         scout_wallet=None, chase_pct=1.0) == len(ev_mod.VARIANTS)
    for px in (2.0, 3.0, 2.3, 1.7, 1.4, 1.0):
        V.on_mark(MINT, px, now + 1000)
    rows = V.rows()
    assert {r["variant"] for r in rows if r["variant"] not in ev_mod.STRUCT} == set(ev_mod.VARIANTS) - set(ev_mod.STRUCT) and all(r["signal_id"] == "sigA" for r in rows)
    r0 = rows[0]
    for k in ("signal_id", "arm", "mint", "variant", "entry_price_sol", "exits", "pnl_sol"):
        assert k in r0
    s = V.summary()
    assert s["all_arms"]["signals"] == 1 and s["live_like"]["signals"] == 1 and s["live_like"]["arms"] == [ARM_A]
    va = s["all_arms"]["variants"]
    assert va["A"]["n"] == 1 and va["E"]["beats_A"] + va["E"]["loses_to_A"] <= 1 and va["A"]["win_rate"] == 1.0
    assert s["all_arms"]["reached_2x"] == 1 and s["open_signals"] >= 1     # v1.27 structure trackers may still be open
    # restart: the open smugcalls trackers come back flagged restart_gap; their rows are excluded by default
    W = ev_mod.ExitVariants()
    W.load()
    assert sum(1 for t in W.trackers.values() if t.signal_id == "sigC" and not t.realistic) == len(ev_mod.VARIANTS)
    # v1.38: the realistic twins are persisted and restored alongside (their rows go to variant_closed_real)
    assert sum(1 for t in W.trackers.values() if t.signal_id == "sigC" and t.realistic) == len(ev_mod.VARIANTS)
    assert all("restart_gap" in t.flags for t in W.trackers.values())
    W.on_mark("M2", 0.5, now + 2000)
    assert all(r["restart_gap"] for r in W.rows() if r["signal_id"] == "sigC")
    assert W.summary()["all_arms"]["signals"] == 1 and W.summary()["restart_gap_rows_excluded"] == len(ev_mod.VARIANTS)
    assert W.summary(include_restart_gap=True)["all_arms"]["signals"] == 2


def test_live_evaluate_exit_unchanged_with_variants_on_or_off(monkeypatch):
    """Live exits (A: 50% at 2x, 30% trail, runner rule) give identical decisions whether the paper add-on is on."""
    from app.live import config as lcfg
    from app.live.engine import LivePos, evaluate_exit

    def scenarios():
        cfg = lcfg.config()
        rules = cfg["rules"]["scout_solo"]
        out = []

        def pos(**kw):
            base = dict(mint=MINT, arm="scout_solo", size_sol=0.1, spent_lamports=100_000_000,
                        tokens_initial=1_000_000, tokens_left=1_000_000, entry_price_sol=1.0, entry_ms=T0,
                        high_price_sol=1.0)
            base.update(kw)
            return LivePos(**base)
        out.append(evaluate_exit(pos(high_price_sol=2.05), 2.05, T0 + 5000, cfg, rules))
        out.append(evaluate_exit(pos(high_price_sol=3.0, tp_done=True, tokens_left=500_000), 2.05, T0 + 9000, cfg, rules))
        out.append(evaluate_exit(pos(high_price_sol=3.0, tp_done=True, tokens_left=500_000, trader_sold=True), 2.5,
                                 T0 + 4_000_000, cfg, rules))
        out.append(evaluate_exit(pos(trader_sold=True), 1.1, T0 + 5000, cfg, rules))
        out.append(evaluate_exit(pos(), 0.6, T0 + 5000, cfg, rules))
        out.append(evaluate_exit(pos(), 0.9, T0 + 601_000, cfg, rules))
        return cfg, out

    monkeypatch.setenv("EXIT_VARIANTS_ENABLED", "true")
    cfg_on, on = scenarios()
    monkeypatch.setenv("EXIT_VARIANTS_ENABLED", "false")
    cfg_off, off = scenarios()
    assert on == off
    assert on[0] == [("sell_frac", 0.5, "tp_2x")] and on[1] == [("sell_all", 1.0, "trail")]
    assert on[2] == [] and on[3] == [("sell_all", 1.0, "trader_sold")] and on[4][0][2] == "hard_stop"
    assert on[5][0][2] == "below_entry_min10"
    for k in ("tp_mult", "tp_frac", "trail_pct", "hard_stop_pct", "min10_s", "max_hold_s", "rules", "arms"):
        assert cfg_on[k] == cfg_off[k]
    assert (cfg_on["tp_mult"], cfg_on["tp_frac"], cfg_on["trail_pct"]) == (2.0, 0.5, 0.30)


def test_module_never_touches_live_engine_or_env():
    src = open(ev_mod.__file__).read()
    assert "app.live.engine" not in src and "live_engine" not in src
    assert "os.environ[" not in src and "setenv" not in src and "putenv" not in src
    assert "jupiter" not in src.lower() and "sendtransaction" not in src.lower()


# ---- integration with the scout paper engine (paper positions must be byte-identical with the add-on on/off) ----
def _curve(vsr=VSR0, vtr=VTR0):
    return {"virtual_token_reserves": vtr, "virtual_quote_reserves": vsr, "real_token_reserves": 793_100_000 * 10**6,
            "real_quote_reserves": max(0, vsr - VSR0), "complete": False, "quote_is_sol": True, "creator": CREATOR,
            "read_at_ms": int(time.time() * 1000)}


def _ev(sol=1.0, is_buy=True):
    return {"mint": MINT, "sol_amount": int(sol * 1e9), "token_amount": 34_000_000 * 10**6, "is_buy": is_buy,
            "user": SCOUT, "timestamp": int(time.time()), "virtual_sol_reserves": VSR0 + 10**9,
            "virtual_token_reserves": VTR0 - 34_000_000 * 10**6, "real_sol_reserves": 10**9,
            "real_token_reserves": 700_000_000 * 10**6, "creator": CREATOR}


def _run_flow(tmp_path, monkeypatch, enabled_flag):
    monkeypatch.setenv("EXIT_VARIANTS_ENABLED", enabled_flag)
    monkeypatch.setattr(eng_mod, "LOG_PATH", tmp_path / f"scout_{enabled_flag}.jsonl")
    monkeypatch.setattr(filt_mod, "PATH", tmp_path / f"f_{enabled_flag}.json")
    monkeypatch.setenv("SCOUT_LATENCIES", "2,5")
    eng_mod.engine.__init__()
    filt_mod.filters.__init__()
    roster_mod.roster.__init__()
    roster_mod.roster.add(SCOUT, "Jijo", "scout")
    ev_mod.variants.__init__()
    from app.fomo_onchain import market
    monkeypatch.setattr(market, "sol_usd", lambda: 150.0)
    state = {"curve": _curve()}

    async def age(mint, max_pages=3):
        return {"ok": True, "created_s": time.time() - 120, "first_sigs": ["c"] + [f"s{i}" for i in range(25)]}

    async def rc(mint):
        return dict(state["curve"], read_at_ms=int(time.time() * 1000))

    async def rcs(mints):
        return {m: dict(state["curve"], read_at_ms=int(time.time() * 1000)) for m in mints}
    monkeypatch.setattr(src_mod.source, "token_age", age)
    monkeypatch.setattr(src_mod.source, "read_curve", rc)
    monkeypatch.setattr(src_mod.source, "read_curves", rcs)
    monkeypatch.setattr(eng_mod.engine, "scan_first_buyers", lambda mint: asyncio.sleep(0))
    calls = []
    from app.live.engine import engine as live_engine
    monkeypatch.setattr(live_engine, "note_trader_sell", lambda w, m: calls.append((w, m)))

    async def go():
        E = eng_mod.engine
        E.on_trade(roster_mod.roster.by_wallet[SCOUT], _ev(), "sigA", int(time.time() * 1000) - 10_000)
        for _ in range(50):
            await asyncio.sleep(0.01)
            if not E._tasks:
                break
        for mult in (1.55, 2.0, 1.1, 0.75):
            state["curve"] = _curve(vsr=int(VSR0 * mult), vtr=int(VTR0 / mult))
            await E.mark_mints([MINT])
        E.on_trade(roster_mod.roster.by_wallet[SCOUT], _ev(is_buy=False), "sigS", int(time.time() * 1000))
        await E.mark_mints([MINT])
        for _ in range(20):
            await asyncio.sleep(0.01)
        return E
    E = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(go())
    snap = sorted((p.rule, p.lat_s, p.status, p.exit_reason, p.tp_done, [(f["reason"], f["tokens"]) for f in p.fills])
                  for p in E.positions.values())
    return E, snap, calls


def test_scout_paper_positions_identical_with_addon_on_and_off(tmp_path, monkeypatch):
    E_on, snap_on, calls_on = _run_flow(tmp_path, monkeypatch, "true")
    rows_on = ev_mod.variants.rows()
    rows_on_raw = list(rows_on)
    E_off, snap_off, calls_off = _run_flow(tmp_path, monkeypatch, "false")
    assert snap_on == snap_off and len(snap_on) == 8
    assert calls_on == calls_off == [(SCOUT, MINT)]          # live trader-sell hook still fires exactly once
    # add-on: 6 variants on the +2 s fill only, all closed by this path, one JSONL row each
    rows_on = [r for r in rows_on if r["variant"] not in ev_mod.STRUCT]     # v1.27 structure variants judged elsewhere
    assert {r["variant"] for r in rows_on} == set(ev_mod.VARIANTS) - set(ev_mod.STRUCT)
    assert all(r["arm"] == ARM_A and r["lat_s"] == 2 and r["reached_2x"] for r in rows_on)
    assert ev_mod.variants.rows() == rows_on_raw               # off run wrote nothing new


def test_mark_loop_keeps_running_for_open_variant_only(tmp_path, monkeypatch):
    eng_mod.engine.__init__()
    V = ev_mod.variants
    V.open_signal(arm=ARM_A, mint=MINT, signal_id="z", lat_s=2, entry_ms=int(time.time() * 1000),
                  entry_price_sol=1.0, scout_wallet=None)
    assert eng_mod.engine._open_mints() == {MINT}
    monkeypatch.setenv("EXIT_VARIANTS_ENABLED", "false")
    assert eng_mod.engine._open_mints() == set()


def test_summary_endpoint():
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        r = c.get("/api/exit-variants/summary")
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] and d["paper_only"] and set(d["all_arms"]["variants"]) == set(ev_mod.VARIANTS)
        assert "live_like" in d and d["rules"]["live_numbers"]["trail_pct"] == 0.30
