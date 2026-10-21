"""Classify a getTransaction(jsonParsed) result for one tracked wallet.

Kept only when fee payer == FOMO co-signer AND the tracked wallet signed. Buy = a non-quote token in
while USDC/USDT/SOL goes out; sell = token out while quote comes in. A USDC payment into Relay with no
token received is a cross-chain buy (token lands on another chain; no Solana mint to act on).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.fomo_onchain import (FOMO_COSIGNER, RELAY_DEPOSIT_PROGRAM, USDC_MINT, USDT_MINT, WSOL_MINT)

STABLES = {USDC_MINT, USDT_MINT}
QUOTES = STABLES | {WSOL_MINT}
SOL_DUST_LAMPORTS = 10_000_000  # ignore <0.01 SOL native moves (rent / ATA creation)


def _keys(tx: Dict[str, Any]) -> List[Dict[str, Any]]:
    ks = ((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or []
    return [k if isinstance(k, dict) else {"pubkey": k, "signer": False} for k in ks]


def _programs(tx: Dict[str, Any]) -> List[str]:
    msg = (tx.get("transaction") or {}).get("message") or {}
    out = [ix.get("programId") for ix in msg.get("instructions") or [] if isinstance(ix, dict)]
    for g in (tx.get("meta") or {}).get("innerInstructions") or []:
        out += [ix.get("programId") for ix in g.get("instructions") or [] if isinstance(ix, dict)]
    return [p for p in out if p]


def _token_deltas(meta: Dict[str, Any], owner: str) -> Dict[str, Dict[str, Any]]:
    """mint -> {delta_ui, pre_ui, post_ui, decimals} for token accounts owned by `owner`."""
    acc: Dict[str, Dict[str, Any]] = {}
    for side in ("pre", "post"):
        for b in meta.get(f"{side}TokenBalances") or []:
            if b.get("owner") != owner:
                continue
            ui = b.get("uiTokenAmount") or {}
            amt = float(ui.get("uiAmountString") or ui.get("uiAmount") or 0)
            d = acc.setdefault(b.get("mint"), {"pre_ui": 0.0, "post_ui": 0.0, "decimals": ui.get("decimals")})
            d[f"{side}_ui"] += amt
    for d in acc.values():
        d["delta_ui"] = d["post_ui"] - d["pre_ui"]
    return acc


def classify(tx: Optional[Dict[str, Any]], wallet: str, *, sol_usd: Optional[float] = None,
             signature: Optional[str] = None) -> Dict[str, Any]:
    """Return {kind, fomo, ...}. kind ∈ buy|sell|xchain_buy|transfer|other|not_fomo|failed|missing."""
    if not tx:
        return {"kind": "missing", "fomo": False, "signature": signature}
    meta = tx.get("meta") or {}
    keys = _keys(tx)
    sig = signature or ((tx.get("transaction") or {}).get("signatures") or [None])[0]
    fee_payer = keys[0]["pubkey"] if keys else None
    signed = any(k.get("pubkey") == wallet and k.get("signer") for k in keys)
    base = {"signature": sig, "wallet": wallet, "block_time": tx.get("blockTime"), "slot": tx.get("slot"),
            "fee_payer": fee_payer, "wallet_signed": signed}
    if meta.get("err") is not None:
        return {**base, "kind": "failed", "fomo": False}
    fomo = fee_payer == FOMO_COSIGNER and signed
    if not fomo:
        return {**base, "kind": "not_fomo", "fomo": False}
    deltas = _token_deltas(meta, wallet)
    sol_delta = 0
    for i, k in enumerate(keys):
        if k.get("pubkey") == wallet:
            pre, post = (meta.get("preBalances") or [])[i:i + 1], (meta.get("postBalances") or [])[i:i + 1]
            if pre and post:
                sol_delta = post[0] - pre[0]
    stable_delta = sum(deltas[m]["delta_ui"] for m in deltas if m in STABLES)
    wsol_delta = deltas.get(WSOL_MINT, {}).get("delta_ui", 0.0)
    native_sol = sol_delta / 1e9 if abs(sol_delta) >= SOL_DUST_LAMPORTS else 0.0
    sol_total = wsol_delta + native_sol
    quote_usd = stable_delta + (sol_total * sol_usd if sol_usd else 0.0)
    quote_ccy = "USDC" if abs(stable_delta) > 1e-9 else ("SOL" if abs(sol_total) > 0 else None)
    tokens = {m: d for m, d in deltas.items() if m not in QUOTES and abs(d["delta_ui"]) > 0}
    gains = sorted(((m, d) for m, d in tokens.items() if d["delta_ui"] > 0), key=lambda x: -x[1]["delta_ui"])
    losses = sorted(((m, d) for m, d in tokens.items() if d["delta_ui"] < 0), key=lambda x: x[1]["delta_ui"])
    quote_out = stable_delta < -1e-6 or sol_total < 0
    quote_in = stable_delta > 1e-6 or sol_total > 0
    progs = set(_programs(tx))
    out = {**base, "fomo": True, "usdc_delta": round(stable_delta, 6), "sol_delta": round(sol_total, 9),
           "quote_ccy": quote_ccy, "programs": sorted(p[:8] for p in progs)}
    if gains and quote_out:
        m, d = gains[0]
        usd = abs(stable_delta) + (abs(sol_total) * sol_usd if sol_usd and sol_total < 0 else 0.0)
        return {**out, "kind": "buy", "mint": m, "token_amount_ui": d["delta_ui"], "token_pre_ui": d["pre_ui"],
                "usd": round(usd, 4) if usd else None,
                "sol_spent": round(-sol_total, 9) if sol_total < 0 else None,
                "fill_price_usd": (usd / d["delta_ui"]) if usd and d["delta_ui"] else None,
                "extra_mints": [g[0] for g in gains[1:]]}
    if losses and quote_in:
        m, d = losses[0]
        usd = stable_delta + (sol_total * sol_usd if sol_usd and sol_total > 0 else 0.0)
        sold = -d["delta_ui"]
        return {**out, "kind": "sell", "mint": m, "token_amount_ui": sold, "token_pre_ui": d["pre_ui"],
                "sold_fraction": round(min(1.0, sold / d["pre_ui"]), 4) if d["pre_ui"] > 0 else 1.0,
                "usd": round(usd, 4) if usd else None,
                "fill_price_usd": (usd / sold) if usd and sold else None}
    if quote_out and not gains and RELAY_DEPOSIT_PROGRAM in progs:
        return {**out, "kind": "xchain_buy", "usd": round(abs(stable_delta), 4)}
    if quote_in and not losses and RELAY_DEPOSIT_PROGRAM in progs:
        return {**out, "kind": "xchain_sell_payout", "usd": round(stable_delta, 4)}
    if tokens or abs(quote_usd) > 0:
        return {**out, "kind": "transfer"}
    return {**out, "kind": "other"}
