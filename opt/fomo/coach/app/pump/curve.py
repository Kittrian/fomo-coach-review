"""Direct on-chain pump.fun bonding-curve reader (read-only, no keys, no paid services).

BondingCurve account (after the 8-byte Anchor discriminator), per the official IDL in
pump-fun/pump-public-docs and chainstack's protocol notes (Apache-2.0):
  u64 virtual_token_reserves, u64 virtual_quote_reserves (was virtual_sol_reserves),
  u64 real_token_reserves, u64 real_quote_reserves (was real_sol_reserves),
  u64 token_total_supply, bool complete, pubkey creator            (73 bytes)
  bool is_mayhem_mode                                               (74)
  bool is_cashback_coin                                             (75)
  pubkey quote_mint  (all-zero == SOL)                              (107)
  u64 creator_fee_bps, bool can_edit_creator_fee, bool is_holder_reward  (create_v2 → 125 total)
Accounts can be extended past 125 bytes — never filter on length; decode what is present.

After graduation (`complete`), liquidity lives in the canonical PumpSwap pool; its price uses
EFFECTIVE quote reserves = quote_vault.amount + Pool.virtual_quote_reserves (i128 at offset 245,
which CAN BE NEGATIVE since 2026-09-30 per pump-public-docs).
"""
from __future__ import annotations

import asyncio
import struct
import time
from typing import Any, Dict, Optional

import httpx

from app.pump import rpc
from app.pump.solana_pda import (
    PUMP_AMM_PROGRAM_ID, PUMP_GLOBAL, PUMP_PROGRAM_ID, WSOL_MINT, b58encode,
    bonding_curve_pda, canonical_pumpswap_pool, is_pubkey,
)

CURVE_DISCRIMINATOR = struct.pack("<Q", 6966180631402821399)
TOKEN_DECIMALS = 6
SOL_DECIMALS = 9
DEFAULT_INITIAL_REAL_TOKEN_RESERVES = 793_100_000 * 10**TOKEN_DECIMALS  # fallback only
GLOBAL_INITIAL_REAL_TOKEN_RESERVES_OFFSET = 89
POOL_BASE_VAULT_OFFSET = 139
POOL_QUOTE_VAULT_OFFSET = 171
POOL_COIN_CREATOR_OFFSET = 211
POOL_VIRTUAL_QUOTE_OFFSET = 245  # i128, signed
TOKEN_ACCOUNT_AMOUNT_OFFSET = 64
ZERO_KEY = b58encode(bytes(32))

_global_cache: Dict[str, Any] = {"value": None, "at": 0.0}
_curve_cache: Dict[str, Dict[str, Any]] = {}

# v1.33 speed: one keep-alive client (no TLS handshake per read); warm() pre-opens it
_shared_client: Optional[httpx.AsyncClient] = None


def shared_client() -> httpx.AsyncClient:
    global _shared_client
    if _shared_client is None or _shared_client.is_closed:
        _shared_client = httpx.AsyncClient(timeout=3.0, limits=httpx.Limits(max_keepalive_connections=8, keepalive_expiry=120))
    return _shared_client


async def warm_loop(interval_s: float = 25.0) -> None:
    """Keep the RPC connection hot (read-only getHealth/getSlot)."""
    while True:
        try:
            await rpc.call("getSlot", [{"commitment": "processed"}], client=shared_client(), timeout=3.0)
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(interval_s)
CURVE_CACHE_TTL_S = 1.5


def decode_curve(data: bytes) -> Dict[str, Any]:
    if len(data) < 8 + 41 or data[:8] != CURVE_DISCRIMINATOR:
        raise ValueError("not a pump.fun BondingCurve account")
    vtr, vqr, rtr, rqr, supply = struct.unpack_from("<QQQQQ", data, 8)
    n = len(data) - 8
    out: Dict[str, Any] = {
        "virtual_token_reserves": vtr,
        "virtual_quote_reserves": vqr,
        "real_token_reserves": rtr,
        "real_quote_reserves": rqr,
        "token_total_supply": supply,
        "complete": bool(data[48]),
        "account_len": len(data),
    }
    if n >= 73:
        out["creator"] = b58encode(data[49:81])
    if n >= 74:
        out["is_mayhem_mode"] = bool(data[81])
    if n >= 75:
        out["is_cashback_coin"] = bool(data[82])
    qm = WSOL_MINT
    if n >= 107:
        raw_qm = b58encode(data[83:115])
        qm = WSOL_MINT if raw_qm == ZERO_KEY else raw_qm
    out["quote_mint"] = qm
    out["quote_is_sol"] = qm == WSOL_MINT
    if n >= 117:  # create_v2 layout (125 bytes); older 151-byte accounts read zero padding here
        out["creator_fee_bps"] = struct.unpack_from("<Q", data, 115)[0]
        out["is_holder_reward"] = bool(data[124])
    return out


def curve_price_sol(c: Dict[str, Any]) -> Optional[float]:
    """Spot price in quote units (SOL) per whole token from virtual reserves."""
    vtr, vqr = c.get("virtual_token_reserves") or 0, c.get("virtual_quote_reserves") or 0
    if vtr <= 0 or vqr <= 0:
        return None
    return (vqr / 10**SOL_DECIMALS) / (vtr / 10**TOKEN_DECIMALS)


def buy_tokens_out(c: Dict[str, Any], sol_in_lamports: int, fee_bps: int = 125) -> int:
    """Constant-product curve buy simulation (raw token units) after protocol+creator fee."""
    vtr, vqr = int(c["virtual_token_reserves"]), int(c["virtual_quote_reserves"])
    net = sol_in_lamports * (10_000 - fee_bps) // 10_000
    if net <= 0 or vtr <= 0:
        return 0
    k = vtr * vqr
    new_vtr = k // (vqr + net) + 1
    out = vtr - new_vtr
    rtr = int(c.get("real_token_reserves") or out)
    return max(0, min(out, rtr))


def sell_sol_out(c: Dict[str, Any], tokens_raw: int, fee_bps: int = 125) -> int:
    """Constant-product curve sell simulation (lamports) after fee."""
    vtr, vqr = int(c["virtual_token_reserves"]), int(c["virtual_quote_reserves"])
    if tokens_raw <= 0 or vtr <= 0:
        return 0
    k = vtr * vqr
    gross = vqr - (k // (vtr + tokens_raw) + 1)
    gross = max(0, min(gross, int(c.get("real_quote_reserves") or gross)))
    return gross * (10_000 - fee_bps) // 10_000


def decode_pool(data: bytes) -> Dict[str, Any]:
    if len(data) < POOL_VIRTUAL_QUOTE_OFFSET + 16:
        raise ValueError(f"pool account too short ({len(data)})")
    return {
        "base_vault": b58encode(data[POOL_BASE_VAULT_OFFSET:POOL_BASE_VAULT_OFFSET + 32]),
        "quote_vault": b58encode(data[POOL_QUOTE_VAULT_OFFSET:POOL_QUOTE_VAULT_OFFSET + 32]),
        "coin_creator": b58encode(data[POOL_COIN_CREATOR_OFFSET:POOL_COIN_CREATOR_OFFSET + 32]),
        "virtual_quote_reserves": int.from_bytes(
            data[POOL_VIRTUAL_QUOTE_OFFSET:POOL_VIRTUAL_QUOTE_OFFSET + 16], "little", signed=True),
    }


def token_amount(data: bytes) -> int:
    return struct.unpack_from("<Q", data, TOKEN_ACCOUNT_AMOUNT_OFFSET)[0]


async def initial_real_token_reserves(client: httpx.AsyncClient) -> int:
    if _global_cache["value"] and time.time() - _global_cache["at"] < 3600:
        return _global_cache["value"]
    try:
        acc = await rpc.get_account(PUMP_GLOBAL, client=client)
        v = struct.unpack_from("<Q", acc["data"], GLOBAL_INITIAL_REAL_TOKEN_RESERVES_OFFSET)[0] if acc else 0
    except Exception:  # noqa: BLE001
        v = 0
    v = v or DEFAULT_INITIAL_REAL_TOKEN_RESERVES
    _global_cache.update(value=v, at=time.time())
    return v


async def read_pumpswap_pool(mint: str, quote_mint: str, client: httpx.AsyncClient) -> Dict[str, Any]:
    pool = canonical_pumpswap_pool(mint, quote_mint)
    acc = await rpc.get_account(pool, client=client)
    if not acc or acc.get("owner") != PUMP_AMM_PROGRAM_ID:
        return {"status": "not_found", "pool": pool}
    p = decode_pool(acc["data"])
    vaults = await rpc.get_multiple_accounts([p["base_vault"], p["quote_vault"]], client=client)
    if not all(vaults):
        return {"status": "vaults_missing", "pool": pool}
    base, quote_raw = token_amount(vaults[0]["data"]), token_amount(vaults[1]["data"])
    eff = quote_raw + p["virtual_quote_reserves"]  # signed add; protocol guarantees eff >= 0
    price = (eff / 10**SOL_DECIMALS) / (base / 10**TOKEN_DECIMALS) if base > 0 and eff > 0 else None
    return {
        "status": "ok", "pool": pool, "base_reserve": base, "quote_vault_amount": quote_raw,
        "virtual_quote_reserves": p["virtual_quote_reserves"], "effective_quote_reserves": eff,
        "price_sol": price, "coin_creator": p["coin_creator"],
    }


async def read_curve(mint: str, *, client: Optional[httpx.AsyncClient] = None,
                     use_cache: bool = True, timeout: float = 3.0) -> Dict[str, Any]:
    """Read + decode the bonding curve for `mint`. Never raises; returns {status,...}."""
    t0 = time.perf_counter()
    mint = (mint or "").strip()
    if not is_pubkey(mint):
        return {"status": "invalid_mint", "mint": mint}
    hit = _curve_cache.get(mint)
    if use_cache and hit and time.time() - hit["_at"] < CURVE_CACHE_TTL_S:
        return {**{k: v for k, v in hit.items() if k != "_at"}, "cached": True}
    own = False                     # v1.33: shared keep-alive client is never closed per call
    c = client or shared_client()
    try:
        pda = bonding_curve_pda(mint)
        acc = await rpc.get_account(pda, client=c, timeout=timeout)
        base: Dict[str, Any] = {"mint": mint, "bonding_curve": pda, "rpc": rpc.rpc_label(),
                                "read_only": True}
        if not acc:
            res = {**base, "status": "not_pump_curve", "note": "no bonding-curve account (not a pump.fun coin)"}
        elif acc.get("owner") != PUMP_PROGRAM_ID:
            res = {**base, "status": "not_pump_curve", "note": "account not owned by pump program"}
        else:
            cv = decode_curve(acc["data"])
            init_rtr = await initial_real_token_reserves(c)
            progress = None
            if not cv["complete"] and init_rtr:
                progress = max(0.0, min(100.0, 100.0 * (1 - cv["real_token_reserves"] / init_rtr)))
            price = curve_price_sol(cv) if not cv["complete"] else None
            if not cv["quote_is_sol"]:
                # non-SOL quote curve (e.g. USDC-quoted): reserves aren't lamports → no SOL price/sim
                price = None
            res = {
                **base, **cv, "status": "ok",
                "phase": "migrated" if cv["complete"] else "curve",
                "price_sol": price,
                "real_sol_reserves_sol": (cv["real_quote_reserves"] / 10**SOL_DECIMALS) if cv["quote_is_sol"] else None,
                "progress_pct": 100.0 if cv["complete"] else progress,
                "mcap_sol": (price * cv["token_total_supply"] / 10**TOKEN_DECIMALS) if price else None,
            }
            if cv["complete"]:
                try:
                    res["pumpswap"] = await read_pumpswap_pool(mint, cv["quote_mint"], c)
                    if not cv["quote_is_sol"]:
                        # v1.16.1: non-SOL pool reserves are quote units, not lamports → no SOL price from them
                        res["pumpswap"]["price_sol"] = None
                    if res["pumpswap"].get("price_sol"):
                        res["price_sol"] = res["pumpswap"]["price_sol"]
                        res["mcap_sol"] = res["price_sol"] * cv["token_total_supply"] / 10**TOKEN_DECIMALS
                except Exception as exc:  # noqa: BLE001
                    res["pumpswap"] = {"status": "error", "error": str(exc)[:160]}
        res["elapsed_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        res["read_at_ms"] = int(time.time() * 1000)
        if res["status"] in ("ok", "not_pump_curve"):
            _curve_cache[mint] = {**res, "_at": time.time()}
        return res
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "mint": mint, "error": str(exc)[:200], "rpc": rpc.rpc_label(),
                "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1)}
    finally:
        if own:
            await c.aclose()


def curve_summary(c: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Small card-friendly view."""
    if not c:
        return {"status": "missing"}
    keys = ("status", "phase", "price_sol", "mcap_sol", "progress_pct", "real_sol_reserves_sol",
            "complete", "creator", "quote_is_sol", "bonding_curve", "elapsed_ms", "rpc", "note", "error")
    return {k: c.get(k) for k in keys if k in c}


def run_sync(mint: str) -> Dict[str, Any]:
    return asyncio.run(read_curve(mint, use_cache=False))
