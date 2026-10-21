#!/usr/bin/env python3
"""Monitor the known Fluffy Solana launch window: Wed Oct 7 2026 19:00 UTC.

Writes status JSON the coach / BTCP can poll. Also logs countdown.
Does not invent a CA — only reminds until a mint is ingested from Telegram.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

LAUNCH_UTC = datetime(2026, 10, 7, 19, 0, 0, tzinfo=timezone.utc)
OUT = Path(os.path.expanduser(
    os.getenv("LAUNCH_STATUS_PATH", "~/Library/Application Support/FOMO-Signal-Coach/fluffy-launch-window.json")
))
HOOK = Path(os.path.expanduser(
    os.getenv("BTCP_PUMP_HOOK", "~/Library/Application Support/BTCP-Drafts/pump-from-telegram.json")
))


def snapshot() -> dict:
    now = datetime.now(timezone.utc)
    secs = (LAUNCH_UTC - now).total_seconds()
    mint = None
    if HOOK.exists():
        try:
            mint = (json.loads(HOOK.read_text()).get("mint"))
        except Exception:
            pass
    return {
        "event": "Fluffyalpha Solana launch (hyped post t.me/Fluffyalpha/5344)",
        "launch_at_utc": LAUNCH_UTC.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "launch_at_chicago": "2026-10-07T14:00:00-05:00",  # CDT
        "now_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "seconds_to_launch": int(secs),
        "phase": (
            "pre" if secs > 3600 else
            "T-60m" if secs > 0 else
            "live_window" if secs > -3600 else
            "post"
        ),
        "ca_seen": bool(mint),
        "mint_preview": (mint[:8] + "…") if mint else None,
        "note": "No CA in the hype post — watcher will ingest when Fluffy posts a mint. Entry=CA announce, not dip. Recommend-only.",
    }


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    # one-shot by default; LAUNCH_MONITOR_LOOP=1 to poll
    loop = os.getenv("LAUNCH_MONITOR_LOOP", "0").strip() in {"1", "true", "yes"}
    while True:
        snap = snapshot()
        tmp = OUT.with_suffix(".tmp")
        tmp.write_text(json.dumps(snap, indent=2) + "\n")
        tmp.replace(OUT)
        print(json.dumps({k: snap[k] for k in ("phase", "seconds_to_launch", "ca_seen", "mint_preview")}, indent=None))
        if not loop:
            break
        time.sleep(30)


if __name__ == "__main__":
    main()
