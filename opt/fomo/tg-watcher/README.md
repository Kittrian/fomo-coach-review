# fomo-telegram-pump

Telethon-based Telegram watcher that listens for PUMP-style signals in configured groups (default: **Zyrox Insider Calls**, **Fluffy Alpha**) and writes the latest parsed event to a JSON file the BTCP Drafts Electron app can ingest later.

**This turn:** scaffold only. No interactive login was run (no phone / API secrets yet).

## Get Telegram API credentials

1. Open [https://my.telegram.org](https://my.telegram.org) and sign in with your phone number.
2. Go to **API development tools**.
3. Create an application if prompted; copy **api_id** and **api_hash**.
4. Never commit secrets. `.env` and `*.session` are gitignored.

## Setup (human steps)

```bash
cd /Users/drewdrew/Developer/fomo-telegram-pump
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env:
#   TELEGRAM_API_ID=<from my.telegram.org>
#   TELEGRAM_API_HASH=<from my.telegram.org>
#   TELEGRAM_PHONE=+1XXXXXXXXXX   # E.164
#   SESSION_NAME=fomo_pump
#   TARGET_GROUPS=Zyrox Insider Calls,Fluffy Alpha
```

### Create session once (interactive)

```bash
python login_once.py
```

Telethon will ask for the login code sent to Telegram (and 2FA if enabled). This creates `fomo_pump.session` in this directory.

### Run the watcher

```bash
python watcher.py
```

On each matching `NewMessage` it updates:

`~/Library/Application Support/BTCP-Drafts/pump-from-telegram.json`

Override with `BTCP_PUMP_HOOK` in `.env` if needed.

## Event JSON shape

```json
{
  "source_group": "Zyrox Insider Calls",
  "text": "…raw message…",
  "pump_at_iso": "2026-10-01T20:30:00Z",
  "time_zone": "America/New_York",
  "mint": "SoLanaMintBase58…",
  "symbol": "TICKER",
  "sell_now": false,
  "raw_urls": ["https://…"],
  "received_at": "2026-10-01T19:01:02Z"
}
```

Parsing is heuristic:

- **Time / zone:** clock times + EST/EDT/CST/CDT/PST/PDT/UTC/ET/CT/PT or IANA names; date optional (defaults to today / next day if already past).
- **Mint:** Solana-style base58 (~32–44 chars), preferred near `ca` / `mint` / explorer links.
- **Symbol:** `$TICKER` or `ticker:` / `symbol:` nearby.
- **Sell now:** phrases like `sell now`, `exit now`, `gtfo`, etc.

If a configured group title is missing, the watcher soft-fails: prints similar dialog titles and continues with whatever matched.

## BTCP Drafts PUMP tab — how Electron can ingest later

In `btcp-drafts/src/renderer/app.js` the PUMP tab persists to **localStorage** under:

```js
const PUMP_STORAGE_KEY = 'btcp-pump-timer-v1';
// shape: { targetMs: number|null, timeZone: string, localInput: string }
```

- `targetMs` — absolute UTC epoch milliseconds for the countdown.
- `timeZone` — IANA zone used for the wall-clock input (UI default `America/Chicago`).
- `localInput` — `datetime-local` string (no offset).

**Suggested bridge (not implemented this turn):** main process or a small preload poll/watch on

`~/Library/Application Support/BTCP-Drafts/pump-from-telegram.json`

then map:

| JSON field | → localStorage / UI |
|---|---|
| `pump_at_iso` | `Date.parse(…)` → `targetMs` |
| `time_zone` | `timeZone` (fallback `America/Chicago`) |
| derived wall clock in that zone | `localInput` |
| `sell_now` | status banner / notification |
| `mint` / `symbol` | optional PUMP subtitle (future UI) |

Do **not** write localStorage from Python; keep the file as the IPC surface so the existing `pumpLoadPersisted` / `pumpPersist` path can be extended from Electron.

## Safety

- Never commit `.env` or `*.session`.
- Session file is equivalent to a logged-in client — treat like a password.
- Watcher refuses interactive `client.start()`; authorize only via `login_once.py`.

## FOMO Signal Coach bridge (v1.10+)

When a target chat posts a **Solana CA** (and it is not a sell/dip message), the watcher POSTs to:

`POST $COACH_INGEST_URL/api/ingest/buy` with `source=fluffy/telegram` (or `telegram`).

The coach runs QP Data Mesh enrichment + JEV/mesh gate (recommend-only; `JUPITER_EXEC_ENABLED=false`).

Env (see `.env.example`):

```
COACH_INGEST_ENABLED=true
COACH_INGEST_URL=http://127.0.0.1:8790
TARGET_GROUPS=Fluffyalpha,Fluffy Alpha,Zyrox Insider Calls
```

### Session / OTP (required once)

`fomo_pump.session` may exist but still be **unauthorized** until OTP completes.

```bash
cd ~/Developer/fomo-telegram-pump
.venv/bin/python check_session.py          # exit 2 = need OTP
.venv/bin/python login_drive.py            # sends code to Telegram app
# When the code arrives in Telegram:
echo 'YOUR_CODE' > pending_login_code.txt  # do not paste the code into chat
# If 2FA: echo 'password' > pending_login_2fa.txt
.venv/bin/python check_session.py          # should print authorized=True
launchctl kickstart -k gui/$(id -u)/com.drewdrew.fomo-telegram-pump
```

LaunchAgent: `com.drewdrew.fomo-telegram-pump` (retries every 60s until session authorized).

### Fluffy launch window (Wed Oct 7 2026 19:00 UTC / 14:00 CDT)

```bash
.venv/bin/python launch_window_monitor.py
# status JSON:
# ~/Library/Application Support/FOMO-Signal-Coach/fluffy-launch-window.json
```

LaunchAgent `com.drewdrew.fluffy-launch-window` runs at load and at T-60m / T-0 / T+5m local.


## Fluffy pump→dump playbook (coach)

On CA announce the coach attaches a recommend-only plan (see coach `PLAN.md`):
- **Entry size: 0.40 SOL** (Andrew-set). Live Jupiter exec stays **off**.
- Exit ladder: 50% @ 2×, 25% @ 3×, 12.5% @ 5×; trail remainder; hard stop −35%.
- Mesh/JEV block still wins — no auto-buy.
