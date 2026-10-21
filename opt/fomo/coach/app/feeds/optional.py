from __future__ import annotations

from typing import Any, Dict

from app.config import settings


def soft_feed_status() -> Dict[str, Any]:
    """Every optional feed soft-fails — never take down the coach."""
    return {
        "fomoapi": {
            "configured": settings.fomoapi_configured,
            "status": "configured" if settings.fomoapi_configured else "unset",
            "note": "primary keyed feed when key present; WS not auto-started in rehydrate scaffold",
        },
        "birdeye": {
            "configured": settings.birdeye_configured,
            "status": "soft_fail_ready" if settings.birdeye_configured else "unset",
            "note": "Standard plan often 401 on smart-money — discovery soft-fails",
        },
        "helius": {
            "configured": settings.helius_configured,
            "webhook_ready": settings.helius_webhook_ready,
            "status": (
                "webhook_ready"
                if settings.helius_webhook_ready
                else ("key_set_needs_https_tunnel" if settings.helius_configured else "unset")
            ),
        },
        "bubblemaps": {
            "configured": settings.bubblemaps_configured,
            "status": "soft_fail_ready" if settings.bubblemaps_configured else "unset",
        },
        "discord": {
            "configured": settings.discord_configured,
            "status": "soft_fail_ready" if settings.discord_configured else "unset",
        },
        "telegram": {
            "configured": settings.telegram_configured,
            "min_score": settings.telegram_min_score,
            "status": "configured" if settings.telegram_configured else "unset",
        },
        "debot_narrative": {
            "enabled": settings.debot_narrative_enabled,
            "status": "soft_enrichment_stub",
            "endpoint": "https://app.debot.ai/api/v1/nitter/story/latest?ca_address=",
            "note": "soft enrichment chip on GET /api/signals/{token}",
        },
        "jev_gate": {
            "enabled": settings.jev_gate_enabled,
            "configured": settings.jev_configured,
            "status": (
                "configured"
                if settings.jev_configured
                else ("disabled" if not settings.jev_gate_enabled else "unset")
            ),
            "model": "typesafe/jev",
            "note": "Cloudflare Workers AI; missing creds → jev_status unavailable / escalate_human",
        },
    }
