"""Pure-Python Solana helpers: base58, ed25519 on-curve check, PDA + ATA derivation.

No solders dependency. Matches solana-program `Pubkey::find_program_address`:
sha256(seeds || [bump] || program_id || b"ProgramDerivedAddress") must be OFF the ed25519 curve.
"""
from __future__ import annotations

import functools
import hashlib
from typing import Iterable, Tuple

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_IDX = {c: i for i, c in enumerate(_B58)}

PUMP_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMP_AMM_PROGRAM_ID = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
PUMP_GLOBAL = "4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf"
WSOL_MINT = "So11111111111111111111111111111111111111112"
TOKEN_PROGRAM_ID = "REDACTED"
TOKEN_2022_PROGRAM_ID = "REDACTED"
ATA_PROGRAM_ID = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"


def b58decode(s: str) -> bytes:
    n = 0
    for ch in s:
        if ch not in _B58_IDX:
            raise ValueError(f"invalid base58 char {ch!r}")
        n = n * 58 + _B58_IDX[ch]
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = len(s) - len(s.lstrip("1"))
    return b"\x00" * pad + body


def b58encode(b: bytes) -> str:
    n = int.from_bytes(b, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    pad = len(b) - len(b.lstrip(b"\x00"))
    return "1" * pad + out


def pubkey_bytes(s: str) -> bytes:
    b = b58decode(s.strip())
    if len(b) != 32:
        raise ValueError(f"not a 32-byte pubkey (got {len(b)} bytes)")
    return b


def is_pubkey(s: str) -> bool:
    try:
        pubkey_bytes(s)
        return True
    except Exception:  # noqa: BLE001
        return False


# --- ed25519 point decompression (on-curve test) ---
_P = 2**255 - 19
_D = (-121665 * pow(121666, _P - 2, _P)) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)


def is_on_curve(b: bytes) -> bool:
    if len(b) != 32:
        return False
    y = int.from_bytes(b, "little") & ((1 << 255) - 1)
    sign = b[31] >> 7
    if y >= _P:
        return False
    y2 = y * y % _P
    u = (y2 - 1) % _P
    v = (_D * y2 + 1) % _P
    x2 = u * pow(v, _P - 2, _P) % _P
    if x2 == 0:
        return sign == 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P != 0:
        x = x * _SQRT_M1 % _P
        if (x * x - x2) % _P != 0:
            return False
    return True


def create_program_address(seeds: Iterable[bytes], program_id: str) -> bytes:
    h = hashlib.sha256()
    for s in seeds:
        if len(s) > 32:
            raise ValueError("seed too long")
        h.update(s)
    h.update(pubkey_bytes(program_id))
    h.update(b"ProgramDerivedAddress")
    d = h.digest()
    if is_on_curve(d):
        raise ValueError("on curve")
    return d


def find_program_address(seeds: Iterable[bytes], program_id: str) -> Tuple[str, int]:
    return _fpa_cached(tuple(bytes(s) for s in seeds), program_id)    # v1.40b (ms-audit C): 0.7 ms -> ~1 us on repeat


@functools.lru_cache(maxsize=65536)
def _fpa_cached(seeds: Tuple[bytes, ...], program_id: str) -> Tuple[str, int]:
    seeds = list(seeds)
    for bump in range(255, -1, -1):
        try:
            return b58encode(create_program_address(seeds + [bytes([bump])], program_id)), bump
        except ValueError:
            continue
    raise ValueError("no viable bump")


def bonding_curve_pda(mint: str) -> str:
    return find_program_address([b"bonding-curve", pubkey_bytes(mint)], PUMP_PROGRAM_ID)[0]


def associated_token_address(owner: str, mint: str, token_program: str = TOKEN_PROGRAM_ID) -> str:
    return find_program_address(
        [pubkey_bytes(owner), pubkey_bytes(token_program), pubkey_bytes(mint)], ATA_PROGRAM_ID
    )[0]


def pool_authority_pda(mint: str) -> str:
    return find_program_address([b"pool-authority", pubkey_bytes(mint)], PUMP_PROGRAM_ID)[0]


def canonical_pumpswap_pool(mint: str, quote_mint: str = WSOL_MINT) -> str:
    """Pool a pump.fun migration creates: PDA(["pool", u16 0, pool_authority, base, quote]) under pump-amm."""
    return find_program_address(
        [b"pool", (0).to_bytes(2, "little"), pubkey_bytes(pool_authority_pda(mint)),
         pubkey_bytes(mint), pubkey_bytes(quote_mint)],
        PUMP_AMM_PROGRAM_ID,
    )[0]
