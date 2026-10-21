"""v1.37 realistic scout paper (Sky, Oct 18 2026: "have paper act more like live"). PAPER ONLY.

One realistic position per scout paper signal, priced and exited the way the live scout arm really trades:
  * entry = pump curve read at detect + the live p50 signal→land latency (scout route), our live size simulated
    against those reserves, never below the scout's own fill (app.paper_realism.entry_fill);
  * exits = the LIVE rule set of the live twin arm, evaluated together (app.live.engine.evaluate_exit: tp2x_trail +
    live trail, min10, 35% hard stop, trader sells inside the trader-sell window, 15 s candle stop, max hold), on the
    scout engine's own 2 s marks; fills via live sell ratios + live fees; price-proxy liquidity drain;
  * every entry tagged with the live gates that would refuse it (chase filter, night pause, trades/day cap, loss limit,
    max open) on a per-arm virtual live ledger.
The legacy scout paper rows (per rule × +2 s/+5 s) are unchanged and kept for comparison.
Log data/scout_real.jsonl, state data/scout_real_state.json. Never imports the signer, never calls Jupiter.
"""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from app import paper_realism as pr
from app.paths import data_dir

logger = logging.getLogger("coach.scout.real")


def _paths():
    return (Path(os.getenv("SCOUT_REAL_LOG_PATH", str(data_dir() / "scout_real.jsonl"))),
            Path(os.getenv("SCOUT_REAL_STATE_PATH", str(data_dir() / "scout_real_state.json"))))


class ScoutReal:
    def __init__(self) -> None:
        self.pos: Dict[str, Any] = {}                    # id -> LivePos (realistic)
        self.meta: Dict[str, Dict[str, Any]] = {}
        self.ledgers: Dict[str, pr.GateLedger] = collections.defaultdict(pr.GateLedger)
        self.drain = pr.DrainWatch()
        self.seen: set = set()                           # (arm, mint)
        self.stats: Dict[str, int] = collections.defaultdict(int)
        self.loaded = False
        self.tags: Dict[str, set] = {}                   # v1.40d mint -> tags (judged in report()['by_tag'])

    def tag(self, mint: str, tag: str, signal_id: Optional[str] = None) -> None:
        """v1.40d: tag a mint's realistic entries (open now or opened later)."""
        if tag in self.tags.setdefault(mint, set()):
            return
        self.tags[mint].add(tag)
        self.stats["tagged_" + tag] += 1
        for pid, p in list(self.pos.items()):
            if p.mint == mint:
                self._tag_pid(pid, mint, tag, signal_id)

    def _tag_pid(self, pid: str, mint: str, tag: str, signal_id: Optional[str] = None) -> None:
        m = self.meta.setdefault(pid, {})
        if tag in (m.get("tags") or []):
            return
        m.setdefault("tags", []).append(tag)
        self._log("real_tag", id=pid, arm=m.get("arm"), mint=mint, tag=tag, signal_id=signal_id or m.get("signal_id"))

    # ---------------- persistence ----------------
    def _log(self, kind: str, **rec: Any) -> None:
        p, _ = _paths()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"ts_ms": int(time.time() * 1000), "kind": kind, "paper": True, "realistic": True,
                                     **rec}, default=str) + "\n")
        except Exception as exc:  # noqa: BLE001
            logger.warning("scout real log failed: %s", type(exc).__name__)

    def load(self) -> None:
        self.loaded = True
        _, sp = _paths()
        try:
            st = json.loads(sp.read_text())
        except Exception:  # noqa: BLE001
            return
        from app.live.engine import LivePos
        for pid, d in (st.get("open") or {}).items():
            try:
                self.pos[pid] = LivePos(**d["pos"])
                self.meta[pid] = d.get("meta") or {}
                self.seen.add((self.meta[pid].get("arm"), self.pos[pid].mint))
            except Exception:  # noqa: BLE001
                continue
        for arm, d in (st.get("ledgers") or {}).items():
            self.ledgers[arm].load(d)

    def save(self) -> None:
        _, sp = _paths()
        try:
            sp.parent.mkdir(parents=True, exist_ok=True)
            tmp = sp.with_suffix(".tmp")
            tmp.write_text(json.dumps({"open": {pid: {"pos": asdict(p), "meta": self.meta.get(pid, {})}
                                                for pid, p in self.pos.items() if p.status != "closed"},
                                       "ledgers": {a: l.to_dict() for a, l in self.ledgers.items()}}, default=str))
            tmp.replace(sp)
        except Exception as exc:  # noqa: BLE001
            logger.warning("scout real save failed: %s", type(exc).__name__)

    def open_mints(self) -> set:
        return {p.mint for p in self.pos.values() if p.status != "closed"}

    # ---------------- entry ----------------
    async def open(self, arm: str, mint: str, signal_id: str, detect_ms: int, ref_price: Optional[float],
                   scout_wallet: Optional[str]) -> Optional[str]:
        if not pr.enabled():
            return None
        if not self.loaded:
            self.load()
        if (arm, mint) in self.seen:
            return None
        self.seen.add((arm, mint))
        P = pr.params(arm)
        onchain = None
        if pr.onchain_enabled():
            wait = (detect_ms + P["lat_ms"] - time.time() * 1000) / 1000
            if wait > 0:
                await asyncio.sleep(min(wait, 15))
            onchain = await pr.onchain_price(mint)
        ef = pr.entry_fill(arm, ref_price, onchain, None, P["size_sol"], P)
        if (ef.get("ok") and ef.get("src") == "trader_fill+live_slip" and pr.onchain_enabled() and onchain is None
                and os.environ.get("PAPER_REALISM_SKIP_UNPRICED", "true").strip().lower() not in ("0", "false", "no", "off")):
            # v1.46: no SOL on-chain price (PUMP/USDC-quoted curve or unreadable) -> trader fill is in the wrong units
            # and produced fake 30x+ paper wins; skip instead of pricing off it.
            ef = dict(ef, ok=False, reason="v1.46 no SOL on-chain price (non-SOL quote/unreadable): trader-fill units untrusted, realistic entry skipped")
        info = {k: ef.get(k) for k in ("ok", "px", "src", "mark_px", "sim_px", "floor_px", "trader_px", "lat_ms",
                                         "slip_pct", "route", "rejected", "reason", "floored_to_trader")}
        if not ef.get("ok"):
            self.stats[f"{arm}_skipped"] += 1
            self._log("real_skip", arm=arm, mint=mint, signal_id=signal_id, **info)
            return None
        from app.live.engine import LivePos
        now = int(time.time() * 1000)
        epx = float(ef["px"])
        spent, tokens = pr.entry_position(P, epx, P["size_sol"])
        refused = self.ledgers[arm].refusals(arm, now, would_live_buy=None, trader_px=ref_price, entry_px=epx, P=P)
        pid = f"sr{now:x}{mint[:4]}"
        if not refused:
            self.ledgers[arm].opened(pid, now)
        self.pos[pid] = LivePos(mint=mint, arm=P["live_arm"], size_sol=P["size_sol"], spent_lamports=spent,
                                tokens_initial=tokens, tokens_left=tokens, entry_price_sol=epx, entry_ms=now,
                                decimals=6, id=pid, trader_wallets=[scout_wallet] if scout_wallet else [],
                                high_price_sol=epx, last_price_sol=(onchain or {}).get("px") or epx, last_mark_ms=now,
                                mark_source=ef.get("src"), flags=["paper", "realistic"])
        self.meta[pid] = {"arm": arm, "signal_id": signal_id, "ref_price_sol": ref_price, "gates_refused": refused,
                          "gated": not refused, "fill_vs_trader_pct": round((epx / ref_price - 1) * 100, 2)
                          if ref_price else None, "entry": info}
        self.stats[f"{arm}_opened"] += 1
        self._log("real_open", id=pid, arm=arm, mint=mint, signal_id=signal_id, entry_price_sol=epx,
                  size_sol=P["size_sol"], spent_lamports=spent, gates_refused=refused, gated=not refused,
                  live_arm=P["live_arm"], fill_vs_trader_pct=self.meta[pid]["fill_vs_trader_pct"],
                  **{k: v for k, v in info.items() if k != "px"})
        for tg in sorted(self.tags.get(mint, ())):           # v1.40d tag known before the open
            self._tag_pid(pid, mint, tg, signal_id)
        self.save()
        return pid

    # ---------------- marks / exits ----------------
    def on_trader_sell(self, wallet: str, mint: str) -> None:
        for p in self.pos.values():
            if p.status != "closed" and p.mint == mint and wallet in (p.trader_wallets or []) and not p.trader_sold:
                p.trader_sold = True
                p.meta["trader_sold_ms"] = int(time.time() * 1000)

    def on_mark(self, mint: str, price: float, now: int) -> None:
        if not self.pos:
            return
        from app.live.config import config as live_config
        from app.live.engine import evaluate_exit, is_partial
        lc = live_config()
        changed = False
        for pid, p in list(self.pos.items()):
            if p.mint != mint or p.status == "closed":
                continue
            P = pr.params(self.meta[pid].get("arm") or "scout_solo")
            p.last_price_sol, p.last_mark_ms = price, now
            p.high_price_sol = max(p.high_price_sol or price, price)
            acts = evaluate_exit(p, price, now, lc, list((lc.get("rules") or {}).get(p.arm) or []))
            why = self.drain.check(pid, price, now, P)
            if why and not acts:
                acts = [("sell_all", 1.0, "rug_flag_liquidity_drain")]
                self.meta[pid]["drain"] = why
            for kind, frac, reason in acts:
                if kind == "sell_frac":
                    p.tp_done = p.tp_done or not reason.startswith("tpfast")
                    if reason not in p.ladder_done:
                        p.ladder_done.append(reason)
                    tokens = int(p.tokens_left * frac)
                else:
                    tokens = p.tokens_left
                if tokens <= 0:
                    continue
                sell_px = pr.exit_px(P, reason, price, p.entry_price_sol, lc)
                basis = p.cost_basis(tokens)
                lam = pr.proceeds_lamports(P, tokens, sell_px)
                p.tokens_left -= tokens
                p.received_lamports += lam
                fill = {"ts_ms": now, "reason": reason, "tokens": tokens, "sol": round(lam / 1e9, 6),
                        "pnl_sol": round((lam - basis) / 1e9, 6), "price_sol": sell_px, "mark_price_sol": price,
                        "multiple": round(sell_px / p.entry_price_sol, 4)}
                p.sells.append(fill)
                self._log("real_sell", id=pid, arm=self.meta[pid].get("arm"), mint=mint, **fill)
                changed = True
                if p.tokens_left <= 0 or not is_partial(reason):
                    p.status, p.exit_reason, p.closed_ms = "closed", reason, now
                    pn = p.pnl()
                    m = self.meta[pid]
                    self.ledgers[m.get("arm") or "scout_solo"].closed(pid, now, pn["pnl_sol"])
                    self._log("real_close", id=pid, arm=m.get("arm"), mint=mint, reason=reason, pnl_sol=pn["pnl_sol"],
                              pnl_pct=pn["pnl_pct"], size_sol=p.size_sol, hold_s=round((now - p.entry_ms) / 1000),
                              gated=m.get("gated"), gates_refused=m.get("gates_refused"),
                              fill_vs_trader_pct=m.get("fill_vs_trader_pct"),
                              max_multiple=round((p.high_price_sol or 0) / p.entry_price_sol, 4))
                    self.drain.drop(pid)
                    break
        for pid in [k for k, p in self.pos.items() if p.status == "closed"]:
            self.pos.pop(pid, None)
            self.meta.pop(pid, None)
        if changed:
            self.save()

    # ---------------- report ----------------
    def report(self) -> Dict[str, Any]:
        lp, _ = _paths()
        rows: List[Dict[str, Any]] = []
        try:
            with lp.open() as fh:
                for line in fh:
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        continue
        except FileNotFoundError:
            pass
        closes = [r for r in rows if r.get("kind") == "real_close"]

        def agg(rs):
            n = len(rs)
            tot = sum(r.get("pnl_sol") or 0 for r in rs)
            w = sum(1 for r in rs if (r.get("pnl_sol") or 0) > 0)
            return {"closed": n, "wins": w, "win_rate": round(w / n, 3) if n else None, "pnl_sol": round(tot, 6),
                    "avg_pnl_pct": round(sum(r.get("pnl_pct") or 0 for r in rs) / n, 2) if n else None}
        arms = sorted({r.get("arm") for r in rows if r.get("arm")})
        return {"basis": "realistic (live-calibrated entry/exit/fees, live combined rule set)",
                "by_arm": {a: {"all": agg([r for r in closes if r.get("arm") == a]),
                               "gated": agg([r for r in closes if r.get("arm") == a and r.get("gated")]),
                               "opened": sum(1 for r in rows if r.get("kind") == "real_open" and r.get("arm") == a),
                               "skipped": sum(1 for r in rows if r.get("kind") == "real_skip" and r.get("arm") == a),
                               "open_now": sum(1 for pid, p in self.pos.items()
                                               if self.meta.get(pid, {}).get("arm") == a and p.status != "closed")}
                           for a in arms},
                "by_tag": {t: agg([r for r in closes if r.get("id") in {x.get("id") for x in rows
                                                                       if x.get("kind") == "real_tag" and x.get("tag") == t}])
                           for t in sorted({x.get("tag") for x in rows if x.get("kind") == "real_tag" and x.get("tag")})},
                "realism": pr.describe(), "stats": dict(self.stats)}


real = ScoutReal()
