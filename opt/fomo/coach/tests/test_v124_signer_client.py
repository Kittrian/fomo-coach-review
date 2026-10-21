"""v1.24 policy-signer client: default path unchanged; socket path never touches a key and fails closed."""
import asyncio
import base64
import json
import os
import tempfile

import pytest
from solders.hash import Hash
from solders.keypair import Keypair
from solders.message import MessageV0, to_bytes_versioned
from solders.signature import Signature
from solders.system_program import TransferParams, transfer
from solders.transaction import VersionedTransaction

from app.live import jupiter, signer_client


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("SIGNER_BACKEND", "SIGNER_SOCKET", "SIGNER_SHADOW_ENABLED"):
        monkeypatch.delenv(k, raising=False)
    signer_client._cache.clear()
    yield
    signer_client._cache.clear()


def _unsigned(kp):
    ix = transfer(TransferParams(from_pubkey=kp.pubkey(), to_pubkey=Keypair().pubkey(), lamports=1))
    m = MessageV0.try_compile(kp.pubkey(), [ix], [], Hash.default())
    return base64.b64encode(bytes(VersionedTransaction.populate(m, [Signature.default()]))).decode(), m


def test_default_backend_is_local():
    assert signer_client.backend() == "local" and not signer_client.enabled()


def test_socket_backend_never_loads_a_key(monkeypatch):
    monkeypatch.setenv("SIGNER_BACKEND", "socket")
    monkeypatch.setenv("SOLANA_WALLET", str(Keypair().pubkey()))
    with pytest.raises(RuntimeError):
        jupiter.keypair()
    assert jupiter.pubkey() == os.environ["SOLANA_WALLET"]


def _fake_signer(kp, reply):
    d = tempfile.mkdtemp(dir="/tmp")
    path = os.path.join(d, "s.sock")
    seen = []

    async def handle(r, w):
        req = json.loads(await r.readline())
        seen.append(req)
        res = reply(req)
        w.write((json.dumps(res) + "\n").encode())
        await w.drain()
        w.close()
    return path, seen, handle


def test_sign_via_socket_roundtrip_and_refusal(monkeypatch):
    kp = Keypair()
    monkeypatch.setenv("SIGNER_BACKEND", "socket")
    monkeypatch.setenv("SOLANA_WALLET", str(kp.pubkey()))
    tx_b64, m = _unsigned(kp)

    def reply(req):
        if req["intent"].get("amount") == 666:
            return {"ok": False, "signed": False, "reasons": ["SOL transfer to non-allowed destination"]}
        msg = base64.b64decode(req["msg_b64"])
        return {"ok": True, "signed": True, "signature_b64": base64.b64encode(bytes(kp.sign_message(msg))).decode()}

    path, seen, handle = _fake_signer(kp, reply)
    monkeypatch.setenv("SIGNER_SOCKET", path)

    async def go():
        srv = await asyncio.start_unix_server(handle, path=path)
        async with srv:
            signed = await jupiter.sign_tx(tx_b64, {"kind": "buy", "amount": 1})
            tx = VersionedTransaction.from_bytes(base64.b64decode(signed))
            assert tx.signatures[0].verify(kp.pubkey(), to_bytes_versioned(m))
            with pytest.raises(signer_client.SignerRefused, match="non-allowed"):
                await jupiter.sign_tx(tx_b64, {"kind": "buy", "amount": 666})
    asyncio.run(go())
    assert seen[0]["intent"]["kind"] == "buy" and "msg_b64" in seen[0]


def test_unreachable_signer_fails_closed(monkeypatch):
    kp = Keypair()
    monkeypatch.setenv("SIGNER_BACKEND", "socket")
    monkeypatch.setenv("SOLANA_WALLET", str(kp.pubkey()))
    monkeypatch.setenv("SIGNER_SOCKET", "/tmp/definitely-not-a-signer.sock")
    tx_b64, _ = _unsigned(kp)
    with pytest.raises(signer_client.SignerRefused, match="unreachable"):
        asyncio.run(jupiter.sign_tx(tx_b64, {"kind": "buy", "amount": 1}))


def test_jupiter_swap_sign_stage_on_refusal(monkeypatch):
    kp = Keypair()
    monkeypatch.setenv("SIGNER_BACKEND", "socket")
    monkeypatch.setenv("SOLANA_WALLET", str(kp.pubkey()))
    monkeypatch.setenv("SIGNER_SOCKET", "/tmp/definitely-not-a-signer.sock")
    tx_b64, _ = _unsigned(kp)

    async def fake_order(**kw):
        return {"ok": True, "transaction": tx_b64, "request_id": "r", "router": "metis"}
    monkeypatch.setattr(jupiter, "order", fake_order)
    called = []

    async def fake_execute(*a, **k):
        called.append(1)
        return {"ok": True}
    monkeypatch.setattr(jupiter, "execute", fake_execute)
    r = asyncio.run(jupiter.swap(input_mint=jupiter.WSOL, output_mint=str(Keypair().pubkey()), amount=1,
                                 slippage_bps=100, max_priority_lamports=1, simulate=False))
    assert r["stage"] == "sign" and not r["ok"] and not called


def test_manual_sign_endpoint_disabled_with_signer(monkeypatch):
    from app import jupiter_exec
    monkeypatch.setenv("SIGNER_BACKEND", "socket")
    assert jupiter_exec.sign_transaction_base64("AA==")["error"] == "signer_policy"


def test_shadow_off_by_default(monkeypatch):
    from app.live import signer_shadow
    monkeypatch.setenv("SIGNER_BACKEND", "socket")
    assert not signer_shadow.enabled()
    signer_shadow.on_refused("scout_solo", "x")        # no loop, disabled → no-op, no raise
    s = signer_shadow.summary()
    assert s["enabled"] is False and s["signed_any"] is False
