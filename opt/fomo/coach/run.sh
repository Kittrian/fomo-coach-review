#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [[ ! -d .venv ]]; then
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi
# shellcheck disable=SC1091
set -a
[[ -f .env ]] && source .env
set +a
HOST="${HOST:-127.0.0.1}"   # v1.14: loopback only (live/exec endpoints also refuse non-loopback clients)
PORT="${PORT:-8787}"
exec .venv/bin/uvicorn app.main:app --host "$HOST" --port "$PORT"
