"""v1.17 faster-fill PREP — everything here is OFF by default and costs nothing until Andrew turns it on.

With LIVE_FAST_ENABLED unset/false (the default) the live engine behaves exactly as before: Jupiter /order →
local sign → simulateTransaction → Jupiter /execute, with the existing LIVE_MAX_PRIORITY_LAMPORTS cap.

When LIVE_FAST_ENABLED=true (needs a paid/private endpoint — see the v1.17 handoff for prices):
  LIVE_SEND_MODE=jupiter  (default) keep Jupiter /execute; only the priority fee / WS overrides below apply
  LIVE_SEND_MODE=rpc      send the same signed, simulated tx with sendTransaction to LIVE_FAST_RPC_URL
                          (skipPreflight, maxRetries 0) and confirm by polling signature status
  LIVE_SEND_MODE=jito     send a 2-tx bundle [swap, tip transfer] to LIVE_JITO_URL; tip = LIVE_JITO_TIP_LAMPORTS
                          (must be > 0 and ≤ LIVE_JITO_TIP_MAX_LAMPORTS) to a tip account from getTipAccounts
  LIVE_PRIORITY_FEE_LAMPORTS  overrides the priority-fee cap sent to Jupiter /order (still ≤ the order check)
  LIVE_FAST_WS_URL            used by the open-position account watcher (app/live/watch.py)

Safety: JupiterZ (RFQ) transactions always go through /execute (the market maker signs there). If the alternate
sender fails BEFORE anything was sent (config, tip accounts, HTTP refused), it falls back to /execute. If something was
sent and the outcome is unknown, the result is `ambiguous` and the engine's existing on-chain reconcile takes over.
The URLs may embed API keys: they are never logged or shown.
"""
from __future__ import annotations

import asyncio
import base64
import os
import random
import time
from typing import Any, Dict, List, Optional

import httpx

MODES = ("jupiter", "rpc", "jito")
DEFAULT_JITO_URL = "https://mainnet.block-engine.jito.wtf/api/v1/bundles"


def _on(name: str, d: str = "false") -> bool:
    return os.getenv(name, d).strip().lower() in {"1", "true", "yes", "on"}


def _i(name: str, d: int) -> int:
    try:
        return int(float(os.getenv(name, str(d)).strip()))
    except ValueError:
        return d


def cfg() -> Dict[str, Any]:
    mode = os.getenv("LIVE_SEND_MODE", "jupiter").strip().lower()
    prio = os.getenv("LIVE_PRIORITY_FEE_LAMPORTS", "").strip()
    return {
        "enabled": _on("LIVE_FAST_ENABLED"),
        "send_mode": mode if mode in MODES else "jupiter",
        "rpc_url": os.getenv("LIVE_FAST_RPC_URL", "").strip(),
        "ws_url_set": bool(os.getenv("LIVE_FAST_WS_URL", "").strip()),
        "jito_url": os.getenv("LIVE_JITO_URL", "").strip() or DEFAULT_JITO_URL,
        "jito_tip_lamports": _i("LIVE_JITO_TIP_LAMPORTS", 0),
        "jito_tip_max_lamports": _i("LIVE_JITO_TIP_MAX_LAMPORTS", 200_000),
        "priority_lamports": int(float(prio)) if prio.replace(".", "", 1).isdigit() else None,
        "confirm_timeout_s": float(os.getenv("LIVE_FAST_CONFIRM_TIMEOUT_S", "30")),
    }


def priority_lamports(default_cap: int) -> int:
    """The priority-fee cap for Jupiter /order. Unchanged unless fast fills are enabled with an override."""
    c = cfg()
    if c["enabled"] and c["priority_lamports"] is not None and c["priority_lamports"] >= 0:
        return c["priority_lamports"]
    return default_cap


def active_mode(router: Optional[str]) -> str:
    """Which sender to use for this order. Anything incomplete → 'jupiter' (the unchanged default path)."""
    c = cfg()
    if not c["enabled"] or router == "jupiterz":
        return "jupiter"
    if c["send_mode"] == "rpc" and c["rpc_url"]:
        return "rpc"
    if c["send_mode"] == "jito" and 0 < c["jito_tip_lamports"] <= c["jito_tip_max_lamports"]:
        return "jito"
    return "jupiter"


def summary() -> Dict[str, Any]:
    c = cfg()
    return {"enabled": c["enabled"], "send_mode": c["send_mode"], "active_for_metis": active_mode("metis"),
            "fast_rpc_set": bool(c["rpc_url"]), "fast_ws_set": c["ws_url_set"],
            "jito_tip_lamports": c["jito_tip_lamports"], "priority_override": c["priority_lamports"]}


async def _rpc(url: str, method: str, params: List[Any], client: httpx.AsyncClient, timeout: float = 8.0) -> Any:
    r = await client.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=timeout)
    if r.status_code != 200:
        raise RuntimeError(f"{method} http {r.status_code}")
    j = r.json()
    if "error" in j:
        raise RuntimeError(f"{method} error {str(j['error'])[:120]}")
    return j.get("result")


def build_tip_tx(signed_swap_b64: str, tip_account: str, lamports: int) -> str:
    """A SystemProgram transfer (our wallet → Jito tip account) on the swap's blockhash, signed locally."""
    from solders.hash import Hash  # type: ignore  # noqa: F401
    from solders.message import Message  # type: ignore
    from solders.pubkey import Pubkey  # type: ignore
    from solders.system_program import TransferParams, transfer  # type: ignore
    from solders.transaction import Transaction, VersionedTransaction  # type: ignore
    from app.live.jupiter import keypair
    kp = keypair()
    bh = VersionedTransaction.from_bytes(base64.b64decode(signed_swap_b64)).message.recent_blockhash
    ix = transfer(TransferParams(from_pubkey=kp.pubkey(), to_pubkey=REDACTED(tip_account), lamports=int(lamports)))
    msg = Message.new_with_blockhash([ix], kp.pubkey(), bh)
    tx = Transaction([kp], msg, bh)
    return base64.b64encode(bytes(tx)).decode("ascii")


async def build_tip_tx_any(signed_swap_b64: str, tip_account: str, lamports: int) -> str:
    """v1.24: local (default) or fomo-signer (intent=tip; the signer only allows Jito tip accounts ≤ its cap)."""
    from app.live import signer_client
    if not signer_client.enabled():
        return build_tip_tx(signed_swap_b64, tip_account, lamports)
    from solders.message import Message, to_bytes_versioned  # type: ignore
    from solders.pubkey import Pubkey  # type: ignore
    from solders.system_program import TransferParams, transfer  # type: ignore
    from solders.transaction import VersionedTransaction  # type: ignore
    me = Pubkey.from_string(signer_client.wallet() or "")
    bh = VersionedTransaction.from_bytes(base64.b64decode(signed_swap_b64)).message.recent_blockhash
    ix = transfer(TransferParams(from_pubkey=me, to_pubkey=REDACTED(tip_account), lamports=int(lamports)))
    msg = Message.new_with_blockhash([ix], me, bh)
    from solders.signature import Signature  # type: ignore
    from solders.transaction import Transaction  # type: ignore
    vmsg_bytes = to_bytes_versioned(msg)
    sig = await signer_client.sign_message(vmsg_bytes, {"kind": "tip", "amount": int(lamports), "tag": "jito_tip"})
    tx = Transaction.populate(msg, [Signature.from_bytes(sig)])
    return base64.b64encode(bytes(tx)).decode("ascii")


async def confirm(sig: str, url: str, client: httpx.AsyncClient, timeout_s: float) -> Dict[str, Any]:
    t_end = time.time() + timeout_s
    while time.time() < t_end:
        try:
            st = (await _rpc(url, "getSignatureStatuses", [[sig], {"searchTransactionHistory": False}], client) or {})
            v = (st.get("value") or [None])[0]
        except Exception:  # noqa: BLE001
            v = None
        if v and v.get("confirmationStatus") in ("confirmed", "finalized"):
            return {"landed": True, "err": v.get("err")}
        await asyncio.sleep(0.4)
    return {"landed": None}


async def fill_amounts(sig: str, url: str, client: httpx.AsyncClient, wallet: str, input_mint: str,
                       output_mint: str) -> Dict[str, int]:
    """total_in / total_out (raw units) from the landed tx's balance changes for our wallet."""
    from app.scout.txparse import parse_wallet_trades
    WSOL = "So11111111111111111111111111111111111111112"
    try:
        tx = await _rpc(url, "getTransaction", [sig, {"encoding": "json", "commitment": "confirmed",
                                                      "maxSupportedTransactionVersion": 1}], client)
    except Exception:  # noqa: BLE001
        tx = None
    for ev in parse_wallet_trades(tx, wallet) if tx else []:
        if input_mint == WSOL and ev["mint"] == output_mint and ev["is_buy"]:
            return {"total_in": ev["sol_amount"], "total_out": ev["token_amount"]}
        if output_mint == WSOL and ev["mint"] == input_mint and not ev["is_buy"]:
            return {"total_in": ev["token_amount"], "total_out": ev["sol_amount"]}
    return {"total_in": 0, "total_out": 0}


async def send(mode: str, signed_b64: str, sig: Optional[str], *, input_mint: str, output_mint: str,
               client: Optional[httpx.AsyncClient] = None) -> Dict[str, Any]:
    """Alternate sender. Returns an execute()-shaped dict, or {"fallback": True} when nothing was sent."""
    from app.live.jupiter import pubkey
    from app.pump import rpc as _rpc_mod
    c = cfg()
    own = client is None
    cl = client or httpx.AsyncClient(timeout=15.0)
    conf_url = c["rpc_url"] or _rpc_mod.rpc_url()
    try:
        if mode == "rpc":
            try:
                got = await _rpc(c["rpc_url"], "sendTransaction",
                                 [signed_b64, {"encoding": "base64", "skipPreflight": True, "maxRetries": 0}], cl)
            except Exception as exc:  # noqa: BLE001
                # http refusal before acceptance → nothing on the wire from us; Jupiter path is still safe
                if "http 4" in str(exc) or "error" in str(exc):
                    return {"fallback": True, "error": f"fast rpc refused: {type(exc).__name__}"}
                return {"ok": False, "ambiguous": True, "error": "fast rpc send outcome unknown", "signature": sig}
            sig = got or sig
        elif mode == "jito":
            try:
                tips = await _rpc(c["jito_url"].replace("/bundles", "/getTipAccounts") if "/bundles" in c["jito_url"]
                                  else c["jito_url"], "getTipAccounts", [], cl)
                tip_acct = random.choice(list(tips or []))
                tip_tx = await build_tip_tx_any(signed_b64, tip_acct, c["jito_tip_lamports"])
            except Exception as exc:  # noqa: BLE001
                return {"fallback": True, "error": f"jito prep failed: {type(exc).__name__}"}
            try:
                await _rpc(c["jito_url"], "sendBundle", [[signed_b64, tip_tx], {"encoding": "base64"}], cl)
            except Exception as exc:  # noqa: BLE001
                if "http 4" in str(exc) or "error" in str(exc):
                    return {"fallback": True, "error": f"jito refused: {type(exc).__name__}"}
                return {"ok": False, "ambiguous": True, "error": "jito send outcome unknown", "signature": sig}
        else:
            return {"fallback": True, "error": f"unknown mode {mode}"}
        if not sig:
            return {"ok": False, "ambiguous": True, "error": "no signature to confirm", "signature": None}
        st = await confirm(sig, conf_url, cl, c["confirm_timeout_s"])
        if st.get("landed") is None:
            return {"ok": False, "ambiguous": True, "error": f"{mode}: not confirmed in time", "signature": sig}
        if st.get("err"):
            return {"ok": False, "ambiguous": False, "error": f"{mode}: tx failed {str(st['err'])[:80]}", "signature": sig}
        amts = await fill_amounts(sig, conf_url, cl, pubkey() or "", input_mint, output_mint)
        return {"ok": True, "status": "Success", "signature": sig, "ambiguous": False, "sender": mode, **amts}
    finally:
        if own:
            await cl.aclose()
