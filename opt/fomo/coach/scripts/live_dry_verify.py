#!/usr/bin/env python3
"""v1.14 DRY verification: build a real Jupiter Swap v2 /order for the trading wallet, sign it in memory and run
simulateTransaction. NEVER calls /execute and never sends a transaction. Prints no secrets and no tx bytes.

  .venv/bin/python scripts/live_dry_verify.py [--curve-mint <pump mint>] [--sol 0.01]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

import app.config  # noqa: E402,F401  (loads .env)
from app.live import jupiter  # noqa: E402
from app.live.config import config  # noqa: E402
from app.pump import rpc  # noqa: E402
from app.pump.curve import decode_curve  # noqa: E402
from app.pump.solana_pda import PUMP_PROGRAM_ID, bonding_curve_pda  # noqa: E402

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


async def curve_state(mint: str, url: str) -> dict:
    async with httpx.AsyncClient(timeout=10) as c:
        res = await rpc.call("getAccountInfo", [bonding_curve_pda(mint), {"encoding": "base64"}], client=c, url=url,
                             timeout=8)
    acc = rpc._decode_account((res or {}).get("value"))
    if not acc or acc.get("owner") != PUMP_PROGRAM_ID:
        return {"on_curve": False}
    cv = decode_curve(acc["data"])
    return {"on_curve": not cv.get("complete"), "complete": cv.get("complete"),
            "real_sol": round((cv.get("real_quote_reserves") or 0) / 1e9, 3)}


def recent_candidates() -> list:
    out = []
    for path in ("/api/scout/signals", "/api/signals"):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:8790{path}", timeout=5) as r:
                d = json.loads(r.read())
        except Exception:  # noqa: BLE001
            continue
        for s in (d.get("signals") or []):
            m = s.get("mint") or s.get("ca")
            if m and m.endswith("pump") and m not in out:
                out.append(m)
    return out


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--curve-mint")
    ap.add_argument("--sol", type=float, default=0.01)
    a = ap.parse_args()
    cfg = config()
    url = os.getenv("LIVE_RPC_URL") or rpc.rpc_url()
    pk = jupiter.pubkey()
    print(json.dumps({"wallet": pk, "exec_enabled": cfg["exec_enabled"], "autotrade_enabled": cfg["autotrade_enabled"],
                      "rpc": url.split("?")[0], "mode": "DRY simulateTransaction only — /execute is never called"}))
    if not pk:
        print("keypair not loadable")
        return 2
    lam = int(a.sol * 1e9)
    curve = a.curve_mint
    if not curve:
        for m in recent_candidates()[:40]:
            st = await curve_state(m, url)
            if st.get("on_curve") and st.get("real_sol", 0) >= 1:
                curve = m
                break
    results = {}
    for label, mint in (("liquid_USDC", USDC), ("pump_curve", curve)):
        if not mint:
            results[label] = {"ok": False, "error": "no curve candidate found (pass --curve-mint)"}
            continue
        st = await curve_state(mint, url) if label == "pump_curve" else {}
        t0 = time.time()
        r = await jupiter.simulate_order(input_mint=jupiter.WSOL, output_mint=mint, amount=lam,
                                         slippage_bps=cfg["buy_slippage_bps"],
                                         max_priority_lamports=cfg["max_priority_lamports"], rpc_url=url)
        o = r.get("order") or {}
        results[label] = {"mint": mint, "curve": st or None, "sim_ok": r.get("ok"), "err": r.get("err"),
                          "units": r.get("units"), "sig_verified": r.get("sig_verified"), "router": o.get("router"),
                          "route": o.get("route"), "in": o.get("in_amount"), "out": o.get("out_amount"),
                          "fee_bps": o.get("fee_bps"), "priority_lamports": o.get("priority_lamports"),
                          "order_error": o.get("error"), "elapsed_s": round(time.time() - t0, 2),
                          "logs_tail": [x[:140] for x in (r.get("logs_tail") or [])] if not r.get("ok") else None}
        await asyncio.sleep(1.2)   # free-tier 1 RPS
    print(json.dumps(results, indent=1))
    return 0 if all(v.get("sim_ok") for v in results.values()) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
