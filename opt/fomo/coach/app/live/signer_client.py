"""v1.24 policy-signer client (VPS layer 1). SIGNER_BACKEND=local (default) → unchanged: the coach loads the keypair
itself. SIGNER_BACKEND=socket → the coach never holds key material: every signature is requested from fomo-signer over
its unix socket together with a stated intent (buy/sell/tip/sweep, mints, amount, priority cap); the signer
re-derives everything from the message, simulates, and refuses anything that doesn't match. Fail-closed: any error →
SignerRefused (the callers treat that like a failed sign: nothing is sent)."""
from __future__ import annotations

import asyncio
import base64
import json
import os
from typing import Any, Dict, Optional

_cache: Dict[str, Any] = {}


class SignerRefused(RuntimeError):
    pass


def backend() -> str:
    return os.getenv("SIGNER_BACKEND", "local").strip().lower()


def enabled() -> bool:
    return backend() in ("socket", "signer")


def socket_path() -> str:
    return os.getenv("SIGNER_SOCKET", "/run/fomo-signer/signer.sock")


def wallet() -> Optional[str]:
    """Public wallet address (no key). SOLANA_WALLET; cross-checked against the signer once it answers."""
    w = os.getenv("SOLANA_WALLET", "").strip() or None
    if _cache.get("mismatch"):
        return None
    return w


async def call(req: Dict[str, Any], timeout: float = 12.0) -> Dict[str, Any]:
    r, w = await asyncio.wait_for(asyncio.open_unix_connection(socket_path(), limit=1 << 20), timeout=3.0)
    try:
        w.write((json.dumps(req, separators=(",", ":")) + "\n").encode())
        await w.drain()
        line = await asyncio.wait_for(r.readline(), timeout=timeout)
    finally:
        w.close()
    if not line:
        raise SignerRefused("signer closed the connection")
    return json.loads(line)


async def verify_wallet() -> Dict[str, Any]:
    try:
        res = await call({"op": "pubkey"}, timeout=3.0)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": type(exc).__name__}
    ok = bool(res.get("pubkey")) and res.get("pubkey") == os.getenv("SOLANA_WALLET", "").strip()
    _cache["mismatch"] = not ok
    return {"ok": ok}


async def health() -> Dict[str, Any]:
    if not enabled():
        return {"backend": backend()}
    try:
        h = await call({"op": "health"}, timeout=3.0)
        return {"backend": backend(), "key_backend": h.get("backend"),
                **{k: h.get(k) for k in ("ok", "mode", "halted", "stats", "external_fee_payer_allowed")}}
    except Exception as exc:  # noqa: BLE001
        return {"backend": backend(), "ok": False, "error": type(exc).__name__}


async def request(msg_bytes: bytes, intent: Dict[str, Any], *, dry_run: bool = False) -> Dict[str, Any]:
    return await call({"op": "sign", "msg_b64": base64.b64encode(msg_bytes).decode(), "intent": intent,
                       "dry_run": bool(dry_run)})


async def sign_message(msg_bytes: bytes, intent: Dict[str, Any]) -> bytes:
    try:
        res = await request(msg_bytes, intent)
    except SignerRefused:
        raise
    except Exception as exc:  # noqa: BLE001
        raise SignerRefused(f"signer unreachable ({type(exc).__name__})") from None
    if not (res.get("ok") and res.get("signed") and res.get("signature_b64")):
        raise SignerRefused("; ".join(res.get("reasons") or ["not signed"])[:200])
    return base64.b64decode(res["signature_b64"])


async def sign_tx_b64(tx_b64: str, intent: Dict[str, Any]) -> str:
    """Fill our signer slot of a (partially) unsigned versioned tx via fomo-signer. Same contract as
    jupiter.sign_partial."""
    from solders.message import to_bytes_versioned  # type: ignore
    from solders.pubkey import Pubkey  # type: ignore
    from solders.signature import Signature  # type: ignore
    from solders.transaction import VersionedTransaction  # type: ignore
    me_s = wallet()
    if not me_s:
        raise SignerRefused("SOLANA_WALLET not set or signer wallet mismatch")
    tx = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
    msg = tx.message
    keys = list(msg.account_keys)[:msg.header.num_required_signatures]
    me = Pubkey.from_string(me_s)
    if me not in keys:
        raise ValueError("order transaction does not require our signature (taker mismatch)")
    sig = await sign_message(to_bytes_versioned(msg), intent)
    sigs = list(tx.signatures)
    sigs[keys.index(me)] = Signature.from_bytes(sig)
    return base64.b64encode(bytes(VersionedTransaction.populate(msg, sigs))).decode("ascii")
