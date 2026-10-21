#!/usr/bin/env bash
export PYTHONUNBUFFERED=1
set -euo pipefail
cd "$(dirname "$0")"
# shellcheck disable=SC1091
source .venv/bin/activate
# Ensure session authorized before long-running listen
if ! .venv/bin/python check_session.py | while IFS= read -r l; do echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] $l"; done; then
  echo "Telethon session not authorized — run login_drive.py and drop OTP into pending_login_code.txt" >&2
  exit 2
fi
exec .venv/bin/python watcher.py
