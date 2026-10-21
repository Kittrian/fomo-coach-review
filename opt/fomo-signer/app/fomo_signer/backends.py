"""Key backends. `local`: the keypair from the systemd credential (memory only). `turnkey`: Turnkey signs inside its
enclave under its own policy (the key never exists on this host). Both only ever see messages that already passed
fomo_signer.policy. Nothing here logs key material, API secrets, or transaction bytes."""
from __future__ import annotations

import base64
import json
import os
import time
from typing import Optional

import httpx

TURNKEY_API = "https://api.turnkey.com"


class LocalKey:
    name = "local"

    def __init__(self, path: str):
        from solders.keypair import Keypair  # type: ignore
        with open(path, encoding="utf-8") as fh:
            arr = json.load(fh)
        if not isinstance(arr, list) or len(arr) < 64:
            raise ValueError("keypair must be a Solana CLI JSON array")
        raw = bytes(arr[:64])
        self._kp = Keypair.from_bytes(raw)
        del raw, arr
        self.pubkey = str(self._kp.pubkey())

    async def sign(self, msg_bytes: bytes, unsigned_tx_bytes: bytes) -> bytes:
        return bytes(self._kp.sign_message(msg_bytes))


def _stamp(body: str, api_public_hex: str, api_private_hex: str) -> str:
    from cryptography.hazmat.primitives import hashes  # type: ignore
    from cryptography.hazmat.primitives.asymmetric import ec  # type: ignore
    key = ec.derive_private_key(int(api_private_hex, 16), ec.SECP256R1())
    sig = key.sign(body.encode("utf-8"), ec.ECDSA(hashes.SHA256()))
    stamp = {"publicKey": api_public_hex, "signature": sig.hex(), "scheme": "SIGNATURE_SCHEME_TK_API_P256"}
    return base64.urlsafe_b64encode(json.dumps(stamp).encode()).decode().rstrip("=")


class TurnkeyKey:
    """SIGNER_BACKEND=turnkey. ACTIVITY_TYPE_SIGN_TRANSACTION_V2 / TRANSACTION_TYPE_SOLANA, so Turnkey's policy engine
    parses the real transaction (program keys, transfers, ALTs) and can refuse it independently of this host."""
    name = "turnkey"

    def __init__(self, org_id: str, api_public_hex: str, api_private_hex: str, wallet: str,
                 client: Optional[httpx.AsyncClient] = None, base: str = TURNKEY_API):
        if not (org_id and api_public_hex and api_private_hex and wallet):
            raise ValueError("turnkey backend needs TURNKEY_ORGANIZATION_ID, TURNKEY_API_PUBLIC_KEY, "
                             "TURNKEY_API_PRIVATE_KEY, SIGNER_WALLET")
        self.org, self._pub, self._priv, self.pubkey = org_id, api_public_hex, api_private_hex, wallet
        self.base = base.rstrip("/")
        self._client = client

    @classmethod
    def from_env(cls) -> "TurnkeyKey":
        return cls(os.getenv("TURNKEY_ORGANIZATION_ID", ""), os.getenv("TURNKEY_API_PUBLIC_KEY", ""),
                   os.getenv("TURNKEY_API_PRIVATE_KEY", ""), os.getenv("SIGNER_WALLET", ""),
                   base=os.getenv("TURNKEY_API_BASE", TURNKEY_API))

    def request_body(self, unsigned_tx_bytes: bytes) -> str:
        return json.dumps({"type": "ACTIVITY_TYPE_SIGN_TRANSACTION_V2", "timestampMs": str(int(time.time() * 1000)),
                           "organizationId": self.org,
                           "parameters": {"signWith": self.pubkey, "unsignedTransaction": unsigned_tx_bytes.hex(),
                                          "type": "TRANSACTION_TYPE_SOLANA"}}, separators=(",", ":"))

    async def sign(self, msg_bytes: bytes, unsigned_tx_bytes: bytes) -> bytes:
        from solders.pubkey import Pubkey  # type: ignore
        from solders.transaction import VersionedTransaction  # type: ignore
        body = self.request_body(unsigned_tx_bytes)
        headers = {"Content-Type": "application/json", "X-Stamp": _stamp(body, self._pub, self._priv)}
        own = self._client is None
        c = self._client or httpx.AsyncClient(timeout=15.0)
        try:
            r = await c.post(f"{self.base}/public/v1/submit/sign_transaction", content=body, headers=headers)
        finally:
            if own:
                await c.aclose()
        if r.status_code != 200:
            raise RuntimeError(f"turnkey http {r.status_code}")
        act = (r.json() or {}).get("activity") or {}
        if act.get("status") != "ACTIVITY_STATUS_COMPLETED":
            raise RuntimeError(f"turnkey activity {act.get('status')}")       # e.g. policy denied / needs consensus
        signed_hex = (((act.get("result") or {}).get("signTransactionResult") or {}).get("signedTransaction") or "")
        tx = VersionedTransaction.from_bytes(bytes.fromhex(signed_hex))
        keys = list(tx.message.account_keys)[:tx.message.header.num_required_signatures]
        me = Pubkey.from_string(self.pubkey)
        if bytes(tx.message) != bytes(VersionedTransaction.from_bytes(unsigned_tx_bytes).message):
            raise RuntimeError("turnkey returned a different message")
        return bytes(tx.signatures[keys.index(me)])


def from_env():
    kind = os.getenv("SIGNER_BACKEND", "local").strip().lower()
    if kind == "turnkey":
        return TurnkeyKey.from_env()
    path = os.getenv("SOLANA_KEYPAIR_PATH", "")
    if not path:
        raise RuntimeError("SOLANA_KEYPAIR_PATH not set (signer credential missing)")
    return LocalKey(path)
