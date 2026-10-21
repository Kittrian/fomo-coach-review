"""
Fomo-Live-Feed schema → coach buy events.

Contracts (novus77/Fomo-Live-Feed):
  Envelope: { type, topicType: "trading_activity", payload }
  Raw: type swap_buy|swap_sell|swap_withdraw|transfer_out|thesis + userId, ticker, …
  Canonical: TradeEventV1 (schemaVersion: 1, action: buy|sell|…)

Only buy actions are scored; others are soft-ignored (counted, not clustered).
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from app.state import BuyEvent

# Provisional network IDs from Live Feed catalog / handoff
NETWORK_TO_CHAIN: Dict[int, str] = {
    101: "solana",
    1399811149: "solana",
    1: "ethereum",
    56: "bsc",
    8453: "base",
    196: "x-layer",
    900001: "robinhood",
    4663: "robinhood",
    5042: "arc",
}

RAW_TYPE_TO_ACTION = {
    "swap_buy": "buy",
    "swap_sell": "sell",
    "swap_withdraw": "withdraw",
    "transfer_out": "transfer",
    "thesis": "thesis",
}

BUY_ACTIONS = {"buy"}


def _parse_iso_ms(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        # already epoch ms or sec
        v = int(value)
        return v if v > 10_000_000_000 else v * 1000
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            if s.isdigit():
                return _parse_iso_ms(int(s))
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return int(dt.timestamp() * 1000)
        except Exception:
            return None
    return None


def _chain_from_network(network_id: Any, chain_hint: Any = None) -> str:
    if isinstance(chain_hint, str) and chain_hint.strip():
        return chain_hint.strip().lower()
    try:
        nid = int(network_id)
    except (TypeError, ValueError):
        return "unknown"
    return NETWORK_TO_CHAIN.get(nid, "unknown")


def _unwrap_payload(obj: Dict[str, Any]) -> Dict[str, Any]:
    """Accept WS envelope or bare payload / TradeEventV1."""
    if (
        obj.get("topicType") == "trading_activity"
        and isinstance(obj.get("payload"), dict)
    ):
        return obj["payload"]
    if obj.get("type") == "data" and isinstance(obj.get("payload"), dict):
        # envelope without topicType still unwrap
        return obj["payload"]
    return obj


def _is_trade_event_v1(obj: Dict[str, Any]) -> bool:
    return obj.get("schemaVersion") == 1 and "action" in obj and "tokenAddress" in obj


def normalize_one(raw: Any) -> Tuple[Optional[BuyEvent], str, Optional[str]]:
    """
    Returns (buy_event_or_None, disposition, detail).
    disposition: ingested | ignored | invalid
    """
    if not isinstance(raw, dict):
        return None, "invalid", "not_an_object"

    obj = _unwrap_payload(raw)

    # --- TradeEventV1 ---
    if _is_trade_event_v1(obj):
        action = str(obj.get("action") or "").lower()
        if action not in BUY_ACTIONS:
            return None, "ignored", f"action:{action or 'missing'}"
        token = str(obj.get("tokenAddress") or "").strip()
        trader = str(obj.get("traderId") or "").strip()
        if not token or not trader:
            return None, "invalid", "missing_token_or_trader"
        occurred = _parse_iso_ms(obj.get("occurredAt")) or int(time.time() * 1000)
        ev = BuyEvent(
            trader_id=trader,
            trader_handle=str(obj.get("traderHandle") or trader),
            token_address=token,
            token_symbol=str(obj.get("tokenSymbol") or "").strip() or "?",
            chain=_chain_from_network(obj.get("networkId"), obj.get("chain")),
            usd_amount=_num(obj.get("usdAmount")),
            market_cap=_num(obj.get("marketCap")),
            price=_num(obj.get("price")),
            occurred_at_ms=occurred,
            source="fomo_live_feed:TradeEventV1",
            source_event_id=_sid(obj.get("sourceEventId") or obj.get("id")),
            meta={
                "delivery": obj.get("delivery"),
                "networkId": obj.get("networkId"),
                "thesis": obj.get("thesis"),
            },
        )
        return ev, "ingested", None

    # --- Raw trading_activity payload ---
    raw_type = str(obj.get("type") or "").strip()
    action = RAW_TYPE_TO_ACTION.get(raw_type)
    if action is None:
        # maybe already action-shaped without schemaVersion
        action = str(obj.get("action") or "").lower() or None
    if action is None:
        return None, "invalid", "unknown_type"
    if action not in BUY_ACTIONS:
        return None, "ignored", f"type:{raw_type or action}"

    token = str(obj.get("tokenAddress") or "").strip()
    trader = str(obj.get("userId") or obj.get("traderId") or "").strip()
    if not token or not trader:
        return None, "invalid", "missing_token_or_trader"

    occurred = (
        _parse_iso_ms(obj.get("createdAt"))
        or _parse_iso_ms(obj.get("occurredAt"))
        or int(time.time() * 1000)
    )
    comment = obj.get("comment")
    if isinstance(comment, dict):
        comment = comment.get("comment")

    ev = BuyEvent(
        trader_id=trader,
        trader_handle=str(obj.get("userHandle") or obj.get("traderHandle") or trader),
        token_address=token,
        token_symbol=str(obj.get("ticker") or obj.get("tokenSymbol") or "").strip() or "?",
        chain=_chain_from_network(obj.get("networkId"), obj.get("chain")),
        usd_amount=_num(obj.get("usdAmount")),
        market_cap=_num(obj.get("marketCap")),
        price=_num(obj.get("price")),
        occurred_at_ms=occurred,
        source="fomo_live_feed:raw",
        source_event_id=_sid(obj.get("id") or obj.get("tradeId")),
        meta={
            "raw_type": raw_type,
            "networkId": obj.get("networkId"),
            "comment": comment,
        },
    )
    return ev, "ingested", None


def _num(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        n = float(v)
        if n != n or n < 0:  # NaN / negative
            return None
        return n
    except (TypeError, ValueError):
        return None


def _sid(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s[:128] if s else None


def extract_items(body: Any) -> List[Any]:
    """Normalize request body into a list of event-like objects."""
    if body is None:
        return []
    if isinstance(body, list):
        return body
    if not isinstance(body, dict):
        return []
    for key in ("events", "activities", "items", "data"):
        if isinstance(body.get(key), list):
            return body[key]
    # single envelope / payload / TradeEventV1
    return [body]


def adapt_batch(body: Any) -> Dict[str, Any]:
    items = extract_items(body)
    ingested: List[Dict[str, Any]] = []
    ignored: List[Dict[str, Any]] = []
    invalid: List[Dict[str, Any]] = []

    for item in items:
        ev, disposition, detail = normalize_one(item)
        if disposition == "ingested" and ev is not None:
            ingested.append(
                {
                    "trader_id": ev.trader_id,
                    "trader_handle": ev.trader_handle,
                    "token_address": ev.token_address,
                    "token_symbol": ev.token_symbol,
                    "chain": ev.chain,
                    "usd_amount": ev.usd_amount,
                    "source": ev.source,
                    "source_event_id": ev.source_event_id,
                    "_event": ev,
                }
            )
        elif disposition == "ignored":
            ignored.append({"reason": detail})
        else:
            invalid.append({"reason": detail or "invalid"})

    return {
        "received": len(items),
        "ingested": ingested,
        "ignored_count": len(ignored),
        "invalid_count": len(invalid),
        "ignored": ignored[:20],
        "invalid": invalid[:20],
    }
