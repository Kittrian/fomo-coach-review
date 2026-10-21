#!/usr/bin/env python3
"""v1.16.1 DRY replay of today's 7 non-SOL-quote scout signals through the non-SOL decision path.

NEVER sends anything: no /execute, no swap, no coach endpoint. Read-only RPC + Jupiter quotes; the simulateTransaction
check builds the real Jupiter /order for the trading wallet, signs it in memory and simulates it (same mechanism as
scripts/live_dry_verify.py). Prints no secrets, no RPC URLs and no transaction bytes.

  .venv/bin/python scripts/nonsol_dry_replay.py [--no-sim] [--json OUT]

Two views per signal:
  * at signal time  — size rule, whitelist, scout ≥ 0.5 SOL, on curve, mcap (event reserves × quote price NOW, ≈),
                      age at the scout's buy (bonding-curve history before that tx), deployer / scheduled flags
  * now (dry)       — mesh check, fresh pre-buy gate (curve, verified quote price, mcap, buy + sell routes), simulate
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.config  # noqa: E402,F401  (loads .env; values are never printed)
from app.live import jupiter, nonsol  # noqa: E402
from app.live.config import config as live_config  # noqa: E402
from app.live.engine import LiveEngine  # noqa: E402
from app.pump import rpc  # noqa: E402
from app.pump.solana_pda import bonding_curve_pda  # noqa: E402
from app.scout.engine import config as scout_config  # noqa: E402
from app.scout.filters import filters  # noqa: E402

SIGNALS = Path(__file__).parent / "data" / "nonsol_replay_20261007.json"


async def age_at_buy(e: LiveEngine, mint: str, sig: str, bt: int, pages: int = 3):
    """Seconds from token creation (oldest bonding-curve tx) to the scout's buy; None if > pages×1000 txs."""
    before, oldest = sig, None
    for _ in range(pages):
        res = await e._rpc("getSignaturesForAddress", [bonding_curve_pda(mint), {"limit": 1000, "before": before,
                                                                                 "commitment": "confirmed"}])
        await asyncio.sleep(1.1)
        if res is None:
            return None, "rpc"
        if not res:
            break
        oldest = res[-1]
        before = oldest["signature"]
        if len(res) < 1000:
            break
    else:
        return None, f">{pages * 1000} txs before the buy"
    if not oldest or not oldest.get("blockTime"):
        return 0, "buy is the first curve tx"
    return bt - int(oldest["blockTime"]), "ok"


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-sim", action="store_true")
    ap.add_argument("--json")
    a = ap.parse_args()
    lc, sc, nc = live_config(), scout_config(), nonsol.cfg()
    filters.load()
    e = LiveEngine()
    rows = []
    for s in json.loads(SIGNALS.read_text()):
        qm, sym = s["quote_mint"], nonsol.quote_symbol(s["quote_mint"])
        size, size_note = nonsol.size_for(s["scout_sol"], max_sol_per_trade=lc["max_sol_per_trade"],
                                          min_scout_sol=sc["min_scout_buy_sol"])
        r = {"time_ct": datetime.fromtimestamp(s["block_time"]).strftime("%-I:%M %p"), "scout": s["scout"],
             "mint": s["mint"], "quote": sym, "scout_sol": s["scout_sol"], "size_sol": size, "gates": {}, "now": {}}
        g = r["gates"]
        g["flag"] = "pass" if nc["enabled"] else "BLOCK (LIVE_SCOUT_NONSOL_ENABLED=false)"
        g["whitelist"] = "pass" if nonsol.allowed(qm) else f"BLOCK ({sym} not whitelisted)"
        g["scout_buy>=0.5"] = "pass" if s["scout_sol"] >= sc["min_scout_buy_sol"] else "BLOCK"
        g["on_curve"] = "pass" if (s.get("rtr") or 0) > 0 else "BLOCK (curve complete)"
        info, why = await nonsol.price(qm) if qm in nonsol.KNOWN_QUOTES else (None, "unknown quote")
        if info:
            m = nonsol.curve_metrics({"virtual_quote_reserves": s["vqr"], "virtual_token_reserves": s["vtr"],
                                      "token_total_supply": 10**15}, info)
            r["mcap_usd_at_signal"] = round(m["mcap_usd"])
            g["mcap<20K"] = "pass" if m["mcap_usd"] < sc["max_mcap_usd"] else f"BLOCK (${m['mcap_usd']:,.0f})"
        else:
            g["mcap<20K"] = f"BLOCK ({why})"
        age, note = await age_at_buy(e, s["mint"], s["sig"], s["block_time"])
        r["age_s_at_signal"] = age
        g["age<15m"] = ("pass" if age is not None and age <= sc["max_age_s"]
                        else f"BLOCK ({age / 60:.1f} min)" if age is not None else f"BLOCK (age unknown: {note})")
        dflags = filters.deployer_flags(s.get("creator"))
        g["deployer/scheduled"] = ("BLOCK (" + "; ".join(dflags + (["scheduled"] if s["mint"] in filters.scheduled_mints
                                                                    else [])) + ")"
                                   if dflags or s["mint"] in filters.scheduled_mints else "pass")
        r["would_enter_at_signal"] = size is not None and all(v == "pass" for v in g.values())
        # ---- now (dry) ----
        n = r["now"]
        ok_mesh, why_mesh = await e._mesh_check(s["mint"])
        n["mesh"] = "pass" if ok_mesh else f"BLOCK ({why_mesh[:90]})"
        if size and nonsol.allowed(qm):
            ok, why_pb, facts = await nonsol.prebuy(s["mint"], qm, size, max_mcap_usd=sc["max_mcap_usd"],
                                                    read_curve=e._read_curve_raw, quote=e._jup_quote)
            n["prebuy"] = "pass" if ok else f"BLOCK ({why_pb[:110]})"
            n["prebuy_facts"] = {k: facts.get(k) for k in ("mcap_usd", "entry_premium_pct", "roundtrip")}
        else:
            n["prebuy"] = "not run (" + ("not whitelisted" if size else size_note) + ")"
        if not a.no_sim and size:
            try:
                sim = await jupiter.simulate_order(input_mint=jupiter.WSOL, output_mint=s["mint"], amount=int(size * 1e9),
                                                   slippage_bps=lc["buy_slippage_bps"],
                                                   max_priority_lamports=lc["max_priority_lamports"],
                                                   rpc_url=lc["sim_rpc_url"] or rpc.rpc_url())
                o = sim.get("order") or {}
                n["simulate"] = "pass" if sim.get("ok") else f"FAIL ({str(sim.get('err') or o.get('error'))[:80]})"
                n["route"] = " → ".join(x for x in (o.get("route") or []) if x)
            except Exception as exc:  # noqa: BLE001
                n["simulate"] = f"FAIL ({type(exc).__name__})"
        rows.append(r)
        await asyncio.sleep(1.2)
    print(json.dumps({"mode": "DRY — nothing sent", "enabled": nc["enabled"], "fraction": nc["fraction"],
                      "quotes_allowed": sorted(q["symbol"] for q in nc["allowed"].values()),
                      "at": datetime.now().strftime("%Y-%m-%d %-I:%M %p"), "signals": rows}, indent=1, default=str))
    if a.json:
        Path(a.json).write_text(json.dumps(rows, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
