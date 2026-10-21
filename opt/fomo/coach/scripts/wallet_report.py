#!/usr/bin/env python3
"""v1.17 wallet scores report — ADVISORY ONLY (nothing here changes the coach's wallets or weights).

  python3 scripts/wallet_report.py              # print the ranked table from data/wallet_scores.json
  python3 scripts/wallet_report.py --role scout # one role (scout | confirmer | fomo)
  python3 scripts/wallet_report.py --dead       # only the dead-wallet recommendations
  python3 scripts/wallet_report.py --refresh    # recompute now in THIS process (alias --rebuild)
      faster one-off: WALLET_SCORES_CONCURRENCY=4 WALLET_SCORES_RPS=3 WALLET_SCORES_GT_PER_MIN=25 (run it from a
      different host than the live coach so its public-RPC budget is untouched); GT calls are grouped per pool
  python3 scripts/wallet_report.py --json       # raw JSON
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.learning import wallet_scores as ws  # noqa: E402


def _pct(x):
    return "   -  " if x is None else f"{x * 100:+6.1f}%"


def table(d: dict, role: str | None = None, dead_only: bool = False) -> str:
    rows = [w for w in d.get("wallets") or [] if role is None or w.get("role") == role]
    if dead_only:
        rows = [w for w in rows if w.get("recommendations")]
    gen = d.get("generated_ms")
    out = [f"Wallet scores (advisory only) — generated {time.strftime('%Y-%m-%d %H:%M %Z', time.localtime(gen / 1000)) if gen else '?'}"
           f", window {d.get('window_days')} d, counts {json.dumps(d.get('counts'))}",
           f"{'#':>3} {'label':<16} {'role':<9} {'buys7d':>6} {'n15':>4} {'win15':>6} {'win CI':>13} {'med5':>7} "
           f"{'med15':>7} {'med60':>7} {'mean15':>7} {'score':>7} {'days':>5}  flags"]
    for w in rows:
        oc = w.get("onchain") or {}
        h5, h15, h60 = oc.get("5m") or {}, oc.get("15m") or {}, oc.get("60m") or {}
        ci = h15.get("win_rate_ci95")
        out.append(f"{w.get('rank', 0):>3} {str(w.get('label'))[:16]:<16} {str(w.get('role')):<9} {oc.get('buys_7d', 0):>6} "
                   f"{h15.get('n', 0):>4} {(_pct(h15.get('win_rate')) if h15.get('n') else '   -  '):>6} "
                   f"{(f'{ci[0]:.2f}-{ci[1]:.2f}' if ci else '-'):>13} {_pct(h5.get('median')):>7} {_pct(h15.get('median')):>7} "
                   f"{_pct(h60.get('median')):>7} {_pct(h15.get('mean_clipped')):>7} {w.get('score', 0):>+7.3f} "
                   f"{str((w.get('activity') or {}).get('days_since_last_trade', '-')):>5}  {','.join(w.get('flags') or [])}")
    recs = [r for r in d.get("recommendations") or [] if role is None or r.get("role") == role]
    if recs:
        out.append("")
        out.append("Recommendations (advisory — nothing is changed automatically):")
        for r in recs:
            out.append(f"  - {r.get('label')} ({r.get('role')}, {str(r.get('wallet'))[:6]}…): {'; '.join(r.get('why') or [])}")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--role")
    ap.add_argument("--dead", action="store_true")
    ap.add_argument("--refresh", "--rebuild", dest="refresh", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    if a.refresh:
        d = asyncio.run(ws.compute(progress=lambda i, n, w, r: print(f"{i}/{n} {w.get('label')}", file=sys.stderr)))
        ws.write(d)
    else:
        d = ws.read()
        if not d:
            print(f"no scores yet at {ws.path()} — run with --refresh (or wait for the daily job)")
            return 1
    print(json.dumps(d, indent=1) if a.json else table(d, a.role, a.dead))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
