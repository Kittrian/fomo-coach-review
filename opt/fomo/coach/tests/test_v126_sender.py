"""v1.26 Helius Sender on the direct pump path: tip ix added only when on, Sender first, plain-RPC fallback only when
Sender definitely refused, never a Jupiter fallback once Sender may have accepted."""
import asyncio
import base64
import time

import httpx
import pytest
from solders.transaction import VersionedTransaction

from app.live import pumpdirect as pd
from tests.test_v120_direct_pump import FakeRpc, MINT, _curve, _patch_state, keys  # noqa: F401

TIPACCT = "4ACfpUFoaSD9bfPdeu6DBt89gB6ENTeHBXCAi87NhDEE"
SYS = "11111111111111111111111111111111"


@pytest.fixture
def sender_on(monkeypatch):
    monkeypatch.setenv("LIVE_SENDER_ENABLED", "true")
    monkeypatch.setenv("LIVE_SENDER_TIP_LAMPORTS", "5000")

    async def info():
        return {"accounts": [TIPACCT], "max": 5000}
    monkeypatch.setattr(pd, "sender_tip_info", info)


def _sender(monkeypatch, exc=None):
    calls = []

    async def send(signed, cl, c):
        calls.append(signed)
        if exc:
            raise exc
    monkeypatch.setattr(pd, "_sender_send", send)
    return calls


def _tip_transfers(tx_b64):
    tx = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
    keys = [str(k) for k in tx.message.account_keys]
    out = []
    for ix in tx.message.instructions:
        if keys[ix.program_id_index] == SYS and bytes(ix.data)[:4] == b"\x02\x00\x00\x00":
            out.append((keys[ix.accounts[1]], int.from_bytes(bytes(ix.data)[4:12], "little")))
    return out


def _swap(side="buy"):
    return pd.swap(side=side, mint=MINT, amount=200_000_000, client=httpx.AsyncClient())


def test_off_by_default_no_tip_plain_rpc(monkeypatch, keys):
    monkeypatch.delenv("LIVE_SENDER_ENABLED", raising=False)
    monkeypatch.setenv("LIVE_DIRECT_CONFIRM_TIMEOUT_S", "3")
    rpc = FakeRpc(statuses=[{"confirmationStatus": "confirmed", "err": None}])
    monkeypatch.setattr(pd, "_rpc", rpc)
    _patch_state(monkeypatch, _curve())
    s = _sender(monkeypatch)
    r = asyncio.run(_swap())
    sent = [p[0] for m, p in rpc.calls if m == "sendTransaction"]
    assert r["ok"] and not s and _tip_transfers(sent[0]) == [] and r["timing"]["sender"] == "off"


def test_sender_used_with_one_capped_tip_and_priority_fee(monkeypatch, keys, sender_on):
    monkeypatch.setenv("LIVE_DIRECT_CONFIRM_TIMEOUT_S", "3")
    rpc = FakeRpc(statuses=[{"confirmationStatus": "confirmed", "err": None}])
    monkeypatch.setattr(pd, "_rpc", rpc)
    _patch_state(monkeypatch, _curve())
    s = _sender(monkeypatch)
    r = asyncio.run(_swap())
    assert r["ok"] and len(s) == 1 and r["timing"]["sender"] == "sender"
    assert _tip_transfers(s[0]) == [(TIPACCT, 5000)]
    tx = VersionedTransaction.from_bytes(base64.b64decode(s[0]))
    data = [bytes(ix.data) for ix in tx.message.instructions]
    assert any(d[:1] == b"\x03" and int.from_bytes(d[1:9], "little") > 0 for d in data)   # CU price present


def test_tip_clamped_to_signer_cap(monkeypatch, keys, sender_on):
    monkeypatch.setenv("LIVE_SENDER_TIP_LAMPORTS", "1000000")
    monkeypatch.setenv("LIVE_DIRECT_CONFIRM_TIMEOUT_S", "3")
    monkeypatch.setattr(pd, "_rpc", FakeRpc(statuses=[{"confirmationStatus": "confirmed", "err": None}]))
    _patch_state(monkeypatch, _curve())
    s = _sender(monkeypatch)
    asyncio.run(_swap())
    assert _tip_transfers(s[0]) == [(TIPACCT, 5000)]


def test_sender_definite_refusal_falls_back_to_rpc_same_tx(monkeypatch, keys, sender_on):
    monkeypatch.setenv("LIVE_DIRECT_CONFIRM_TIMEOUT_S", "3")
    rpc = FakeRpc(statuses=[{"confirmationStatus": "confirmed", "err": None}])
    monkeypatch.setattr(pd, "_rpc", rpc)
    _patch_state(monkeypatch, _curve())
    s = _sender(monkeypatch, pd.SenderError("sender http 400", definite=True))
    r = asyncio.run(_swap())
    sent = [p[0] for m, p in rpc.calls if m == "sendTransaction"]
    assert r["ok"] and r["timing"]["sender"] == "rpc_fallback" and sent and sent[0] == s[0]


def test_sender_maybe_accepted_then_rpc_refused_is_never_jupiter_fallback(monkeypatch, keys, sender_on):
    rpc = FakeRpc(send_exc=pd.RpcRefused("x"), statuses=[])          # status unknown
    monkeypatch.setattr(pd, "_rpc", rpc)
    _patch_state(monkeypatch, _curve())
    _sender(monkeypatch, pd.SenderError("sender ReadTimeout", definite=False))
    r = asyncio.run(_swap())
    assert not r.get("fallback") and r["ambiguous"] is True


def test_sender_definite_and_rpc_refused_unknown_allows_fallback(monkeypatch, keys, sender_on):
    rpc = FakeRpc(send_exc=pd.RpcRefused("x"), statuses=[])
    monkeypatch.setattr(pd, "_rpc", rpc)
    _patch_state(monkeypatch, _curve())
    _sender(monkeypatch, pd.SenderError("sender http 400", definite=True))
    r = asyncio.run(_swap())
    assert r.get("fallback") is True and r["stage"] == "send_refused"


def test_sell_also_uses_sender(monkeypatch, keys, sender_on):
    monkeypatch.setenv("LIVE_DIRECT_CONFIRM_TIMEOUT_S", "3")
    monkeypatch.setattr(pd, "_rpc", FakeRpc(statuses=[{"confirmationStatus": "confirmed", "err": None}]))
    _patch_state(monkeypatch, _curve(), user_tokens=10**12)
    s = _sender(monkeypatch)
    r = asyncio.run(pd.swap(side="sell", mint=MINT, amount=10**12, client=httpx.AsyncClient()))
    assert len(s) == 1 and _tip_transfers(s[0]) == [(TIPACCT, 5000)] and r["timing"]["sender"] == "sender"


def test_no_signer_tip_info_means_no_tip(monkeypatch, keys):
    monkeypatch.setenv("LIVE_SENDER_ENABLED", "true")
    monkeypatch.setenv("SIGNER_BACKEND", "local")
    monkeypatch.setenv("LIVE_DIRECT_CONFIRM_TIMEOUT_S", "3")
    rpc = FakeRpc(statuses=[{"confirmationStatus": "confirmed", "err": None}])
    monkeypatch.setattr(pd, "_rpc", rpc)
    _patch_state(monkeypatch, _curve())
    s = _sender(monkeypatch)
    r = asyncio.run(_swap())
    assert not s and r["timing"]["sender"] == "off"


def test_sender_url_key_appended_never_logged(monkeypatch):
    monkeypatch.setenv("LIVE_DIRECT_RPC_URL", "https://mainnet.helius-rpc.com/?api-key=SECRETKEY")
    c = pd.cfg()
    assert pd._sender_url(c).endswith("swqos_only=true&api-key=SECRETKEY") and "ewr-sender" in pd._sender_url(c)
    assert "SECRETKEY" not in c["sender_url"]          # key only joined in memory at send time


def test_v128_shadow_records_sender_would_have(monkeypatch):
    import asyncio
    from app.live import pumpdirect as pd
    monkeypatch.setenv("LIVE_SENDER_ENABLED", "true")
    monkeypatch.setenv("LIVE_SENDER_URL", "http://ewr-sender.helius-rpc.com/fast?swqos_only=true&api-key=SECRETKEY")

    async def fake_prepare(*a, **k):
        return {"ok": False, "why": "test stop", "tip_lamports": 5000, "tip_account": "4ACfpUFoaSD9bfPdeu6DBt89gB6ENTeHBXCAi87NhDEE"}
    monkeypatch.setattr(pd, "prepare", fake_prepare)
    rec = asyncio.run(pd.shadow("buy", "M", 1000, client=object()))
    assert rec["sender"] == {"would_use": True, "tip_lamports": 5000, "tip_account": "4ACfpU",
                             "host": "ewr-sender.helius-rpc.com", "fallback": "rpc"}
    assert "SECRETKEY" not in str(rec)


def test_parallel_rpc_copy_prevents_jupiter_fallback_after_sender_refusal(monkeypatch, keys, sender_on):
    """A Sender 4xx is not proof the parallel RPC copy was refused."""
    monkeypatch.setenv("LIVE_PARALLEL_SEND", "true")
    monkeypatch.setenv("LIVE_SENDER_ALSO_RPC", "true")
    monkeypatch.setenv("LIVE_DIRECT_CONFIRM_TIMEOUT_S", "0.06")
    monkeypatch.setenv("LIVE_DIRECT_CONFIRM_POLL_S", "0.01")
    monkeypatch.setenv("LIVE_DIRECT_REBROADCAST_S", "10")
    rpc = FakeRpc(send_exc=pd.RpcRefused("rpc refused"), statuses=[])
    monkeypatch.setattr(pd, "_rpc", rpc)
    _patch_state(monkeypatch, _curve())
    _sender(monkeypatch, pd.SenderError("sender http 400", definite=True))

    r = asyncio.run(_swap())

    # The same signed transaction was attempted by the concurrent RPC path. Even if Sender says 4xx,
    # we cannot safely construct a different Jupiter buy when its landing status is unknown.
    assert not r.get("fallback")
    assert r.get("ambiguous") is True


def test_confirmation_one_shot_event_does_not_busy_poll(monkeypatch, keys):
    """A single WS notification must not turn confirmation-status lag into a tight polling loop."""
    calls = {"n": 0}

    async def unknown_status(sig, cl):
        calls["n"] += 1
        return None

    monkeypatch.setattr(pd, "_status", unknown_status)
    ev = asyncio.Event()
    ev.set()
    tm = {}
    c = {"confirm_poll_s": 0.01, "rebroadcast_s": 10}
    t0 = time.perf_counter()
    result = asyncio.run(pd._confirm_loop("SIG", "SIGNED", None, c, tm, "buy", MINT, {}, False,
                                          t0, time.time() + 0.07, time.time(), ev))
    assert result["ambiguous"] is True
    assert tm.get("ws_woke") is True
    assert 2 <= calls["n"] <= 15, f"unexpected status poll count: {calls['n']}"
