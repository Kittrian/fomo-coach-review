"""v1.45 whale compute-unit price cache (read from the copied wallet's own tx; no extra RPC).

txparse.parse_wallet_trades notes the CU price (micro-lamports/CU) of every watched-wallet BUY it decodes; pumpdirect
reads it for the same mint when building our buy. Off switch: LIVE_MATCH_WHALE_CU=false."""
from __future__ import annotations

import collections
import time
from typing import Any, Dict, Optional

COMPUTE_BUDGET = "ComputeBudget111111111111111111111111111111"
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_cache: "collections.OrderedDict[str, tuple]" = collections.OrderedDict()


def _b58decode(s: str) -> bytes:
    n = 0
    for ch in s:
        n = n * 58 + _B58.index(ch)
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = len(s) - len(s.lstrip("1"))
    return b"\x00" * pad + raw


def cu_price_from_tx(tx: Optional[Dict[str, Any]]) -> Optional[int]:
    """SetComputeUnitPrice (tag 3, u64 LE micro-lamports) from a json / jsonParsed tx. None if absent/unreadable."""
    try:
        msg = ((tx or {}).get("transaction") or {}).get("message") or {}
        keys = [k if isinstance(k, str) else (k or {}).get("pubkey") for k in msg.get("accountKeys") or []]
        for ix in msg.get("instructions") or []:
            prog = ix.get("programId")
            if prog is None and isinstance(ix.get("programIdIndex"), int) and ix["programIdIndex"] < len(keys):
                prog = keys[ix["programIdIndex"]]
            if prog != COMPUTE_BUDGET:
                continue
            parsed = ix.get("parsed")
            if isinstance(parsed, dict) and parsed.get("type") == "setComputeUnitPrice":
                return int((parsed.get("info") or {}).get("microLamports") or 0) or None
            d = ix.get("data")
            if isinstance(d, str) and d:
                b = _b58decode(d)
                if len(b) >= 9 and b[0] == 3:
                    return int.from_bytes(b[1:9], "little") or None
    except Exception:  # noqa: BLE001
        return None
    return None


def note(mint: str, cu_price: Optional[int]) -> None:
    if not mint or not cu_price or cu_price <= 0:
        return
    _cache[mint] = (time.time(), int(cu_price))
    _cache.move_to_end(mint)
    while len(_cache) > 2000:
        _cache.popitem(last=False)


def get(mint: str, max_age_s: float = 30.0) -> Optional[int]:
    v = _cache.get(mint)
    if not v or time.time() - v[0] > max_age_s:
        return None
    return v[1]
