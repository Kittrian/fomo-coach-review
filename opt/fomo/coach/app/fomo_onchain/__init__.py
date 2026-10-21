"""FOMO top-trader on-chain watch (free, read-only, recommend-only / paper only).

Identifies fomo.family trades from public Solana data: every FOMO Solana transaction is fee-paid and
co-signed by FOMO's relayer `AgmLJBMDCqWynYnQiPCuj9ewsNNsBJXyzoUhD9LJzN51` (chainstacklabs/
fomo-solana-rh-listeners docs/07-addresses.md; re-verified 2026-10-07). Wallet seed: the public
fomoapi.io/top100 snapshot (2026-09-09). Never touches FOMO's private API, never uses login tokens,
never signs or sends anything. Low priority: runs on its own RPC endpoints/queue and yields to the
Telegram/Fluffy CA lane.
"""
FOMO_COSIGNER = "AgmLJBMDCqWynYnQiPCuj9ewsNNsBJXyzoUhD9LJzN51"
FOMO_FEE_OWNER = "R4rNJHaffSUotNmqSKNEfDcJE8A7zJUkaoM5Jkd7cYX"
RELAY_DEPOSIT_PROGRAM = "99vQwtBwYtrqqD9YSXbdum3KBdxPAVxYTaQ3cfnJSrN2"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
WSOL_MINT = "So11111111111111111111111111111111111111112"
SOURCE = "fomo-onchain"
KEYLESS_SOURCE = "fomoapi-keyless"
PROFILE = "fomo_top_trader"
SMUG_SOURCE = "smugcalls/telegram"   # v1.13 arm C control feed (~95 calls/day) — never runs the CA fast lane
LOW_PRIORITY_SOURCES = frozenset({SOURCE, KEYLESS_SOURCE, SMUG_SOURCE})
