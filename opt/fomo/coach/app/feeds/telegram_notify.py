"""Soft Telegram alerts — log when unset; never crash the coach."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.config import settings

if TYPE_CHECKING:
    from app.scoring import Signal

logger = logging.getLogger("coach.telegram")


def notify_signal(sig: "Signal") -> None:
    """If score ≥ TELEGRAM_MIN_SCORE, notify (or log would-notify)."""
    if sig.score < settings.telegram_min_score:
        return

    msg = (
        f"Signal Coach recommend-only\n"
        f"${sig.symbol} score={sig.score} "
        f"traders={sig.unique_traders} buys={sig.buy_count}\n"
        f"{sig.token_address}\n"
        f"Hand off to Fomo wallet USDC buy — no auto-trade."
    )

    if not settings.telegram_configured:
        logger.info("would notify telegram (unset): score=%.1f %s %s", sig.score, sig.symbol, msg[:120])
        return

    try:
        import httpx

        url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
        with httpx.Client(timeout=3.0) as client:
            resp = client.post(
                url,
                json={"chat_id": settings.telegram_chat_id, "text": msg},
            )
            if resp.status_code >= 400:
                logger.warning("telegram notify soft-fail http %s", resp.status_code)
            else:
                logger.info("telegram notified score=%.1f %s", sig.score, sig.symbol)
    except Exception as exc:  # noqa: BLE001
        logger.warning("telegram notify soft-fail: %s", exc)
