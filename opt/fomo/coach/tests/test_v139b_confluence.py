"""v1.39b: confluence (3+ tracked scout/confirmer wallets) skips the mesh gate; instant mint checks stay."""
from __future__ import annotations

import asyncio
import time

from tests.test_v119 import live_env, MINT  # noqa: F401
from app.live import engine as eng_mod
from app.live.engine import LiveEngine, instant_mint_verdict


def _acct(owner="TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb", mint_auth=None, freeze=None, ext=None):
    return {"owner": owner, "data": {"parsed": {"info": {"mintAuthority": mint_auth, "freezeAuthority": freeze,
                                                         "extensions": ext or []}}}}


def test_instant_verdict():
    assert instant_mint_verdict(_acct(ext=[{"extension": "metadataPointer", "state": {}},
                                           {"extension": "tokenMetadata", "state": {}}]))[0] is True
    assert instant_mint_verdict(_acct(freeze="F" * 43))[0] is False
    assert instant_mint_verdict(_acct(mint_auth="M" * 43))[0] is False
    assert instant_mint_verdict(_acct(mint_auth="TSLvdd1pWpHVjahSpsvCXUbgwsL3JAcvokwaKt1eokM"))[0] is True
    assert instant_mint_verdict(_acct(ext=[{"extension": "transferHook", "state": {"programId": "H" * 43}}]))[0] is False
    assert instant_mint_verdict(_acct(ext=[{"extension": "transferHook", "state": {"programId": None}}]))[0] is True
    assert instant_mint_verdict(_acct(ext=[{"extension": "permanentDelegate", "state": {"delegate": "D" * 43}}]))[0] is False
    fee = {"extension": "transferFeeConfig", "state": {"olderTransferFee": {"transferFeeBasisPoints": 0},
                                                       "newerTransferFee": {"transferFeeBasisPoints": 300}}}
    assert instant_mint_verdict(_acct(ext=[fee]))[0] is False
    assert instant_mint_verdict(None)[0] is None


def test_confluence_count(live_env, monkeypatch):
    now = int(time.time() * 1000)
    e = LiveEngine()
    monkeypatch.setenv("LIVE_CONFLUENCE_SKIP_GATE_MIN", "3")
    e.note_tracked_buy("A" * 44, MINT, now - 1000)
    e.note_tracked_buy("B" * 44, MINT, now - 900)
    e.note_tracked_buy("A" * 44, MINT, now - 700)
    e.note_tracked_buy("Z" * 44, MINT, now - 40 * 60_000)          # outside the 30 min window
    assert e._confluence_wallets(MINT) == []
    e.note_tracked_buy("D" * 44, MINT, now - 100)
    assert len(e._confluence_wallets(MINT)) == 3
    monkeypatch.setenv("LIVE_CONFLUENCE_SKIP_GATE_MIN", "0")
    assert e._confluence_wallets(MINT) == []


def test_confluence_instant_paths(live_env, monkeypatch):
    e = LiveEngine()
    logged = []
    monkeypatch.setattr(e, "_ledger", lambda kind, **kw: logged.append((kind, kw)))

    async def mesh(mint):
        return False, "mesh hard block: x"
    monkeypatch.setattr(e, "_mesh_check", mesh)

    def run(val):
        async def fake_call(method, params, **kw):
            if isinstance(val, Exception):
                raise val
            return {"value": val}
        monkeypatch.setattr(eng_mod.rpc, "call", fake_call)
        return asyncio.run(e._confluence_instant(MINT, "scout_solo", ["a", "b", "c"], {}))
    ok, why = run(_acct())
    assert ok and "mesh skipped" in why and logged[-1][0] == "gate_skipped_confluence"
    ok, why = run(_acct(freeze="F" * 43))
    assert not ok and "freeze" in why
    ok, why = run(RuntimeError("down"))                              # unknown → full mesh gate
    assert not ok and why == "mesh hard block: x"
