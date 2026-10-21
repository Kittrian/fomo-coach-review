"""Fast-path prototype (staging only): prewarm of the read-only pre-buy checks at detection time, SOL balance read
in parallel with the gates, per-stage buy timings, trader sells that arrive while our buy is in flight, optional
scout websocket commitment. Everything mocked: no network, no live state, no keypair, no real swaps."""
from __future__ import annotations

import asyncio
import time

import pytest

from app.live import jupiter, rugcheck
from app.live.engine import LiveEngine

MINT = "MintAAAA1111111111111111111111111111111pump"
OK_SWAP = {"ok": True, "stage": "execute", "signature": "BUYSIG", "total_in": 100_000_000,
           "total_out": 2_000_000_000_000, "order": {"router": "metis"}}


@pytest.fixture
def env(monkeypatch, tmp_path):
    for k, v in {"JUPITER_EXEC_ENABLED": "true", "LIVE_AUTOTRADE_ENABLED": "true",
                 "LIVE_ARMS": "scout_solo,scout_confirmed,fluffy_pump_dump,zyrox",
                 "LIVE_SIZE_SOL_SCOUT_SOLO": "0.10", "LIVE_MAX_SOL_PER_TRADE": "0.40", "LIVE_MIN_SOL_RESERVE": "0.50",
                 "LIVE_STATE_PATH": str(tmp_path / "state.json"), "LIVE_LEDGER_PATH": str(tmp_path / "ledger.jsonl"),
                 "LIVE_KILL_FILE": str(tmp_path / "KILL"), "CANDIDATES_PATH": str(tmp_path / "candidates.jsonl")}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(jupiter, "pubkey", lambda: "FakePubkey111")
    return tmp_path


def _engine(monkeypatch, *, mesh_s=0.0, bal_s=0.0, swap=None):
    e = LiveEngine()

    async def mesh(self, mint):
        await asyncio.sleep(mesh_s)
        return True, "mesh ok"
    monkeypatch.setattr(LiveEngine, "_mesh_check", mesh)

    async def balance(**kw):
        await asyncio.sleep(bal_s)
        return 2.5

    async def tok(mint):
        return 2_000_000_000_000, 6
    calls = []

    async def default_swap(**kw):
        calls.append(kw)
        return dict(OK_SWAP)
    monkeypatch.setattr(e, "sol_balance", balance)
    monkeypatch.setattr(e, "token_balance", tok)
    monkeypatch.setattr(e, "_start_stream", lambda m: None)
    monkeypatch.setattr(e, "ensure_loop", lambda: None)
    monkeypatch.setattr(jupiter, "swap", swap or default_swap)
    return e, calls


async def _drain(e):
    for _ in range(8):
        if not e._tasks:
            return
        await asyncio.gather(*list(e._tasks), return_exceptions=True)


def _counting_prebuy(monkeypatch, result=None, delay=0.0):
    seen = []

    async def prebuy(mint, arm, *, creator=None):
        seen.append((mint, arm, creator))
        await asyncio.sleep(delay)
        return result or {"ok": True, "refuse": [], "unknown": [], "checks": {}, "elapsed_ms": 1}
    monkeypatch.setattr(rugcheck.checker, "prebuy", prebuy)
    return seen


def test_prewarm_rug_task_is_reused_by_buy(env, monkeypatch):
    seen = _counting_prebuy(monkeypatch)
    e, calls = _engine(monkeypatch)
    sched = []
    import app.mesh as mesh_mod
    monkeypatch.setattr(mesh_mod, "schedule_enrichment", lambda m, c="solana", **k: sched.append(m) or True)

    async def go():
        assert e.prewarm("scout_solo", MINT, creator="CR") is True
        assert e.prewarm("scout_solo", MINT, creator="CR") is True       # idempotent while fresh
        await asyncio.sleep(0)
        e.on_signal("scout_solo", MINT, trader_wallets=["ScoutW"], ref_price_sol=4.5e-8, meta={"creator": "CR"})
        await _drain(e)
    asyncio.run(go())
    assert len(seen) == 1 and seen[0] == (MINT, "scout_solo", "CR") and sched == [MINT]
    assert e.stats["prewarm_started"] == 1 and e.stats["prewarm_used"] == 1
    (p,) = e.open_positions()
    t = p.meta["timing"]
    assert t["prewarmed"] is True and {"mesh_ms", "gates_ms", "pre_swap_ms", "swap_ms", "post_fill_ms"} <= set(t)
    assert len(calls) == 1 and MINT not in e._prewarm


def test_prewarmed_refusal_still_refuses_and_never_swaps(env, monkeypatch):
    _counting_prebuy(monkeypatch, {"ok": False, "refuse": ["rugcheck bundle: launch bundle: 4 wallet(s)"],
                                   "unknown": [], "checks": {}, "strict": False})
    e, calls = _engine(monkeypatch)

    async def go():
        e.prewarm("scout_solo", MINT)
        e.on_signal("scout_solo", MINT, meta={})
        await _drain(e)
    asyncio.run(go())
    assert not calls and not e.open_positions() and MINT not in e.bought_mints and e.day()["trades"] == 0
    assert e.decisions[0]["decision"] == "aborted" and "rugcheck bundle" in e.decisions[0]["reasons"][0]


def test_prewarm_skipped_when_caps_refuse_or_disabled(env, monkeypatch):
    seen = _counting_prebuy(monkeypatch)
    e, _ = _engine(monkeypatch)

    async def go():
        monkeypatch.setenv("LIVE_ARMS", "fluffy_pump_dump")
        assert e.prewarm("scout_solo", MINT) is False                     # arm not live → no work at all
        monkeypatch.setenv("LIVE_ARMS", "scout_solo")
        monkeypatch.setenv("LIVE_PREWARM_ENABLED", "false")
        assert e.prewarm("scout_solo", MINT) is False
        monkeypatch.setenv("LIVE_PREWARM_ENABLED", "true")
        e.bought_mints[MINT] = {"arm": "scout_solo"}
        assert e.prewarm("scout_solo", MINT) is False                     # dedupe-forever mint
    asyncio.run(go())
    assert seen == [] and not e._prewarm


def test_stale_or_mismatched_prewarm_is_not_used(env, monkeypatch):
    seen = _counting_prebuy(monkeypatch)
    e, calls = _engine(monkeypatch)

    async def go():
        e.prewarm("scout_solo", MINT)
        assert e._take_prewarm_rug(MINT, "zyrox") is None                  # Telegram arms fail closed → own check
        e.prewarm("scout_solo", MINT)
        e._prewarm[MINT]["ts"] -= 60                                        # older than LIVE_PREWARM_MAX_AGE_S
        e.on_signal("scout_solo", MINT, meta={})
        await _drain(e)
    asyncio.run(go())
    assert len(seen) == 3 and e.stats["prewarm_used"] == 0 and len(calls) == 1
    assert e.open_positions()[0].meta["timing"]["prewarmed"] is False


def test_balance_read_overlaps_the_gates(env, monkeypatch):
    _counting_prebuy(monkeypatch)
    e, _ = _engine(monkeypatch, mesh_s=0.3, bal_s=0.3)

    async def go():
        e.on_signal("scout_solo", MINT, meta={})
        await _drain(e)
    t0 = time.time()
    asyncio.run(go())
    assert time.time() - t0 < 0.55                                          # serial would be ≥ 0.6 s
    assert e.open_positions()[0].meta["timing"]["pre_swap_ms"] < 550


def test_balance_task_cancelled_when_gate_refuses(env, monkeypatch):
    _counting_prebuy(monkeypatch)
    e, calls = _engine(monkeypatch, bal_s=5.0)

    async def bad(self, mint):
        return False, "mesh hard block: honeypot"
    monkeypatch.setattr(LiveEngine, "_mesh_check", bad)

    async def go():
        e.on_signal("scout_solo", MINT, meta={})
        await _drain(e)
    t0 = time.time()
    asyncio.run(go())
    assert time.time() - t0 < 2.0 and not calls and not e.open_positions()


def test_trader_sell_during_inflight_buy_applies_on_open(env, monkeypatch):
    _counting_prebuy(monkeypatch)
    managed = []
    holder = {}

    async def swap(**kw):
        holder["e"].note_trader_sell("OtherW", MINT)                         # not our trader → ignored
        holder["e"].note_trader_sell("ScoutW", MINT)                         # scout dumps while we are buying
        return dict(OK_SWAP)
    e, _ = _engine(monkeypatch, swap=swap)
    holder["e"] = e

    async def fake_manage(p, fresh=False):
        managed.append(p.id)
    monkeypatch.setattr(e, "manage", fake_manage)

    async def go():
        e.on_signal("scout_solo", MINT, trader_wallets=["ScoutW"], meta={})
        await _drain(e)
    asyncio.run(go())
    (p,) = e.open_positions()
    assert p.trader_sold and "trader_sold_while_inflight" in p.flags and managed == [p.id]
    assert e.stats["trader_sell_while_inflight"] == 1 and not e.pending_trader_sells


def test_trader_sell_during_failed_buy_is_dropped(env, monkeypatch):
    _counting_prebuy(monkeypatch)
    holder = {}

    async def swap(**kw):
        holder["e"].note_trader_sell("ScoutW", MINT)
        return {"ok": False, "stage": "order", "error": "no route"}
    e, _ = _engine(monkeypatch, swap=swap)
    holder["e"] = e

    async def go():
        e.on_signal("scout_solo", MINT, trader_wallets=["ScoutW"], meta={})
        await _drain(e)
    asyncio.run(go())
    assert not e.open_positions() and not e.pending_trader_sells


def test_scout_ws_commitment_default_unchanged(monkeypatch):
    from app.scout import source as src_mod
    s = src_mod.ScoutSource()
    s.sub_method["K"] = "account"
    assert s._sub_msg(1, "K")["params"][1]["commitment"] == "confirmed"
    assert s._sub_msg(2, "L")["params"][1]["commitment"] == "confirmed"
    monkeypatch.setenv("SCOUT_WS_COMMITMENT", "processed")
    assert s._sub_msg(3, "L")["params"][1]["commitment"] == "processed"
    monkeypatch.setenv("SCOUT_WS_COMMITMENT", "finalized-typo")
    assert s._sub_msg(4, "L")["params"][1]["commitment"] == "confirmed"


def test_scout_arm_a_prewarms_before_the_age_lookup(monkeypatch, tmp_path):
    from app.fomo_onchain import market
    from app.live.engine import engine as live_engine
    from app.scout import engine as eng_mod, filters as filt_mod, roster as roster_mod, source as src_mod
    scout, creator = "4BdKaxN8G6ka4GYtQQWk4G4dZRUTX2vQH9GcXdBREFUk", "862TYSvRYoiHAK3F3WwTRYAfuGiQaGdxedN9AGvRGWo2"
    mint = "DwcXyhEcSvzWgakDKpetZLFvbAHutpGpzhbU4iempump"
    vtr0, vsr0 = 1_073_000_000 * 10**6, 30 * 10**9
    monkeypatch.setattr(eng_mod, "LOG_PATH", tmp_path / "scout.jsonl")
    monkeypatch.setattr(filt_mod, "PATH", tmp_path / "f.json")
    monkeypatch.setenv("SCOUT_LATENCIES", "2,5")
    eng_mod.engine.__init__()
    filt_mod.filters.__init__()
    roster_mod.roster.__init__()
    roster_mod.roster.add(scout, "Jijo", "scout")
    monkeypatch.setattr(market, "sol_usd", lambda: 150.0)
    order = []

    async def age(m, max_pages=3):
        order.append("age")
        return {"ok": True, "created_s": time.time() - 120, "first_sigs": ["c"]}
    curve = {"virtual_token_reserves": vtr0, "virtual_quote_reserves": vsr0, "real_token_reserves": 793_100_000 * 10**6,
             "real_quote_reserves": 0, "complete": False, "quote_is_sol": True, "creator": creator}

    async def rc(m):
        return dict(curve, read_at_ms=int(time.time() * 1000))

    async def rcs(ms):
        return {m: dict(curve, read_at_ms=int(time.time() * 1000)) for m in ms}
    monkeypatch.setattr(src_mod.source, "token_age", age)
    monkeypatch.setattr(src_mod.source, "read_curve", rc)
    monkeypatch.setattr(src_mod.source, "read_curves", rcs)
    monkeypatch.setattr(eng_mod.engine, "scan_first_buyers", lambda m: asyncio.sleep(0))
    monkeypatch.setattr(live_engine, "prewarm", lambda arm, m, creator=None: order.append(("prewarm", arm, m, creator)))
    monkeypatch.setattr(live_engine, "on_signal", lambda *a, **k: {"decision": "refused"})
    ev = {"mint": mint, "sol_amount": 10**9, "token_amount": 34_000_000 * 10**6, "is_buy": True, "user": scout,
          "timestamp": int(time.time()), "virtual_sol_reserves": vsr0 + 10**9, "virtual_token_reserves": vtr0 - 34_000_000 * 10**6,
          "real_sol_reserves": 10**9, "real_token_reserves": 700_000_000 * 10**6, "creator": creator}

    async def go():
        E = eng_mod.engine
        E.on_trade(roster_mod.roster.by_wallet[scout], ev, "sigA", int(time.time() * 1000) - 10_000)
        for _ in range(50):
            await asyncio.sleep(0.01)
            if not E._tasks:
                break
    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(go())
    assert order[:2] == [("prewarm", "scout_solo", mint, creator), "age"]
