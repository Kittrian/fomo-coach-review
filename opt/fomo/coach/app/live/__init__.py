"""v1.14 live auto-trade path (Jupiter Swap API v2 /order + /execute, signed locally).

OFF by default. New buys need BOTH JUPITER_EXEC_ENABLED=true and LIVE_AUTOTRADE_ENABLED=true plus the arm
in LIVE_ARMS. Every setting is env-driven: flipping is a .env change + restart.
Never logs key material, the keypair path contents, or the local API token.
"""
