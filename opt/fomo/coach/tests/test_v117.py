"""v1.17: wallet scoring (advisory), rug / bundle pre-buy checks, open-position rug watch, gap-down exits, fast-TP flag,
fast-fill prep (OFF), account-watch poll fallback, learning-log stage rows. Everything mocked: no network, no live
state, no keypair, no real swaps."""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from app.learning import scorecard
from app.learning import wallet_scores as ws
from app.live import config as live_config, fastfill, jupiter, rugcheck
from app.live.engine import LiveEngine, LivePos, evaluate_exit, fast_tp, gap_down

MINT = "MintAAAA1111111111111111111111111111111pump"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def env(monkeypatch, tmp_path):
    for k, v in {"JUPITER_EXEC_ENABLED": "true", "LIVE_AUTOTRADE_ENABLED": "true",
                 "LIVE_ARMS": "scout_solo,scout_confirmed,fluffy_pump_dump,zyrox",
                 "LIVE_SIZE_SOL_SCOUT_SOLO": "0.10", "LIVE_SIZE_SOL_FLUFFY_PUMP_DUMP": "0.40",
                 "LIVE_SIZE_SOL_ZYROX": "0.40", "LIVE_MAX_SOL_PER_TRADE": "0.40", "LIVE_MIN_SOL_RESERVE": "0.50",
                 "LIVE_STATE_PATH": str(tmp_path / "state.json"), "LIVE_LEDGER_PATH": str(tmp_path / "ledger.jsonl"),
                 "LIVE_KILL_FILE": str(tmp_path / "KILL"), "LIVE_SELL_RETRY_S": "0",
                 "LIVE_SELL_STEP_BACKOFF_S": "0", "CANDIDATES_PATH": str(tmp_path / "candidates.jsonl"),
                 "LEARNING_ENABLED": "true"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(jupiter, "pubkey", lambda: "FakePubkey111")

    async def ok(self, mint):
        return True, "mesh ok"
    monkeypatch.setattr(LiveEngine, "_mesh_check", ok)
    return tmp_path


def _pos(**kw) -> LivePos:
    base = dict(mint=MINT, arm="scout_solo", size_sol=0.1, spent_lamports=100_000_000, tokens_initial=1_000_000_000,
                tokens_left=1_000_000_000, entry_price_sol=0.0001, entry_ms=int(time.time() * 1000))
    base.update(kw)
    return LivePos(**base)


async def _drain(e):
    for _ in range(6):
        if not e._tasks:
            return
        await asyncio.gather(*list(e._tasks), return_exceptions=True)


def _rows(path):
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


# ======================= wallet scoring (pure parts) =======================
def test_returns_from_candles_entry_and_horizons():
    t = 1_000_020                                    # inside the minute starting 999_960
    t0 = t // 60 * 60
    cs = [[t0, 1, 1, 1, 1.0, 0], [t0 + 300, 0, 0, 0, 1.5, 0], [t0 + 900, 0, 0, 0, 0.5, 0], [t0 + 3600, 0, 0, 0, 2.0, 0]]
    r = ws.returns_from_candles(cs, t, now_s=t + 7200)
    assert r["p0"] == 1.0 and r["ret_5m"] == 0.5 and r["ret_15m"] == -0.5 and r["ret_60m"] == 1.0
    # horizon not reached yet → None; no candle at/before the buy → no entry
    r2 = ws.returns_from_candles(cs, t, now_s=t + 600)
    assert r2["ret_5m"] == 0.5 and r2["ret_15m"] is None and r2["ret_60m"] is None
    assert ws.returns_from_candles([[t0 + 600, 0, 0, 0, 1.0, 0]], t, t + 7200)["p0"] is None


def test_wilson_and_bootstrap():
    lo, hi = ws.wilson(5, 10)
    assert 0.2 < lo < 0.5 < hi < 0.8
    assert ws.wilson(0, 0) is None
    ci = ws.boot_ci([0.1] * 20)
    assert ci == [0.1, 0.1]


def _c(**kw):
    c = ws.cfg()
    c.update(kw)
    return c


def test_score_wallet_flags_dead_and_poor():
    now = 2_000_000_000.0
    w = {"wallet": "W1", "label": "x", "role": "scout"}
    # inactive: last trade 9 days ago
    meta = {"history_ok": True, "trades": [{"bt": now - 9 * 86400, "is_buy": True, "mint": "M", "sol_amount": 1e9}]}
    r = ws.score_wallet(w, [], meta, {}, _c(), now)
    assert "dead_inactive" in r["flags"] and r["recommendations"]
    # active but statistically poor with n ≥ 20
    buys = [{"ret_5m": -0.3, "ret_15m": -0.4, "ret_60m": -0.6} for _ in range(25)]
    meta2 = {"history_ok": True, "trades": [{"bt": now - 3600, "is_buy": True, "mint": "M", "sol_amount": 1e9}]}
    r2 = ws.score_wallet(w, buys, meta2, {}, _c(), now)
    assert "dead_poor_record" in r2["flags"] and r2["onchain"]["15m"]["n"] == 25
    assert r2["score"] < 0
    # small sample → insufficient_n, never flagged poor
    r3 = ws.score_wallet(w, buys[:5], meta2, {}, _c(), now)
    assert "insufficient_n" in r3["flags"] and "dead_poor_record" not in r3["flags"]
    # history unavailable → never called dead
    r4 = ws.score_wallet(w, [], {"history_ok": False, "trades": []}, {}, _c(), now)
    assert r4["flags"] == ["history_unavailable", "insufficient_n"] and not r4["recommendations"]


def test_score_shrinks_small_samples():
    now = 2_000_000_000.0
    meta = {"history_ok": True, "trades": [{"bt": now - 60, "is_buy": True, "mint": "M"}]}
    w = {"wallet": "W", "label": "w", "role": "scout"}
    one = ws.score_wallet(w, [{"ret_15m": 1.0}], meta, {}, _c(), now)["score"]
    many = ws.score_wallet(w, [{"ret_15m": 1.0}] * 40, meta, {}, _c(), now)["score"]
    assert 0 < one < many < 1.0


def test_compute_is_advisory_and_ranked(monkeypatch):
    async def trades(self, w, since):
        return {"history_ok": True, "trades": [{"bt": time.time() - 4000, "is_buy": True, "mint": "M" + w["wallet"],
                                                "sol_amount": 1e9}], "txs_fetched": 1, "sigs_in_window": 1}

    async def rets(self, mint, t_s, now_s=None):
        return {"p0": 1.0, "ret_5m": 0.1, "ret_15m": 0.5 if mint.endswith("A") else -0.5, "ret_60m": 0.0, "src": "x"}
    monkeypatch.setattr(ws.Collector, "wallet_trades", trades)
    monkeypatch.setattr(ws.Collector, "returns_after", rets)
    out = run(ws.compute(wallets=[{"wallet": "B", "label": "b", "role": "scout"},
                                  {"wallet": "A", "label": "a", "role": "scout"}]))
    assert out["advisory_only"] is True and [r["wallet"] for r in out["wallets"]] == ["A", "B"]
    assert out["wallets"][0]["rank"] == 1
    ws.write(out)
    assert ws.read()["wallets"][0]["wallet"] == "A"


def test_wallet_scores_endpoint_read_only():
    from fastapi.testclient import TestClient
    from app.main import app
    ws.write({"generated_ms": 1, "wallets": [{"wallet": "A", "role": "scout"}, {"wallet": "B", "role": "fomo"}],
              "note": "Advisory only"})
    with TestClient(app) as c:
        j = c.get("/api/wallets/scores?role=fomo").json()
        assert j["advisory_only"] and j["read_only"] and [w["wallet"] for w in j["wallets"]] == ["B"]
        r = c.get("/api/live/rugcheck").json()
        assert r["read_only"] and "thresholds" in r


def test_nothing_reads_scores_for_weighting():
    """Advisory only: no trading / scout module imports wallet_scores."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1] / "app"
    users = [p for p in root.rglob("*.py") if "wallet_scores" in p.read_text() and p.name != "wallet_scores.py"]
    # v1.22: paper_momentum.py (PAPER ONLY, Sky 2026-10-09) weights its cluster signal by wallet score — no live use
    assert sorted(p.name for p in users) == ["main.py", "paper_momentum.py"]


# ======================= rug checks (pure evaluate) =======================
def _rc(**kw):
    c = rugcheck.cfg()
    c.update(kw)
    return c


LAUNCH_OK = {"ok": True, "creator": "Dev", "dev_buy_tokens": 50 * 10**12, "bundle_buyers": 1,
             "bundle_supply_pct": 4.0, "linked": ["L1"]}


def test_evaluate_all_pass():
    facts = {"launch": LAUNCH_OK, "holdings": {"Dev": 50 * 10**12, "L1": 10**12},
             "jup": {"top_holders_pct": 20.0, "dev_mints": 3}, "creator": None}
    res = rugcheck.evaluate(facts, _rc())
    assert {k: v["status"] for k, v in res.items()} == {k: "pass" for k in rugcheck.CHECKS}


def test_evaluate_each_failure():
    big = {"ok": True, "creator": "Dev", "dev_buy_tokens": 50 * 10**12, "bundle_buyers": 9,
           "bundle_supply_pct": 30.0, "linked": ["L1", "L2"]}
    facts = {"launch": big, "holdings": {"Dev": 10**12, "L1": 150 * 10**12, "L2": 100 * 10**12},
             "jup": {"top_holders_pct": 60.0, "dev_mints": 300}}
    res = rugcheck.evaluate(facts, _rc())
    assert res["bundle"]["status"] == "fail" and res["holdings"]["status"] == "fail"
    assert res["top10"]["status"] == "fail" and res["dev"]["status"] == "fail"       # creator sold 98% of launch buy
    assert "sold" in res["dev"]["why"] and "serial" not in res["dev"]["why"]           # devMints cut OFF by default
    res2 = rugcheck.evaluate(facts, _rc(dev_max_mints=100))
    assert "serial launcher" in res2["dev"]["why"]


def test_evaluate_na_vs_unknown():
    deep = {"ok": False, "reason": "history deeper than 1000 txs"}
    res = rugcheck.evaluate({"launch": deep, "holdings": None, "jup": {"top_holders_pct": 5.0, "dev_balance_pct": 30.0,
                                                                     "dev": "Dev"}}, _rc())
    assert res["bundle"]["status"] == "na" and res["holdings"]["status"] == "fail"     # creator 30% via Jupiter
    nohist = rugcheck.evaluate({"launch": {"ok": False, "reason": "no history"}, "holdings": None,
                                "jup": {"top_holders_pct": 5.0}}, _rc())
    assert nohist["bundle"]["status"] == "na" and nohist["holdings"]["status"] == "na"
    timeout = rugcheck.evaluate({"launch": None, "holdings": None, "jup": None}, _rc())
    assert all(v["status"] == "unknown" for v in timeout.values())


def test_evaluate_per_check_flag_off():
    c = _rc()
    c["checks"] = dict(c["checks"], bundle=False)
    big = dict(LAUNCH_OK, bundle_buyers=20, bundle_supply_pct=50.0)
    res = rugcheck.evaluate({"launch": big, "holdings": {}, "jup": {"top_holders_pct": 1.0}}, c)
    assert res["bundle"]["status"] == "off"


def test_rugger_list_hit(monkeypatch):
    rugcheck.rugger_path().write_text(json.dumps({"creators": {"BadDev": {"mints": ["x"]}}}))
    rugcheck._rug_cache.update(t=0.0)
    res = rugcheck.evaluate({"launch": dict(LAUNCH_OK, creator="BadDev"), "holdings": {}, "jup": None}, _rc())
    assert res["rugger"]["status"] == "fail"
    rugcheck.rugger_path().unlink()
    rugcheck._rug_cache.update(t=0.0)


def test_parse_launch_bundle_and_shared_payer():
    def tx(payer, user, tokens, creator="Dev"):
        return {"transaction": {"message": {"accountKeys": [payer]}}, "meta": {"logMessages": []}}, \
            [{"kind": "pump_trade", "mint": MINT, "is_buy": True, "user": user, "token_amount": tokens,
              "sol_amount": 10**9, "creator": creator}]
    txs, evmap = [], {}
    for i, (payer, user, tok) in enumerate([("Dev", "Dev", 30 * 10**12), ("P1", "A", 40 * 10**12),
                                            ("P1", "B", 40 * 10**12)]):
        t, ev = tx(payer, user, tok)
        t["_ev"] = ev
        txs.append(({"slot": 100 + (i > 0), "signature": f"s{i}"}, t))
    import app.live.rugcheck as m
    orig = m.parse_all_logs
    m.parse_all_logs = lambda logs: []
    orig_inner = m.parse_inner_events
    m.parse_inner_events = lambda meta: next(t["_ev"] for _, t in txs if t["meta"] is meta)
    try:
        out = rugcheck.parse_launch(txs, MINT, None)
    finally:
        m.parse_all_logs, m.parse_inner_events = orig, orig_inner
    assert out["creator"] == "Dev" and out["dev_buy_pct"] == 3.0
    assert out["bundle_buyers"] == 2 and out["bundle_supply_pct"] == 8.0 and out["shared_payer_wallets"] == 2


def test_launch_profile_structural_reasons():
    async def rpc_deep(method, params):
        return [{"signature": f"s{i}", "slot": 1} for i in range(1000)]
    assert run(rugcheck.launch_profile(MINT, rpc_deep, max_pages=1))["reason"].startswith("history deeper")

    async def rpc_none(method, params):
        return []
    assert run(rugcheck.launch_profile(MINT, rpc_none))["reason"] == "no history"

    async def rpc_err(method, params):
        return None
    assert run(rugcheck.launch_profile(MINT, rpc_err))["reason"] == "rpc"


def _checker(facts, delay=0.0):
    ch = rugcheck.RugChecker()

    async def gather(mint, *, creator, c, timeout_s):
        if delay:
            await asyncio.sleep(delay)
        return facts
    ch.gather_facts = gather
    return ch


def test_prebuy_tg_fails_closed_scout_skips_unknown():
    facts = {"launch": None, "holdings": None, "jup": {"top_holders_pct": 10.0, "dev": "Dev"}}
    tg = run(_checker(facts).prebuy(MINT, "zyrox"))
    sc = run(_checker(facts).prebuy(MINT, "scout_solo"))
    assert not tg["ok"] and any("fails closed" in r for r in tg["refuse"]) and tg["strict"]
    assert sc["ok"] and "bundle" in sc["unknown"] and not sc["refuse"]
    # n/a never refuses, even on a Telegram arm
    na = {"launch": {"ok": False, "reason": "no history"}, "holdings": None,
          "jup": {"top_holders_pct": 10.0, "dev": "Dev"}}
    tg2 = run(_checker(na).prebuy(MINT, "fluffy_pump_dump"))
    assert tg2["ok"] and "bundle" in tg2["na"]


def test_prebuy_failures_refuse_every_arm():
    facts = {"launch": dict(LAUNCH_OK, bundle_buyers=12, bundle_supply_pct=40.0), "holdings": {"Dev": 50 * 10**12},
             "jup": {"top_holders_pct": 10.0}}
    for arm in ("scout_solo", "scout_confirmed", "fluffy_pump_dump", "zyrox"):
        out = run(_checker(facts).prebuy(MINT, arm))
        assert not out["ok"] and any("bundle" in r for r in out["refuse"])


def test_prebuy_disabled(monkeypatch):
    monkeypatch.setenv("LIVE_RUG_CHECKS_ENABLED", "false")
    out = run(_checker({"launch": None}).prebuy(MINT, "zyrox"))
    assert out["ok"] and out.get("disabled")


def test_gather_facts_times_out_to_none(monkeypatch):
    ch = rugcheck.RugChecker()

    async def slow(*a, **k):
        await asyncio.sleep(5)
    monkeypatch.setattr(rugcheck, "launch_profile", slow)
    monkeypatch.setattr(rugcheck, "jup_audit", slow)
    t = time.time()
    f = run(ch.gather_facts(MINT, creator=None, c=rugcheck.cfg(), timeout_s=0.2))
    assert time.time() - t < 1.5 and f["launch"] is None and f["jup"] is None


# ======================= engine: refusals → learning rows =======================
def test_engine_rug_refusal_logs_stage_row_and_never_swaps(env, monkeypatch):
    calls = []

    async def swap(**kw):
        calls.append(kw)
        return {"ok": True}
    monkeypatch.setattr(jupiter, "swap", swap)

    async def refuse(mint, arm, *, creator=None):
        return {"ok": False, "refuse": ["rugcheck bundle: launch bundle: 9 wallet(s) bought 30.0%"], "unknown": [],
                "checks": {"bundle": {"status": "fail", "value": {"buyers": 9}}}, "strict": False}
    monkeypatch.setattr(rugcheck.checker, "prebuy", refuse)
    e = LiveEngine()

    async def go():
        e.on_signal("scout_solo", MINT, meta={})
        await _drain(e)
    run(go())
    assert not calls and e.decisions[0]["decision"] == "aborted" and "rugcheck bundle" in e.decisions[0]["reasons"][0]
    assert MINT not in e.bought_mints and not e.pending_buys and e.day()["trades"] == 0
    rows = _rows(env / "candidates.jsonl")
    assert rows and rows[-1]["stage"] == "live_prebuy" and rows[-1]["skip_reason"].startswith("rugcheck bundle")
    assert rows[-1]["features"]["rug_checks"] == {"bundle": "fail"} and e.stats["rug_refused"] == 1


def test_engine_rug_timeout_tg_fail_closed(env, monkeypatch):
    monkeypatch.setenv("LIVE_RUG_TIMEOUT_S", "0.05")
    calls = []

    async def swap(**kw):
        calls.append(kw)
        return {"ok": True}
    monkeypatch.setattr(jupiter, "swap", swap)

    async def hang(mint, arm, *, creator=None):
        await asyncio.sleep(10)
    monkeypatch.setattr(rugcheck.checker, "prebuy", hang)
    e = LiveEngine()

    async def go():
        e.on_signal("zyrox", MINT, meta={"tg_gate_passed": True})
        await _drain(e)
    run(go())
    assert not calls and any("fails closed" in r for r in e.decisions[0]["reasons"])


def test_engine_other_refusals_also_logged(env, monkeypatch):
    async def bad_mesh(self, mint):
        return False, "mesh: honeypot"
    monkeypatch.setattr(LiveEngine, "_mesh_check", bad_mesh)
    e = LiveEngine()

    async def go():
        e.on_signal("scout_solo", MINT, meta={})
        await _drain(e)
    run(go())
    rows = _rows(env / "candidates.jsonl")
    assert rows[-1]["stage"] == "live_prebuy" and "honeypot" in rows[-1]["reasons"][0]


def test_stage_rows_are_never_tracked(env, monkeypatch):
    from app.learning import candidates as learn
    from app.learning.outcomes import tracker
    seen = []
    monkeypatch.setattr(tracker, "track", lambda *a, **k: seen.append(a))
    learn.log_candidate(arm="scout_solo", source="live_prebuy", mint=MINT, decision="skip", reasons=["x"],
                        stage="live_prebuy", ref_price_sol=1e-7)
    assert not seen


def test_scorecard_separates_stage_rows(env, monkeypatch):
    from app import __version__
    now = int(time.time() * 1000)
    p = env / "candidates.jsonl"
    base = {"version": __version__, "source": "s", "mint": MINT, "features": {}, "reasons": []}
    rows = [dict(base, ts_ms=now, cid="a", arm="scout_solo", decision="skip", entered=False, skip_reason="x"),
            dict(base, ts_ms=now, cid="b", arm="scout_solo", decision="skip", entered=False,
                 skip_reason="rugcheck bundle", stage="live_prebuy",
                 features={"rug_unknown": ["top10"]})]
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    s = scorecard.summarize(days=1, now_ms=now + 1)
    assert s["live_prebuy"]["rows"] == 1 and s["live_prebuy"]["rug_unknown_checks"] == {"top10": 1}
    total = sum(a.get("candidates", a.get("n", 0)) for a in (s.get("arms") or {}).values()) \
        if isinstance(s.get("arms"), dict) else None
    assert total in (None, 1)


# ======================= open-position rug watch =======================
def test_creator_dump_forces_urgent_rug_exit(env, monkeypatch):
    monkeypatch.setenv("LIVE_RUG_WATCH_ENABLED", "true")
    e = LiveEngine()
    p = _pos()
    e.positions[p.id] = p
    e._rug_baseline(p, {"creator": "Dev", "linked": ["L1"], "launch_holdings": {"Dev": 60 * 10**12, "L1": 0}})
    sells = []

    async def fake_sell(pp, frac, reason, **kw):
        sells.append((reason, kw))
    monkeypatch.setattr(e, "_sell", fake_sell)

    async def bal(mint, owners, rpc):
        return {"Dev": 5 * 10**12, "L1": 0}                    # sold 55M of 60M = 5.5% of supply
    monkeypatch.setattr(rugcheck, "holdings_now", bal)

    async def go():
        await e.rug_watch([p])
        await _drain(e)
    run(go())
    assert sells == [("rug_flag_creator_dump", {"urgent": True})] and e.stats["rug_watch_dump"] == 1


def test_small_creator_sell_ignored(env, monkeypatch):
    monkeypatch.setenv("LIVE_RUG_WATCH_ENABLED", "true")
    e = LiveEngine()
    p = _pos()
    e.positions[p.id] = p
    e._rug_baseline(p, {"creator": "Dev", "linked": [], "launch_holdings": {"Dev": 30 * 10**12}})
    assert not e._dump_check(p, 10**12, "balances")            # 0.1% of supply
    assert not e._dump_check(p, 10 * 10**12, "balances")       # 1% < 2% floor


def test_liquidity_drain_forces_urgent_exit(env, monkeypatch):
    monkeypatch.setenv("LIVE_RUG_WATCH_ENABLED", "true")
    e = LiveEngine()
    p = _pos()
    e.positions[p.id] = p
    sells = []

    async def fake_sell(pp, frac, reason, **kw):
        sells.append((reason, kw))
    monkeypatch.setattr(e, "_sell", fake_sell)

    async def go():
        assert not e._drain_check(p, 30 * 10**9)
        assert not e._drain_check(p, 20 * 10**9)               # −33%: a normal dump, the trail handles it
        assert e._drain_check(p, 10 * 10**9)                   # −67% within the window → rug flag
        await _drain(e)
    run(go())
    assert sells == [("rug_flag_liquidity_drain", {"urgent": True})]


def test_rug_watch_off_by_flag(env, monkeypatch):
    monkeypatch.setenv("LIVE_RUG_WATCH_ENABLED", "false")
    e = LiveEngine()
    p = _pos()
    assert not e._drain_check(p, 30 * 10**9) and not e._drain_check(p, 1)


def test_older_force_exits_keep_stepped_slippage(env, monkeypatch):
    e = LiveEngine()
    p = _pos()
    e.positions[p.id] = p
    sells = []

    async def fake_sell(pp, frac, reason, **kw):
        sells.append((reason, kw))
    monkeypatch.setattr(e, "_sell", fake_sell)

    async def go():
        e.force_exit(MINT, "mesh_block", signals=["honeypot detected"])
        await _drain(e)
    run(go())
    assert sells == [("mesh_block", {})]


# ======================= exits: gap-down, fast TP, poll fallback =======================
def test_gap_down_rule():
    c = dict(live_config.config())
    assert c["trail_pct"] == pytest.approx(0.20)                # v1.33: live trail 30% -> 20%
    c["trail_pct"] = 0.30                                       # geometry below is written for a 30% line
    # trail line = 70; prev above, new price 62 ≤ 70 × 0.9 = 63 → gap-down
    assert gap_down(80, 62, 100, 50, "trail", c)
    assert not gap_down(80, 66, 100, 50, "trail", c)            # crossed but within 10% → normal stepped sell
    assert not gap_down(69, 50, 100, 50, "trail", c)            # already below the line on the previous tick
    assert not gap_down(80, 50, 100, 50, "max_hold", c)
    hs = 1 - c["hard_stop_pct"]
    assert gap_down(1.0, hs * 0.85, None, 1.0, "hard_stop", c)


def test_gap_down_sells_at_cap_once(env, monkeypatch):
    monkeypatch.setenv("LIVE_TRAIL_PCT", "0.30")                # v1.33: geometry written for a 30% line
    e = LiveEngine()
    p = _pos(entry_price_sol=1.0, high_price_sol=2.0, last_price_sol=1.2, tp_done=True, ladder_done=["tp_2x"])
    p.last_mark_ms = int(time.time() * 1000)
    e.positions[p.id] = p
    e._prev_px[p.id] = 1.6                                      # line = 1.4; 1.2 ≤ 1.26 → gap-down
    calls = []

    async def swap(**kw):
        calls.append(kw)
        return {"ok": True, "total_in": p.tokens_left, "total_out": 10**8, "signature": "S"}
    monkeypatch.setattr(jupiter, "swap", swap)

    async def tb(mint):
        return p.tokens_left, 6
    monkeypatch.setattr(e, "token_balance", tb)

    async def go():
        await e.manage(p)
    run(go())
    cap = max(live_config.config()["sell_slippage_steps_bps"])
    assert [c["slippage_bps"] for c in calls] == [cap] and "gap_down" in p.flags and e.stats["gap_down_sells"] == 1


def test_normal_trail_keeps_stepped_slippage(env, monkeypatch):
    e = LiveEngine()
    p = _pos(entry_price_sol=1.0, high_price_sol=2.0, last_price_sol=1.39, tp_done=True, ladder_done=["tp_2x"])
    p.last_mark_ms = int(time.time() * 1000)
    e.positions[p.id] = p
    e._prev_px[p.id] = 1.45
    calls = []

    async def swap(**kw):
        calls.append(kw)
        return {"ok": True, "total_in": p.tokens_left, "total_out": 10**8, "signature": "S"}
    monkeypatch.setattr(jupiter, "swap", swap)

    async def tb(mint):
        return p.tokens_left, 6
    monkeypatch.setattr(e, "token_balance", tb)
    run(e.manage(p))
    assert calls[0]["slippage_bps"] == live_config.config()["sell_slippage_steps_bps"][0]
    assert "gap_down" not in p.flags


def test_fast_tp_off_by_default_and_never_arms_trail(monkeypatch):
    c = live_config.config()
    assert c["fast_tp_enabled"] is False
    p = _pos(entry_price_sol=1.0)
    now = p.entry_ms + 30_000
    assert evaluate_exit(p, 1.8, now, c, c["rules"]["scout_solo"]) == []
    monkeypatch.setenv("LIVE_FAST_TP_ENABLED", "true")
    c2 = live_config.config()
    acts = evaluate_exit(p, 1.8, now, c2, c2["rules"]["scout_solo"])
    assert acts == [("sell_frac", c2["fast_tp_frac"], "tpfast_1.5x")]
    assert fast_tp(p, 1.8, c2["fast_tp_window_s"] + 1, c2) is None             # only inside the window
    p.ladder_done.append("tpfast_1.5x")
    assert fast_tp(p, 1.8, 10, c2) is None                                      # once per position


def test_fast_tp_manage_does_not_set_tp_done(env, monkeypatch):
    monkeypatch.setenv("LIVE_FAST_TP_ENABLED", "true")
    e = LiveEngine()
    p = _pos(entry_price_sol=1.0, last_price_sol=1.7, high_price_sol=1.7)
    p.last_mark_ms = int(time.time() * 1000)
    e.positions[p.id] = p
    got = []

    async def fake_sell(pp, frac, reason, **kw):
        got.append((frac, reason))
    monkeypatch.setattr(e, "_sell", fake_sell)
    run(e.manage(p))
    assert got and got[0][1] == "tpfast_1.5x" and p.tp_done is False


def test_poll_interval_fallback(env, monkeypatch):
    from app.live import engine as eng
    e = LiveEngine()
    monkeypatch.setattr(eng.acct_watch, "healthy", lambda *a, **k: True)
    assert e.poll_interval() == pytest.approx(live_config.config()["mark_s"])
    monkeypatch.setattr(eng.acct_watch, "healthy", lambda *a, **k: False)
    assert e.poll_interval() == pytest.approx(1.0)


def test_account_push_marks_and_manages(env, monkeypatch):
    import struct
    from app.pump.curve import CURVE_DISCRIMINATOR
    e = LiveEngine()
    p = _pos(entry_price_sol=1e-8)
    e.positions[p.id] = p
    managed = []

    async def m(pp, *, fresh=False):
        managed.append(pp.id)
    monkeypatch.setattr(e, "_manage_once", m)
    data = CURVE_DISCRIMINATOR + struct.pack("<QQQQQ", 800 * 10**12, 40 * 10**9, 500 * 10**12, 10 * 10**9, 10**15) \
        + b"\x00" + bytes(32) + bytes(100)

    async def go():
        await e.on_account(MINT, "curve", data, 1)
        await _drain(e)
    run(go())
    assert p.mark_source == "curve_ws" and p.last_price_sol and managed == [p.id]


def test_watch_ws_url_precedence(monkeypatch):
    from app.live import watch
    monkeypatch.setenv("LIVE_FAST_WS_URL", "wss://paid.example")
    monkeypatch.delenv("LIVE_WATCH_WS_URL", raising=False)
    monkeypatch.setenv("LIVE_FAST_ENABLED", "false")
    assert watch.ws_url() != "wss://paid.example"                # paid WS only when fast fills are ON
    monkeypatch.setenv("LIVE_FAST_ENABLED", "true")
    assert watch.ws_url() == "wss://paid.example"
    monkeypatch.setenv("LIVE_WATCH_WS_URL", "wss://explicit.example")
    assert watch.ws_url() == "wss://explicit.example"


# ======================= fast fills (prep, OFF) =======================
def test_fastfill_defaults_off():
    c = fastfill.cfg()
    assert c["enabled"] is False and fastfill.active_mode("metis") == "jupiter"
    assert fastfill.priority_lamports(2_000_000) == 2_000_000
    assert fastfill.summary()["active_for_metis"] == "jupiter"


def test_fastfill_modes_need_complete_config(monkeypatch):
    monkeypatch.setenv("LIVE_FAST_ENABLED", "true")
    monkeypatch.setenv("LIVE_SEND_MODE", "rpc")
    assert fastfill.active_mode("metis") == "jupiter"             # no URL → unchanged path
    monkeypatch.setenv("LIVE_FAST_RPC_URL", "https://paid.example")
    assert fastfill.active_mode("metis") == "rpc" and fastfill.active_mode("jupiterz") == "jupiter"
    monkeypatch.setenv("LIVE_SEND_MODE", "jito")
    assert fastfill.active_mode("metis") == "jupiter"             # tip 0 → off
    monkeypatch.setenv("LIVE_JITO_TIP_LAMPORTS", "999999999")
    assert fastfill.active_mode("metis") == "jupiter"             # above the max → off
    monkeypatch.setenv("LIVE_JITO_TIP_LAMPORTS", "20000")
    assert fastfill.active_mode("metis") == "jito"
    monkeypatch.setenv("LIVE_PRIORITY_FEE_LAMPORTS", "50000")
    assert fastfill.priority_lamports(2_000_000) == 50_000


def _fake_order_flow(monkeypatch, executed, sent=None):
    async def order(**kw):
        return {"ok": True, "transaction": "TX", "request_id": "r", "router": "metis", "in_amount": 1, "out_amount": 2}
    monkeypatch.setattr(jupiter, "order", order)
    monkeypatch.setattr(jupiter, "sign_partial", lambda tx: "SIGNED")
    monkeypatch.setattr(jupiter, "signature_of", lambda signed: "SIG")

    async def sim(*a, **k):
        return {"ok": True}
    monkeypatch.setattr(jupiter, "simulate_signed", sim)

    async def execute(signed, request_id, **kw):
        executed.append(signed)
        return {"ok": True, "status": "Success", "signature": "SIG", "total_in": 1, "total_out": 2}
    monkeypatch.setattr(jupiter, "execute", execute)


def test_swap_default_path_is_jupiter_execute(monkeypatch):
    executed, sends = [], []

    async def send(*a, **k):
        sends.append(a)
        return {"ok": True}
    monkeypatch.setattr(fastfill, "send", send)
    _fake_order_flow(monkeypatch, executed)
    res = run(jupiter.swap(input_mint="A", output_mint="B", amount=1, slippage_bps=100, max_priority_lamports=1))
    assert executed == ["SIGNED"] and not sends and res.get("ok")


def test_swap_fast_mode_falls_back_when_nothing_sent(monkeypatch):
    monkeypatch.setenv("LIVE_FAST_ENABLED", "true")
    monkeypatch.setenv("LIVE_SEND_MODE", "rpc")
    monkeypatch.setenv("LIVE_FAST_RPC_URL", "https://paid.example")
    executed = []

    async def send(mode, signed, sig, **k):
        return {"fallback": True, "error": "refused"}
    monkeypatch.setattr(fastfill, "send", send)
    _fake_order_flow(monkeypatch, executed)
    res = run(jupiter.swap(input_mint="A", output_mint="B", amount=1, slippage_bps=100, max_priority_lamports=1))
    assert executed == ["SIGNED"] and res.get("ok")


def test_fastfill_send_rpc_mocked(monkeypatch):
    monkeypatch.setenv("LIVE_FAST_ENABLED", "true")
    monkeypatch.setenv("LIVE_FAST_RPC_URL", "https://paid.example")
    monkeypatch.setattr(jupiter, "pubkey", lambda: "Me")
    seen = []

    async def rpc(url, method, params, client, timeout=8.0):
        seen.append(method)
        return "SIG"
    monkeypatch.setattr(fastfill, "_rpc", rpc)

    async def conf(sig, url, client, t):
        return {"landed": True, "err": None}
    monkeypatch.setattr(fastfill, "confirm", conf)

    async def amts(*a, **k):
        return {"total_in": 5, "total_out": 7}
    monkeypatch.setattr(fastfill, "fill_amounts", amts)
    out = run(fastfill.send("rpc", "B64", "SIG", input_mint="A", output_mint="B"))
    assert out["ok"] and out["sender"] == "rpc" and seen == ["sendTransaction"] and out["total_out"] == 7


def test_fastfill_unknown_outcome_is_ambiguous(monkeypatch):
    monkeypatch.setenv("LIVE_FAST_ENABLED", "true")
    monkeypatch.setenv("LIVE_FAST_RPC_URL", "https://paid.example")

    async def rpc(url, method, params, client, timeout=8.0):
        raise TimeoutError()
    monkeypatch.setattr(fastfill, "_rpc", rpc)
    out = run(fastfill.send("rpc", "B64", "SIG", input_mint="A", output_mint="B"))
    assert out.get("ambiguous") and not out.get("fallback")      # never re-send through Jupiter blindly


# ======================= version / ledger format =======================
def test_version():
    from app import __version__
    assert __version__ == "1.17.0-demo"


def test_ledger_row_format_unchanged(env, monkeypatch):
    e = LiveEngine()
    e._ledger("close", id="x", mint=MINT, reason="rug_flag_liquidity_drain")
    row = json.loads((env / "ledger.jsonl").read_text().splitlines()[-1])
    assert set(row) >= {"ts_ms", "kind", "id", "mint", "reason"}


def test_wallet_report_table():
    import importlib.util
    import pathlib
    spec = importlib.util.spec_from_file_location("wallet_report", pathlib.Path(__file__).resolve().parents[1] /
                                                  "scripts" / "wallet_report.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    d = {"generated_ms": 1, "window_days": 7, "counts": {}, "wallets": [
        {"rank": 1, "label": "a", "role": "scout", "score": 0.1, "flags": [], "onchain": {"buys_7d": 3, "15m": {
            "n": 3, "win_rate": 0.67, "win_rate_ci95": [0.2, 0.9], "median": 0.1}}, "activity": {}},
        {"rank": 2, "label": "b", "role": "fomo", "score": -0.2, "flags": ["dead_inactive"], "onchain": {},
         "activity": {"days_since_last_trade": 9}, "recommendations": ["x"]}],
        "recommendations": [{"label": "b", "role": "fomo", "wallet": "Bbbbbbbb", "why": ["no trade in 7 days"]}]}
    t = m.table(d)
    assert "advisory" in t and "dead_inactive" in t and "no trade in 7 days" in t
    assert "b " not in m.table(d, role="scout").split("\n", 2)[2]


def test_outcomes_holders_from_jupiter_audit(monkeypatch):
    from app.learning import outcomes
    t = outcomes.OutcomeTracker() if hasattr(outcomes, "OutcomeTracker") else outcomes.tracker
    rows = []
    monkeypatch.setattr(t, "_write", lambda r: rows.append(r))

    async def audit(mint, client):
        return {"top_holders_pct": 22.5, "dev_balance_pct": 3.4, "dev_mints": 2, "holder_count": 120}
    monkeypatch.setattr(rugcheck, "jup_audit", audit)

    async def cl():
        return None
    monkeypatch.setattr(t, "_client", cl)
    run(t._holders_jup("cid1", MINT))
    assert rows and rows[0]["kind"] == "holders" and rows[0]["src"] == "jupiter_audit" and rows[0]["top10_pct"] == 22.5


# ---------------- v1.17: FOMO wallet trade collection (wallet ∩ USDC account) ----------------
def test_fomo_trades_intersection_and_dead_logic(monkeypatch):
    import asyncio
    from app.learning import wallet_scores as ws
    now = int(time.time())
    calls = []

    async def rpc(self, method, params, tries=4):
        calls.append((method, params[0]))
        if method == "getSignaturesForAddress" and params[0] == "W":
            return [{"signature": "own1", "blockTime": now - 100, "err": None},
                    {"signature": "dust", "blockTime": now - 200, "err": None},
                    {"signature": "fee", "blockTime": now - 300, "err": None}]
        if method == "getSignaturesForAddress" and params[0] == "ATA":
            return [{"signature": "own1", "blockTime": now - 100, "err": None},
                    {"signature": "ref", "blockTime": now - 150, "err": None}]
        if method == "getSignaturesForAddress":
            return []
        return {"blockTime": now - 100, "meta": {"err": None}}

    monkeypatch.setattr(ws.Collector, "rpc", rpc)
    monkeypatch.setattr("app.scout.txparse.parse_wallet_trades",
                        lambda tx, w: [{"mint": "M", "is_buy": True, "sol_amount": 0, "venue": "DF1ow4ts…"}])
    col = ws.Collector(_c())
    w = {"wallet": "W", "role": "fomo", "usdc_ata": "ATA", "keys": ["U1", "U2"]}
    meta = asyncio.run(col.wallet_trades(w, now - 7 * 86400))
    fetched = [p for m, p in calls if m == "getTransaction"]
    assert fetched == ["own1"]                      # dust / fee-distribution txs never fetched
    assert meta["trades"] and meta["window_covered"] and meta["sample_complete"]
    r = ws.score_wallet(w, [], meta, {}, _c(), now)
    assert "dead_inactive" not in r["flags"] and r["onchain"]["method"] == "wallet∩usdc_ata+uva"


def test_no_trade_in_partial_window_is_unknown_not_dead():
    from app.learning import wallet_scores as ws
    now = time.time()
    w = {"wallet": "W", "role": "fomo"}
    meta = {"history_ok": True, "trades": [], "window_covered": False, "sample_complete": True}
    r = ws.score_wallet(w, [], meta, {}, _c(), now)
    assert "activity_unknown" in r["flags"] and "dead_inactive" not in r["flags"] and not r["recommendations"]
    meta2 = {"history_ok": True, "trades": [], "window_covered": True, "sample_complete": True}
    assert "dead_inactive" in ws.score_wallet(w, [], meta2, {}, _c(), now)["flags"]


def test_roster_fomo_keys_exclude_flooded_usdc_account(monkeypatch, tmp_path):
    import json as _j
    from app.learning import wallet_scores as ws
    (tmp_path / "scout_wallets.json").write_text(_j.dumps({"wallets": []}))
    (tmp_path / "fomo_onchain_wallets.json").write_text(_j.dumps({"wallets": [
        {"wallet": "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM", "handle": "h", "status": "kept", "usdc_ata": "ATA"}]}))
    monkeypatch.setattr(ws, "data_dir", lambda: tmp_path)
    r = ws.roster()
    assert r[0]["usdc_ata"] == "ATA" and "ATA" not in r[0]["keys"] and len(r[0]["keys"]) == 2


def test_daily_job_recomputes_older_algo(monkeypatch):
    from app.learning import wallet_scores as ws
    assert ws.ALGO >= 2
    ws.write({"generated_ms": int(time.time() * 1000), "wallets": []})       # fresh but no algo → stale method
    assert (ws.read() or {}).get("algo") != ws.ALGO


def test_compute_scores_one_buy_per_coin(monkeypatch):
    import asyncio
    from app.learning import wallet_scores as ws
    now = time.time()

    async def trades(self, w, since_s):
        return {"history_ok": True, "txs_fetched": 4, "sigs_in_window": 4, "trades": [
            {"bt": now - 4000, "is_buy": True, "mint": "A", "sol_amount": 1e9},
            {"bt": now - 3900, "is_buy": True, "mint": "A", "sol_amount": 1e9},
            {"bt": now - 3800, "is_buy": True, "mint": "A", "sol_amount": 1e9},
            {"bt": now - 3700, "is_buy": True, "mint": "B", "sol_amount": 1e9}]}
    seen = []

    async def rets(self, mint, t_s, now_s=None):
        seen.append((mint, t_s))
        return {"p0": 1.0, "ret_5m": 0.1, "ret_15m": 0.1, "ret_60m": 0.1, "src": "x"}
    monkeypatch.setattr(ws.Collector, "wallet_trades", trades)
    monkeypatch.setattr(ws.Collector, "returns_after", rets)
    out = asyncio.run(ws.compute(c=_c(), wallets=[{"wallet": "W", "label": "w", "role": "scout", "keys": []}]))
    assert sorted(m for m, _ in seen) == ["A", "B"]
    assert ("A", int(now - 4000)) in seen                        # the FIRST buy of A is the sample
    r = out["wallets"][0]
    assert r["onchain"]["scored_buys"] == 2 and r["onchain"]["buys_7d"] == 4 and out["algo"] == ws.ALGO


def test_exit_watch_idle_at_zero_open_and_subscribes_on_open(env, monkeypatch):
    """/health exits.watch connected=false with 0 open is expected: the watcher only subscribes for open positions."""
    from app.live import engine as eng, watch
    from app.pump.solana_pda import bonding_curve_pda
    monkeypatch.setenv("LIVE_WATCH_ENABLED", "true")
    w = watch.AccountWatch()
    w.sync({}, lambda *a: None)                                   # 0 open → no socket, idle (not a fault)
    assert w.task is None and w.status()["connected"] is False and w.status()["accounts"] == 0
    seen = []
    monkeypatch.setattr(eng.acct_watch, "sync", lambda wanted, cb: seen.append(dict(wanted)))
    e = LiveEngine()
    p = _pos(entry_price_sol=1e-8)
    e.positions[p.id] = p
    run(e._sync_watch(e.open_positions()))                        # mark loop (started on fill) subscribes the curve
    assert seen[-1] == {bonding_curve_pda(MINT): (MINT, "curve")}
    run(e._sync_watch([]))                                       # last position closed → unsubscribe
    assert seen[-1] == {}
    monkeypatch.setattr(eng.acct_watch, "healthy", lambda *a, **k: False)
    assert e.poll_interval() == pytest.approx(1.0)                # socket down/idle → ~1 s poll fallback


# ---------------- wallet scores: faster collection (grouped candles, endpoint pool) ----------------
def test_grouped_candles_one_gt_call_per_pool_window(monkeypatch):
    col = ws.Collector(_c(gt_window=1000))
    now = 2_000_000_000
    base = now - 86400
    calls = []

    async def gt(self, url, params):
        calls.append((url, params))
        before = params["before_timestamp"]
        lst = [[before - 60 * k, 1, 1.0 + k * 1e-3, 1, 1.0 + k * 1e-3, 1] for k in range(1, params["limit"] + 1)]
        return {"data": {"attributes": {"ohlcv_list": lst}}, "meta": {"quote": {"address": ws.WSOL}}}
    monkeypatch.setattr(ws.Collector, "gt", gt)
    times = [base, base + 3600, base + 5 * 3600]                  # three wallets buy the same coin within 5 h
    assert run(col.prefetch("POOL", times, now)) == 1
    assert run(col.prefetch("POOL", times, now)) == 0              # already covered → no call
    before = int(base + 3600 + 61 * 60 + 60)
    sl = run(col._ohlcv("POOL", before, 75))
    assert len(sl) == 75 and max(c[0] for c in sl) < before and len(calls) == 1
    assert col.stats["gt_wide_hits"] == 1
    # a buy outside the grouped window falls back to the normal narrow call
    far = run(col.prefetch("POOL", [base - 3 * 86400], now))
    assert far == 1 and len(calls) == 2


def test_rpc_endpoint_pool_backs_off_429_per_endpoint(monkeypatch):
    from app.pump import rpc as _rpc
    col = ws.Collector(_c(rpc_urls=["https://a.example", "https://b.example"], rps=1000))
    used = []

    async def call(method, params, *, client=None, url=None, timeout=None):
        used.append(url)
        if url == "https://a.example":
            raise _rpc.RpcError("rpc http 429")
        return {"ok": url}
    monkeypatch.setattr(_rpc, "call", call)
    assert run(col.rpc("getSlot", [])) == {"ok": "https://b.example"}
    assert run(col.rpc("getSlot", [])) == {"ok": "https://b.example"}    # a is cooling down → not retried
    assert used.count("https://a.example") == 1 and col.stats["rpc_429"] == 1
    a = next(e for e in col.eps if e["url"] == "https://a.example")
    assert a["cool"] > time.monotonic()


def test_compute_parallel_trades_then_grouped_candles(monkeypatch):
    seen = {"prefetch": None, "concurrent": 0, "max": 0}

    async def trades(self, w, since):
        seen["concurrent"] += 1
        seen["max"] = max(seen["max"], seen["concurrent"])
        await asyncio.sleep(0.01)
        seen["concurrent"] -= 1
        return {"history_ok": True, "trades": [{"bt": time.time() - 4000, "is_buy": True, "mint": "SAME",
                                                "sol_amount": 1e9}], "txs_fetched": 1, "sigs_in_window": 1}

    async def pre(self, by_mint, now_s, lead_s=0):
        seen.setdefault("calls", []).append(lead_s)
        seen["prefetch"] = {k: len(v) for k, v in by_mint.items()}

    async def rets(self, mint, t_s, now_s=None):
        return {"p0": 1.0, "ret_5m": 0.1, "ret_15m": 0.2, "ret_60m": 0.0, "src": "x"}
    monkeypatch.setattr(ws.Collector, "wallet_trades", trades)
    monkeypatch.setattr(ws.Collector, "prefetch_candles", pre)
    monkeypatch.setattr(ws.Collector, "returns_after", rets)
    monkeypatch.setenv("WALLET_SCORES_CONCURRENCY", "3")
    wl = [{"wallet": f"W{i}", "label": f"w{i}", "role": "fomo"} for i in range(6)]
    out = run(ws.compute(wallets=wl))
    assert seen["max"] == 3 and seen["prefetch"] == {"SAME": 6}
    assert seen["calls"][0] == 240 * 60 and seen["calls"][-1] == 0      # early (lead) fetch, then the final pass
    assert len(out["wallets"]) == 6 and out["wallets"][0]["onchain"]["15m"]["n"] == 1


def test_wallet_scores_defaults_stay_gentle_in_coach():
    c = ws.cfg()
    assert c["rpc_urls"] == ["https://api.mainnet-beta.solana.com"] and c["concurrency"] == 1 and c["rps"] == 1.5


def test_early_fetch_with_lead_covers_later_buys(monkeypatch):
    col = ws.Collector(_c(gt_window=1000))
    now = 2_000_000_000
    base = now - 86400
    calls = []

    async def gt(self, url, params):
        calls.append(params["before_timestamp"])
        b = params["before_timestamp"]
        return {"data": {"attributes": {"ohlcv_list": [[b - 60 * k, 1, 1, 1, 1, 1] for k in range(1, params["limit"] + 1)]}}}
    monkeypatch.setattr(ws.Collector, "gt", gt)
    run(col.prefetch("P", [base], now, lead_s=240 * 60))
    assert run(col.prefetch("P", [base, base + 3 * 3600], now)) == 0 and len(calls) == 1   # later buy already covered


def test_non_pump_mints_skip_curve_lookups(monkeypatch):
    col = ws.Collector(_c())
    urls = []

    async def gt(self, url, params):
        urls.append(url)
        if url.endswith("/pools"):
            return {"data": [{"attributes": {"address": "TOPPOOL"}}]}
        b = params["before_timestamp"]
        return {"data": {"attributes": {"ohlcv_list": [[b - 60 * k, 1, 1, 1, 1, 1] for k in range(1, params["limit"] + 1)]}}}
    monkeypatch.setattr(ws.Collector, "gt", gt)
    now = 2_000_000_000
    run(col.prefetch_candles({"So11111111111111111111111111111111111111112": [now - 86400]}, now))
    assert len(urls) == 2 and "TOPPOOL" in urls[1]                 # top pool + one grouped candle call, no 404 probes
    r = run(col.returns_after("So11111111111111111111111111111111111111112", now - 86400, now))
    assert r["src"] == "gt_top_pool" and len(urls) == 2            # served from the grouped cache
