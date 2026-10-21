# Fomo Signal Coach

**Product:** recommend-only meme Signal Coach — handoff to Fomo wallet USDC buy. **No** in-app auto-trade (`auto_trade` always false).

Primary data path **without FOMOAPI**: Live Feed adapter `POST /api/ingest/fomo-activity` + `POST /api/ingest/buy`. Optional feeds (Telegram / Helius / Birdeye / Discord) soft-fail when unset.

**Jev gate (v1.8+):** TypeSafe Jev via Cloudflare Workers AI model `typesafe/jev` annotates every signal card with Choice/Score/Noul decisions (`skip` | `show` | `escalate_human`, `signal_quality`, `safe_to_surface`). Missing Cloudflare credentials → fail closed (`jev_status: unavailable`, `action: escalate_human`). Demo still runs.

## Run

```bash
cd /workspace/fomo-signal-coach
cp .env.example .env   # leave FOMOAPI_KEY empty; DEMO_MODE=true
./run.sh
curl -s http://127.0.0.1:8787/health | python3 -m json.tool
```

- JSON root: `GET /`
- Health: `GET /health` (includes `jev_gate` + `jupiter_exec` status)
- UI (recommend cards): [http://127.0.0.1:8787/ui/](http://127.0.0.1:8787/ui/)

## Demo loop

```bash
# with coach running on :8787
.venv/bin/python scripts/demo_feed.py
curl -s 'http://127.0.0.1:8787/api/signals?min_score=50' | python3 -m json.tool
```

`scripts/demo_feed.py` POSTs Live Feed–shaped buys for 1–2 fake tokens with ≥3 traders so a high score fires. See `DEMO.md` for the verified command set.

## TypeSafe Jev gate (Cloudflare Workers AI)

1. In the Cloudflare dashboard, copy **Account ID** and create a **Workers AI** API token.
2. Set in `.env` (never commit real tokens):

```bash
JEV_GATE_ENABLED=true
CLOUDFLARE_ACCOUNT_ID=your_account_id
CLOUDFLARE_API_TOKEN=REDACTED
```

3. Restart the coach. Signals gain a `jev` object:

```json
{
  "jev_status": "ok",
  "action": "show",
  "signal_quality": 1.6,
  "safe_to_surface": true,
  "recommend_only": true,
  "auto_trade": false
}
```

Without credentials (default demo):

```json
{
  "jev_status": "unavailable",
  "action": "escalate_human",
  "safe_to_surface": false,
  "recommend_only": true,
  "auto_trade": false
}
```

Live smoke (skips when env missing):

```bash
.venv/bin/python scripts/jev_live_check.py
```

Docs: [typesafe/jev on Workers AI](https://developers.cloudflare.com/ai/models/typesafe/jev/)

## QP Data Mesh enrichment + risk gate (v1.10+)

Every signal is enriched from the free **QP Data Mesh** (`MESH_BASE_URL`, default `https://quantumpools.io/api/mesh`; GET only, 2 req/s per IP). `app/mesh.py` throttles client-side (`MESH_RPS=1.5`, burst 4), caches per op, single-flights per mint, and warms enrichment in the background on every buy / CA ingest.

| Field | Mesh op |
|---|---|
| price (+24h) | `price?chain=solana&token=<mint>` |
| symbol/name, token program | `token_meta?chain=solana&token=<mint>` |
| risk flags (RugCheck / Jupiter / GoPlus / GMGN): mint/freeze authority, honeypot, taxes, top holders | `token_risk?chain=solana&token=<mint>` |
| liquidity, FDV/mcap, supply, 24h flow | `compat/dexpaprika/networks/solana/tokens/<mint>` |
| pool TVL + on-chain verify (only if a buy carries `meta.pool`) | `pool` / `verify_onchain?chain=solana&address=<pool>` |
| pump.fun curve progress / migrated | auto-detected when the mesh index lists a `pump_curve`-style op; otherwise `lane_unavailable` |

**Final gate** (`signal.gate`, recommend-only, dry-run): mesh hard block (mint authority live, freeze authority live, honeypot / ≥50% sell tax, RugCheck rugged, any high/danger flag) → `skip` and always wins; mesh down/pending → `escalate_human`; Jev ok → Jev's action; Jev unavailable (e.g. 402 no credits) → mesh rules (`show`). Blocked tokens never trigger Telegram.

```bash
MESH_ENABLED=true
MESH_BASE_URL=https://quantumpools.io/api/mesh
curl -s localhost:8790/api/mesh/enrich/DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263 | python3 -m json.tool
```
`/health` → `mesh` block (status, provider states, pump lane, client stats).

## Ingest

- `POST /api/ingest/buy` — canonical buy event
- `POST /api/ingest/fomo-activity` — **Fomo-Live-Feed adapter**: WS envelope / raw `swap_buy` / `TradeEventV1` / batch. Only **buy** actions cluster; sells soft-ignored.

Optional header: `X-Ingest-Secret: $INGEST_SECRET`

## Signals (scoring)

Cluster window + `MIN_WALLET_CONVERGENCE` from settings. Score (0–100) from unique traders, buy count, optional USD / market-cap hints. Computed on read. **Jev annotate** runs before list/detail responses.

- `GET /api/signals?min_score=50` — list (+ `jev` on each card)
- `GET /api/signals/{token_address}` — detail (+ Jev + soft DeBot narrative)

## Soft extras

- **Jev gate** — Cloudflare Workers AI `typesafe/jev`; fail closed when unset
- **DeBot narrative** — enrichment chip on signal detail; never blocks scoring
- **Telegram** — if configured and score ≥ `TELEGRAM_MIN_SCORE`, notify; else log `would notify`


## Jupiter Order & Execute (confirm-to-execute)

Free/keyless Jupiter Swap v2 (`/order` + `/execute`). **Off by default.** Recommend-only stays on; no auto-trade.

```bash
# .env — keep false until you intentionally enable
JUPITER_EXEC_ENABLED=false
JUPITER_API_KEY=            # optional free portal key; keyless works at low RPS
SOLANA_RPC_URL=https://api.mainnet-beta.solana.com
SOLANA_KEYPAIR_PATH=        # optional local Solana CLI JSON; prefer Phantom sign when unset
```

- `POST /api/exec/quote` — always dry (quote + optional unsigned tx)
- `POST /api/exec/swap` — dry-run unless `JUPITER_EXEC_ENABLED=true` **and** `confirm=true` **and** a signed tx (client `signed_transaction` or local keypath + `solders`)
- Health: `jupiter_exec` status chip
- UI: “Quote buy/sell via Jupiter” chips on signal cards (quote only)

Safe enable checklist: leave recommend-only product intent; never put private keys in git/logs; start with quote-only; enable only on a dedicated wallet with tiny balance; prefer returning unsigned tx for Phantom rather than server-side keys.

Docs: [Order & Execute](https://developers.jup.ag/docs/swap/order-and-execute)

## Tests

```bash
.venv/bin/python -m pytest tests/ -q
# or without pytest:
.venv/bin/python tests/test_live_feed_adapter.py
.venv/bin/python tests/test_scoring.py
.venv/bin/python tests/test_jev_gate.py
.venv/bin/python tests/test_mesh_gate.py
.venv/bin/python tests/test_jupiter_exec.py
```

## v1.12.0-demo — FOMO top-trader on-chain watch (free, paper only)

- **Source `fomo-onchain`** (`app/fomo_onchain/`): seeds from `data/fomo_top100_wallets.json` (public fomoapi.io/top100
  snapshot). Each kept wallet is `logsSubscribe`d (mentions = wallet) on a free public Solana WS (≤40
  subs/connection, ≤100 total, backoff + resubscribe); dust is dropped from the logs alone (must invoke a FOMO route
  program: DFlow/Relay/Jupiter…) before any RPC fetch. (The USDC token account is *not* subscribed: it is flooded by
  FOMO referral-fee inflows from copiers.) The verification sweep uses wallet-signatures ∩ USDC-account-signatures. A tx counts only if **fee payer = FOMO co-signer
  `AgmLJBMDCqWynYnQiPCuj9ewsNNsBJXyzoUhD9LJzN51` and the wallet signed**. Buys → `/api/ingest/buy` (source
  `fomo-onchain`, trader handle); sells → follow-the-trader exit for open `fomo_top_trader` paper positions
  (advisory note only on Fluffy positions). Does not touch FOMO's servers or private API.
- **Wallet quality 0–100** from verified co-signed activity (activity 45 + recency 30 + FOMO purity 15 + live 10);
  wallets with no co-signed trades are **dropped** with a reason. `/api/fomo-onchain/wallets`, UI table.
- **Never slows the Fluffy CA lane**: own WS + own HTTPS RPC endpoints and client, one bounded drop-oldest queue,
  one token-bucket worker (`FOMO_ONCHAIN_RPS`), pauses while a Telegram CA pipeline runs; low-priority ingests skip
  the curve fast lane, mesh, JEV and Jupiter, and are excluded from `/api/signals` (`FOMO_ONCHAIN_IN_SIGNALS=false`).
  Marks come from DexScreener's free API, not Jupiter.
- **`fomoapi_keyless`**: `wss://api.fomoapi.io/ws/alerts` (60 s delayed) — handle→wallet mapping + thesis notes only,
  dedup on `eventId`, **never an entry trigger**, degrades to `unavailable` if the feed disappears. No key sent.
- **Profile `fomo_top_trader`** ("buy low, sell high"): skip tokens already up big (m5 ≥15%, h1 ≥30%, h6 ≥80%,
  h24 ≥300%), flag moderate run-ups (h1 ≥7.7% / h6 ≥15% — crowd buys tend to follow a +7.7% 4h run, median −22%
  next day) and then require convergence; skip if price is >15% above the first top-wallet fill; prefer early
  (on-curve/young/low-mcap) tokens and ≥2 top wallets within 30 min (solo only for quality ≥75 on early tokens);
  skip mint/freeze authority set or liquidity <$8k. Exits: hard stop −20% → breakeven (+3%) once +15% → 15% trailing
  once +25% (ratchets, never loosens), TP ladder 1.5×/2×/3×, follow-the-trader, 4 h max hold.
- **Paper**: separate book and size (`FOMO_PAPER_ENTRY_SOL=0.10`) — never occupies Fluffy's 0.40 SOL slot.
- ⚠️ **No-loss is NOT guaranteed.** Stops fill at the next observed mark (~15 s); gaps, rugs and slippage can fill
  well below the stop. Paper only; `JUPITER_EXEC_ENABLED=false`, no signing, no keypair.

## v1.11.0-demo — pump.fun CA lane (Phase 0, free, read-only)

- **On-chain bonding-curve reader** (`app/pump/curve.py`): derives the curve PDA (`["bonding-curve", mint]`), decodes reserves / `complete` / creator / mayhem / quote mint for 151- and 125-byte layouts; migrated coins read the canonical PumpSwap pool incl. the signed i128 `virtual_quote_reserves`. Own code; layout per the official pump-fun public IDL/docs, patterns after chainstack `pumpfun-bonkfun-bot` (Apache-2.0). No third-party SDK dependency.
- **Latency timeline** per ingest: `tg_message → tg_recv → ingest → curve_read → paper_entry → mesh → jev → quote` (`/api/latency`, card chip). Watcher must send `meta.tg_recv_ms`/`tg_message_ms` for the Telegram stages.
- **Free trade stream**: standard Solana WS `logsSubscribe` (mentions = mint), decodes pump `TradeEvent`s (`/api/pump/streams`, `/api/pump/stream/{mint}`). No PumpPortal.
- **Exit cards**: creator sell, sells>buys below high, curve SOL draining, max hold, migration, trailing stop (arms only in profit), hard stop. Recommend-only.
- **Paper trading** of the 0.40 SOL fluffy ladder on curve sim / Jupiter quote-only / PumpSwap pool sim → `data/paper_trades.jsonl`, `/api/paper`.
- **Gate**: pre-migration curve coins → `curve_fast_gate` decides, JEV advisory (skip ⇒ size_hint reduce). Mesh hard block always wins; migrated / non-pump coins keep the v1.10 gate.
- Still: `JUPITER_EXEC_ENABLED=false`, no signing, no keypair.

## v1.13.0-demo — scout-wallet paper test (paper only)

- **Watched wallets:** `data/scout_wallets.json` lists 6 scouts (Jijo, Wugi, The Doc, Fox, OGAntD, R4) and 5 confirmers (Decu, Trunoest, Cupsey, nyhrox, Cented). Cented has a 12 s median hold, so he is a confirmer only. The 56 FOMO wallets also act as confirmers. Truncated addresses are skipped.
- **Source (`app/scout/source.py`):** subscribes over the free websocket to each wallet's pump `user_volume_accumulator` PDA instead of the wallet itself. The wallets get flooded with dust, but the PDA only shows up in that wallet's own pump-curve buys and sells, from any router. Trades come from the existing TradeEvent decode. The source has its own websocket connections, its own RPC (publicnode by default) and token buckets (`SCOUT_RPS` / `SCOUT_BG_RPS`). Background scans yield to the Fluffy fast lane. The free public websocket drops some notifications, so a gap-fill poller also checks scouts and confirmers with `getSignaturesForAddress` on their PDA (`SCOUT_POLL_RPS`, default 2). SOL/USD comes from Jupiter lite price v3 or Coinbase spot, because DexScreener returns no WSOL pair.
- **Arms** (separate paper book `data/scout_trades.jsonl`, 0.10 SOL each):
  - **A `scout_solo`:** a scout buys ≥0.5 SOL of a token still on the curve, under $20K mcap and under 15 min old.
  - **B `scout_confirmed`:** A plus another watched wallet buying the same mint within 60 s. Fires a watcher alert (Saved Messages + macOS).
  - **C `smugcalls_control`:** every SmugCalls CA (`smugcalls/telegram`). No push alert.
- **Skip filters** (`app/scout/filters.py`):
  - migrated or non-SOL curve
  - deployer with a previously observed rug (learned from our own marks)
  - scheduled-launch mints (Fluffy / `pump_at_iso`)
  - dump-wallet k≥3 among the first 20 buyers. This is post-hoc only: it is recorded as an annotation and the report shows PnL with those tokens excluded. Real-time skipping is a TODO.
- **Fills:** simulated on the curve at detection +2 s and +5 s, including slippage and fees. The edge gap is our fill price vs the scout's entry price.
- **Exit rules,** run in parallel for every entry:
  - `tp2x_trail`: sell half at 2x, then trail 30%
  - `min10_check`: exit at minute 10 if below entry
  - `hard_stop_35`: −35%
  - `scout_sells`: exit when the scout sells (arms A/B only)
  - All rules also have a 60 min max hold.
- **API:** `/api/scout` (report per arm × rule × latency), `/api/scout/signals`, `/api/scout/alerts`. The dashboard has a scout panel.
- **Watcher:** SmugCalls is added to `TARGET_GROUPS`. It auto-joins public channels only from a hard-coded allowlist containing just SmugCalls, and never posts anything. `NO_ALERT_GROUPS=smugcalls`. The arm B alert poller is on.

## v1.14.0 — live auto-trade (off by default)

- `app/live/`: a router hooked into the existing paper gates. It covers the Fluffy/Zyrox curve and migrated lanes after the mesh gate, scout arm A/B entries, and fomo_top_trader entries. When **both** `JUPITER_EXEC_ENABLED` and `LIVE_AUTOTRADE_ENABLED` are true, the arm is in `LIVE_ARMS`, and every cap passes, it places a real buy:
  - Jupiter Swap API v2 `/order` then `/execute`.
  - The transaction is signed locally with `SOLANA_KEYPAIR_PATH`.
- **Exit engine** (mirrors the paper rules): TP 50% at 2x then a 30% trail, a one-shot underwater exit at minute 10, a −35% hard stop, an exit when the scout/trader sells, and a 60-minute max hold.
  - Prices come from the pump trade stream and the curve account. Migrated tokens are priced with Jupiter quotes.
  - Sells escalate slippage 2500 → 4000 → 6000 → 10000 bps and then retry every 5 s until they fill.
  - State lives in `data/live_state.json`, with a ledger in `data/live_trades.jsonl`. Positions resume after a restart.
- **Caps:** per-arm `LIVE_SIZE_SOL_<ARM>` (bounded by `LIVE_MAX_SOL_PER_TRADE`), max open positions, trades per day, and a daily loss limit (realized plus unrealized losses; it halts buys for the day in America/Chicago). It never spends below `LIVE_MIN_SOL_RESERVE`, and each mint is bought live at most once, ever.
- **Kill switch:** create the file `~/Library/Application Support/FOMO-Signal-Coach/KILL`, call `POST /api/live/kill {"sell_all":bool}` / `POST /api/live/resume`, or use the dashboard buttons.
- `/api/live/*` and `/api/exec/swap` accept loopback clients only and require an `X-Live-Token` header. The token lives in `~/.config/fomo-coach/live-api-token` (0600, generated on first start). The server binds to 127.0.0.1.
- Every live buy and sell raises an alert. The watcher polls `/api/live/alerts` and posts a macOS notification plus a Saved Messages note with a solscan link.
- DRY check: `scripts/live_dry_verify.py` builds and simulates a 0.01 SOL buy. It never calls `/execute`.

## v1.14.1 — let runners run

- A live position becomes a **runner** once TP1 sells (50% at 2x), or once its high reaches 2x while that partial sell is still retrying, as long as the price is not below entry.
- A runner exits **only** on the 30% trail (`LIVE_TRAIL_PCT`). The 60-minute max hold, the minute-10 underwater rule and the scout/trader-sells exit are ignored.
- A mesh hard block force-exits a runner only if its reasons mention rug or honeypot. The kill-switch sell-all still applies.
- Optional `LIVE_RUNNER_MAX_HOLD_MIN` (0 = off) puts a time cap on runners.
- Losers and flat positions keep the hard stop, the minute-10 rule, trader sells and max hold. A runner that falls back below entry loses runner status, so max hold applies to it again.
- Live mints are pinned in the trade-stream manager, so they never hit the 30-minute TTL and are never evicted. The live loop re-checks and restarts dropped streams every 30 s, and falls back to curve or Jupiter quote polling. Runner state (tp_done, high) is saved to `data/live_state.json`.
- Paper mirrors:
  - Scout tp2x_trail runners skip max hold.
  - Fluffy paper after TP1: the trail can fire even below entry, and flow-flip/curve-drain are off.
  - fomo_top_trader with the trailing stop active: max hold and follow-trader are off.
