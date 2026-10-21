"""v1.48 `paper_fresh5` — PAPER-ONLY test of the Oct 20 sell-side lead (Sky approved Oct 20 2026 6:17 PM CT).

Rule: every fomo_top_trader candidate (entered OR skipped) whose pair is < PAPER_FRESH5_MAX_AGE_S (600 s) old at
decision time → one simulated 0.10 SOL buy, sold after PAPER_FRESH5_HOLD_S (300 s). One entry per mint.
Backtest (13 days, candidate_outcomes marks): n=121, +1.30 SOL ex-best-3, both halves positive. Needs ~7 days of
fresh, realistic paper data before any live decision (bar: profitable in both halves without the best 3).

Hard guarantees: PAPER ONLY. Never imports the signer, never calls Jupiter, never touches live positions, caps, loss
limit, kill switch or wallet. "paper_fresh5" is not in app.live.config.ARMS, so LIVE_ARMS can never route it live.
Pricing (app.paper_realism, the live-calibrated model): entry = first on-chain price (pump curve / PumpSwap pool) read
after the measured live signal→land delay, our size simulated against those reserves, never below the decision ref
price; exit = on-chain price at +300 s × live generic sell ratio; live fee bps + priority + tip on both legs.
No on-chain price (non-pump DEX) → logged as kind=skip, no trade.
Log: data/paper_fresh5.jsonl (kind open / close / skip; close rows carry pnl_sol). Off: PAPER_FRESH5_ENABLED=false.
Open positions are not persisted; a restart orphans them (logged open without close → ignored by the grader).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Dict, Optional

from app.paths import data_dir

logger = logging.getLogger("coach.paper.fresh5")
ARM = "paper_fresh5"
SOURCE_ARM = "fomo_top_trader"


def _f(name: str, d: float) -> float:
    try:
        return float(os.getenv(name, str(d)).strip())
    except ValueError:
        return d


def cfg() -> Dict[str, Any]:
    return {"enabled": os.getenv("PAPER_FRESH5_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"},
            "max_age_s": _f("PAPER_FRESH5_MAX_AGE_S", 600), "hold_s": _f("PAPER_FRESH5_HOLD_S", 300),
            "size_sol": _f("PAPER_FRESH5_SIZE_SOL", 0.10), "max_open": int(_f("PAPER_FRESH5_MAX_OPEN", 30)),
            "log_path": data_dir() / "paper_fresh5.jsonl"}


class PaperFresh5:
    def __init__(self) -> None:
        self.seen: set = set()
        self.open = 0
        self._tasks: set = set()
        self._loaded = False

    def _load(self) -> None:
        self._loaded = True
        p = cfg()["log_path"]
        try:
            if p.exists():
                for line in p.open(errors="replace"):
                    try:
                        self.seen.add(json.loads(line).get("mint"))
                    except ValueError:
                        pass
        except Exception:  # noqa: BLE001
            pass

    def _log(self, kind: str, **rec: Any) -> None:
        try:
            p = cfg()["log_path"]
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"ts_ms": int(time.time() * 1000), "kind": kind, "arm": ARM, "paper": True, **rec},
                                    default=str) + "\n")
        except Exception as exc:  # noqa: BLE001
            logger.warning("paper_fresh5 log failed: %s", type(exc).__name__)

    def on_candidate(self, row: Dict[str, Any]) -> None:
        """Called by app.learning.candidates.log_candidate after a row is written. Never raises."""
        try:
            if row.get("arm") != SOURCE_ARM or row.get("stage") or row.get("excluded"):
                return
            c = cfg()
            if not c["enabled"]:
                return
            if ARM in {a.strip() for a in os.getenv("LIVE_ARMS", "").split(",")}:
                return                                         # belt and braces: never alongside a live arm
            if not self._loaded:
                self._load()
            mint = row.get("mint")
            f = row.get("features") or {}
            pc = f.get("pair_created_ms")
            if not mint or not pc or mint in self.seen:
                return
            age_s = (int(row.get("decision_ms") or row["ts_ms"]) - int(pc)) / 1000
            if age_s < 0 or age_s >= c["max_age_s"]:
                return
            self.seen.add(mint)
            if self.open >= c["max_open"]:
                self._log("skip", mint=mint, cid=row.get("cid"), why="paper max_open")
                return
            t = asyncio.get_running_loop().create_task(self._trade(row, mint, age_s, c))
            self._tasks.add(t)
            t.add_done_callback(self._tasks.discard)
        except RuntimeError:
            pass                                               # no running loop (offline script) → nothing to do
        except Exception as exc:  # noqa: BLE001
            logger.warning("paper_fresh5 hook: %s", type(exc).__name__)

    async def _trade(self, row: Dict[str, Any], mint: str, age_s: float, c: Dict[str, Any]) -> None:
        from app import paper_realism as pr
        self.open += 1
        try:
            P = pr.params(ARM)
            size = c["size_sol"]
            await asyncio.sleep(max(0.0, P["lat_ms"] / 1000))
            oc = await pr.onchain_price(mint)
            ref = row.get("ref_price_sol")
            base = {"mint": mint, "cid": row.get("cid"), "src_entered": row.get("entered"),
                    "src_skip": row.get("skip_reason"), "pair_age_s": round(age_s, 1),
                    "dex": (row.get("features") or {}).get("dex"), "ref_price_sol": ref}
            if not oc:
                self._log("skip", why="no on-chain price (realistic entry skipped)", **base)
                return
            ef = pr.entry_fill(ARM, None, onchain=oc, size_sol=size, P=P)
            if not ef.get("ok"):
                self._log("skip", why=ef.get("reason") or "entry_fill refused", **base)
                return
            px = max(float(ef["px"]), float(ref) if ref and 0.2 <= ref / float(ef["px"]) <= 5 else 0.0)
            spent, tokens = pr.entry_position(P, px, size)
            t0 = int(time.time() * 1000)
            self._log("open", entry_px=px, spent_lamports=spent, tokens=tokens, entry_src=ef.get("src"),
                      lat_ms=P["lat_ms"], **base)
            await asyncio.sleep(c["hold_s"])
            oc2 = await pr.onchain_price(mint)
            mark = (oc2 or {}).get("px")
            if not mark:
                # no price after 5 min (rugged / unreadable) → count as a total loss, conservative
                got, xp, why = 0, 0.0, "no price at exit (counted -100%)"
            else:
                xp = pr.exit_px(P, "time_exit", float(mark), px)
                got, why = pr.proceeds_lamports(P, tokens, xp), "time_exit_300s"
            pnl = (got - spent) / 1e9
            self._log("close", exit_px=xp, mark_px=mark, received_lamports=got, spent_lamports=spent,
                      pnl_sol=round(pnl, 6), ret=round(pnl / size, 4), exit_reason=why,
                      held_s=round((time.time() * 1000 - t0) / 1000, 1), **base)
        except Exception as exc:  # noqa: BLE001
            logger.warning("paper_fresh5 trade %s: %s", mint[:6], type(exc).__name__)
        finally:
            self.open -= 1


paper = PaperFresh5()
