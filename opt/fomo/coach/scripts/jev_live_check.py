#!/usr/bin/env python3
"""
Live Cloudflare Workers AI typesafe/jev smoke check.

Skips (exit 0) when CLOUDFLARE_ACCOUNT_ID / CLOUDFLARE_API_TOKEN are unset.
Never prints the API token. Recommend-only — does not trade.

Usage:
  cd /workspace/fomo-signal-coach
  .venv/bin/python scripts/jev_live_check.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import settings
from app.jev_gate import (
    JEV_MODEL,
    build_state,
    call_workers_ai,
    credentials_ready,
    parse_answers,
)
from app.scoring import score_buys
from app.state import BuyEvent
import time


def _demo_sig(symbol: str = "DEMOA"):
    token = (
        "DemoToken1111111111111111111111111111111"
        if symbol == "DEMOA"
        else "DemoToken2222222222222222222222222222222"
    )
    n = 4 if symbol == "DEMOA" else 3
    buys = [
        BuyEvent(
            trader_id=f"{symbol}-{i}",
            trader_handle=f"h{i}",
            token_address=token,
            token_symbol=symbol,
            chain="solana",
            usd_amount=120 + i * 20,
            market_cap=180_000 + i * 5_000,
            price=0.00012,
            occurred_at_ms=int(time.time() * 1000),
            source="jev_live_check",
        )
        for i in range(n)
    ]
    return score_buys(buys)


def main() -> int:
    print(f"model={JEV_MODEL}")
    print(f"JEV_GATE_ENABLED={settings.jev_gate_enabled}")
    print(f"account_id_set={bool(settings.cloudflare_account_id)}")
    print(f"api_token_set={bool(settings.cloudflare_api_token)}")

    if not credentials_ready():
        print(
            "SKIP: CLOUDFLARE_ACCOUNT_ID / CLOUDFLARE_API_TOKEN unset — "
            "demo coach continues with jev_status=unavailable (fail closed)."
        )
        return 0

    out = {}
    for symbol in ("DEMOA", "DEMOB"):
        sig = _demo_sig(symbol)
        assert sig is not None
        state = build_state(sig)
        try:
            payload = call_workers_ai(state)
            decision = parse_answers(payload)
            out[symbol] = {
                "coach_score": sig.score,
                "jev": {
                    "jev_status": decision.jev_status,
                    "action": decision.action,
                    "signal_quality": decision.signal_quality,
                    "safe_to_surface": decision.safe_to_surface,
                    "model": decision.model,
                    "usage": decision.usage,
                },
            }
        except Exception as exc:  # noqa: BLE001
            out[symbol] = {"error": str(exc)[:300], "fail_closed_action": "escalate_human"}

    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
