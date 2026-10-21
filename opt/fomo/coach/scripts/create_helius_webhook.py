#!/usr/bin/env python3
"""Register Helius enhanced webhook once PUBLIC_BASE_URL is an HTTPS tunnel.

Does not print secrets. Requires HELIUS_API_KEY + PUBLIC_BASE_URL in .env.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")


def main() -> int:
    key = os.getenv("HELIUS_API_KEY", "").strip()
    base = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    path = os.getenv("HELIUS_WEBHOOK_PATH", "/api/ingest/helius")
    if not key:
        print("HELIUS_API_KEY unset — abort", file=sys.stderr)
        return 1
    if not base.startswith("https://"):
        print("PUBLIC_BASE_URL must be https://… (ngrok/cloudflared) — abort", file=sys.stderr)
        return 1
    webhook_url = f"{base}{path}"
    print("Would register Helius webhook →", webhook_url)
    print("Watchlist wallets: load from data/helius_wallets.json when present")
    wallets_path = ROOT / "data" / "helius_wallets.json"
    wallets = []
    if wallets_path.exists():
        wallets = json.loads(wallets_path.read_text()).get("wallets", [])
        print(f"Loaded {len(wallets)} wallets (not registering in stub)")
    print("Stub only — implement HELIUS REST call when ready. No auto-trade.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
