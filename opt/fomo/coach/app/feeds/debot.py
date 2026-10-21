"""Soft DeBot narrative enrichment — never blocks scoring."""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from app.config import settings

logger = logging.getLogger("coach.debot")

DEBOT_URL = "https://app.debot.ai/api/v1/nitter/story/latest"


def _extract_text(data: Any) -> Optional[str]:
    if not isinstance(data, dict):
        return None
    for key in ("story", "narrative", "text"):
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    nested = data.get("data")
    if isinstance(nested, dict):
        return _extract_text(nested)
    return None


def enrich_narrative(token_address: str, symbol: str = "?") -> Optional[Dict[str, Any]]:
    """
    Attach a narrative chip when DEBOT_NARRATIVE_ENABLED.
    Non-blocking: short timeout; on failure return stub text so UI still shows a chip.
    """
    if not settings.debot_narrative_enabled:
        return None

    ca = (token_address or "").strip()
    if not ca:
        return None

    text: Optional[str] = None
    status = "stub"
    note = "demo stub — live DeBot fetch soft-failed or skipped"

    try:
        import httpx

        with httpx.Client(timeout=1.5) as client:
            resp = client.get(DEBOT_URL, params={"ca_address": ca})
            if resp.status_code == 200:
                parsed = _extract_text(resp.json())
                if parsed:
                    text = parsed
                    status = "ok"
                    note = "debot live"
            else:
                note = f"debot http {resp.status_code}"
    except Exception as exc:  # noqa: BLE001
        logger.info("debot fetch soft-fail for %s: %s", ca[:12], exc)
        note = f"soft_fail: {type(exc).__name__}"

    if not text:
        text = (
            f"Narrative stub for ${symbol}: clustered wallets converging on "
            f"{ca[:8]}… — verify thesis before handoff to Fomo wallet."
        )
        status = "stub"

    return {
        "status": status,
        "source": "debot",
        "text": text,
        "note": note,
        "token_address": ca,
    }
