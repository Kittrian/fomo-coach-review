"""POST Solana CA announces from Telegram into FOMO Signal Coach (recommend-only).

Entry rule: ingest on CA / announce posts, not sell/dip phrases.
Mesh + JEV gate run inside the coach; Jupiter exec stays off.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Optional


def coach_base() -> str:
    return os.getenv("COACH_INGEST_URL", "http://127.0.0.1:8790").rstrip("/")


def source_for(handle: str) -> str:
    """Coach source tag. SmugCalls → arm C control feed (paper only; never the Fluffy CA fast lane)."""
    h = (handle or "").lower()
    if "fluff" in h:
        return "fluffy/telegram"
    if "smugcall" in h or "smug calls" in h:
        return "smugcalls/telegram"
    return "telegram"


def build_ingest_body(
    *,
    mint: str,
    symbol: Optional[str],
    source_group: str,
    text: str,
    message_id: Any = None,
    chat_id: Any = None,
    pump_at_iso: Optional[str] = None,
    tg_recv_ms: Optional[int] = None,
    tg_message_ms: Optional[int] = None,
    first_seen: Optional[bool] = None,
    repeat_count: Optional[int] = None,
) -> dict[str, Any]:
    """Pure payload builder for POST /api/ingest/buy (no network)."""
    handle = (source_group or "telegram").strip()[:64] or "telegram"
    trader_id = f"tg:{handle}".lower().replace(" ", "_")
    return {
        "trader_id": trader_id,
        "trader_handle": handle,
        "token_address": mint,
        "token_symbol": (symbol or "?").strip()[:32],
        "chain": "solana",
        "usd_amount": None,
        "market_cap": None,
        "price": None,
        "source": source_for(handle),
        "source_event_id": f"{chat_id}:{message_id}" if message_id is not None else None,
        "meta": {
            "source_group": source_group,
            "telegram_message_id": message_id,
            "telegram_chat_id": chat_id,
            "entry_rule": "ca_announce_not_dip",
            "pump_at_iso": pump_at_iso,
            "snippet": (text or "")[:280],
            "tg_recv_ms": tg_recv_ms,
            "tg_message_ms": tg_message_ms,
            # coach v1.15 Telegram pre-buy gate: refuses a live buy unless this is the watcher's first sighting
            "first_seen": first_seen,
            "repeat_count": repeat_count,
        },
    }


def post_ca_announce(
    *,
    mint: str,
    symbol: Optional[str],
    source_group: str,
    text: str,
    message_id: Any = None,
    chat_id: Any = None,
    sell_now: bool = False,
    pump_at_iso: Optional[str] = None,
    tg_recv_ms: Optional[int] = None,
    tg_message_ms: Optional[int] = None,
    first_seen: Optional[bool] = None,
    repeat_count: Optional[int] = None,
) -> dict[str, Any]:
    """POST /api/ingest/buy — CA announce as a synthetic buy cluster seed.

    Returns {"ok": bool, ...}. Soft-fails on network errors (never raises to Telethon loop).
    """
    if sell_now:
        return {"ok": False, "skipped": True, "reason": "sell_now_not_entry"}
    if not mint:
        return {"ok": False, "skipped": True, "reason": "no_mint"}

    body = build_ingest_body(
        mint=mint, symbol=symbol, source_group=source_group, text=text, message_id=message_id,
        chat_id=chat_id, pump_at_iso=pump_at_iso, tg_recv_ms=tg_recv_ms, tg_message_ms=tg_message_ms,
        first_seen=first_seen, repeat_count=repeat_count,
    )
    url = f"{coach_base()}/api/ingest/buy"
    data = json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json", "User-Agent": "fomo-telegram-pump/coach-bridge"}
    secret = os.getenv("COACH_INGEST_SECRET", "").strip() or os.getenv("INGEST_SECRET", "").strip()
    if secret:
        headers["X-Ingest-Secret"] = secret
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                out = json.loads(raw)
            except json.JSONDecodeError:
                out = {"ok": resp.status < 400, "raw": raw[:200]}
            out.setdefault("http_status", getattr(resp, "status", None))
            return out
    except urllib.error.HTTPError as exc:
        body_txt = exc.read().decode("utf-8", errors="replace")[:240]
        return {"ok": False, "http_status": exc.code, "error": body_txt}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:160]}"}
