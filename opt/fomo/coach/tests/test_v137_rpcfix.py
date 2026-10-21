"""v1.37 (Oct 18 2026): sells retry a signer-RPC 429 instead of dropping the exit; the rug check overlaps the unsigned
order build and nothing is signed without a pass; the exit watcher finds non-SOL PumpSwap pools and stops re-querying
a missing pool every loop. Real jupiter.swap with order/sign/simulate/execute stubbed — no network, no keypair."""
from __future__ import annotations

import asyncio
import time

import pytest

from app.live import jupiter, rugcheck
from app.live import engine as eng
from app.live.engine import LiveEngine, LivePos
from app.live.signer_client import SignerRefused

MINT = "MintDDDD1111111111111111111111111111111pump"
E429 = "evaluation error RuntimeError: getMultipleAccounts http 429"


@pytest.fixture
def env(monkeypatch, tmp_path):
    for k, v in {"JUPITER_EXEC_ENABLED": "true", "LIVE_AUTOTRADE_ENABLED": "true",
                 "LIVE_ARMS": "scout_solo,scout_confirmed,fluffy_pump_dump,zyrox",
                 "LIVE_SIZE_SOL_SCOUT_SOLO": "0.10", "LIVE_MAX_SOL_PER_TRADE": "0.40", "LIVE_MIN_SOL_RESERVE": "0.50",
                 "LIVE_STATE_PATH": str(tmp_path / "state.json"), "LIVE_LEDGER_PATH": str(tmp_path / "ledger.jsonl"),
                 "LIVE_KILL_FILE": str(tmp_path / "KILL"), "LIVE_SELL_RETRY_S": "0", "LIVE_SELL_STEP_BACKOFF_S": "0",
                 "LIVE_SELL_SIGN_RPC_BACKOFF_S": "0.05", "CANDIDATES_PATH": str(tmp_path / "candidates.jsonl"),
                 "LIVE_DIRECT_PUMP_LIVE_ARMS": "", "LIVE_WATCH_ENABLED": "true"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("LIVE_SELL_SKIP_BALANCE", "false")
    monkeypatch.setattr(jupiter, "pubkey", lambda: "FakePubkey111")
    return tmp_path


class Rig:
    def __init__(self, monkeypatch, *, sign_fail=0, sign_err=E429, order_s=0.0):
        self.ev, self.execs, self.signs, self.orders = [], 0, 0, 0
        left = {"n": sign_fail}

        async def order(**kw):
            self.orders += 1
            await asyncio.sleep(order_s)
            self.ev.append(("order", time.time()))
            return {"ok": True, "transaction": "TX", "request_id": "RID", "router": "metis", "out_amount": 50_000_000}

        async def sign_tx(tx, intent):
            self.ev.append(("sign", time.time()))
            if left["n"] > 0:
                left["n"] -= 1
                raise SignerRefused(sign_err)
            self.signs += 1
            return "SIGNED"

        async def simulate(signed, router, *, rpc_url="", client=None, unsigned=False):
            return {"ok": True}

        async def execute(signed, rid, client=None):
            self.execs += 1
            self.ev.append(("execute", time.time()))
            return {"ok": True, "status": "Success", "signature": "SIG", "total_in": 1_000_000_000,
                    "total_out": 50_000_000, "ambiguous": False}
        monkeypatch.setattr(jupiter, "order", order)
        monkeypatch.setattr(jupiter, "sign_tx", sign_tx)
        monkeypatch.setattr(jupiter, "simulate_signed", simulate)
        monkeypatch.setattr(jupiter, "execute", execute)
        monkeypatch.setattr(jupiter, "signature_of", lambda s: "SIG")


def _pos(**kw) -> LivePos:
    base = dict(mint=MINT, arm="scout_solo", size_sol=0.1, spent_lamports=100_000_000, tokens_initial=1_000_000_000,
                tokens_left=1_000_000_000, entry_price_sol=0.0001, entry_ms=int(time.time() * 1000))
    base.update(kw)
    return LivePos(**base)


def _sell_engine(monkeypatch):
    e = LiveEngine()

    async def tok(mint):
        return 1_000_000_000, 6
    monkeypatch.setattr(e, "token_balance", tok)
    return e


# ---------------- transient classifier ----------------
@pytest.mark.parametrize("err,want", [
    ("sign SignerRefused: evaluation error RuntimeError: getMultipleAccounts http 429", True),
    ("sign SignerRefused: evaluation error RuntimeError: simulateTransaction http 429", True),
    ("sign SignerRefused: evaluation error RuntimeError: simulateTransaction http 503", True),
    ("sign SignerRefused: program not allowed: proVF4pMXVaYqmy4NjniPh4pqKNfMmsihgd4wdkCX3u", False),
    ("sign SignerRefused: 24h buy volume cap", False),
    ("order HTTP 429", False),
    (None, False),
])
def test_transient_sign_error(err, want):
    assert eng._transient_sign_error(err) is want


# ---------------- sells ----------------
def test_tp_sell_survives_two_signer_429s(env, monkeypatch):
    e = _sell_engine(monkeypatch)
    r = Rig(monkeypatch, sign_fail=2)
    p = _pos()
    e.positions[p.id] = p
    asyncio.run(e._sell(p, 0.5, "tp_2x"))
    assert r.execs == 1 and e.stats["sell_sign_rpc_retry"] == 2
    assert p.tokens_left < 1_000_000_000 and p.sell_failures == 0


def test_stop_sell_survives_429_full_exit(env, monkeypatch):
    e = _sell_engine(monkeypatch)
    r = Rig(monkeypatch, sign_fail=1)
    p = _pos()
    e.positions[p.id] = p
    asyncio.run(e._sell(p, 1.0, "hard_stop"))
    assert r.execs == 1 and p.status == "closed"


def test_retries_are_capped_then_old_failure_path(env, monkeypatch):
    monkeypatch.setenv("LIVE_SELL_SIGN_RPC_RETRIES", "2")
    e = _sell_engine(monkeypatch)
    r = Rig(monkeypatch, sign_fail=99)
    p = _pos()
    e.positions[p.id] = p
    asyncio.run(e._sell(p, 1.0, "hard_stop"))
    assert r.execs == 0 and e.stats["sell_sign_rpc_retry"] == 2
    assert p.exit_pending == "hard_stop" and p.sell_failures == 1          # unchanged v1.15 retry/backoff afterwards


def test_policy_refusal_is_not_retried(env, monkeypatch):
    e = _sell_engine(monkeypatch)
    r = Rig(monkeypatch, sign_fail=5, sign_err="program not allowed: XYZ")
    p = _pos()
    e.positions[p.id] = p
    asyncio.run(e._sell(p, 1.0, "trail"))
    assert r.execs == 0 and e.stats["sell_sign_rpc_retry"] == 0 and r.orders == 1


# ---------------- buys: rug check overlapped, sign only after a pass ----------------
def _buy_engine(monkeypatch, *, rug=None, rug_s=0.0):
    e = LiveEngine()

    async def mesh(self, mint):
        return True, "mesh ok"
    monkeypatch.setattr(LiveEngine, "_mesh_check", mesh)

    async def prebuy(mint, arm, *, creator=None):
        await asyncio.sleep(rug_s)
        return rug or {"ok": True, "refuse": [], "unknown": [], "checks": {}, "elapsed_ms": int(rug_s * 1000)}
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


def _buy(e):
    async def go():
        e.on_signal("scout_solo", MINT, meta={})
        for _ in range(10):
            if not e._tasks:
                return
            await asyncio.gather(*list(e._tasks), return_exceptions=True)
    asyncio.run(go())


def test_overlap_refusal_never_signs(env, monkeypatch):
    monkeypatch.setenv("LIVE_RUG_OVERLAP_ENABLED", "true")
    r = Rig(monkeypatch, order_s=0.05)
    e = _buy_engine(monkeypatch, rug={"ok": False, "refuse": ["rugcheck bundle: 6 buyers"], "unknown": [],
                                      "checks": {}, "elapsed_ms": 150}, rug_s=0.15)
    _buy(e)
    assert r.orders == 1 and r.signs == 0 and r.execs == 0
    assert not e.open_positions() and MINT not in e.pending_buys
    assert any("bundle" in x for x in e.decisions[0]["reasons"]) and e.stats["rug_refused"] == 1


def test_overlap_pass_order_built_while_rug_runs(env, monkeypatch):
    monkeypatch.setenv("LIVE_RUG_OVERLAP_ENABLED", "true")
    r = Rig(monkeypatch, order_s=0.05)
    e = _buy_engine(monkeypatch, rug_s=0.2)
    t0 = time.time()
    _buy(e)
    assert r.execs == 1 and len(e.open_positions()) == 1
    names = [n for n, _ in r.ev]
    assert names.index("order") < names.index("sign")
    t_order = [t for n, t in r.ev if n == "order"][0]
    t_sign = [t for n, t in r.ev if n == "sign"][0]
    assert t_order - t0 < 0.15 <= t_sign - t0                # order fetched before the rug answer; sign after it
    tmg = e.open_positions()[0].meta["timing"]
    assert tmg["rug_overlap"] is True and tmg["rug_wait_ms"] >= 100


def test_overlap_timeout_fails_closed_for_telegram_arm(env, monkeypatch):
    monkeypatch.setenv("LIVE_RUG_OVERLAP_ENABLED", "true")
    monkeypatch.setenv("LIVE_RUG_TIMEOUT_S", "0.05")
    monkeypatch.setenv("LIVE_SIZE_SOL_FLUFFY_PUMP_DUMP", "0.10")
    r = Rig(monkeypatch)
    e = _buy_engine(monkeypatch, rug_s=3.0)

    async def go():
        e.on_signal("fluffy_pump_dump", MINT, meta={})
        for _ in range(10):
            if not e._tasks:
                return
            await asyncio.gather(*list(e._tasks), return_exceptions=True)
    asyncio.run(go())
    assert r.signs == 0 and r.execs == 0 and not e.open_positions()


def test_overlap_off_is_old_serial_order(env, monkeypatch):
    monkeypatch.delenv("LIVE_RUG_OVERLAP_ENABLED", raising=False)
    r = Rig(monkeypatch)
    e = _buy_engine(monkeypatch, rug_s=0.15)
    t0 = time.time()
    _buy(e)
    t_order = [t for n, t in r.ev if n == "order"][0]
    assert t_order - t0 >= 0.15 and r.execs == 1


# ---------------- exit watcher coverage ----------------
def test_watch_pool_lookup_negative_cache_and_unwatched(env, monkeypatch):
    from app.live.watch import watch
    e = LiveEngine()
    calls = []

    async def rpc(method, params):
        calls.append(method)
        return {"value": None}
    monkeypatch.setattr(e, "_rpc", rpc)

    async def curve(mint):
        return None
    monkeypatch.setattr(e, "_read_curve_raw", curve)
    synced = []
    monkeypatch.setattr(watch, "sync", lambda wanted, cb: synced.append(dict(wanted)))
    p = _pos(migrated=True)
    for _ in range(5):
        asyncio.run(e._sync_watch([p]))
    assert calls.count("getAccountInfo") == 1 and watch.unwatched == 1 and synced[-1] == {}


def test_watch_finds_non_sol_quote_pool(env, monkeypatch):
    import base64
    from app.live.watch import watch
    from app.pump import curve as curve_mod
    from app.pump import solana_pda
    e = LiveEngine()
    QM = "pumpCmXqMfrsAkQ5r49WcJnRayYRqmXz6ae8H7H9Dfn"
    seen = []

    async def rpc(method, params):
        seen.append(params[0])
        return {"value": {"owner": "x", "lamports": 1, "data": [base64.b64encode(b"P").decode(), "base64"]}} \
            if params[0] == solana_pda.canonical_pumpswap_pool(MINT, QM) else {"value": None}
    monkeypatch.setattr(e, "_rpc", rpc)
    monkeypatch.setattr(eng.rpc, "_decode_account", lambda v: {"data": b"P"} if v else None)
    monkeypatch.setattr(curve_mod, "decode_pool", lambda d: {"base_vault": "BV", "quote_vault": "QV"})

    async def curve(mint):
        return {"complete": True, "quote_is_sol": False, "quote_mint": QM}
    monkeypatch.setattr(e, "_read_curve_raw", curve)
    synced = []
    monkeypatch.setattr(watch, "sync", lambda wanted, cb: synced.append(dict(wanted)))
    p = _pos(migrated=True)
    asyncio.run(e._sync_watch([p]))
    assert synced[-1] == {"BV": (MINT, "pool_base_x"), "QV": (MINT, "pool_quote_x")} and watch.unwatched == 0


def test_non_sol_pool_notification_drain_only(env, monkeypatch):
    e = LiveEngine()
    p = _pos(migrated=True)
    e.positions[p.id] = p
    drains = []
    monkeypatch.setattr(e, "_drain_check", lambda pos, q: drains.append(q) or False)
    applied = []
    monkeypatch.setattr(e, "_apply_price", lambda *a, **k: applied.append(a))
    import app.pump.curve as cm
    monkeypatch.setattr(cm, "token_amount", lambda d: 12345)
    asyncio.run(e.on_account(MINT, "pool_quote_x", b"x", 1))
    asyncio.run(e.on_account(MINT, "pool_base_x", b"x", 1))
    assert drains == [12345] and applied == []
