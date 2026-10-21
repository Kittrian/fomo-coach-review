"""Decode pump.fun / PumpSwap trade events from `Program data:` log lines and emit_cpi inner instructions.

pump.fun TradeEvent (official IDL, pump-fun/pump-public-docs idl/pump.json, checked 2026-10-07). Fields were
appended over time, so every field after `real_token_reserves` is optional and decoded only when present:
  [8] discriminator  189,219,127,211,78,230,97,238
  mint(32) sol_amount(u64) token_amount(u64) is_buy(bool) user(32) timestamp(i64)
  virtual_sol_reserves(u64) virtual_token_reserves(u64) real_sol_reserves(u64) real_token_reserves(u64)
  fee_recipient(32) fee_basis_points(u64) fee(u64) creator(32) creator_fee_basis_points(u64) creator_fee(u64)
  track_volume(bool) total_unclaimed_tokens(u64) total_claimed_tokens(u64) current_sol_volume(u64)
  last_update_timestamp(i64) ix_name(string) mayhem_mode(bool) cashback_fee_basis_points(u64) cashback(u64)
  buyback_fee_basis_points(u64) buyback_fee(u64) shareholders(vec<{pubkey,u16}>) quote_mint(32)
  quote_amount(u64) virtual_quote_reserves(u64) real_quote_reserves(u64) holder_rewards_bps(u64)
  holder_rewards(u64) creator_fee_unclaimed(u64)

v1.16: coins can be quoted in a non-SOL mint (USDC, another pump coin, an xStock). For those the event carries
sol_amount = 0 and virtual_sol_reserves = 0, and the real size is in quote_amount (quote-mint units). The old
decoder read them as "0.00 SOL" buys. `quote_is_sol` tells the caller which case it is; the SOL value of a
non-SOL-quote trade has to come from the transaction's balance changes (see app/scout/txparse.py).

PumpSwap BuyEvent / SellEvent (idl/pump_amm.json) are decoded too (no mint in the event: the caller resolves the
mint from the transaction's token balances).
"""
from __future__ import annotations

import base64
import binascii
import struct
from typing import Any, Dict, Iterable, List, Optional

from app.pump.solana_pda import b58decode, b58encode

TRADE_EVENT_DISCRIMINATOR = bytes([189, 219, 127, 211, 78, 230, 97, 238])
AMM_BUY_EVENT_DISCRIMINATOR = bytes([103, 244, 82, 31, 44, 245, 119, 119])
AMM_SELL_EVENT_DISCRIMINATOR = bytes([62, 47, 55, 10, 165, 3, 220, 42])
EMIT_CPI_TAG = bytes.fromhex("e445a52e51cb9a1d")   # sha256("anchor:event")[:8], Anchor emit_cpi! self-CPI prefix
WSOL_MINT = "So11111111111111111111111111111111111111112"
ZERO_KEY = b58encode(bytes(32))
_MIN_LEN = 129
_CREATOR_END = 209


class _R:
    def __init__(self, raw: bytes, o: int) -> None:
        self.raw, self.o = raw, o

    def need(self, n: int) -> bool:
        return self.o + n <= len(self.raw)

    def u64(self) -> int:
        (v,) = struct.unpack_from("<Q", self.raw, self.o)
        self.o += 8
        return v

    def i64(self) -> int:
        (v,) = struct.unpack_from("<q", self.raw, self.o)
        self.o += 8
        return v

    def u32(self) -> int:
        (v,) = struct.unpack_from("<I", self.raw, self.o)
        self.o += 4
        return v

    def i128(self) -> int:
        v = int.from_bytes(self.raw[self.o:self.o + 16], "little", signed=True)
        self.o += 16
        return v

    def b(self) -> bool:
        v = bool(self.raw[self.o])
        self.o += 1
        return v

    def key(self) -> str:
        v = b58encode(self.raw[self.o:self.o + 32])
        self.o += 32
        return v


def decode_trade_event(raw: bytes) -> Optional[Dict[str, Any]]:
    """Any TradeEvent version. Returns None only when it is not a TradeEvent or shorter than the v1 prefix."""
    if len(raw) < _MIN_LEN or raw[:8] != TRADE_EVENT_DISCRIMINATOR:
        return None
    r = _R(raw, 8)
    mint = r.key()
    sol_amount, token_amount = r.u64(), r.u64()
    is_buy = r.b()
    user = r.key()
    ts = r.i64()
    vsr, vtr, rsr, rtr = r.u64(), r.u64(), r.u64(), r.u64()
    ev: Dict[str, Any] = {
        "kind": "pump_trade", "mint": mint, "sol_amount": sol_amount, "token_amount": token_amount, "is_buy": is_buy,
        "user": user, "timestamp": ts,
        "virtual_sol_reserves": vsr, "virtual_token_reserves": vtr,
        "real_sol_reserves": rsr, "real_token_reserves": rtr, "event_version": "v1",
    }
    try:
        if not r.need(32 + 16 + 32 + 16):
            return _finish(ev)
        r.o += 32 + 16                       # fee_recipient, fee_basis_points, fee
        ev["creator"] = r.key()
        r.o += 16                            # creator_fee_basis_points, creator_fee
        if not r.need(1 + 8 * 4 + 8 + 4):
            return _finish(ev)
        ev["track_volume"] = r.b()
        r.o += 8 * 3                         # total_unclaimed_tokens, total_claimed_tokens, current_sol_volume
        r.i64()                              # last_update_timestamp
        n = r.u32()
        if n > 64 or not r.need(n):
            return _finish(ev)
        ev["ix_name"] = raw[r.o:r.o + n].decode("utf-8", "replace")
        r.o += n
        ev["event_version"] = "v1.x"
        if not r.need(1 + 32):
            return _finish(ev)
        ev["mayhem_mode"] = r.b()
        r.o += 32                            # cashback bps, cashback, buyback bps, buyback fee
        if not r.need(4):
            return _finish(ev)
        n = r.u32()
        if n > 256 or not r.need(n * 34 + 32 + 24):
            return _finish(ev)
        r.o += n * 34                        # shareholders: pubkey + u16
        ev["quote_mint"] = r.key()
        ev["quote_amount"], ev["virtual_quote_reserves"], ev["real_quote_reserves"] = r.u64(), r.u64(), r.u64()
        ev["event_version"] = "v2"
    except (struct.error, IndexError):
        pass
    return _finish(ev)


def _finish(ev: Dict[str, Any]) -> Dict[str, Any]:
    q = ev.get("quote_mint")
    ev["quote_is_sol"] = q in (None, WSOL_MINT, ZERO_KEY)
    return ev


def decode_amm_event(raw: bytes) -> Optional[Dict[str, Any]]:
    """PumpSwap BuyEvent / SellEvent prefix (amounts, pool, user). The base mint is NOT in the event."""
    d = raw[:8]
    if d not in (AMM_BUY_EVENT_DISCRIMINATOR, AMM_SELL_EVENT_DISCRIMINATOR):
        return None
    is_buy = d == AMM_BUY_EVENT_DISCRIMINATOR
    r = _R(raw, 8)
    try:
        ts = r.i64()
        base_amount = r.u64()
        r.u64()                              # max_quote_amount_in | min_quote_amount_out
        r.o += 16                            # user base/quote token reserves
        pool_base, pool_quote = r.u64(), r.u64()
        quote_amount = r.u64()               # quote_amount_in | quote_amount_out
        r.o += 8 * 4                         # lp fee bps, lp fee, protocol fee bps, protocol fee
        r.o += 8 * 2                         # quote_amount_in_with_lp_fee/user_quote_amount_in | ...without_lp_fee/user_quote_amount_out
        pool = r.key()
        user = r.key()
    except (struct.error, IndexError):
        return None
    return {"kind": "amm_buy" if is_buy else "amm_sell", "is_buy": is_buy, "timestamp": ts, "user": user,
            "pool": pool, "base_amount": base_amount, "quote_amount": quote_amount,
            "pool_base_reserves": pool_base, "pool_quote_reserves": pool_quote}


def decode_any(raw: bytes) -> Optional[Dict[str, Any]]:
    if raw[:8] == EMIT_CPI_TAG:
        raw = raw[8:]
    return decode_trade_event(raw) or decode_amm_event(raw)


def _program_data(logs: Iterable[str]) -> Iterable[bytes]:
    for line in logs or []:
        if not isinstance(line, str) or not line.startswith("Program data: "):
            continue
        try:
            yield base64.b64decode(line[len("Program data: "):].strip())
        except (binascii.Error, ValueError):
            continue


def parse_logs(logs: Iterable[str], mint: Optional[str] = None) -> List[Dict[str, Any]]:
    """pump.fun TradeEvents only (all versions), optionally filtered to one mint."""
    out: List[Dict[str, Any]] = []
    for raw in _program_data(logs):
        ev = decode_trade_event(raw)
        if ev and (mint is None or ev["mint"] == mint):
            out.append(ev)
    return out


def parse_all_logs(logs: Iterable[str]) -> List[Dict[str, Any]]:
    """pump.fun TradeEvents + PumpSwap Buy/Sell events from log lines."""
    out: List[Dict[str, Any]] = []
    for raw in _program_data(logs):
        ev = decode_trade_event(raw) or decode_amm_event(raw)
        if ev:
            out.append(ev)
    return out


def parse_inner_events(meta: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """emit_cpi events from a getTransaction(json) result's innerInstructions (survives log truncation)."""
    out: List[Dict[str, Any]] = []
    for grp in (meta or {}).get("innerInstructions") or []:
        for ix in grp.get("instructions") or []:
            data = ix.get("data")
            if not isinstance(data, str) or len(data) < 20:
                continue
            try:
                raw = b58decode(data)
            except ValueError:
                continue
            if raw[:8] != EMIT_CPI_TAG:
                continue
            ev = decode_any(raw)
            if ev:
                out.append(ev)
    return out


def logs_truncated(logs: Iterable[str]) -> bool:
    return any(isinstance(l, str) and l.startswith("Log truncated") for l in logs or [])


def logs_show_migration(logs: Iterable[str]) -> bool:
    return any(isinstance(l, str) and "Instruction: Migrate" in l for l in logs or [])


def event_price_sol(ev: Dict[str, Any]) -> Optional[float]:
    """Curve spot price in SOL per token. None for non-SOL-quote coins (reserves are in quote units)."""
    if not ev.get("quote_is_sol", True):
        return None
    vtr, vsr = ev.get("virtual_token_reserves") or 0, ev.get("virtual_sol_reserves") or 0
    if vtr <= 0 or vsr <= 0:
        return None
    return (vsr / 1e9) / (vtr / 1e6)


def encode_trade_event_for_tests(**kw: Any) -> bytes:
    """Build a synthetic TradeEvent payload (tests only). `v2=True` appends the full v2 tail (ix_name, quote)."""
    from app.pump.solana_pda import pubkey_bytes
    z = bytes(32)
    b = bytearray(TRADE_EVENT_DISCRIMINATOR)
    b += pubkey_bytes(kw["mint"])
    b += struct.pack("<QQ", kw.get("sol_amount", 0), kw.get("token_amount", 0))
    b += bytes([1 if kw.get("is_buy", True) else 0])
    b += pubkey_bytes(kw["user"])
    b += struct.pack("<q", kw.get("timestamp", 0))
    b += struct.pack("<QQQQ", kw.get("vsr", 30 * 10**9), kw.get("vtr", 1_073_000_000 * 10**6),
                     kw.get("rsr", 0), kw.get("rtr", 793_100_000 * 10**6))
    b += z + struct.pack("<QQ", 95, 0)
    b += pubkey_bytes(kw["creator"]) if kw.get("creator") else z
    b += struct.pack("<QQ", 30, 0)
    if kw.get("v2"):
        name = kw.get("ix_name", "buy_v3").encode()
        b += bytes([1]) + struct.pack("<QQQ", 0, 0, 0) + struct.pack("<q", 0)
        b += struct.pack("<I", len(name)) + name
        b += bytes([0]) + struct.pack("<QQQQ", 0, 0, 0, 0)
        shs = kw.get("shareholders", 1)
        b += struct.pack("<I", shs) + (z + struct.pack("<H", 100)) * shs
        b += pubkey_bytes(kw.get("quote_mint", WSOL_MINT))
        b += struct.pack("<QQQ", kw.get("quote_amount", kw.get("sol_amount", 0)), kw.get("vqr", kw.get("vsr", 30 * 10**9)),
                         kw.get("rqr", kw.get("rsr", 0)))
        b += struct.pack("<QQQ", 0, 0, 0)
    return bytes(b)


def encode_amm_event_for_tests(*, is_buy: bool, user: str, pool: str, base_amount: int, quote_amount: int) -> bytes:
    from app.pump.solana_pda import pubkey_bytes
    b = bytearray(AMM_BUY_EVENT_DISCRIMINATOR if is_buy else AMM_SELL_EVENT_DISCRIMINATOR)
    b += struct.pack("<q", 0) + struct.pack("<QQ", base_amount, 0) + struct.pack("<QQ", 0, 0)
    b += struct.pack("<QQ", 10**15, 10**11) + struct.pack("<Q", quote_amount) + struct.pack("<QQQQ", 0, 0, 0, 0)
    b += struct.pack("<QQ", 0, 0) + pubkey_bytes(pool) + pubkey_bytes(user) + bytes(32 * 4)
    return bytes(b)
