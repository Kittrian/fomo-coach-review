# Fluffy pump→dump plan (recommend-only)

**Context:** Fluffy Alpha often posts a Solana CA that pumps hard then dumps. Coach watches Telegram (`fomo-telegram-pump` → `/api/ingest/buy` with `source=fluffy/telegram`). Mesh + JEV gate stay fail-closed. **Live swaps stay OFF** (`JUPITER_EXEC_ENABLED=false`) until Andrew explicitly enables them with a keypair / Phantom confirm.

## Entry (already wired)
- **Trigger:** Fluffy CA announce only — not dips, not “sell now”.
- **Size (wallet ~1.0–1.1 SOL):** recommend **0.40 SOL** on Fluffy CA announce (Andrew-set). Still not the whole bag; live exec stays off.
- **Gate:** if mesh hard-blocks (mint/freeze live, honeypot, high rug flags) → **skip / no entry**. If escalate_human → wait. Only treat as actionable when gate says **show** (or human overrides after review).
- **Quote:** Jupiter quote dry-run with watch wallet `SOLANA_WALLET` as taker (unsigned tx only).

## Exit ladder (research preference — recommend levels only)
Assume entry fill price = `E` (first actionable quote/mesh price after announce).

| Step | Multiple of E | Sell fraction of **remaining** position* | Notes |
|------|---------------|------------------------------------------|--------|
| TP1  | **2×**        | **50%** of position at entry            | Bank half; removes most risk |
| TP2  | **3×**        | **50%** of what is left (~25% original) | Scale out into strength |
| TP3  | **5×**        | **50%** of what is left (~12.5% original)| Leave a moon-bag |
| Trail| after TP1     | remainder                               | Trailing stop **~25%** off local high (recommend); tighten if dump pattern |
| Hard stop | **−35%** from E | 100% of remaining                   | Cut loser if thesis fails / rug flags flip |

\*Ladder fractions in the coach payload are expressed as **% of original entry size** so the UI is unambiguous: 50% @ 2×, 25% @ 3×, 12.5% @ 5×, rest trailed / stopped.

## Rules of engagement
1. Mesh/JEV **block always wins** — no “FOMO through” a rug flag.
2. No auto-buy / auto-sell. Human confirms every step (or Phantom later).
3. Prefer speed on entry **only after** gate show; prefer discipline on exits — take TP1.
4. If liquidity is thin (mesh warning) or sell tax / honeypot appears mid-trade → escalate / exit remainder.

## Config knobs (`.env`)
```
PLAN_ENTRY_SOL_MIN=0.40
PLAN_ENTRY_SOL_MAX=0.40
PLAN_ENTRY_SOL_DEFAULT=0.40
PLAN_TP1_MULT=2
PLAN_TP1_FRAC=0.50
PLAN_TP2_MULT=3
PLAN_TP2_FRAC=0.25
PLAN_TP3_MULT=5
PLAN_TP3_FRAC=0.125
PLAN_TRAIL_PCT=0.25
PLAN_HARD_STOP_PCT=0.35
```

## Enabling live later (not now)
1. Confirm wallet + sizing mentally.
2. Put Solana CLI keypair at `SOLANA_KEYPAIR_PATH` **or** sign Jupiter unsigned txs in Phantom.
3. Only then set `JUPITER_EXEC_ENABLED=true` and use `/api/exec/swap` with `confirm=true`.
