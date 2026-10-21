"""Turnkey backend: request shape, API-key stamp verifies, signature extracted, tampered responses refused."""
import asyncio
import base64
import json

import httpx
import pytest

pytest.importorskip("cryptography")
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from solders.hash import Hash  # noqa: E402
from solders.keypair import Keypair  # noqa: E402
from solders.message import MessageV0, to_bytes_versioned  # noqa: E402
from solders.signature import Signature  # noqa: E402
from solders.system_program import TransferParams, transfer  # noqa: E402
from solders.transaction import VersionedTransaction  # noqa: E402

from fomo_signer.backends import TurnkeyKey  # noqa: E402

KP = Keypair()
API = ec.generate_private_key(ec.SECP256R1())
PRIV_HEX = format(API.private_numbers().private_value, "064x")
PUB_HEX = API.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.CompressedPoint).hex()


def unsigned():
    ix = transfer(TransferParams(from_pubkey=KP.pubkey(), to_pubkey=Keypair().pubkey(), lamports=5))
    m = MessageV0.try_compile(KP.pubkey(), [ix], [], Hash.default())
    return m, bytes(VersionedTransaction.populate(m, [Signature.default()]))


def client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_sign_transaction_roundtrip_and_stamp():
    m, raw = unsigned()
    seen = {}

    def handler(req):
        body = req.content.decode()
        seen["body"] = json.loads(body)
        stamp = json.loads(base64.urlsafe_b64decode(req.headers["X-Stamp"] + "=="))
        API.public_key().verify(bytes.fromhex(stamp["signature"]), body.encode(), ec.ECDSA(hashes.SHA256()))
        seen["stamp"] = stamp
        signed = VersionedTransaction.populate(m, [KP.sign_message(to_bytes_versioned(m))])
        return httpx.Response(200, json={"activity": {"status": "ACTIVITY_STATUS_COMPLETED", "result": {
            "signTransactionResult": {"signedTransaction": bytes(signed).hex()}}}})

    tk = TurnkeyKey("org-1", PUB_HEX, PRIV_HEX, str(KP.pubkey()), client=client(handler))
    sig = asyncio.run(tk.sign(to_bytes_versioned(m), raw))
    assert Signature.from_bytes(sig).verify(KP.pubkey(), to_bytes_versioned(m))
    b = seen["body"]
    assert b["type"] == "ACTIVITY_TYPE_SIGN_TRANSACTION_V2" and b["organizationId"] == "org-1"
    assert b["parameters"]["type"] == "TRANSACTION_TYPE_SOLANA" and b["parameters"]["signWith"] == str(KP.pubkey())
    assert bytes.fromhex(b["parameters"]["unsignedTransaction"]) == raw
    assert seen["stamp"]["scheme"] == "SIGNATURE_SCHEME_TK_API_P256" and seen["stamp"]["publicKey"] == PUB_HEX


def test_policy_denied_raises():
    m, raw = unsigned()
    h = lambda req: httpx.Response(200, json={"activity": {"status": "ACTIVITY_STATUS_FAILED"}})  # noqa: E731
    with pytest.raises(RuntimeError, match="FAILED"):
        asyncio.run(TurnkeyKey("o", PUB_HEX, PRIV_HEX, str(KP.pubkey()), client=client(h)).sign(to_bytes_versioned(m), raw))


def test_swapped_message_refused():
    m, raw = unsigned()
    other_ix = transfer(TransferParams(from_pubkey=KP.pubkey(), to_pubkey=Keypair().pubkey(), lamports=10**9))
    evil = MessageV0.try_compile(KP.pubkey(), [other_ix], [], Hash.default())

    def handler(req):
        signed = VersionedTransaction.populate(evil, [KP.sign_message(to_bytes_versioned(evil))])
        return httpx.Response(200, json={"activity": {"status": "ACTIVITY_STATUS_COMPLETED", "result": {
            "signTransactionResult": {"signedTransaction": bytes(signed).hex()}}}})
    with pytest.raises(RuntimeError, match="different message"):
        asyncio.run(TurnkeyKey("o", PUB_HEX, PRIV_HEX, str(KP.pubkey()), client=client(handler)).sign(to_bytes_versioned(m), raw))


def test_missing_config_refused():
    with pytest.raises(ValueError):
        TurnkeyKey("", "", "", "")
