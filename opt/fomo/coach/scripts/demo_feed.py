#!/usr/bin/env python3
"""
Post synthetic Live Feed–shaped buy activities so the coach fires a high score.

Usage (coach must be running; default :8790 from PORT or LaunchAgent):
  cd /workspace/fomo-signal-coach
  .venv/bin/python scripts/demo_feed.py
  # or: curl after this script — see DEMO.md / README
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

def _default_base() -> str:
    import os
    port = os.getenv("PORT", "8790").strip() or "8790"
    return f"http://127.0.0.1:{port}"


BASE = sys.argv[1] if len(sys.argv) > 1 else _default_base()
INGEST = f"{BASE.rstrip('/')}/api/ingest/fomo-activity"

# Two fake tokens; first gets ≥3 distinct traders → high score
TOKEN_A = "REDACTED"
TOKEN_B = "REDACTED"

NOW = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def envelope(payload: dict) -> dict:
    return {
        "type": "data",
        "topicType": "trading_activity",
        "payload": payload,
    }


def buy(
    *,
    user_id: str,
    handle: str,
    token: str,
    ticker: str,
    usd: float,
    mcap: float,
    network_id: int = 101,
) -> dict:
    return envelope(
        {
            "type": "swap_buy",
            "id": f"demo-{user_id}-{ticker}-{int(time.time() * 1000)}",
            "userId": user_id,
            "userHandle": handle,
            "ticker": ticker,
            "tokenAddress": token,
            "networkId": network_id,
            "createdAt": NOW,
            "usdAmount": usd,
            "marketCap": mcap,
            "price": 0.00012,
        }
    )


ACTIVITIES = [
    # Token A — 4 traders, strong cluster
    buy(user_id="trader-alpha", handle="alpha", token=TOKEN_A, ticker="DEMOA", usd=150, mcap=180_000),
    buy(user_id="trader-beta", handle="beta", token=TOKEN_A, ticker="DEMOA", usd=220, mcap=185_000),
    buy(user_id="trader-gamma", handle="gamma", token=TOKEN_A, ticker="DEMOA", usd=90, mcap=190_000),
    buy(user_id="trader-delta", handle="delta", token=TOKEN_A, ticker="DEMOA", usd=310, mcap=195_000),
    # Token B — 3 traders
    buy(user_id="trader-echo", handle="echo", token=TOKEN_B, ticker="DEMOB", usd=80, mcap=420_000),
    buy(user_id="trader-foxtrot", handle="foxtrot", token=TOKEN_B, ticker="DEMOB", usd=95, mcap=430_000),
    buy(user_id="trader-golf", handle="golf", token=TOKEN_B, ticker="DEMOB", usd=110, mcap=440_000),
]


def post(body: dict) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        INGEST,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def main() -> int:
    print(f"POST {INGEST} ({len(ACTIVITIES)} activities)")
    try:
        result = post({"events": ACTIVITIES})
    except urllib.error.URLError as exc:
        print(f"ERROR: coach not reachable at {BASE}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    print()
    print(f"Check: curl -s '{BASE}/api/signals?min_score=50' | python3 -m json.tool")
    print(f"UI:    {BASE}/ui/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
