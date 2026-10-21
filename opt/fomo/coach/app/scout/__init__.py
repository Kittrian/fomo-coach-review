"""Scout-wallet paper test (v1.13, PAPER ONLY, recommend-only).

Arms (separate paper profiles, 0.10 SOL paper each by default):
  A scout_solo        — an S-tier scout buys a pump.fun token still on the curve (<$20K mcap, <15 min old,
                        scout buy ≥0.5 SOL)
  B scout_confirmed   — A plus a second watched wallet (scout / fast trader / FOMO wallet) within 60 s
  C smugcalls_control — every SmugCalls (t.me/SmugCalls) CA post from the Telegram watcher
Each entry is simulated at detection +2 s and +5 s with bonding-curve slippage, and tracked under four
exit rules in parallel. Nothing is signed or sent; JUPITER_EXEC_ENABLED stays false.
"""
ARM_A = "scout_solo"
ARM_B = "scout_confirmed"
ARM_C = "smugcalls_control"
ARMS = (ARM_A, ARM_B, ARM_C)
RULES = ("tp2x_trail", "min10_check", "hard_stop_35", "scout_sells")
SMUG_SOURCE = "smugcalls/telegram"
# Telegram sources whose CAs are scheduled/deployer launches (skip filter for the scout arms)
SCHEDULED_SOURCES = frozenset({"fluffy/telegram"})
