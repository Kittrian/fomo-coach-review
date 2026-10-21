"""pump.fun on-chain lane (read-only): PDA derivation, bonding-curve reader, trade stream, exits, paper.

Layout notes written from the official pump-fun/pump-public-docs IDLs and the
chainstacklabs/pumpfun-bonkfun-bot protocol notes (Apache-2.0,
https://github.com/chainstacklabs/pumpfun-bonkfun-bot — docs/pumpfun-protocol.md,
cookbook/pumpfun/read/*, src/platforms/pumpfun/pumpswap_market.py). Code here is
original; no third-party trading code is imported or executed. Nothing in this
package signs or sends transactions.
"""
