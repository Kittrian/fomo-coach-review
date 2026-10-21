"""Jupiter Swap Order & Execute — free/keyless, confirm-to-execute only.

Docs: https://developers.jup.ag/docs/swap/order-and-execute
Base: https://api.jup.ag/swap/v2

Safety:
  - JUPITER_EXEC_ENABLED defaults false → quote/swap endpoints dry-run only
  - Never auto-trade; /execute requires ENABLE + explicit confirm + signed tx
  - Prefer returning unsigned tx for Phantom/local sign when keypath missing
  - Never log key material or keypair path contents
"""
from __future__ import annotations

import base64
import json
import os
import logging
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

from app.config import settings

logger = logging.getLogger("coach.jupiter_exec")

JUPITER_SWAP_V2_BASE = "https://api.jup.ag/swap/v2"
SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

# Default demo quote size: 0.01 SOL (lamports) for buys; caller should override sells
DEFAULT_BUY_AMOUNT_LAMPORTS = "10000000"


def jupiter_status_summary() -> Dict[str, Any]:
    """Health chip — never exposes secrets."""
    keypair_ok = settings.solana_keypair_configured
    wallet_ok = settings.solana_wallet_configured
    enabled = settings.jupiter_exec_enabled
    auth = "portal_key" if settings.jupiter_api_key_configured else "keyless"
    if enabled and keypair_ok:
        mode = "confirm_to_execute"
        note = "ENABLED — /api/exec/swap may land only with confirm=true + signed tx"
    elif enabled:
        mode = "confirm_unsigned"
        note = (
            "ENABLED but no local keypair — returns unsigned tx for Phantom/local sign; "
            "execute only when client posts signed_transaction + confirm"
        )
    else:
        mode = "recommend_only_dry_run"
        note = "JUPITER_EXEC_ENABLED=false (default) — quote/swap dry-run; no /execute"
    return {
        "status": "enabled" if enabled else "disabled",
        "mode": mode,
        "auth": auth,
        "api_base": JUPITER_SWAP_V2_BASE,
        "keypair_configured": keypair_ok,
        "watch_wallet_configured": wallet_ok,
        "watch_wallet": (
            settings.solana_wallet[:4] + "…" + settings.solana_wallet[-4:]
            if wallet_ok else None
        ),
        "rpc_configured": bool(settings.solana_rpc_url),
        "recommend_only": True,
        "auto_trade": False,
        "endpoints": {
            "quote": "/api/exec/quote",
            "swap": "/api/exec/swap",
        },
        "note": note,
    }


def _headers() -> Dict[str, str]:
    h = {"Accept": "application/json"}
    if settings.jupiter_api_key:
        h["x-api-key"] = settings.jupiter_api_key
    return h


def resolve_mints(
    *,
    mint: Optional[str] = None,
    side: Optional[str] = None,
    input_mint: Optional[str] = None,
    output_mint: Optional[str] = None,
    quote_mint: str = SOL_MINT,
) -> tuple[str, str]:
    """Resolve input/output mints from explicit pair or mint+side helper."""
    if input_mint and output_mint:
        return input_mint.strip(), output_mint.strip()
    if not mint or not side:
        raise ValueError("Provide input_mint+output_mint, or mint+side (buy|sell)")
    m = mint.strip()
    q = (quote_mint or SOL_MINT).strip()
    s = side.strip().lower()
    if s == "buy":
        return q, m  # spend quote (SOL/USDC) → get token
    if s == "sell":
        return m, q  # sell token → quote
    raise ValueError("side must be 'buy' or 'sell'")


def fetch_order(
    *,
    input_mint: str,
    output_mint: str,
    amount: str,
    taker: Optional[str] = None,
    client: Optional[httpx.Client] = None,
    timeout: float = 30.0,
) -> Dict[str, Any]:
    """GET /order — quote (+ assembled unsigned tx when taker set)."""
    params: Dict[str, str] = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": str(amount),
    }
    if taker:
        params["taker"] = taker

    owns = client is None
    http = client or httpx.Client(timeout=timeout)
    try:
        resp = http.get(
            f"{JUPITER_SWAP_V2_BASE}/order",
            params=params,
            headers=_headers(),
        )
        text = resp.text
        if resp.status_code >= 400:
            logger.warning("jupiter /order HTTP %s", resp.status_code)
            return {
                "ok": False,
                "error": "order_http_error",
                "http_status": resp.status_code,
                "detail": text[:500],
            }
        try:
            data = resp.json()
        except Exception:
            return {
                "ok": False,
                "error": "order_invalid_json",
                "http_status": resp.status_code,
                "detail": text[:500],
            }
        if not isinstance(data, dict):
            return {"ok": False, "error": "order_unexpected_shape", "raw_type": type(data).__name__}
        return {"ok": True, "order": data}
    finally:
        if owns:
            http.close()


def execute_signed(
    *,
    signed_transaction: str,
    request_id: str,
    last_valid_block_height: Optional[int] = None,
    client: Optional[httpx.Client] = None,
    timeout: float = 60.0,
) -> Dict[str, Any]:
    """POST /execute — only call when ENABLE + confirm + signed."""
    body: Dict[str, Any] = {
        "signedTransaction": signed_transaction,
        "requestId": request_id,
    }
    if last_valid_block_height is not None:
        body["lastValidBlockHeight"] = last_valid_block_height

    headers = {**_headers(), "Content-Type": "application/json"}
    owns = client is None
    http = client or httpx.Client(timeout=timeout)
    try:
        resp = http.post(
            f"{JUPITER_SWAP_V2_BASE}/execute",
            headers=headers,
            json=body,
        )
        text = resp.text
        if resp.status_code >= 400:
            logger.warning("jupiter /execute HTTP %s", resp.status_code)
            return {
                "ok": False,
                "error": "execute_http_error",
                "http_status": resp.status_code,
                "detail": text[:500],
            }
        try:
            data = resp.json()
        except Exception:
            return {
                "ok": False,
                "error": "execute_invalid_json",
                "http_status": resp.status_code,
                "detail": text[:500],
            }
        return {"ok": True, "result": data if isinstance(data, dict) else {"raw": data}}
    finally:
        if owns:
            http.close()


def _load_keypair_bytes(path: str) -> bytes:
    """Load Solana CLI JSON keypair bytes. Never log contents."""
    p = Path(path).expanduser()
    raw = p.read_text(encoding="utf-8")
    arr = json.loads(raw)
    if not isinstance(arr, list) or len(arr) < 64:
        raise ValueError("keypair file must be a Solana CLI JSON array (≥64 ints)")
    return bytes(arr[:64])


def sign_transaction_base64(unsigned_b64: str) -> Dict[str, Any]:
    """Sign Jupiter versioned tx with local keypair if configured.

    Requires optional `solders`. Returns unsigned path note when unavailable.
    Never logs secret material.
    """
    if os.getenv("SIGNER_BACKEND", "local").strip().lower() in ("socket", "signer"):
        return {"ok": False, "error": "signer_policy", "note": "manual signing is disabled: keys live in fomo-signer"}
    if not settings.solana_keypair_configured:
        return {
            "ok": False,
            "error": "keypair_missing",
            "note": "Set SOLANA_KEYPAIR_PATH or post signed_transaction from Phantom",
        }
    try:
        from solders.keypair import Keypair  # type: ignore
        from solders.transaction import VersionedTransaction  # type: ignore
    except ImportError:
        return {
            "ok": False,
            "error": "solders_missing",
            "note": "pip install solders for local keypath signing; else use Phantom-signed tx",
        }

    try:
        secret = _load_keypair_bytes(settings.solana_keypair_path)
        kp = Keypair.from_bytes(secret)
        # scrub
        del secret
        raw = base64.b64decode(unsigned_b64)
        tx = VersionedTransaction.from_bytes(raw)
        # Partial sign: JupiterZ may need MM signature at /execute
        signed = VersionedTransaction(tx.message, [kp])
        signed_b64 = base64.b64encode(bytes(signed)).decode("ascii")
        pubkey = str(kp.pubkey())
        return {"ok": True, "signed_transaction": signed_b64, "signer": pubkey}
    except Exception as exc:  # noqa: BLE001 — surface safe message only
        logger.warning("local sign failed: %s", type(exc).__name__)
        return {
            "ok": False,
            "error": "sign_failed",
            "detail": type(exc).__name__,
            "note": "Could not sign; return unsigned for wallet",
        }


def watch_wallet_pubkey() -> Optional[str]:
    """Public watch/quote wallet from env (SOLANA_WALLET / JUPITER_USER_PUBLIC_KEY). No private key."""
    if settings.solana_wallet_configured:
        return settings.solana_wallet
    return None


def resolve_taker(explicit: Optional[str] = None) -> Optional[str]:
    """Prefer request taker → env watch wallet → keypair-derived pubkey."""
    if explicit and str(explicit).strip():
        return str(explicit).strip()
    return watch_wallet_pubkey() or public_key_from_keypair()


def public_key_from_keypair() -> Optional[str]:
    """Optional taker pubkey from local keyfile (no secret logged)."""
    if not settings.solana_keypair_configured:
        return None
    try:
        from solders.keypair import Keypair  # type: ignore
    except ImportError:
        return None
    try:
        secret = _load_keypair_bytes(settings.solana_keypair_path)
        kp = Keypair.from_bytes(secret)
        del secret
        return str(kp.pubkey())
    except Exception:
        logger.warning("could not derive pubkey from keypair path")
        return None


def build_quote_response(
    order_wrap: Dict[str, Any],
    *,
    input_mint: str,
    output_mint: str,
    amount: str,
    taker: Optional[str],
    side: Optional[str] = None,
    mint: Optional[str] = None,
) -> Dict[str, Any]:
    """Normalize /order into coach API shape (always dry / unsigned)."""
    base = {
        "ok": order_wrap.get("ok", False),
        "dry_run": True,
        "recommend_only": True,
        "auto_trade": False,
        "jupiter_exec_enabled": settings.jupiter_exec_enabled,
        "input_mint": input_mint,
        "output_mint": output_mint,
        "amount": str(amount),
        "taker": taker,
        "side": side,
        "mint": mint,
        "auth": "portal_key" if settings.jupiter_api_key_configured else "keyless",
    }
    if not order_wrap.get("ok"):
        base.update({k: v for k, v in order_wrap.items() if k != "ok"})
        return base

    order = order_wrap.get("order") or {}
    tx = order.get("transaction")
    has_tx = isinstance(tx, str) and len(tx) > 0
    base.update(
        {
            "request_id": order.get("requestId"),
            "out_amount": order.get("outAmount"),
            "in_amount": order.get("inAmount"),
            "router": order.get("router"),
            "mode": order.get("mode"),
            "fee_bps": order.get("feeBps"),
            "fee_mint": order.get("feeMint"),
            "platform_fee": order.get("platformFee"),
            "error_code": order.get("errorCode"),
            "error_message": order.get("errorMessage"),
            "last_valid_block_height": order.get("lastValidBlockHeight"),
            "expire_at": order.get("expireAt"),
            "unsigned_transaction": tx if has_tx else None,
            "transaction_ready": has_tx,
            "note": (
                "Quote only — sign externally (Phantom) or enable JUPITER_EXEC_ENABLED "
                "and POST /api/exec/swap with confirm=true"
                if has_tx
                else (
                    "Quote pricing only (no assembled tx — set taker for unsigned tx)"
                    if tx is None
                    else "Router quoted but could not build tx — see error_code"
                )
            ),
            "raw_order_keys": sorted(order.keys()),
        }
    )
    return base


def run_swap_flow(
    *,
    input_mint: str,
    output_mint: str,
    amount: str,
    taker: Optional[str] = None,
    confirm: bool = False,
    signed_transaction: Optional[str] = None,
    request_id: Optional[str] = None,
    client: Optional[httpx.Client] = None,
) -> Dict[str, Any]:
    """Quote + optional execute. Dry-run unless enabled + confirm + signed."""
    enabled = settings.jupiter_exec_enabled

    # Resolve taker from local key if not provided
    effective_taker = resolve_taker(taker)

    # If client already has signed tx + request_id, skip re-order when executing
    order_data: Optional[Dict[str, Any]] = None
    unsigned: Optional[str] = None
    rid = request_id

    if not (signed_transaction and request_id and confirm and enabled):
        order_wrap = fetch_order(
            input_mint=input_mint,
            output_mint=output_mint,
            amount=amount,
            taker=effective_taker,
            client=client,
        )
        quote = build_quote_response(
            order_wrap,
            input_mint=input_mint,
            output_mint=output_mint,
            amount=amount,
            taker=effective_taker,
        )
        if not order_wrap.get("ok"):
            return {
                **quote,
                "executed": False,
                "dry_run": True,
                "swap_status": "order_failed",
            }
        order_data = order_wrap["order"]
        unsigned = order_data.get("transaction") if isinstance(order_data.get("transaction"), str) else None
        if unsigned == "":
            unsigned = None
        rid = order_data.get("requestId")
    else:
        quote = {
            "ok": True,
            "dry_run": False,
            "recommend_only": True,
            "auto_trade": False,
            "jupiter_exec_enabled": True,
            "input_mint": input_mint,
            "output_mint": output_mint,
            "amount": str(amount),
            "taker": effective_taker,
            "request_id": request_id,
            "note": "Using client-provided signed_transaction",
        }

    # Gate: never execute without ENABLE
    if not enabled:
        return {
            **quote,
            "executed": False,
            "dry_run": True,
            "swap_status": "dry_run_disabled",
            "unsigned_transaction": unsigned or quote.get("unsigned_transaction"),
            "request_id": rid or quote.get("request_id"),
            "note": (
                "Dry-run: JUPITER_EXEC_ENABLED=false. Returning unsigned tx for "
                "Phantom/local sign. Set ENABLE + confirm=true to land via /execute."
            ),
        }

    if not confirm:
        return {
            **quote,
            "executed": False,
            "dry_run": True,
            "swap_status": "awaiting_confirm",
            "unsigned_transaction": unsigned or quote.get("unsigned_transaction"),
            "request_id": rid or quote.get("request_id"),
            "note": "Jupiter exec enabled but confirm=false — no /execute. Re-POST with confirm=true.",
        }

    # Need a signed transaction
    signed = signed_transaction
    signer_note = None
    if not signed:
        u = unsigned or quote.get("unsigned_transaction")
        if not u:
            return {
                **quote,
                "executed": False,
                "dry_run": True,
                "swap_status": "no_transaction",
                "note": "No unsigned transaction to sign (missing taker or router error)",
            }
        sign_res = sign_transaction_base64(u)
        if not sign_res.get("ok"):
            return {
                **quote,
                "executed": False,
                "dry_run": True,
                "swap_status": "unsigned_for_wallet",
                "unsigned_transaction": u,
                "request_id": rid,
                "sign_error": sign_res.get("error"),
                "note": sign_res.get("note")
                or "Sign with Phantom and POST signed_transaction + request_id + confirm=true",
            }
        signed = sign_res["signed_transaction"]
        signer_note = sign_res.get("signer")

    if not rid:
        return {
            **quote,
            "executed": False,
            "dry_run": True,
            "swap_status": "missing_request_id",
            "note": "request_id required for /execute",
        }

    exec_wrap = execute_signed(
        signed_transaction=signed,
        request_id=rid,
        last_valid_block_height=(order_data or {}).get("lastValidBlockHeight")
        if order_data
        else None,
        client=client,
    )
    if not exec_wrap.get("ok"):
        return {
            **quote,
            "executed": False,
            "dry_run": False,
            "swap_status": "execute_failed",
            "request_id": rid,
            "signer": signer_note,
            "execute_error": {k: v for k, v in exec_wrap.items() if k != "ok"},
        }

    result = exec_wrap.get("result") or {}
    status = result.get("status")
    return {
        **quote,
        "ok": True,
        "executed": status == "Success",
        "dry_run": False,
        "swap_status": "success" if status == "Success" else "failed",
        "request_id": rid,
        "signer": signer_note,
        "signature": result.get("signature"),
        "execute_result": result,
        "auto_trade": False,
        "recommend_only": True,
        "note": "Landed via Jupiter /execute"
        if status == "Success"
        else "Jupiter /execute returned non-success — see execute_result",
    }
