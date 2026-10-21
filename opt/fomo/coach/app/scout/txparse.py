"""v1.16: turn one getTransaction(json) result into the watched wallet's trades, whatever the route.

Why: pump.fun events only tell the truth for SOL-quoted coins traded directly. Router / multi-hop buys
(`multi_hop_curve_swap`, PumpSwap `multi_hop_swap`, Jupiter/OKX/Axiom routes) and non-SOL-quote coins (quoted in
USDC or another pump coin) report `sol_amount = 0`, a different `user`, or no pump event at all. The wallet's own
balance changes do not lie: SOL spent (native + wrapped, fee excluded) and tokens received.

Pure function, no I/O. Amounts are in base units; tokens use their own decimals (pump coins: 6).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.pump.events import WSOL_MINT, parse_all_logs, parse_inner_events

STABLE_MINTS = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",   # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",   # USDT
}
QUOTE_LIKE = STABLE_MINTS | {WSOL_MINT}
PUMP_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMP_AMM_PROGRAM_ID = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
KNOWN_ROUTERS = {
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4": "jupiter",
    "6m2CDdhRgxpH4WjvdzxAYbGxwdGUz5MziiL5jek2kBma": "okx",
    "proVF4pMXVaYqmy4NjniPh4pqKNfMmsihgd4wdkCX3u": "okx",
}
MIN_SOL_LAMPORTS = 1_000_000          # 0.001 SOL: below this a "trade" is noise (fees, rent dust)


def account_keys(tx: Dict[str, Any]) -> List[str]:
    msg = ((tx or {}).get("transaction") or {}).get("message") or {}
    keys = [k if isinstance(k, str) else (k or {}).get("pubkey") for k in msg.get("accountKeys") or []]
    la = ((tx or {}).get("meta") or {}).get("loadedAddresses") or {}
    return keys + list(la.get("writable") or []) + list(la.get("readonly") or [])


def programs(tx: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    for line in ((tx or {}).get("meta") or {}).get("logMessages") or []:
        if isinstance(line, str) and line.startswith("Program ") and " invoke [" in line:
            p = line.split()[1]
            if p not in out:
                out.append(p)
    return out


def _token_deltas(meta: Dict[str, Any], owner: str) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for side, sign in (("preTokenBalances", -1), ("postTokenBalances", 1)):
        for b in meta.get(side) or []:
            if b.get("owner") != owner:
                continue
            ui = b.get("uiTokenAmount") or {}
            try:
                amt = int(ui.get("amount") or 0)
            except (TypeError, ValueError):
                continue
            d = out.setdefault(b.get("mint"), {"delta": 0, "decimals": ui.get("decimals")})
            d["delta"] += sign * amt
            if d.get("decimals") is None:
                d["decimals"] = ui.get("decimals")
    return {m: d for m, d in out.items() if m and d["delta"] != 0}


def parse_wallet_trades(tx: Optional[Dict[str, Any]], wallet: str) -> List[Dict[str, Any]]:
    """Normalized trades for `wallet` in this tx, shaped like a decoded pump TradeEvent plus extras:
    mint, sol_amount (lamports, from balances), token_amount, is_buy, user, quote_mint, venue, ix_name,
    virtual_* reserves (SOL-quoted pump curve only), price_sol (fill), spot_sol (curve spot when known),
    decode_path='tx'. Empty when the tx failed or the wallet traded nothing."""
    if not tx:
        return []
    meta = tx.get("meta") or {}
    if meta.get("err") is not None:
        return []
    keys = account_keys(tx)
    if wallet not in keys:
        return []
    i = keys.index(wallet)
    pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
    lamports = (post[i] - pre[i]) if i < len(pre) and i < len(post) else 0
    if i == 0:
        lamports += int(meta.get("fee") or 0)            # fee is not trade value
    toks = _token_deltas(meta, wallet)
    wsol = toks.pop(WSOL_MINT, {"delta": 0})["delta"]
    sol_delta = lamports + wsol                          # native + wrapped, signed (negative = spent)
    stables = {m: toks.pop(m) for m in list(toks) if m in STABLE_MINTS}
    if not toks:
        return []
    evs = parse_all_logs(meta.get("logMessages") or [])
    if not any(e.get("kind") == "pump_trade" for e in evs):
        evs += parse_inner_events(meta)
    pump_by_mint: Dict[str, Dict[str, Any]] = {}
    for e in evs:
        if e.get("kind") == "pump_trade" and e["mint"] in toks:
            pump_by_mint.setdefault(e["mint"], e)
    progs = programs(tx)
    venue = next((KNOWN_ROUTERS[p] for p in progs if p in KNOWN_ROUTERS), None)
    if venue is None:
        venue = "pump_curve" if PUMP_PROGRAM_ID in progs and PUMP_AMM_PROGRAM_ID not in progs else \
            "pumpswap" if PUMP_AMM_PROGRAM_ID in progs else (progs[0][:8] + "…" if progs else "unknown")
    # One SOL leg per tx → one trade per tx: the primary mint. Quote coins of the wallet's pump events (dust left
    # from a multi-hop route) are never the primary; a mint with a pump event for the wallet is preferred.
    quote_mints = {e.get("quote_mint") for e in evs if e.get("kind") == "pump_trade" and e.get("user") == wallet}
    cands = {m: d for m, d in toks.items() if m not in quote_mints} or toks
    movers = sorted(cands.items(), key=lambda kv: (kv[0] not in pump_by_mint, -abs(kv[1]["delta"])))[:1]
    try:                                                 # v1.45: whale CU price for our fee match
        from app.live import whalefee as _wf
        cu_px = _wf.cu_price_from_tx(tx)
    except Exception:  # noqa: BLE001
        _wf, cu_px = None, None
    out: List[Dict[str, Any]] = []
    for n, (mint, d) in enumerate(movers):
        is_buy = d["delta"] > 0
        if is_buy and sol_delta >= 0 and not stables:
            continue                                     # tokens in, no SOL out: transfer/airdrop, not a buy
        if not is_buy and sol_delta <= 0 and not stables:
            continue                                     # tokens out, no SOL in: transfer, not a sell
        sol = abs(sol_delta) if n == 0 else 0
        if n == 0 and sol < MIN_SOL_LAMPORTS and not stables:
            continue
        dec = d.get("decimals") if isinstance(d.get("decimals"), int) else 6
        tok_ui = abs(d["delta"]) / (10 ** dec)
        pe = pump_by_mint.get(mint) or {}
        v = venue
        if pe and pe.get("quote_is_sol") is False and v == "pump_curve":
            v = "multi_hop→pump_curve"                   # SOL → quote coin → this coin, in one tx
        ev: Dict[str, Any] = {
            "kind": "wallet_trade", "mint": mint, "user": wallet, "is_buy": is_buy,
            "sol_amount": int(sol), "token_amount": int(abs(d["delta"]) * (10 ** (6 - dec))) if dec != 6 else abs(d["delta"]),
            "token_decimals": dec, "venue": v, "decode_path": "tx",
            "quote_mint": pe.get("quote_mint") or (WSOL_MINT if pe else None),
            "quote_is_sol": pe.get("quote_is_sol", True) if pe else None,
            "ix_name": pe.get("ix_name"), "event_version": pe.get("event_version"),
            "creator": pe.get("creator"),
            "price_sol": (sol / 1e9) / tok_ui if sol and tok_ui else None,
            "multi_mint": len(toks) > 1,
            "stable_leg": {m: s["delta"] for m, s in stables.items()} or None,
        }
        if pe:
            for k in ("virtual_sol_reserves", "virtual_token_reserves", "real_sol_reserves", "real_token_reserves",
                      "virtual_quote_reserves", "real_quote_reserves", "quote_amount", "mayhem_mode"):
                if k in pe:
                    ev[k] = pe[k]
        if pe and pe.get("quote_is_sol", True) and pe.get("virtual_token_reserves") and pe.get("virtual_sol_reserves"):
            ev["spot_sol"] = (pe["virtual_sol_reserves"] / 1e9) / (pe["virtual_token_reserves"] / 1e6)
        else:
            ev["spot_sol"] = ev["price_sol"]             # router / non-SOL quote: our best spot is the fill price
            ev["spot_from_fill"] = True
        ev["cu_price"] = cu_px
        if is_buy and _wf is not None and cu_px:
            _wf.note(mint, cu_px)
        out.append(ev)
    return out


def classify_route(tx: Optional[Dict[str, Any]], wallet: str, *, uva_pump: str, uva_amm: str) -> Dict[str, Any]:
    """Which of our triggers would see this tx: pump UVA logs sub, PumpSwap UVA logs sub, wallet lamports."""
    keys = account_keys(tx or {})
    evs = parse_all_logs(((tx or {}).get("meta") or {}).get("logMessages") or [])
    return {"pump_uva": uva_pump in keys, "amm_uva": uva_amm in keys,
            "pump_event_for_wallet": any(e.get("user") == wallet and e.get("kind") == "pump_trade" for e in evs),
            "amm_event_for_wallet": any(e.get("user") == wallet and e.get("kind", "").startswith("amm_") for e in evs),
            "programs": programs(tx or {})}
