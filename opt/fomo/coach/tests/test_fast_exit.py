"""v1.17.2 item 3: sells start from the known position size while the balance read runs; the read is awaited in
the pre-send gate, so a wrong / zero balance never reaches /execute. Plus the 0.2 s lamport-resolve delay.
Real jupiter.swap with order/sign/simulate/execute stubbed — no network, no keypair."""
from __future__ import annotations

import asyncio
import time

import pytest

from app.live import jupiter
from app.live.engine import LiveEngine, LivePos

MINT = "MintCCCC1111111111111111111111111111111pump"


@pytest.fixture
def env(monkeypatch, tmp_path):
    for k, v in {"JUPITER_EXEC_ENABLED": "true", "LIVE_AUTOTRADE_ENABLED": "true",
                 "LIVE_ARMS": "scout_solo,scout_confirmed,fluffy_pump_dump,zyrox",
                 "LIVE_STATE_PATH": str(tmp_path / "state.json"), "LIVE_LEDGER_PATH": str(tmp_path / "ledger.jsonl"),
                 "LIVE_KILL_FILE": str(tmp_path / "KILL"), "LIVE_SELL_RETRY_S": "0", "LIVE_SELL_STEP_BACKOFF_S": "0",
                 "CANDIDATES_PATH": str(tmp_path / "candidates.jsonl")}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("LIVE_SELL_SKIP_BALANCE", raising=False)
    monkeypatch.setattr(jupiter, "pubkey", lambda: "FakePubkey111")
    return tmp_path


def _pos(**kw) -> LivePos:
    base = dict(mint=MINT, arm="scout_solo", size_sol=0.1, spent_lamports=100_000_000, tokens_initial=1_000_000_000,
                tokens_left=1_000_000_000, entry_price_sol=0.0001, entry_ms=int(time.time() * 1000))
    base.update(kw)
    return LivePos(**base)


class Rig:
    def __init__(self, monkeypatch, e, *, onchain=1_000_000_000, bal_s=0.15, sim_fail_over_balance=False):
        self.ev, self.orders, self.execs = [], [], []

        async def tok(mint):
            await asyncio.sleep(bal_s)
            self.ev.append("balance")
            return onchain, 6

        async def order(**kw):
            self.ev.append("order")
            self.orders.append(kw["amount"])
            await asyncio.sleep(0.05)
            return {"ok": True, "transaction": "TX", "request_id": "RID", "router": "metis",
                    "out_amount": 50_000_000, "_amount": kw["amount"]}

        async def simulate(signed, router, *, rpc_url="", client=None):
            self.ev.append("simulate")
            if sim_fail_over_balance and self.orders[-1] > onchain:
                return {"ok": False, "err": "insufficient funds"}
            return {"ok": True}

        async def execute(signed, rid, client=None):
            self.ev.append("execute")
            self.execs.append(self.orders[-1])
            return {"ok": True, "status": "Success", "signature": "SIG", "total_in": self.orders[-1],
                    "total_out": 50_000_000, "ambiguous": False}
        monkeypatch.setattr(e, "token_balance", tok)
        monkeypatch.setattr(jupiter, "order", order)
        monkeypatch.setattr(jupiter, "sign_partial", lambda tx: "SIGNED")
        monkeypatch.setattr(jupiter, "simulate_signed", simulate)
        monkeypatch.setattr(jupiter, "execute", execute)
        monkeypatch.setattr(jupiter, "signature_of", lambda s: "SIG")


def _sell(e, p, frac=1.0, reason="trader_sold"):
    e.positions[p.id] = p
    asyncio.run(e._sell(p, frac, reason))


def test_fast_sell_orders_before_balance_and_checks_it_before_execute(env, monkeypatch):
    e = LiveEngine()
    r = Rig(monkeypatch, e, bal_s=0.15)
    p = _pos()
    _sell(e, p)
    assert r.ev.index("order") < r.ev.index("balance") < r.ev.index("execute")
    assert r.execs == [1_000_000_000] and p.status == "closed" and e.stats["sell_fast_path"] == 1


def test_zero_balance_never_executes_and_closes(env, monkeypatch):
    e = LiveEngine()
    r = Rig(monkeypatch, e, onchain=0)
    p = _pos()
    _sell(e, p)
    assert r.execs == [] and p.status == "closed"


def test_smaller_balance_fixes_amount_before_sending(env, monkeypatch):
    e = LiveEngine()
    r = Rig(monkeypatch, e, onchain=990_000_000, bal_s=0.0)          # e.g. token-2022 transfer fee
    p = _pos()
    _sell(e, p)
    assert r.execs == [990_000_000] and r.orders[-1] == 990_000_000 and e.stats["sell_amount_fixed"] == 1


def test_sim_failure_from_wrong_amount_is_fixed_and_retried(env, monkeypatch):
    e = LiveEngine()
    r = Rig(monkeypatch, e, onchain=990_000_000, bal_s=0.0, sim_fail_over_balance=True)
    p = _pos()
    _sell(e, p)
    assert r.execs == [990_000_000] and p.tokens_left == 10_000_000 and p.exit_pending   # same as the old path


def test_full_exit_sells_extra_dust_like_before(env, monkeypatch):
    e = LiveEngine()
    r = Rig(monkeypatch, e, onchain=1_000_000_500, bal_s=0.0)
    p = _pos()
    _sell(e, p)
    assert r.execs == [1_000_000_500]


def test_partial_sell_amount_unchanged(env, monkeypatch):
    e = LiveEngine()
    r = Rig(monkeypatch, e, bal_s=0.05)
    p = _pos()
    _sell(e, p, frac=0.5, reason="tp_2x")
    assert r.execs == [500_000_000] and p.tokens_left == 500_000_000


def test_failed_before_uses_old_serial_path(env, monkeypatch):
    e = LiveEngine()
    r = Rig(monkeypatch, e, bal_s=0.05)
    p = _pos(sell_failures=1)
    _sell(e, p)
    assert r.ev.index("balance") < r.ev.index("order") and e.stats["sell_fast_path"] == 0


def test_flag_off_uses_old_serial_path(env, monkeypatch):
    monkeypatch.setenv("LIVE_SELL_SKIP_BALANCE", "false")
    e = LiveEngine()
    r = Rig(monkeypatch, e, bal_s=0.05)
    p = _pos()
    _sell(e, p)
    assert r.ev.index("balance") < r.ev.index("order") and r.execs == [1_000_000_000]


def test_unreadable_balance_proceeds_with_known_size(env, monkeypatch):
    e = LiveEngine()
    Rig(monkeypatch, e)
    execs = []

    async def tok(mint):
        return None, None
    monkeypatch.setattr(e, "token_balance", tok)
    orig = jupiter.execute

    async def ex(signed, rid, client=None):
        execs.append(1)
        return await orig(signed, rid, client=client)
    monkeypatch.setattr(jupiter, "execute", ex)
    p = _pos()
    _sell(e, p)
    assert execs == [1] and p.status == "closed"


def test_lamport_resolve_delay_default_is_0_2(monkeypatch):
    from app.scout import source as src
    monkeypatch.delenv("SCOUT_LAMPORT_RESOLVE_DELAY_S", raising=False)
    s = src.ScoutSource()
    delays = []
    real_sleep = asyncio.sleep

    async def fake_sleep(d, *a, **k):
        delays.append(d)
        await real_sleep(0)

    async def call(method, params, **kw):
        return []
    monkeypatch.setattr(src.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(s, "call", call)

    class W:
        wallet = "W1"
    asyncio.run(s._resolve_wallet(W(), 100, 10**9, 0))
    assert delays[0] == 0.2
