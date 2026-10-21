# Fomo Signal Coach — sanitized code export for review

Live Solana pump.fun copy-trading / signal bot. This is a **code-only, secret-scrubbed** snapshot (Oct 20, 2026) so other AIs can review it and suggest ideas. No data, logs, .env, credentials, keypairs or wallet files are included. Values that looked like secrets were replaced with `REDACTED…` placeholders (some harmless constants such as program IDs may also have been redacted — fail-closed). Public wallet addresses were left in.

## Layout (mirrors server paths)
- `opt/fomo/coach/` — main app (FastAPI `app.main`, port 8790 local only), `app/` (live engine, scout, pump, scoring, paper), `scripts/` (learn_daily, learn_ticks, sniper_check, paper_tgpump), `tests/`.
- `opt/fomo-signer/` — isolated signer service (`fomo_signer`: policy, chain, backends, server), `tripwire/` (watches wallet; any unexpected outflow/manual send trips it), `sweep/` (sweeps profit above reserve).
- `opt/fomo/tg-watcher/` — public Telegram channel watcher (paper only) for scheduled pump/launch channels.
- `opt/fomo/txsub-shadow/` — shadow tx-subscription latency test.
- `opt/fomo/bin/credexec.py` — loads systemd encrypted credentials into env for a child process.
- `etc/systemd/system/fomo-*.service`, `usr/local/sbin/fomo-{guard,backup,egress}` — units, guard, backup, egress firewall.

## Architecture
- **Arms** (strategies): `scout_solo`, `scout_confirmed`, `fluffy_pump_dump`, `zyrox` live; `kol_confluence`/CryptoHub on paper; plus paper arms (top-trader young-coin <10 min, hold 5 min; TG pump watcher). Detection on processed commitment via Helius WS; whale wallets tracked.
- **Buy path** (~1.6–2.1 s whale→us): parallel prefetch + checks, blockhash cache, parallel Sender + RPC send, WS confirm, min_tokens_out bound at whale price ×1.055, max chase 5%, CU price matched to whale (cap 2M µlamports), Sender tip ≥0.001 SOL. Jupiter fallback (PumpSwap direct not built).
- **Signer/tripwire**: coach never holds keys; it asks the signer, which enforces policy (caps, tip cap, allow-listed programs). Tripwire hashes signer code and watches the wallet; sweep enforces a 0.50 SOL reserve.
- **Paper realism**: paper fills use real latency/chase; `PAPER_REALISM_SKIP_UNPRICED` drops trades without a real price (removed fake 30–48x wins).
- Every change is behind rollback env flags (`LIVE_PREFETCH`, `LIVE_BH_CACHE`, `LIVE_PARALLEL_SEND`, `LIVE_WS_CONFIRM`, `LIVE_DIRECT_PRICE_BOUND`, `LIVE_MATCH_WHALE_CU`, …).

## Hard rules
- Trading is 24/7: never pause; restart only at 0 open / 0 in-flight / 0 pending; never set KILL/HALT.
- Signer guards, tripwire and sweep always on; signer changes only via tripwire hash update.
- No wallet keys outside the signer; no secrets in logs; no new paid services without owner approval.

## Current results (as of Oct 20, 2026)
- Live: −0.117 SOL last 24 h; wallet near 0.50 SOL reserve. Copied whale buys −0.42 SOL over 7 d (ex-top-3): by the time we see the buy, coin is up 25–136%.
- "Fast" wallets are mostly bait launchers (self-launch, 5 SOL first-block buy, dump in 14–50 s) or wash bots.
- Launch-sniping backtest (520 coins): loses at our speed under every rule; only same-block entries win.
- Lead: top-trader signal on coins <10 min old, exit at 5 min: +1.30 SOL / 121 trades ex-top-3 historically (1 of 738 variants — likely overfit). Now paper-testing to 20 trades.

## What we want ideas on
1. **Edge**: signals that don't depend on being first (sell-side / "don't buy" filters, holder/cluster analysis, avoiding bait launchers).
2. **Exits**: better take-profit / trailing / time exits; detecting dev/insider dumps early.
3. **Speed**: anything free or ≤$100/mo that cuts send→land time (currently dominant over detection).
4. Bugs, risky code, or test gaps you notice.
