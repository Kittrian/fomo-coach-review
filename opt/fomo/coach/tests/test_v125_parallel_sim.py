"""v1.25 LIVE_SIGNER_PARALLEL_SIM: coach simulate overlaps the signer request; both must pass; flag off = unchanged."""
import asyncio

import pytest

from app.live import jupiter, signer_client


def _setup(monkeypatch, *, flag, sim_ok=True, sign_fail=False):
    calls = {"sim": [], "exec": 0, "order": 0}
    monkeypatch.setenv("SIGNER_BACKEND", "socket")
    monkeypatch.setenv("LIVE_SIGNER_PARALLEL_SIM", "true" if flag else "false")

    async def order(**kw):
        calls["order"] += 1
        return {"ok": True, "transaction": "UNSIGNED", "router": "metis", "request_id": "r"}

    async def sign_tx(tx, intent):
        await asyncio.sleep(0.2)
        if sign_fail:
            raise signer_client.SignerRefused("policy")
        return "SIGNED"

    async def sim(tx, router, *, rpc_url="", client=None, unsigned=False):
        calls["sim"].append((tx, unsigned, asyncio.get_running_loop().time()))
        await asyncio.sleep(0.2)
        return {"ok": sim_ok, "transport_error": False, "err": None if sim_ok else "boom"}

    async def execute(signed, rid, client=None):
        calls["exec"] += 1
        return {"ok": True, "signature": "S"}

    monkeypatch.setattr(jupiter, "order", order)
    monkeypatch.setattr(jupiter, "sign_tx", sign_tx)
    monkeypatch.setattr(jupiter, "simulate_signed", sim)
    monkeypatch.setattr(jupiter, "execute", execute)
    monkeypatch.setattr(jupiter, "signature_of", lambda s: "S")
    from app.live import fastfill
    monkeypatch.setattr(fastfill, "active_mode", lambda r: "jupiter")
    return calls


def _swap():
    return jupiter.swap(input_mint="A", output_mint="B", amount=1, slippage_bps=100, max_priority_lamports=1)


def test_flag_off_serial_signed_sim(monkeypatch):
    c = _setup(monkeypatch, flag=False)
    t0 = asyncio.run(asyncio.wait_for(_timed(_swap()), 5))
    assert c["sim"][0][:2] == ("SIGNED", False) and c["exec"] == 1 and t0 >= 0.38


def test_flag_on_parallel_unsigned_sim_faster(monkeypatch):
    c = _setup(monkeypatch, flag=True)
    t0 = asyncio.run(asyncio.wait_for(_timed(_swap()), 5))
    assert c["sim"][0][:2] == ("UNSIGNED", True) and c["exec"] == 1 and t0 < 0.35


def test_parallel_sim_failure_never_sends(monkeypatch):
    c = _setup(monkeypatch, flag=True, sim_ok=False)
    r = asyncio.run(_swap())
    assert r["stage"] == "simulate" and c["exec"] == 0


def test_parallel_signer_refusal_never_sends(monkeypatch):
    c = _setup(monkeypatch, flag=True, sign_fail=True)
    r = asyncio.run(_swap())
    assert r["stage"] == "sign" and c["exec"] == 0


def test_flag_needs_signer_backend(monkeypatch):
    monkeypatch.setenv("SIGNER_BACKEND", "local")
    monkeypatch.setenv("LIVE_SIGNER_PARALLEL_SIM", "true")
    assert jupiter.parallel_sim_enabled() is False


async def _timed(coro):
    import time
    t = time.perf_counter()
    await coro
    return time.perf_counter() - t
