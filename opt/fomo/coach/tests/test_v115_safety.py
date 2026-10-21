"""v1.15 safety fixes: Telegram pre-buy gate, mesh-before-buy, simulate, reserve lock, pending_buy/fail-closed state,
stale marks, sell slippage cap / no escalation / ambiguous partials, async kill, Fluffy/Zyrox ladder, force-close.
Everything is mocked: no network, no live state, no keypair."""
from __future__ import annotations

import asyncio
import inspect
import json
import time

import pytest

from app.live import config as live_config, jupiter, tg_gate
from app.live.engine import LiveEngine, LivePos, evaluate_exit, force_close_signal

MINT = "MintAAAA1111111111111111111111111111111pump"
MINT2 = "MintBBBB1111111111111111111111111111111pump"


@pytest.fixture
def env(monkeypatch, tmp_path):
    for k, v in {"JUPITER_EXEC_ENABLED": "true", "LIVE_AUTOTRADE_ENABLED": "true",
                 "LIVE_ARMS": "scout_solo,scout_confirmed,fluffy_pump_dump,zyrox",
                 "LIVE_SIZE_SOL_SCOUT_SOLO": "0.10", "LIVE_SIZE_SOL_FLUFFY_PUMP_DUMP": "0.40",
                 "LIVE_SIZE_SOL_ZYROX": "0.40", "LIVE_MAX_SOL_PER_TRADE": "0.40", "LIVE_MIN_SOL_RESERVE": "0.50",
                 "LIVE_STATE_PATH": str(tmp_path / "state.json"), "LIVE_LEDGER_PATH": str(tmp_path / "ledger.jsonl"),
                 "LIVE_KILL_FILE": str(tmp_path / "KILL"), "LIVE_SELL_RETRY_S": "0",
                 "LIVE_SELL_STEP_BACKOFF_S": "0"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(jupiter, "pubkey", lambda: "FakePubkey111")
    return tmp_path


def _pos(**kw) -> LivePos:
    base = dict(mint=MINT, arm="scout_solo", size_sol=0.1, spent_lamports=100_000_000, tokens_initial=1_000_000_000,
                tokens_left=1_000_000_000, entry_price_sol=0.0001, entry_ms=int(time.time() * 1000))
    base.update(kw)
    return LivePos(**base)


def _fake_swap(results, calls):
    async def swap(**kw):
        calls.append(kw)
        return results.pop(0)
    return swap


def _mesh_ok(monkeypatch):
    async def ok(self, mint):
        return True, "mesh ok"
    monkeypatch.setattr(LiveEngine, "_mesh_check", ok)


def _ledger(env):
    p = env / "ledger.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


async def _drain(e):
    for _ in range(5):
        if not e._tasks:
            return
        await asyncio.gather(*list(e._tasks), return_exceptions=True)


# ======================= fix 1: Telegram pre-buy gate =======================
CURVE = {"status": "ok", "complete": False, "price_sol": 3e-8, "creator": "Dep111"}


def test_tg_gate_static_reasons(env, monkeypatch):
    from app.scout.filters import filters as sf
    monkeypatch.setattr(sf, "scheduled_mints", {})
    monkeypatch.setattr(sf, "deployers", {})
    ok = tg_gate.static_reasons(MINT, CURVE, "curve", {"first_seen": True, "repeat_count": 1}, paper_seen_before=False)
    assert ok == []
    R = lambda **kw: tg_gate.static_reasons(MINT, kw.pop("curve", CURVE), kw.pop("lane", "curve"),  # noqa: E731
                                            kw.pop("meta", {}), paper_seen_before=kw.pop("paper", False))
    assert any("lane migrated" in w for w in R(lane="migrated"))
    assert any("complete" in w for w in R(curve={**CURVE, "complete": True}))
    assert any("pump_at_iso" in w for w in R(meta={"pump_at_iso": "2099-10-07T20:00:00Z"}))   # future launch → refuse
    assert not any("pump_at_iso" in w for w in R(meta={"pump_at_iso": "2026-10-07T20:00:00Z"}))  # past/now → ok
    assert any("paper already opened" in w for w in R(paper=True))
    assert any("first sighting" in w for w in R(meta={"first_seen": False}))
    assert any("repeat #3" in w for w in R(meta={"repeat_count": 3}))
    sf.deployers["Dep111"] = {"tokens": [], "rugs": ["x"]}
    assert any("rug" in w for w in R())
    sf.deployers.clear()
    sf.scheduled_mints[MINT] = {"source": "fluffy/telegram", "ts_ms": int(time.time() * 1000)}
    assert any("scheduled_mints" in w for w in R())                         # strict (default)
    monkeypatch.setenv("LIVE_TG_SCHEDULED_STRICT", "false")
    assert R() == []                                                       # tagged by this very call → ok
    sf.scheduled_mints[MINT]["ts_ms"] -= 60_000                            # announced a minute earlier
    assert any("pre-announced" in w for w in tg_gate.static_reasons(MINT, CURVE, "curve", {}, paper_seen_before=False,
                                                                   ingest_ms=int(time.time() * 1000)))


def test_engine_refuses_tg_arm_without_gate(env, monkeypatch):
    e = LiveEngine()
    monkeypatch.setattr(e, "_spawn", lambda c: c.close())
    d = e.on_signal("fluffy_pump_dump", MINT)
    assert d["decision"] == "refused" and any("pre-buy gate" in w for w in d["reasons"])
    assert MINT not in e.bought_mints
    r = e.refuse("zyrox", MINT2, ["token age 900s > 600s"])
    assert r["decision"] == "refused" and e.stats["tg_gate_refused"] == 1
    assert _ledger(env)[-1]["kind"] == "tg_buy_refused"


def _decide_env(monkeypatch, *, age=30.0, fresh=None):
    from app.live.engine import engine as le
    from app.pump import fast_lane as F
    seen = []
    monkeypatch.setattr(le, "on_signal", lambda arm, mint, **kw: seen.append(("buy", arm, kw)) or
                        {"decision": "buy", "reasons": []})
    monkeypatch.setattr(le, "refuse", lambda arm, mint, why, **kw: seen.append(("refuse", arm, why)) or
                        {"decision": "refused", "reasons": why})

    async def age_s(mint):
        return age

    async def rc(mint, **kw):
        seen.append(("fresh_curve", kw.get("use_cache")))
        return fresh or dict(CURVE)
    monkeypatch.setattr(tg_gate, "token_age_s", age_s)
    monkeypatch.setattr(F, "read_curve", rc)
    from app.scout.filters import filters as sf
    monkeypatch.setattr(sf, "scheduled_mints", {})
    from app.fomo_onchain import market                  # v1.16: no entry without a SOL/USD price
    monkeypatch.setitem(market._cache, market.WSOL_MINT, {"price_usd": 150.0, "_t": __import__("time").time()})
    return F, seen


def test_live_decide_passes_only_when_all_checks_ok(env, monkeypatch):
    F, seen = _decide_env(monkeypatch)
    t = F.new_timeline(MINT, source="fluffy/telegram", event_id=None, meta={})
    ok_rule = {"mesh_status": "ok", "blocked": False}
    d = asyncio.run(F._live_decide(t, MINT, "fluffy_pump_dump", lane="curve", curve=CURVE, meta={"first_seen": True},
                                   ingest_ms=1, paper_seen_before=False, mesh={}, rule=ok_rule))
    assert d["decision"] == "buy" and seen[0] == ("fresh_curve", False)
    assert seen[1][2]["meta"]["tg_gate_passed"] is True
    for kw, frag in [(dict(rule={"mesh_status": "timeout", "blocked": False}), "mesh timeout"),
                     (dict(rule={"mesh_status": "ok", "blocked": True, "reasons": ["honeypot"]}), "hard block"),
                     (dict(mesh={"stale": True, "timed_out": True}), "stale"),
                     (dict(mesh={"sources": {"token_risk": {"stale": True}}}), "stale"),
                     (dict(paper_seen_before=True), "paper already")]:
        seen.clear()
        args = dict(lane="curve", curve=CURVE, meta={}, ingest_ms=1, paper_seen_before=False, mesh={}, rule=ok_rule)
        args.update(kw)
        d = asyncio.run(F._live_decide(t, MINT, "zyrox", **args))
        assert d["decision"] == "refused" and any(frag in w for w in d["reasons"]), (frag, d)
        assert not any(s[0] == "buy" for s in seen)


def test_live_decide_age_and_fresh_curve(env, monkeypatch):
    ok_rule = {"mesh_status": "ok", "blocked": False}
    for age, fresh, frag in [(None, None, "age unknown"), (900.0, None, "900s > 600s"),
                             (10.0, {**CURVE, "complete": True}, "completed before")]:
        F, seen = _decide_env(monkeypatch, age=age, fresh=fresh)
        t = F.new_timeline(MINT, source="fluffy/telegram", event_id=None, meta={})
        d = asyncio.run(F._live_decide(t, MINT, "fluffy_pump_dump", lane="curve", curve=CURVE, meta={},
                                       ingest_ms=1, paper_seen_before=False, mesh={}, rule=ok_rule))
        assert d["decision"] == "refused" and any(frag in w for w in d["reasons"]), (frag, d)


def test_live_decide_arm_not_enabled_is_cheap(env, monkeypatch):
    monkeypatch.setenv("LIVE_ARMS", "scout_solo,scout_confirmed")
    F, seen = _decide_env(monkeypatch)
    t = F.new_timeline(MINT, source="fluffy/telegram", event_id=None, meta={})
    asyncio.run(F._live_decide(t, MINT, "fluffy_pump_dump", lane="curve", curve=CURVE, meta={}, ingest_ms=1,
                               paper_seen_before=False, mesh={}, rule={"mesh_status": "ok"}))
    assert seen[0][0] == "buy" and "tg_gate_passed" not in seen[0][2]["meta"]   # engine refuses: arm not live


def test_pipeline_live_buy_waits_for_mesh(env, monkeypatch):
    """Curve lane: the live decision happens AFTER the mesh answer (v1.14 fired it before the paper sim)."""
    import app.mesh as M
    import app.risk_gate as RG
    import app.scoring as SC
    from app.pump import paper as P
    from app.pump.trade_stream import streams
    F, seen = _decide_env(monkeypatch)
    order = []

    async def rc(mint, **kw):
        order.append("curve" if kw.get("use_cache", True) else "fresh_curve")
        return dict(CURVE)

    async def mesh(mint, chain, **kw):
        order.append("mesh")
        return {"status": "ok"}

    async def none(*a, **k):
        return None
    monkeypatch.setattr(F, "read_curve", rc)
    monkeypatch.setattr(M, "enrich_with_timeout", mesh)
    monkeypatch.setattr(RG, "mesh_rule_decision", lambda m: {"mesh_status": "ok", "blocked": False})
    monkeypatch.setattr(SC, "get_signal", lambda *a, **k: None)
    monkeypatch.setattr(streams, "start", lambda *a, **k: None)
    monkeypatch.setattr(P.paper, "open_from_curve", none)
    monkeypatch.setattr(P.paper, "attach_jupiter_entry_quote", none)
    monkeypatch.setattr(P.paper, "already_opened", lambda m: False)
    monkeypatch.setattr(P, "_jupiter_quote", lambda *a: {"ok": False})
    from app.live.engine import engine as le
    orig = le.on_signal
    monkeypatch.setattr(le, "on_signal", lambda *a, **k: order.append("live_buy") or orig(*a, **k))
    v = asyncio.run(F.run_pipeline(MINT, source="fluffy/telegram", meta={"first_seen": True}, event_id=None,
                                   ingest_ms=int(time.time() * 1000)))
    assert v["status"] == "done"
    assert order.index("mesh") < order.index("live_buy") and order[0] == "curve" and "fresh_curve" in order
    assert any(s[0] == "buy" for s in seen)


def test_ingest_dedupe_persists(env, monkeypatch, tmp_path):
    from app.ingest import buy as B
    monkeypatch.setattr(B, "SEEN_PATH", tmp_path / "seen.json")
    B._seen_pipeline.clear()
    assert B._note_seen("fluffy/telegram", MINT) == 0
    assert B._note_seen("fluffy/telegram", MINT) == 1
    B._seen_pipeline.clear()                          # "restart"
    B._load_seen()
    assert B._note_seen("fluffy/telegram", MINT) >= 1
    B._seen_pipeline.clear()


# ======================= fix 2: mesh before every live buy =======================
@pytest.mark.parametrize("rule,mesh,frag", [
    ({"mesh_status": "ok", "blocked": True, "reasons": ["honeypot"]}, {}, "hard block"),
    ({"mesh_status": "timeout", "blocked": False}, {}, "mesh timeout"),
    ({"mesh_status": "ok", "blocked": False}, {"stale": True, "timed_out": True}, "stale"),
    ({"mesh_status": "ok", "blocked": False}, {"sources": {"token_risk": {"stale": True}}}, "stale"),
])
def test_buy_refused_without_clean_mesh(env, monkeypatch, rule, mesh, frag):
    import app.mesh as M
    import app.risk_gate as RG

    async def enrich(mint, chain, **kw):
        return mesh
    monkeypatch.setattr(M, "enrich_with_timeout", enrich)
    monkeypatch.setattr(RG, "mesh_rule_decision", lambda m: rule)
    e = LiveEngine()
    calls = []
    monkeypatch.setattr(jupiter, "swap", _fake_swap([], calls))

    async def go():
        e.on_signal("scout_solo", MINT)
        await _drain(e)
    asyncio.run(go())
    assert not calls and MINT not in e.bought_mints and e.decisions[0]["decision"] == "aborted"
    assert any(frag in r for r in e.decisions[0]["reasons"]) and not e.pending_buys


def test_mesh_error_means_no_buy(env, monkeypatch):
    import app.mesh as M

    async def boom(*a, **k):
        raise RuntimeError("down")
    monkeypatch.setattr(M, "enrich_with_timeout", boom)
    ok, why = asyncio.run(LiveEngine()._mesh_check(MINT))
    assert not ok and "error" in why


def test_force_close_words_and_inflight_block(env, monkeypatch):
    assert force_close_signal(["mint authority live"]) and force_close_signal(["Freeze authority live"])
    assert force_close_signal(["verdict critical"]) and not force_close_signal(["low liquidity"])
    e = LiveEngine()
    spawned = []
    monkeypatch.setattr(e, "_spawn", lambda c: (spawned.append(c.cr_code.co_name), c.close()))
    monkeypatch.setattr(e, "_start_stream", lambda m: None)
    monkeypatch.setattr(e, "ensure_loop", lambda: None)
    e.inflight.add(MINT)
    e.force_exit(MINT, "mesh_hard_block_after_entry", signals=["honeypot"])
    assert MINT in e.pending_blocks and not spawned
    e._open_position("scout_solo", MINT, 0.1, 100_000_000, 10**9, 6, "SIG", "metis", [], [], {})
    assert spawned == ["_sell"] and MINT not in e.pending_blocks


# ======================= fix 3: simulate before execute =======================
def test_swap_simulates_before_execute(monkeypatch):
    ex = []

    async def order(**kw):
        return {"ok": True, "transaction": "tx", "request_id": "r", "router": "metis", "out_amount": 5}

    async def execute(signed, rid, **kw):
        ex.append(signed)
        return {"ok": True, "signature": "S"}
    sim = {"ok": False, "transport_error": False, "err": {"InstructionError": [2, {"Custom": 6001}]}}

    async def simulate(*a, **k):
        return dict(sim)
    monkeypatch.setattr(jupiter, "order", order)
    monkeypatch.setattr(jupiter, "sign_partial", lambda tx: "signed")
    monkeypatch.setattr(jupiter, "execute", execute)
    monkeypatch.setattr(jupiter, "simulate_signed", simulate)
    signed_cb = []
    r = asyncio.run(jupiter.swap(input_mint="a", output_mint="b", amount=1, slippage_bps=1, max_priority_lamports=0,
                                 on_signed=signed_cb.append))
    assert r["stage"] == "simulate" and not ex and not signed_cb
    sim.update(transport_error=True, err="simulate ConnectError")
    r = asyncio.run(jupiter.swap(input_mint="a", output_mint="b", amount=1, slippage_bps=1, max_priority_lamports=0))
    assert r["stage"] == "simulate" and not ex                     # sim RPC down → still no send by default
    r = asyncio.run(jupiter.swap(input_mint="a", output_mint="b", amount=1, slippage_bps=1, max_priority_lamports=0,
                                 allow_sim_transport_error=True, on_signed=signed_cb.append))
    assert r["ok"] and ex == ["signed"] and signed_cb == [None]


def test_buy_simulation_failure_refunds(env, monkeypatch):
    _mesh_ok(monkeypatch)
    e = LiveEngine()
    monkeypatch.setattr(jupiter, "swap", _fake_swap([{"ok": False, "stage": "simulate", "error": "simulation failed"}], []))

    async def bal(**kw):
        return 2.0
    monkeypatch.setattr(e, "sol_balance", bal)

    async def go():
        e.on_signal("scout_solo", MINT)
        await _drain(e)
    asyncio.run(go())
    assert MINT not in e.bought_mints and not e.pending_buys and e.alerts[0]["kind"] == "live_buy_sim_failed"


def test_sell_simulation_failure_keeps_position(env, monkeypatch):
    e = LiveEngine()
    p = _pos()
    e.positions[p.id] = p
    calls = []
    monkeypatch.setattr(jupiter, "swap", _fake_swap([{"ok": False, "stage": "simulate", "error": "sim"}] * 3, calls))

    async def bal(mint):
        return 10**9, 6
    monkeypatch.setattr(e, "token_balance", bal)
    asyncio.run(e._sell(p, 1.0, "hard_stop"))
    assert p.status == "open" and p.exit_pending == "hard_stop" and p.sell_failures == 1
    assert e.alerts[0]["kind"] == "live_sell_failed" and e.alerts[0]["stage"] == "simulate"
    assert all(not c.get("allow_sim_transport_error") for c in calls)
    p.sell_failures = 6                                    # after N failures a down sim RPC no longer blocks exits
    monkeypatch.setattr(jupiter, "swap", _fake_swap([{"ok": True, "total_in": 10**9, "total_out": 1}], calls))
    asyncio.run(e._sell(p, 1.0, "hard_stop"))
    assert calls[-1]["allow_sim_transport_error"] is True and p.status == "closed"


# ======================= fix 4: reserve lock =======================
def test_reserve_counts_inflight_buys(env, monkeypatch):
    _mesh_ok(monkeypatch)
    e = LiveEngine()
    calls = []
    monkeypatch.setattr(jupiter, "swap", _fake_swap([], calls))

    async def bal(**kw):
        return 1.0                                       # 1.0 − 0.40 inflight − 0.10 − fee < 0.50
    monkeypatch.setattr(e, "sol_balance", bal)
    e.inflight.add(MINT2)
    e.inflight_sizes[MINT2] = 0.40

    async def go():
        e.on_signal("scout_solo", MINT)
        await _drain(e)
    asyncio.run(go())
    assert not calls and e.decisions[0]["decision"] == "aborted" and "inflight" in e.decisions[0]["reasons"][0]
    assert MINT2 in e.inflight_sizes and MINT not in e.inflight_sizes


# ======================= fix 5: pending_buy + fail-closed state =======================
def test_pending_buy_persisted_with_signature_before_execute(env, monkeypatch):
    _mesh_ok(monkeypatch)
    e = LiveEngine()
    snap = {}

    async def swap(**kw):
        kw["on_signed"]("PRESIG")
        snap.update(json.loads((env / "state.json").read_text())["pending_buys"])
        return {"ok": True, "stage": "execute", "signature": "PRESIG", "total_in": 100_000_000, "total_out": 10**9}
    monkeypatch.setattr(jupiter, "swap", swap)

    async def bal(**kw):
        return 2.0

    async def tok(mint):
        return 10**9, 6
    monkeypatch.setattr(e, "sol_balance", bal)
    monkeypatch.setattr(e, "token_balance", tok)
    monkeypatch.setattr(e, "_start_stream", lambda m: None)
    monkeypatch.setattr(e, "ensure_loop", lambda: None)

    async def go():
        e.on_signal("scout_solo", MINT)
        await _drain(e)
    asyncio.run(go())
    assert snap[MINT]["stage"] == "sending" and snap[MINT]["signature"] == "PRESIG"
    assert len(e.open_positions()) == 1 and not e.pending_buys


def test_reconcile_creates_position_for_held_tokens(env, monkeypatch):
    e = LiveEngine()
    monkeypatch.setattr(e, "_start_stream", lambda m: None)
    monkeypatch.setattr(e, "ensure_loop", lambda: None)
    now = int(time.time() * 1000)
    e.pending_buys = {MINT: {"arm": "fluffy_pump_dump", "size_sol": 0.4, "ts_ms": now, "stage": "unknown_fill",
                             "signature": "S1"}}
    e.bought_mints = {MINT2: {"arm": "scout_solo", "ts_ms": now, "size_sol": 0.1}}

    async def tok(mint):
        return (5 * 10**9, 6) if mint == MINT else (0, 6)
    monkeypatch.setattr(e, "token_balance", tok)
    r = asyncio.run(e.reconcile())
    assert r["created"] == [MINT] and not r["unresolved"]
    (p,) = e.open_positions()
    assert p.arm == "fluffy_pump_dump" and "reconciled_on_startup" in p.flags and p.tokens_left == 5 * 10**9
    assert p.spent_lamports == 400_000_000 and not e.pending_buys
    assert any(x["kind"] == "buy_reconciled" for x in _ledger(env))


def test_reconcile_clears_reserved_with_no_tokens(env, monkeypatch):
    e = LiveEngine()
    e.pending_buys = {MINT: {"arm": "scout_solo", "size_sol": 0.1, "ts_ms": int(time.time() * 1000), "stage": "reserved"}}

    async def tok(mint):
        return 0, 6
    monkeypatch.setattr(e, "token_balance", tok)
    assert asyncio.run(e.reconcile())["cleared"] == [MINT] and not e.pending_buys


def test_unreadable_state_fails_closed(env):
    sp = env / "state.json"
    sp.write_text("{not json")
    e = LiveEngine()
    e.load()
    assert e.state_error and not e.check_caps("scout_solo", MINT)[0]
    assert any("state" in w for w in e.summary()["warnings"])
    assert e.killed()["active"] is False                 # never triggers kill-sell-all
    e.save()
    assert sp.read_text() == "{not json" and (env / "state.json.recovery.json").exists()


def test_bad_rows_fail_closed(env):
    sp = env / "state.json"
    sp.write_text(json.dumps({"positions": [{"bogus": 1}], "bought_mints": {}, "daily": {}}))
    e = LiveEngine()
    e.load()
    assert e.state_error and e.bad_rows == [{"bogus": 1}] and not e.check_caps("scout_solo", MINT)[0]


def test_old_state_rows_still_load(env):
    p = _pos().__dict__.copy()
    for k in ("ladder_done", "mark_src_ms", "quote_fail_since_ms", "ambiguous_sell"):
        p.pop(k)
    (env / "state.json").write_text(json.dumps({"positions": [p], "bought_mints": {}, "daily": {}}))
    e = LiveEngine()
    e.load()
    assert not e.state_error and len(e.open_positions()) == 1


# ======================= fix 6: stale marks =======================
class _S:
    def __init__(self, price, ms):
        self.mint, self.last_price_sol, self.price_ms, self.migrated = MINT, price, ms, False


def test_stream_price_needs_advancing_source_time(env, monkeypatch):
    e = LiveEngine()
    monkeypatch.setattr(e, "_spawn", lambda c: c.close())
    p = _pos(last_price_sol=0.0001)
    e.positions[p.id] = p
    asyncio.run(e.on_stream(_S(0.00012, 1000)))
    assert p.last_price_sol == 0.00012 and p.mark_src_ms == 1000
    p.last_mark_ms = 1
    asyncio.run(e.on_stream(_S(0.00012, 1000)))          # same source time re-notified → not a fresh mark
    assert p.last_mark_ms == 1


def test_mintstream_price_ms():
    from app.pump.trade_stream import MintStream
    s = MintStream(mint=MINT)
    s.apply_curve({"status": "ok", "price_sol": 1e-8, "read_at_ms": 2000})
    assert s.price_ms == 2000
    s.apply_curve({"status": "ok", "price_sol": 5e-9, "read_at_ms": 1500})   # older cached read
    assert s.last_price_sol == 1e-8 and s.price_ms == 2000


def test_stale_mark_is_not_traded_then_quote_then_sell(env, monkeypatch):
    e = LiveEngine()
    old = int(time.time() * 1000) - 120_000
    p = _pos(last_price_sol=0.00005, last_mark_ms=old, entry_ms=old)    # stale price would say hard stop
    e.positions[p.id] = p
    sold = []

    async def fake_sell(pos, frac, reason):
        sold.append(reason)
    monkeypatch.setattr(e, "_sell", fake_sell)
    asyncio.run(e.manage(p))
    assert sold == []                                     # stale mark → no decision on it
    assert any("stale mark" in w for w in e.summary()["warnings"])

    async def no_quote(**kw):
        return {"ok": False}
    monkeypatch.setattr(jupiter, "order", no_quote)

    async def rpc_fail(*a, **k):
        raise RuntimeError("rpc")
    monkeypatch.setattr(e, "_rpc", rpc_fail)
    asyncio.run(e.mark([p]))
    assert p.quote_fail_since_ms
    p.quote_fail_since_ms -= 181_000
    asyncio.run(e.manage(p))
    assert sold == ["stale_mark_no_quote"]


def test_stale_mark_quote_refreshes(env, monkeypatch):
    e = LiveEngine()
    old = int(time.time() * 1000) - 120_000
    p = _pos(last_mark_ms=old, migrated=False)

    async def quote(**kw):
        return {"ok": True, "out_amount": 200_000_000}
    monkeypatch.setattr(jupiter, "order", quote)

    async def rpc_none(*a, **k):
        return {"value": []}
    monkeypatch.setattr(e, "_rpc", rpc_none)
    asyncio.run(e.mark([p]))
    assert p.mark_source == "jupiter_quote" and p.last_price_sol == pytest.approx(0.0002) and not e.mark_stale(p)


# ======================= fix 7a: sell slippage cap / escalation =======================
def test_sell_slippage_capped(monkeypatch):
    monkeypatch.setenv("LIVE_SELL_SLIPPAGE_STEPS", "2500,6000,10000")
    assert live_config.config()["sell_slippage_steps_bps"] == [2500, 5000]
    monkeypatch.delenv("LIVE_SELL_SLIPPAGE_STEPS")
    assert max(live_config.config()["sell_slippage_steps_bps"]) <= 5000


def test_no_escalation_on_order_error(env, monkeypatch):
    e = LiveEngine()
    p = _pos()
    e.positions[p.id] = p
    calls = []
    monkeypatch.setattr(jupiter, "swap", _fake_swap([{"ok": False, "stage": "order", "error": "no route"}] * 3, calls))

    async def bal(mint):
        return 10**9, 6
    monkeypatch.setattr(e, "token_balance", bal)
    asyncio.run(e._sell(p, 1.0, "hard_stop"))
    assert len(calls) == 1 and p.exit_pending == "hard_stop"


def test_ambiguous_partial_stops_and_reconciles(env, monkeypatch):
    e = LiveEngine()
    p = _pos(last_price_sol=0.00021, high_price_sol=0.00021)
    e.positions[p.id] = p
    calls = []
    monkeypatch.setattr(jupiter, "swap", _fake_swap([{"ok": False, "stage": "execute", "ambiguous": True,
                                                       "signature": "AMB", "order": {"out_amount": 100_000_000}}] * 3,
                                                     calls))
    held = {"n": 10**9}

    async def bal(mint):
        return held["n"], 6
    monkeypatch.setattr(e, "token_balance", bal)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda s, *a: real_sleep(0))
    asyncio.run(e.manage(p))
    assert len(calls) == 1 and p.ambiguous_sell and p.ambiguous_sell["reason"] == "tp_2x"
    asyncio.run(e.manage(p))                               # still unresolved → no second partial
    assert len(calls) == 1
    held["n"] = 5 * 10**8                                  # chain shows it landed
    asyncio.run(e.manage(p))
    assert p.ambiguous_sell is None and p.tokens_left == 5 * 10**8 and p.sells[-1]["reason"] == "tp_2x"
    assert "ambiguous_next_cycle" in p.sells[-1]["flags"]


def test_sell_retry_backoff(env, monkeypatch):
    monkeypatch.setenv("LIVE_SELL_RETRY_S", "5")
    e = LiveEngine()
    p = _pos(exit_pending="hard_stop", sell_failures=4, last_sell_try_ms=int(time.time() * 1000) - 30_000)
    e.positions[p.id] = p
    sold = []

    async def fake_sell(pos, frac, reason):
        sold.append(reason)
    monkeypatch.setattr(e, "_sell", fake_sell)
    asyncio.run(e.manage(p))                               # 5·2^3 = 40 s > 30 s → wait
    assert sold == []
    p.last_sell_try_ms -= 15_000
    asyncio.run(e.manage(p))
    assert sold == ["hard_stop"]


# ======================= fix 7b: async kill + persisted sell_all =======================
def test_kill_endpoint_async_and_sell_all_persists(env, monkeypatch):
    from app.live import api
    assert inspect.iscoroutinefunction(api.live_kill) and inspect.iscoroutinefunction(api.live_resume)
    e = LiveEngine()
    p = _pos()
    e.positions[p.id] = p
    sold = []

    async def fake_sell(pos, frac, reason):
        sold.append(reason)
    monkeypatch.setattr(e, "_sell", fake_sell)

    async def go():
        e.kill(sell_all=True)
        await _drain(e)
    asyncio.run(go())
    assert sold == ["kill_switch_sell_all"]
    e2 = LiveEngine()
    e2.load()
    assert e2.api_kill and e2.api_kill_sell_all and e2.summary()["kill_sell_all"] is True


# ======================= fix 7c: Fluffy/Zyrox ladder =======================
def test_tg_arms_use_ladder(monkeypatch):
    monkeypatch.setenv("LIVE_EXIT_RULES", "tp2x_trail,min10_check")     # global setting does not touch TG arms
    for arm in ("fluffy_pump_dump", "zyrox"):
        r = live_config.arm_rules(arm)
        assert r[0] == "ladder" and "hard_stop" in r and "tp2x_trail" not in r
    assert "tp2x_trail" in live_config.arm_rules("scout_solo")
    assert [x[:2] for x in live_config.ladder()] == [(2.0, 0.5), (3.0, 0.25), (5.0, 0.125)]


def test_ladder_levels_then_trail_and_hard_stop():
    cfg = {**live_config.config(), "trail_pct": 0.30, "hard_stop_pct": 0.35, "max_hold_s": 3600}
    rules = live_config.arm_rules("fluffy_pump_dump")
    t0 = int(time.time() * 1000)
    p = _pos(arm="fluffy_pump_dump", entry_ms=t0)
    assert evaluate_exit(p, 0.000064, t0 + 1000, cfg, rules) == [("sell_all", 1.0, "hard_stop")]
    assert evaluate_exit(p, 0.00019, t0 + 1000, cfg, rules) == []
    assert evaluate_exit(p, 0.00020, t0 + 1000, cfg, rules) == [("sell_frac", 0.5, "tp1_2x")]
    p.ladder_done, p.tp_done, p.tokens_left, p.high_price_sol = ["tp1_2x"], True, 5 * 10**8, 0.0003
    assert evaluate_exit(p, 0.00031, t0 + 1000, cfg, rules) == [("sell_frac", 0.5, "tp2_3x")]
    p.ladder_done.append("tp2_3x"); p.tokens_left = 25 * 10**7; p.high_price_sol = 0.0005
    assert evaluate_exit(p, 0.00051, t0 + 1000, cfg, rules) == [("sell_frac", 0.5, "tp3_5x")]
    p.ladder_done.append("tp3_5x"); p.tokens_left = 125 * 10**6; p.high_price_sol = 0.001
    assert evaluate_exit(p, 0.00075, t0 + 9 * 3_600_000, cfg, rules) == []          # runner: no max hold
    assert evaluate_exit(p, 0.00069, t0 + 1000, cfg, rules) == [("sell_all", 1.0, "trail")]


def test_ladder_partial_failure_is_retried_not_full_exit(env, monkeypatch):
    e = LiveEngine()
    p = _pos(arm="zyrox", last_price_sol=0.00021, high_price_sol=0.00021)
    e.positions[p.id] = p
    monkeypatch.setattr(jupiter, "swap", _fake_swap([{"ok": False, "stage": "execute", "error": "x"}] * 3, []))

    async def bal(mint):
        return 10**9, 6
    monkeypatch.setattr(e, "token_balance", bal)
    asyncio.run(e.manage(p))
    assert p.exit_pending is None and p.ladder_done == [] and p.tp_done is False and p.status == "open"


# ======================= per-position manage =======================
def test_manage_once_is_per_position_and_swallows_errors(env, monkeypatch):
    e = LiveEngine()
    p = _pos()

    async def boom(pos, fresh=False):
        raise RuntimeError("x")
    monkeypatch.setattr(e, "manage", boom)
    asyncio.run(e._manage_once(p))                        # no exception escapes
    assert p.id not in e._managing


def test_stale_price_source_alone_does_not_block():
    assert not tg_gate.mesh_stale({"stale": True, "sources": {"price": {"stale": True}, "token_risk": {"stale": False}}})
    assert tg_gate.mesh_stale({"stale": True, "timed_out": True})
