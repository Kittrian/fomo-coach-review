"""v1.17.2 item 1: the mesh check runs concurrently with Jupiter order+sign+simulate and gates ONLY /execute.
A blocking / failing / erroring mesh result must still prevent the buy (nothing sent). Uses the REAL jupiter.swap
with order/sign/simulate/execute stubbed — no network, no keypair, no real swaps."""
from __future__ import annotations

import asyncio
import time

import pytest

from app import mesh as mesh_mod
from app.live import jupiter, rugcheck
from app.live.engine import LiveEngine

MINT = "MintBBBB1111111111111111111111111111111pump"


@pytest.fixture
def env(monkeypatch, tmp_path):
    for k, v in {"JUPITER_EXEC_ENABLED": "true", "LIVE_AUTOTRADE_ENABLED": "true",
                 "LIVE_ARMS": "scout_solo,scout_confirmed,fluffy_pump_dump,zyrox",
                 "LIVE_SIZE_SOL_SCOUT_SOLO": "0.10", "LIVE_MAX_SOL_PER_TRADE": "0.40", "LIVE_MIN_SOL_RESERVE": "0.50",
                 "LIVE_STATE_PATH": str(tmp_path / "state.json"), "LIVE_LEDGER_PATH": str(tmp_path / "ledger.jsonl"),
                 "LIVE_KILL_FILE": str(tmp_path / "KILL"), "CANDIDATES_PATH": str(tmp_path / "candidates.jsonl")}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("LIVE_MESH_OVERLAP_ENABLED", raising=False)
    monkeypatch.setattr(jupiter, "pubkey", lambda: "FakePubkey111")
    return tmp_path


class Jup:
    """Stubs for the real jupiter.swap internals; records the order of events."""

    def __init__(self, monkeypatch, events, *, order_s=0.0, sim_ok=True):
        self.events, self.order_calls, self.exec_calls = events, 0, 0

        async def order(**kw):
            self.order_calls += 1
            await asyncio.sleep(order_s)
            events.append(("order", time.time()))
            return {"ok": True, "transaction": "TX", "request_id": "RID", "router": "metis", "out_amount": 2_000_000}

        async def simulate(signed, router, *, rpc_url="", client=None):
            events.append(("simulate", time.time()))
            return {"ok": sim_ok, "err": None if sim_ok else "boom"}

        async def execute(signed, rid, client=None):
            self.exec_calls += 1
            events.append(("execute", time.time()))
            return {"ok": True, "status": "Success", "signature": "SIG", "total_in": 100_000_000,
                    "total_out": 2_000_000_000_000, "ambiguous": False}
        monkeypatch.setattr(jupiter, "order", order)
        monkeypatch.setattr(jupiter, "sign_partial", lambda tx: "SIGNED")
        monkeypatch.setattr(jupiter, "simulate_signed", simulate)
        monkeypatch.setattr(jupiter, "execute", execute)
        monkeypatch.setattr(jupiter, "signature_of", lambda s: "SIG")


def _engine(monkeypatch, events, *, mesh_s=0.0, mesh_result=(True, "mesh ok"), mesh_exc=None, rug=None, rug_s=0.0):
    e = LiveEngine()

    async def mesh(self, mint):
        await asyncio.sleep(mesh_s)
        events.append(("mesh_done", time.time()))
        if mesh_exc:
            raise mesh_exc
        return mesh_result
    monkeypatch.setattr(LiveEngine, "_mesh_check", mesh)

    async def prebuy(mint, arm, *, creator=None):
        await asyncio.sleep(rug_s)
        return rug or {"ok": True, "refuse": [], "unknown": [], "checks": {}, "elapsed_ms": 1}
    monkeypatch.setattr(rugcheck.checker, "prebuy", prebuy)

    async def balance(**kw):
        return 2.5

    async def tok(mint):
        return 2_000_000_000_000, 6
    monkeypatch.setattr(e, "sol_balance", balance)
    monkeypatch.setattr(e, "token_balance", tok)
    monkeypatch.setattr(e, "_start_stream", lambda m: None)
    monkeypatch.setattr(e, "ensure_loop", lambda: None)
    return e


async def _drain(e):
    for _ in range(10):
        if not e._tasks:
            return
        await asyncio.gather(*list(e._tasks), return_exceptions=True)


def _buy(e):
    async def go():
        e.on_signal("scout_solo", MINT, meta={})
        await _drain(e)
    asyncio.run(go())


def _names(events):
    return [n for n, _ in events]


def test_slow_mesh_block_after_simulate_never_executes(env, monkeypatch):
    ev = []
    j = Jup(monkeypatch, ev)
    e = _engine(monkeypatch, ev, mesh_s=0.3, mesh_result=(False, "mesh hard block: honeypot"))
    _buy(e)
    assert j.order_calls == 1 and j.exec_calls == 0                       # built + simulated, never sent
    assert _names(ev).index("simulate") < _names(ev).index("mesh_done")  # overlap really happened
    assert not e.open_positions() and MINT not in e.pending_buys
    d = e.decisions[0]
    assert d["decision"] == "aborted" and any("honeypot" in r for r in d["reasons"])   # same as the v1.15 path
    assert e.stats["mesh_gate_refused"] == 1 and e.stats["mesh_refused"] == 1


def test_fast_mesh_block_refuses_before_any_order(env, monkeypatch):
    ev = []
    j = Jup(monkeypatch, ev)
    e = _engine(monkeypatch, ev, mesh_s=0.0, mesh_result=(False, "mesh unavailable — no buy"), rug_s=0.05)
    _buy(e)
    assert j.order_calls == 0 and j.exec_calls == 0 and not e.open_positions()


@pytest.mark.parametrize("res", [(False, "mesh pending — no buy without the rug/honeypot check"),
                                 (False, "mesh data stale (timed out / stale risk source) — no buy")])
def test_mesh_not_ok_variants_never_execute(env, monkeypatch, res):
    ev = []
    j = Jup(monkeypatch, ev)
    e = _engine(monkeypatch, ev, mesh_s=0.2, mesh_result=res)
    _buy(e)
    assert j.exec_calls == 0 and not e.open_positions()


def test_mesh_exception_never_executes(env, monkeypatch):
    ev = []
    j = Jup(monkeypatch, ev)
    e = _engine(monkeypatch, ev, mesh_s=0.2, mesh_exc=RuntimeError("x"))
    _buy(e)
    assert j.exec_calls == 0 and not e.open_positions() and MINT not in e.pending_buys


def test_slow_mesh_ok_executes_after_mesh_and_records_timing(env, monkeypatch):
    ev = []
    j = Jup(monkeypatch, ev)
    e = _engine(monkeypatch, ev, mesh_s=0.3)
    _buy(e)
    n = _names(ev)
    assert j.exec_calls == 1 and n.index("simulate") < n.index("mesh_done") < n.index("execute")
    p = e.open_positions()[0]
    t = p.meta["timing"]
    assert t["mesh_overlap"] is True and t["mesh_wait_ms"] >= 150 and "mesh_ms" in t


def test_kill_during_mesh_wait_never_executes(env, monkeypatch):
    ev = []
    j = Jup(monkeypatch, ev)
    e = _engine(monkeypatch, ev, mesh_s=0.3)

    async def go():
        e.on_signal("scout_solo", MINT, meta={})
        await asyncio.sleep(0.1)
        (env / "KILL").write_text("stop")
        await _drain(e)
    asyncio.run(go())
    assert j.exec_calls == 0 and not e.open_positions()


def test_very_slow_mesh_requotes_once(env, monkeypatch):
    monkeypatch.setenv("LIVE_MESH_GATE_REQUOTE_S", "0.2")
    ev = []
    j = Jup(monkeypatch, ev)
    e = _engine(monkeypatch, ev, mesh_s=0.4)
    _buy(e)
    assert j.order_calls == 2 and j.exec_calls == 1 and e.stats["mesh_gate_requotes"] == 1
    assert e.open_positions()[0].meta["timing"]["requoted"] is True


def test_overlap_off_restores_serial_order(env, monkeypatch):
    monkeypatch.setenv("LIVE_MESH_OVERLAP_ENABLED", "false")
    ev = []
    j = Jup(monkeypatch, ev)
    e = _engine(monkeypatch, ev, mesh_s=0.2)
    _buy(e)
    n = _names(ev)
    assert j.exec_calls == 1 and n.index("mesh_done") < n.index("order")


def test_overlap_off_block_never_orders(env, monkeypatch):
    monkeypatch.setenv("LIVE_MESH_OVERLAP_ENABLED", "false")
    ev = []
    j = Jup(monkeypatch, ev)
    e = _engine(monkeypatch, ev, mesh_s=0.1, mesh_result=(False, "mesh hard block: x"))
    _buy(e)
    assert j.order_calls == 0 and j.exec_calls == 0


def test_swap_gate_unit(monkeypatch):
    ev = []
    j = Jup(monkeypatch, ev)
    signed = []

    async def no(age):
        return False, "blocked", False

    async def boom(age):
        raise RuntimeError("x")

    async def go(g):
        return await jupiter.swap(input_mint="A", output_mint="B", amount=1, slippage_bps=1, max_priority_lamports=0,
                                  on_signed=signed.append, pre_execute=g)
    r1 = asyncio.run(go(no))
    r2 = asyncio.run(go(boom))
    assert r1["stage"] == "gate" and r1["error"] == "blocked" and r2["stage"] == "gate" and not r2["ok"]
    assert j.exec_calls == 0 and signed == []                         # never persisted as sent, never sent


def test_mesh_live_budget_and_shared_client(monkeypatch):
    seen = []                                                          # (phase, live flag at GET time, bucket used)
    phase = {"p": 1}

    class Resp:
        status_code = 200
        headers = {}

        def json(self):
            return {"ok": True, "data": {}}

    async def fake_get(self, url, params=None):
        if (params or {}).get("token") == MINT_SO or url.endswith(MINT_SO):
            seen.append((phase["p"], mesh_mod._live_ctx.get()))
        return Resp()
    import httpx
    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    used = []
    orig_live, orig_bg = mesh_mod._live_bucket.acquire, mesh_mod._bucket.acquire

    async def live_acq(**kw):
        used.append((phase["p"], "live"))
        return await orig_live(**kw)

    async def bg_acq(**kw):
        used.append((phase["p"], "bg"))
        return await orig_bg(**kw)
    monkeypatch.setattr(mesh_mod._live_bucket, "acquire", live_acq)
    monkeypatch.setattr(mesh_mod._bucket, "acquire", bg_acq)
    mesh_mod.clear_mesh_cache()
    mesh_mod._cache.clear()

    async def go():
        c1 = mesh_mod._shared_client()
        assert c1 is mesh_mod._shared_client()                         # one keep-alive client per loop
        await mesh_mod.enrich_with_timeout(MINT_SO, timeout=5, live=True)
        phase["p"] = 2
        mesh_mod.clear_mesh_cache()
        mesh_mod._cache.clear()
        await mesh_mod.enrich_with_timeout(MINT_SO, timeout=5)
    asyncio.run(go())
    p1 = [f for ph, f in seen if ph == 1]
    p2 = [f for ph, f in seen if ph == 2]
    assert len(p1) >= 4 and all(p1)                                    # live check: all 4 GETs on the live budget
    assert len(p2) >= 4 and not any(p2)                                # background: never the live budget
    assert ("live" in [b for ph, b in used if ph == 1]) and ("live" not in [b for ph, b in used if ph == 2])
    assert mesh_mod._live_ctx.get() is False                           # flag never leaks out of the task


MINT_SO = "So11111111111111111111111111111111111111112"


def test_429_cooldown_pauses_live_budget_too():
    b = mesh_mod.TokenBucket(100, 10)
    live = mesh_mod.TokenBucket(100, 10)
    b.cooldown_until = time.monotonic() + 0.25

    async def go():
        t = time.monotonic()
        await live.acquire(shared_cooldown=b)
        return time.monotonic() - t
    assert asyncio.run(go()) >= 0.2
