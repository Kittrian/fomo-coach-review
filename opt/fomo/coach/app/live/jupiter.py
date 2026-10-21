"""Jupiter Swap API v2 order → local sign → execute (or simulate). Key material stays in memory only."""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import time
from typing import Any, Dict, Optional

import httpx

from app.config import settings
from app.jupiter_exec import JUPITER_SWAP_V2_BASE, _headers, _load_keypair_bytes

logger = logging.getLogger("coach.live.jupiter")
WSOL = "So11111111111111111111111111111111111111112"
_kp: Dict[str, Any] = {}


def keypair():
    """Load the local keypair once (solders). Never logs contents. Raises if unavailable.
    v1.24: with SIGNER_BACKEND=socket the coach holds no key at all → always raises (callers fail closed)."""
    from app.live import signer_client
    if signer_client.enabled():
        raise RuntimeError("key material lives in fomo-signer (SIGNER_BACKEND=socket)")
    if _kp.get("path") == settings.solana_keypair_path and _kp.get("kp") is not None:
        return _kp["kp"]
    from solders.keypair import Keypair  # type: ignore
    if not settings.solana_keypair_configured:
        raise RuntimeError("SOLANA_KEYPAIR_PATH not configured")
    secret = _load_keypair_bytes(settings.solana_keypair_path)
    kp = Keypair.from_bytes(secret)
    del secret
    _kp.update(path=settings.solana_keypair_path, kp=kp)
    return kp


def pubkey() -> Optional[str]:
    from app.live import signer_client
    if signer_client.enabled():
        return signer_client.wallet()
    try:
        return str(keypair().pubkey())
    except Exception:  # noqa: BLE001
        return None


def sign_partial(tx_b64: str) -> str:
    """Sign our signer slot only (JupiterZ RFQ adds the market-maker signature at /execute)."""
    from solders.message import to_bytes_versioned  # type: ignore
    from solders.transaction import VersionedTransaction  # type: ignore
    kp = keypair()
    tx = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
    msg = tx.message
    n = msg.header.num_required_signatures
    keys = list(msg.account_keys)[:n]
    me = kp.pubkey()
    if me not in keys:
        raise ValueError("order transaction does not require our signature (taker mismatch)")
    sigs = list(tx.signatures)
    sigs[keys.index(me)] = kp.sign_message(to_bytes_versioned(msg))
    signed = VersionedTransaction.populate(msg, sigs)
    return base64.b64encode(bytes(signed)).decode("ascii")


async def sign_tx(tx_b64: str, intent: Dict[str, Any]) -> str:
    """v1.24: sign our slot — locally (default) or through fomo-signer with the stated intent."""
    from app.live import signer_client
    if signer_client.enabled():
        return await signer_client.sign_tx_b64(tx_b64, intent)
    return sign_partial(tx_b64)


def swap_intent(input_mint: str, output_mint: str, amount: int, max_priority_lamports: int,
                router: Optional[str] = None, tag: str = "") -> Dict[str, Any]:
    return {"kind": "buy" if input_mint == WSOL else "sell", "input_mint": input_mint, "output_mint": output_mint,
            "amount": int(amount), "max_priority_lamports": int(max_priority_lamports), "router": router, "tag": tag}


async def order(*, input_mint: str, output_mint: str, amount: int, slippage_bps: int,
                max_priority_lamports: int, client: Optional[httpx.AsyncClient] = None,
                extra_exclude_routers: Optional[list] = None) -> Dict[str, Any]:
    params = {"inputMint": input_mint, "outputMint": output_mint, "amount": str(int(amount)),
              "taker": pubkey() or "", "slippageBps": str(int(slippage_bps))}
    _ex = os.getenv("LIVE_JUPITER_EXCLUDE_ROUTERS", "okx").strip()   # v1.39: OKX router program is not signer-allowlisted
    if extra_exclude_routers:                                         # v1.42 dust retry: also skip the refused router
        _ex = ",".join(dict.fromkeys([x for x in (_ex.split(",") if _ex else []) + list(extra_exclude_routers) if x]))
    if _ex:
        params["excludeRouters"] = _ex
    fee_plan = None
    from app.live import fees as _fees    # v1.38: buys pay an exact, floored priority from the live tip feed
    if max_priority_lamports > 0 and input_mint == "So11111111111111111111111111111111111111112" and _fees.enabled():
        fee_plan = _fees.plan(int(amount), side="buy", route="jupiter")
        params.update(priorityFeeLamports=str(min(int(fee_plan["priority_lamports"]), int(max_priority_lamports))),
                      broadcastFeeType="exactFee")
    elif max_priority_lamports > 0:
        params.update(priorityFeeLamports=str(int(max_priority_lamports)), broadcastFeeType="maxCap")
    own = client is None
    c = client or httpx.AsyncClient(timeout=15.0)
    t0 = time.perf_counter()
    try:
        r = await c.get(f"{JUPITER_SWAP_V2_BASE}/order", params=params, headers=_headers(), timeout=15.0)
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code != 200:
            return {"ok": False, "error": f"order HTTP {r.status_code}", "detail": str(body)[:200]}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"order {type(exc).__name__}"}
    finally:
        if own:
            await c.aclose()
    o = body or {}
    out = {"ok": bool(o.get("transaction")), "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
           "request_id": o.get("requestId"), "router": o.get("router"), "mode": o.get("mode"),
           "in_amount": int(o.get("inAmount") or 0), "out_amount": int(o.get("outAmount") or 0),
           "min_out": int(o.get("otherAmountThreshold") or 0), "slippage_bps": o.get("slippageBps"),
           "price_impact": o.get("priceImpact"), "fee_bps": o.get("feeBps"),
           "priority_lamports": o.get("prioritizationFeeLamports"), "gasless": o.get("gasless"),
           "route": [((s.get("swapInfo") or {}).get("label")) for s in (o.get("routePlan") or [])],
           "transaction": o.get("transaction") or None}
    if fee_plan:
        out["fee_plan"] = {k: fee_plan.get(k) for k in ("budget_lamports", "priority_lamports", "total_lamports",
                                                      "pct_of_size", "p75_tip_feed", "capped")}
    if not out["ok"]:
        out["error"] = f"no transaction ({o.get('router')} errorCode {o.get('errorCode')}: {o.get('errorMessage')})"
    elif o.get("inputMint") not in (None, input_mint) or o.get("outputMint") not in (None, output_mint) \
            or (out["in_amount"] and out["in_amount"] != int(amount)):
        out.update(ok=False, error="order response does not match the request — refused")
    elif max_priority_lamports > 0 and (out["priority_lamports"] or 0) > max_priority_lamports and not o.get("gasless"):
        out.update(ok=False, error=f"priority fee {out['priority_lamports']} > cap {max_priority_lamports} — refused")
    return out


async def execute(signed_b64: str, request_id: str, client: Optional[httpx.AsyncClient] = None) -> Dict[str, Any]:
    own = client is None
    c = client or httpx.AsyncClient(timeout=60.0)
    try:
        r = await c.post(f"{JUPITER_SWAP_V2_BASE}/execute", json={"signedTransaction": signed_b64, "requestId": request_id},
                         headers={**_headers(), "Content-Type": "application/json"}, timeout=60.0)
        j = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code != 200:
            return {"ok": False, "ambiguous": r.status_code >= 500, "error": f"execute HTTP {r.status_code}",
                    "detail": str(j)[:200], "signature": (j or {}).get("signature")}
    except Exception as exc:  # noqa: BLE001  timeout etc → outcome unknown, caller reconciles on-chain
        return {"ok": False, "ambiguous": True, "error": f"execute {type(exc).__name__}"}
    finally:
        if own:
            await c.aclose()
    return {"ok": j.get("status") == "Success", "status": j.get("status"), "code": j.get("code"),
            "signature": j.get("signature"), "error": j.get("error"),
            "total_in": int(j.get("totalInputAmount") or 0), "total_out": int(j.get("totalOutputAmount") or 0),
            "ambiguous": False}


def signature_of(signed_b64: str) -> Optional[str]:
    """Base58 tx signature (= first signature, the fee payer = our taker) of a signed tx. Known BEFORE /execute."""
    try:
        from solders.transaction import VersionedTransaction  # type: ignore
        from solders.signature import Signature  # type: ignore
        tx = VersionedTransaction.from_bytes(base64.b64decode(signed_b64))
        first = tx.signatures[0]
        return None if first == Signature.default() else str(first)   # RFQ: MM slot unsigned → id unknown yet
    except Exception:  # noqa: BLE001
        return None


def parallel_sim_enabled() -> bool:
    """v1.25 LIVE_SIGNER_PARALLEL_SIM: with SIGNER_BACKEND=socket, run the coach's own simulateTransaction on the
    UNSIGNED message at the same time as the fomo-signer request instead of after it. Both must pass before anything
    is sent; the signer's own policy + simulation are unchanged (no-drain guarantees untouched)."""
    from app.live import signer_client
    return signer_client.enabled() and os.getenv("LIVE_SIGNER_PARALLEL_SIM", "false").strip().lower() in (
        "1", "true", "yes", "on")


async def simulate_signed(signed_b64: str, router: Optional[str], *, rpc_url: str = "",
                          client: Optional[httpx.AsyncClient] = None, unsigned: bool = False) -> Dict[str, Any]:
    """v1.15: simulateTransaction on the exact signed tx before /execute.

    → {"ok": bool, "transport_error": bool, "err": ..., "logs_tail": [...]}. transport_error=True means the RPC
    could not be asked (not that the tx would fail). JupiterZ RFQ txs lack the market-maker signature until
    /execute, so they are simulated with sigVerify off + replaceRecentBlockhash."""
    from app.pump import rpc as _rpc
    full = router != "jupiterz"
    # unsigned (v1.25 parallel): no sig check, but keep the real blockhash (still validates it) except JupiterZ
    params = [signed_b64, {"encoding": "base64", "sigVerify": full and not unsigned, "replaceRecentBlockhash": not full,
                           "commitment": "processed"}]
    try:
        res = await _rpc.call("simulateTransaction", params, client=client, url=rpc_url or None, timeout=8.0)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "transport_error": True, "err": f"simulate {type(exc).__name__}"}
    v = (res or {}).get("value") or {}
    if not v:
        return {"ok": False, "transport_error": True, "err": "simulate: empty result"}
    logs = v.get("logs") or []
    return {"ok": v.get("err") is None, "transport_error": False, "err": v.get("err"),
            "units": v.get("unitsConsumed"), "logs_tail": [str(x)[:160] for x in logs[-3:]]}


def dust_retry_enabled() -> bool:
    """v1.42 LIVE_JUPITER_DUST_RETRY: buys only. Default off."""
    return os.getenv("LIVE_JUPITER_DUST_RETRY", "false").strip().lower() in ("1", "true", "yes", "on")


_DUST_REASONS = ("unexpected token change on our account", "program not allowed")


def _dust_retry_router(exc: Exception, o: Dict[str, Any], input_mint: str,
                       prefetched: Optional[Dict[str, Any]]) -> Optional[str]:
    """Router to exclude for one re-order, or None (no retry). Only SOL→token buys refused by the signer for a
    stray intermediate-token change / unlisted program, only for real aggregator routers, never for prefetched sells."""
    if not dust_retry_enabled() or input_mint != WSOL or prefetched:
        return None
    if type(exc).__name__ != "SignerRefused":
        return None
    msg = str(exc)
    if not any(r in msg for r in _DUST_REASONS) or "24h buy volume cap" in msg:
        return None
    router = (o.get("router") or "").strip().lower()
    return router if router in ("metis", "dflow", "jupiterz", "okx") else None


async def swap(*, input_mint: str, output_mint: str, amount: int, slippage_bps: int, max_priority_lamports: int,
               client: Optional[httpx.AsyncClient] = None, simulate: bool = True, sim_rpc_url: str = "",
               allow_sim_transport_error: bool = False, on_signed: Optional[Any] = None,
               pre_execute: Optional[Any] = None, pre_sign: Optional[Any] = None,
               pre_sign_quote: Optional[Any] = None, prefetched: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """v1.37 `pre_sign(order_age_s) -> (ok, why, requote)`: awaited after the order and BEFORE signing (a slow
    read-only safety check overlaps the order fetch; ok=False → stage "gate", nothing signed or sent).

    order → sign → simulateTransaction → [pre_execute gate] → (on_signed(sig) persisted by caller) → execute.

    Stages on failure: order | sign | simulate | gate | requote | execute. A simulate failure never reaches /execute
    (unless the simulation RPC itself was unreachable AND allow_sim_transport_error — used only for sell retries).

    v1.17.2 `pre_execute(order_age_s) -> (ok, why, requote)`: awaited AFTER simulate and BEFORE on_signed / any send.
    Lets the caller build+simulate the tx while a slow safety check (the mesh) is still running. ok=False → stage
    "gate", nothing is sent. requote=True → stage "requote" (the order aged while waiting), nothing is sent."""
    if prefetched and prefetched.get("ok") and prefetched.get("transaction"):   # v1.40e warm sell route
        o, t_order = prefetched, float(prefetched.get("_fetched_at") or time.time())
    else:
        o = await order(input_mint=input_mint, output_mint=output_mint, amount=amount, slippage_bps=slippage_bps,
                        max_priority_lamports=max_priority_lamports, client=client)
        t_order = time.time()
    info = {k: v for k, v in o.items() if k != "transaction"}
    if not o["ok"]:
        return {"ok": False, "stage": "order", "order": info, "error": o.get("error")}
    if pre_sign_quote is not None:                       # v1.40: chase filter on the order amounts, before signing
        try:
            q_ok, q_why = pre_sign_quote({"in_amount": info.get("in_amount") or amount, "out_amount": info.get("out_amount"),
                                          "path": "jupiter"})
        except Exception as exc:  # noqa: BLE001
            q_ok, q_why = False, f"pre-sign quote gate error ({type(exc).__name__})"
        if not q_ok:
            return {"ok": False, "stage": "gate", "order": info, "error": q_why, "pre_sign": True}
    if pre_sign is not None:
        try:
            p_ok, p_why, p_requote = await pre_sign(time.time() - t_order)
        except Exception as exc:  # noqa: BLE001   a broken gate never signs
            p_ok, p_why, p_requote = False, f"pre-sign gate error ({type(exc).__name__})", False
        if not p_ok:
            return {"ok": False, "stage": "gate", "order": info, "error": p_why}
        if p_requote:
            return {"ok": False, "stage": "requote", "order": info, "error": "order aged while waiting for the pre-sign gate"}
    par = simulate and parallel_sim_enabled()
    sim_t = (asyncio.ensure_future(simulate_signed(o["transaction"], o.get("router"), rpc_url=sim_rpc_url,
                                                   client=client, unsigned=True)) if par else None)
    t_sign = time.perf_counter()
    try:
        signed = await sign_tx(o["transaction"], swap_intent(input_mint, output_mint, amount, max_priority_lamports,
                                                             o.get("router"), "jupiter"))
    except Exception as exc:  # noqa: BLE001
        if sim_t is not None:
            sim_t.cancel()
        retry = _dust_retry_router(exc, o, input_mint, prefetched)
        if retry is None:
            return {"ok": False, "stage": "sign", "order": info, "error": f"sign {type(exc).__name__}: {str(exc)[:80]}"}
        # v1.42: multi-hop route left +dust of an intermediate token on our account → the signer (correctly) refused.
        # Re-order ONCE with that router excluded; every gate below still runs on the new order. Nothing was sent.
        first_err = f"sign {type(exc).__name__}: {str(exc)[:80]}"
        logger.info("jupiter dust retry: %s refused (%s) → re-order excluding %s", o.get("router"), str(exc)[:60], retry)
        o = await order(input_mint=input_mint, output_mint=output_mint, amount=amount, slippage_bps=slippage_bps,
                        max_priority_lamports=max_priority_lamports, client=client, extra_exclude_routers=[retry])
        t_order = time.time()
        info = {k: v for k, v in o.items() if k != "transaction"}
        info["dust_retry"] = {"excluded": retry, "first_error": first_err}
        if not o["ok"]:
            return {"ok": False, "stage": "sign", "order": info, "error": f"{first_err}; dust retry order failed: {o.get('error')}"}
        if pre_sign_quote is not None:
            try:
                q_ok, q_why = pre_sign_quote({"in_amount": info.get("in_amount") or amount,
                                              "out_amount": info.get("out_amount"), "path": "jupiter"})
            except Exception as exc2:  # noqa: BLE001
                q_ok, q_why = False, f"pre-sign quote gate error ({type(exc2).__name__})"
            if not q_ok:
                return {"ok": False, "stage": "gate", "order": info, "error": q_why, "pre_sign": True}
        sim_t = (asyncio.ensure_future(simulate_signed(o["transaction"], o.get("router"), rpc_url=sim_rpc_url,
                                                       client=client, unsigned=True)) if par else None)
        t_sign = time.perf_counter()
        try:
            signed = await sign_tx(o["transaction"], swap_intent(input_mint, output_mint, amount, max_priority_lamports,
                                                                 o.get("router"), "jupiter"))
        except Exception as exc2:  # noqa: BLE001
            if sim_t is not None:
                sim_t.cancel()
            return {"ok": False, "stage": "sign", "order": info,
                    "error": f"{first_err}; dust retry sign {type(exc2).__name__}: {str(exc2)[:80]}"}
    info["sign_ms"] = round((time.perf_counter() - t_sign) * 1000, 1)
    sim: Dict[str, Any] = {}
    if simulate:
        t_sim = time.perf_counter()
        sim = (await sim_t) if sim_t is not None else await simulate_signed(signed, o.get("router"),
                                                                          rpc_url=sim_rpc_url, client=client)
        sim["wait_ms"] = round((time.perf_counter() - t_sim) * 1000, 1)
        sim["parallel"] = par
        if not sim.get("ok") and not (sim.get("transport_error") and allow_sim_transport_error):
            return {"ok": False, "stage": "simulate", "order": info, "simulation": sim,
                    "error": f"simulation failed: {str(sim.get('err'))[:160]}"}
    if pre_execute is not None:
        try:
            if getattr(pre_execute, "wants_quote", False):          # v1.21 chase filter sees the quote
                g_ok, g_why, g_requote = await pre_execute(
                    time.time() - t_order, quote={"in_amount": info.get("in_amount") or amount,
                                                  "out_amount": info.get("out_amount"), "path": "jupiter"})
            else:
                g_ok, g_why, g_requote = await pre_execute(time.time() - t_order)
        except Exception as exc:  # noqa: BLE001   a broken gate never sends
            g_ok, g_why, g_requote = False, f"pre-execute gate error ({type(exc).__name__})", False
        if not g_ok:
            return {"ok": False, "stage": "gate", "order": info, "simulation": sim, "error": g_why}
        if g_requote:
            return {"ok": False, "stage": "requote", "order": info, "simulation": sim,
                    "error": "order aged while waiting for the pre-execute gate"}
    sig = signature_of(signed)
    if on_signed is not None:
        try:
            on_signed(sig)
        except Exception:  # noqa: BLE001
            pass
    # v1.17: optional alternate sender (OFF by default → always Jupiter /execute, exactly as before)
    from app.live import fastfill
    mode = fastfill.active_mode(o.get("router"))
    ex: Dict[str, Any] = {}
    if mode != "jupiter":
        ex = await fastfill.send(mode, signed, sig, input_mint=input_mint, output_mint=output_mint, client=client)
        if ex.get("fallback"):
            ex = {}
    if not ex:
        ex = await execute(signed, o["request_id"], client=client)
    ex["signature_from_server"] = bool(ex.get("signature"))
    if not ex.get("signature") and sig:
        ex["signature"] = sig
    return {**ex, "stage": "execute", "order": info, "simulation": sim, "presigned_signature": sig}


async def simulate_order(*, input_mint: str, output_mint: str, amount: int, slippage_bps: int,
                         max_priority_lamports: int, rpc_url: str) -> Dict[str, Any]:
    """DRY: build the real /order tx for our taker, sign in memory, simulateTransaction. Never /execute."""
    o = await order(input_mint=input_mint, output_mint=output_mint, amount=amount, slippage_bps=slippage_bps,
                    max_priority_lamports=max_priority_lamports)
    info = {k: v for k, v in o.items() if k != "transaction"}
    if not o["ok"]:
        return {"ok": False, "order": info}
    signed = await sign_tx(o["transaction"], swap_intent(input_mint, output_mint, amount, max_priority_lamports,
                                                         o.get("router"), "simulate_order"))
    full = o.get("router") != "jupiterz"     # RFQ tx lacks the MM signature until /execute → no sig verify
    async with httpx.AsyncClient(timeout=20.0) as c:
        r = await c.post(rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": "simulateTransaction", "params": [
            signed, {"encoding": "base64", "sigVerify": full, "replaceRecentBlockhash": not full,
                     "commitment": "processed"}]})
        v = ((r.json() or {}).get("result") or {}).get("value") or {}
    logs = v.get("logs") or []
    return {"ok": v.get("err") is None and bool(v), "err": v.get("err"), "units": v.get("unitsConsumed"),
            "sig_verified": full, "logs_tail": logs[-4:], "order": info}
