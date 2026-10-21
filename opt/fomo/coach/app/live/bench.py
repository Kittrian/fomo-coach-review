"""v1.21 scout benching (Sky, 2026-10-08 11:22 PM CT).

* A scout wallet whose LIVE copies lose LIVE_BENCH_LOSS_STREAK (5) times in a row is benched: its signals are
  refused live (reason "scout bench: … benched") and copied on PAPER instead, with the same live exit logic
  (evaluate_exit + live scout rules, trader-sell exits forwarded).
* It is unbenched after LIVE_UNBENCH_PAPER_WINS (2) paper wins in a row, or manually (POST /api/live/bench/unbench
  or LIVE_BENCH_UNBENCH=<wallet,…> in .env, applied at load).
* Only live copies whose entry is at/after the bench start (first v1.21 start) count, so nobody starts benched.
* Credit: every trigger wallet of the signal (scout wallet + arm-B confirmer). A win resets the streak.
* PAPER ONLY for benched signals: this module never signs, never calls Jupiter/pumpdirect, never touches the wallet,
  the live positions, caps, loss limit or kill switch. Marks: fresh pump curve read (read-only RPC getAccountInfo
  via the injected reader) else DexScreener public marks.
Off switch: LIVE_BENCH_ENABLED (default false in code).
"""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

from app.paths import data_dir

logger = logging.getLogger("coach.live.bench")
SCOUT_ARMS = ("scout_solo", "scout_confirmed")
TZ = ZoneInfo("America/Chicago")
DECIMALS = 6


def _f(name: str, d: float) -> float:
    try:
        return float(os.getenv(name, str(d)).strip())
    except ValueError:
        return d


def _list(name: str) -> List[str]:
    return [x.strip() for x in os.getenv(name, "").split(",") if x.strip()]


def cfg() -> Dict[str, Any]:
    return {
        "enabled": os.getenv("LIVE_BENCH_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"},
        "loss_streak": max(1, int(_f("LIVE_BENCH_LOSS_STREAK", 5))),
        "unbench_wins": max(1, int(_f("LIVE_UNBENCH_PAPER_WINS", 2))),
        "exempt": set(_list("LIVE_BENCH_EXEMPT")),
        "manual_unbench": _list("LIVE_BENCH_UNBENCH"),
        "size_sol": _f("LIVE_BENCH_PAPER_SIZE_SOL", 0.20),
        "cost_bps": int(_f("LIVE_BENCH_PAPER_COST_BPS", 150)),        # per side
        "entry_premium_pct": _f("LIVE_BENCH_PAPER_PREMIUM_PCT", 7.7),  # ref-price fallback only (median live chase)
        "mark_s": _f("LIVE_BENCH_PAPER_MARK_S", 3),
        "max_open": int(_f("LIVE_BENCH_PAPER_MAX_OPEN", 20)),
        "state_path": Path(os.getenv("LIVE_BENCH_STATE_PATH", str(data_dir() / "scout_bench.json"))),
        # v1.38: which paper result drives the unbench streak. "legacy" (default) keeps live behaviour exactly as
        # before; "realistic" would use the live-calibrated twin (changes WHEN wallets return to live → needs Sky's OK).
        "unbench_basis": os.getenv("LIVE_BENCH_UNBENCH_BASIS", "legacy").strip().lower(),
        "log_path": Path(os.getenv("LIVE_BENCH_LOG_PATH", str(data_dir() / "scout_bench.jsonl"))),
    }


def _ct(ms: Optional[int]) -> Optional[str]:
    return datetime.fromtimestamp(ms / 1000, TZ).strftime("%a %b %-d %-I:%M %p CT") if ms else None


def _short(a: str) -> str:
    return f"{a[:4]}…{a[-4:]}" if a and len(a) > 10 else a


class ScoutBench:
    def __init__(self) -> None:
        self.started_ms: Optional[int] = None
        self.wallets: Dict[str, Dict[str, Any]] = {}     # wallet -> {streak, benched, benched_ms, paper_wins, …}
        self.paper: Dict[str, Any] = {}                  # pid -> LivePos (paper)
        self.paper_meta: Dict[str, Dict[str, Any]] = {}
        self.paper_real: Dict[str, Any] = {}             # v1.38 pid -> LivePos (realistic twin, paper)
        self._mark_src: Dict[str, str] = {}
        self.recorded: List[str] = []                    # live position ids already counted (idempotent)
        self.stats: Dict[str, int] = collections.defaultdict(int)
        self.curve_reader: Optional[Callable[[str], Awaitable[Optional[Dict[str, Any]]]]] = None
        self.loaded = False
        self._loop: Optional[asyncio.Task] = None

    # ---------------- persistence ----------------
    def load(self) -> None:
        c = cfg()
        self.loaded = True
        try:
            st = json.loads(c["state_path"].read_text())
        except Exception:  # noqa: BLE001
            st = {}
        from app.live.engine import LivePos
        self.started_ms = st.get("started_ms")
        self.wallets = dict(st.get("wallets") or {})
        self.recorded = list(st.get("recorded") or [])[-500:]
        self.paper, self.paper_meta, self.paper_real = {}, {}, {}
        for pid, b in (st.get("paper") or {}).items():
            try:
                if b.get("pos"):
                    self.paper[pid] = LivePos(**b["pos"])
                self.paper_meta[pid] = b.get("meta") or {}
                if b.get("real_pos"):
                    self.paper_real[pid] = LivePos(**b["real_pos"])
            except Exception:  # noqa: BLE001
                continue
        if c["enabled"] and not self.started_ms:
            self.started_ms = int(time.time() * 1000)
            self._log("start", started_ms=self.started_ms, started_ct=_ct(self.started_ms),
                      loss_streak=c["loss_streak"], unbench_wins=c["unbench_wins"])
        for w in c["manual_unbench"]:
            if self.wallets.get(w, {}).get("benched"):
                self.unbench(w, "env LIVE_BENCH_UNBENCH")
        self.save()
        if self.paper or self.paper_real:
            self.ensure_loop()

    def _ensure(self) -> None:
        if not self.loaded:
            self.load()

    def save(self) -> None:
        c = cfg()
        try:
            c["state_path"].parent.mkdir(parents=True, exist_ok=True)
            tmp = c["state_path"].with_suffix(".tmp")
            tmp.write_text(json.dumps({"started_ms": self.started_ms, "wallets": self.wallets,
                                       "recorded": self.recorded[-500:],
                                       "paper": {pid: {"pos": asdict(self.paper[pid]) if pid in self.paper else None,
                                                       "meta": self.paper_meta.get(pid, {}),
                                                       "real_pos": asdict(self.paper_real[pid])
                                                       if pid in self.paper_real else None}
                                                 for pid in set(self.paper) | set(self.paper_real)}}, default=str))
            tmp.replace(c["state_path"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("scout bench save failed: %s", type(exc).__name__)

    def _log(self, kind: str, **rec: Any) -> None:
        p = cfg()["log_path"]
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"ts_ms": int(time.time() * 1000), "kind": kind, **rec}, default=str) + "\n")
        except Exception as exc:  # noqa: BLE001
            logger.warning("scout bench log failed: %s", type(exc).__name__)

    def _w(self, wallet: str) -> Dict[str, Any]:
        return self.wallets.setdefault(wallet, {"streak": 0, "benched": False, "paper_wins": 0, "live_wins": 0,
                                                "live_losses": 0})

    # ---------------- queries ----------------
    def benched_of(self, wallets: List[Optional[str]]) -> List[str]:
        c = cfg()
        if not c["enabled"]:
            return []
        self._ensure()
        return [w for w in dict.fromkeys(x for x in wallets if x)
                if self.wallets.get(w, {}).get("benched") and w not in c["exempt"]]

    # ---------------- live outcomes ----------------
    def record_live(self, p: Any) -> Optional[str]:
        """Called when a live position closes. Returns 'win' / 'loss' / None (not counted)."""
        c = cfg()
        if not c["enabled"] or p.arm not in SCOUT_ARMS or "paper" in (p.flags or []):
            return None
        self._ensure()
        if not self.started_ms or p.entry_ms < self.started_ms or p.id in self.recorded:
            return None
        pnl = float(p.pnl().get("pnl_sol") or 0.0)
        res = "loss" if pnl < 0 else "win"
        self.recorded.append(p.id)
        wallets = list(dict.fromkeys([*(p.trader_wallets or []), (p.meta or {}).get("confirmer_wallet")]))
        for w in [x for x in wallets if x]:
            s = self._w(w)
            if res == "loss":
                s["streak"] += 1
                s["live_losses"] += 1
            else:
                s["streak"] = 0
                s["live_wins"] += 1
            s["last_live_ms"] = int(time.time() * 1000)
            if res == "loss" and s["streak"] >= c["loss_streak"] and not s["benched"] and w not in c["exempt"]:
                s.update(benched=True, benched_ms=int(time.time() * 1000), paper_wins=0,
                         benched_why=f"{s['streak']} losing live copies in a row")
                self.stats["benched"] += 1
                self._log("bench", wallet=w, streak=s["streak"], last_mint=p.mint, last_pnl_sol=round(pnl, 6))
                logger.warning("scout bench: %s BENCHED after %s losing live copies in a row (last %s %+.4f SOL)",
                               _short(w), s["streak"], p.mint[:8], pnl)
        self._log("live_result", id=p.id, mint=p.mint, arm=p.arm, wallets=wallets, result=res, pnl_sol=round(pnl, 6))
        self.save()
        return res

    def unbench(self, wallet: str, why: str) -> bool:
        self._ensure()
        s = self.wallets.get(wallet)
        if not s or not s.get("benched"):
            return False
        s.update(benched=False, streak=0, paper_wins=0, unbenched_ms=int(time.time() * 1000), unbenched_why=why)
        self.stats["unbenched"] += 1
        self._log("unbench", wallet=wallet, why=why)
        logger.warning("scout bench: %s UNBENCHED (%s)", _short(wallet), why)
        self.save()
        return True

    # ---------------- paper copies ----------------
    def paper_open(self, arm: str, mint: str, wallets: List[Optional[str]], ref_price_sol: Optional[float],
                   meta: Optional[Dict[str, Any]] = None) -> Optional[str]:
        c = cfg()
        if not c["enabled"]:
            return None
        self._ensure()
        bw = self.benched_of(wallets)
        if not bw:
            return None
        if len(self.paper) >= c["max_open"]:
            self._log("paper_skip", mint=mint, why="paper book full")
            return None
        if any(p.mint == mint for p in self.paper.values()):
            return None
        pid = f"bench-{mint[:6]}-{int(time.time() * 1000) % 10**8}"
        self.paper_meta[pid] = {"arm": arm, "mint": mint, "benched": bw, "ref_price_sol": ref_price_sol,
                                "symbol": (meta or {}).get("symbol"), "pending": True}
        self.stats["paper_signals"] += 1
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._enter_sync(pid, None, ref_price_sol)
            return pid
        loop.create_task(self._enter(pid, ref_price_sol))
        return pid

    async def _mark(self, mint: str) -> Optional[float]:
        if self.curve_reader is not None:
            try:
                from app.pump.curve import curve_price_sol
                cv = await asyncio.wait_for(self.curve_reader(mint), timeout=3)
                if cv and not cv.get("complete"):
                    self._mark_src[mint] = "curve"
                    return curve_price_sol(cv)
            except Exception:  # noqa: BLE001
                pass
        try:
            from app.fomo_onchain import WSOL_MINT, market
            mk = await market.marks([mint], force=True)
            sol = (mk.get(WSOL_MINT) or {}).get("price_usd") or market.sol_usd()
            m = mk.get(mint) or {}
            if m.get("price_usd") and sol:
                self._mark_src[mint] = "dexscreener"
                return float(m["price_usd"]) / float(sol)
        except Exception:  # noqa: BLE001
            pass
        return None

    async def _enter(self, pid: str, ref_price_sol: Optional[float]) -> None:
        px = await self._mark(self.paper_meta[pid]["mint"])
        self._enter_sync(pid, px, ref_price_sol)
        self.ensure_loop()

    def _enter_sync(self, pid: str, px: Optional[float], ref: Optional[float]) -> None:
        from app.live.engine import LivePos
        c = cfg()
        m = self.paper_meta[pid]
        src = "curve/dexscreener"
        if not px:
            if not ref:
                self.paper_meta.pop(pid, None)
                self._log("paper_skip", mint=m["mint"], why="no price")
                return
            px, src = float(ref) * (1 + c["entry_premium_pct"] / 100), "ref+premium"
        entry = px * (1 + c["cost_bps"] / 1e4)
        now = int(time.time() * 1000)
        tokens = int(c["size_sol"] / entry * 10 ** DECIMALS)
        self.paper[pid] = LivePos(mint=m["mint"], arm=m["arm"], size_sol=c["size_sol"],
                                  spent_lamports=int(c["size_sol"] * 1e9), tokens_initial=tokens, tokens_left=tokens,
                                  entry_price_sol=entry, entry_ms=now, decimals=DECIMALS, id=pid,
                                  trader_wallets=list(m["benched"]), high_price_sol=entry, last_price_sol=px,
                                  last_mark_ms=now, mark_source=src, flags=["paper", "bench"])
        m.pop("pending", None)
        m["entry_src"] = src
        real = self._open_real(pid, m, px if src != "ref+premium" else None, ref, now)
        self._log("paper_open", id=pid, mint=m["mint"], arm=m["arm"], wallets=m["benched"], entry_price_sol=entry,
                  ref_price_sol=ref, src=src, **real)
        logger.info("scout bench PAPER open %s %s (benched %s)", m["arm"], m["mint"][:8],
                    ",".join(_short(w) for w in m["benched"]))
        self.save()

    # ---------------- v1.38 realistic twin (PAPER ONLY; legacy copy + unbench rule unchanged by default) ----------
    def _open_real(self, pid: str, m: Dict[str, Any], px: Optional[float], ref: Optional[float], now: int) -> Dict[str, Any]:
        try:
            from app import paper_realism as pr
            if not pr.enabled():
                return {}
            from app.live.engine import LivePos
            P = pr.params(m["arm"])
            src = self._mark_src.get(m["mint"])
            onchain = {"px": px, "ms": now, "src": "curve"} if (px and src == "curve") else None
            mark = {"px": px, "ms": now, "src": src or "mark"} if (px and src != "curve") else None
            c_size = cfg()["size_sol"]
            ef = pr.entry_fill(m["arm"], ref, onchain, mark, c_size, P)
            if not ef.get("ok"):
                m["real_skip"] = ef.get("reason")
                return {"real_skipped": ef.get("reason"), "real_rejected": ef.get("rejected")}
            epx = float(ef["px"])
            spent, tokens = pr.entry_position(P, epx, c_size)
            self.paper_real[pid] = LivePos(mint=m["mint"], arm=m["arm"], size_sol=c_size, spent_lamports=spent,
                                           tokens_initial=tokens, tokens_left=tokens, entry_price_sol=epx,
                                           entry_ms=now, decimals=DECIMALS, id=pid + ":real",
                                           trader_wallets=list(m["benched"]), high_price_sol=epx,
                                           last_price_sol=px or epx, last_mark_ms=now, mark_source=ef.get("src"),
                                           flags=["paper", "bench", "realistic"])
            return {"real_entry_price_sol": epx, "real_src": ef.get("src"), "real_rejected": ef.get("rejected"),
                    "real_fill_vs_trader_pct": round((epx / ref - 1) * 100, 2) if ref else None}
        except Exception as exc:  # noqa: BLE001 — the realistic twin must never break the bench copy
            logger.warning("scout bench realistic open failed: %s", type(exc).__name__)
            return {}

    def manage_real(self, pid: str, now: Optional[int] = None) -> Optional[str]:
        from app import paper_realism as pr
        from app.live.config import DEFAULT_RULES, config as live_config
        from app.live.engine import evaluate_exit, is_partial
        p = self.paper_real.get(pid)
        if not p or p.status != "open":
            return None
        lc = live_config()
        P = pr.params(p.arm)
        rules = list((lc.get("rules") or {}).get(p.arm) or DEFAULT_RULES)
        now = now or int(time.time() * 1000)
        stale = now - (p.last_mark_ms or p.entry_ms) > float(lc.get("stale_mark_s", 60)) * 1000
        for kind, frac, reason in evaluate_exit(p, None if stale else p.last_price_sol, now, lc, rules):
            mark = p.last_price_sol or p.entry_price_sol
            if kind == "sell_frac":
                if not reason.startswith("tpfast"):
                    p.tp_done = True
                if reason not in p.ladder_done:
                    p.ladder_done.append(reason)
                tokens = int(p.tokens_left * frac)
            else:
                tokens = p.tokens_left
            if tokens <= 0:
                continue
            spx = pr.exit_px(P, reason, mark, p.entry_price_sol, lc)
            basis = p.cost_basis(tokens)
            lam = pr.proceeds_lamports(P, tokens, spx)
            p.tokens_left -= tokens
            p.received_lamports += lam
            p.sells.append({"ts_ms": now, "reason": reason, "tokens": tokens, "sol": round(lam / 1e9, 6),
                            "pnl_sol": round((lam - basis) / 1e9, 6), "price_sol": spx, "mark_price_sol": mark})
            if p.tokens_left <= 0 or not is_partial(reason):
                p.status, p.exit_reason, p.closed_ms = "closed", reason, now
                self._real_closed(pid, p, reason)
                return reason
        return None

    def _real_closed(self, pid: str, p: Any, reason: str) -> None:
        c = cfg()
        pnl = float(p.pnl().get("pnl_sol") or 0.0)
        win = pnl > 0
        self.stats["paper_closed_real"] += 1
        self._log("paper_close_real", id=pid, mint=p.mint, wallets=p.trader_wallets, reason=reason,
                  pnl_sol=round(pnl, 6), win=win, realistic=True, entry_price_sol=p.entry_price_sol)
        for w in p.trader_wallets or []:
            s = self._w(w)
            s["paper_wins_real"] = s.get("paper_wins_real", 0) + 1 if win else 0
            if s.get("benched") and s["paper_wins_real"] >= c["unbench_wins"]:
                if c["unbench_basis"] == "realistic":
                    self.unbench(w, f"{s['paper_wins_real']} realistic paper wins in a row")
                elif not s.get("would_unbench_realistic_ms"):
                    s["would_unbench_realistic_ms"] = int(time.time() * 1000)
                    self._log("would_unbench_realistic", wallet=w, paper_wins_real=s["paper_wins_real"])
        self.paper_real.pop(pid, None)
        if pid not in self.paper:
            self.paper_meta.pop(pid, None)
        self.save()

    def note_trader_sell(self, wallet: str, mint: str) -> None:
        for p in list(self.paper.values()) + list(self.paper_real.values()):
            if p.mint == mint and wallet in (p.trader_wallets or []) and not p.trader_sold:
                p.trader_sold = True

    def ensure_loop(self) -> None:
        if self._loop and not self._loop.done():
            return
        try:
            self._loop = asyncio.get_running_loop().create_task(self._mark_loop())
        except RuntimeError:
            pass

    async def _mark_loop(self) -> None:
        while self.paper or self.paper_real:
            try:
                now = int(time.time() * 1000)
                for pid in list(set(self.paper) | set(self.paper_real)):
                    p = self.paper.get(pid) or self.paper_real.get(pid)
                    px = await self._mark(p.mint)
                    if px:
                        for q in (self.paper.get(pid), self.paper_real.get(pid)):
                            if q is not None:
                                q.last_price_sol, q.last_mark_ms = px, now
                                q.high_price_sol = max(q.high_price_sol or px, px)
                    if pid in self.paper:
                        self.manage(pid, now)
                    if pid in self.paper_real:
                        try:
                            self.manage_real(pid, now)
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("scout bench realistic manage: %s", type(exc).__name__)
                self.save()
            except Exception as exc:  # noqa: BLE001
                logger.warning("scout bench mark loop: %s", type(exc).__name__)
            await asyncio.sleep(cfg()["mark_s"])

    def manage(self, pid: str, now: Optional[int] = None) -> Optional[str]:
        """Run the live exit logic on one paper copy. Returns the close reason when it closes."""
        from app.live.config import DEFAULT_RULES, config as live_config
        from app.live.engine import evaluate_exit, is_partial
        p = self.paper.get(pid)
        if not p or p.status != "open":
            return None
        lc = live_config()
        rules = list((lc.get("rules") or {}).get(p.arm) or DEFAULT_RULES)
        now = now or int(time.time() * 1000)
        stale = now - (p.last_mark_ms or p.entry_ms) > float(lc.get("stale_mark_s", 60)) * 1000
        c = cfg()
        for kind, frac, reason in evaluate_exit(p, None if stale else p.last_price_sol, now, lc, rules):
            px = p.last_price_sol or p.entry_price_sol
            if kind == "sell_frac":
                if not reason.startswith("tpfast"):
                    p.tp_done = True
                if reason not in p.ladder_done:
                    p.ladder_done.append(reason)
                tokens = int(p.tokens_left * frac)
            else:
                tokens = p.tokens_left
            if tokens <= 0:
                continue
            basis = p.cost_basis(tokens)
            lam = int(tokens / 10 ** DECIMALS * px * 1e9 * (1 - c["cost_bps"] / 1e4))
            p.tokens_left -= tokens
            p.received_lamports += lam
            p.sells.append({"ts_ms": now, "reason": reason, "tokens": tokens, "sol": round(lam / 1e9, 6),
                            "pnl_sol": round((lam - basis) / 1e9, 6), "price_sol": px})
            if p.tokens_left <= 0 or not is_partial(reason):
                p.status, p.exit_reason, p.closed_ms = "closed", reason, now
                self._paper_closed(pid, p, reason)
                return reason
        return None

    def _paper_closed(self, pid: str, p: Any, reason: str) -> None:
        c = cfg()
        pnl = float(p.pnl().get("pnl_sol") or 0.0)
        win = pnl > 0
        self.stats["paper_closed"] += 1
        self._log("paper_close", id=pid, mint=p.mint, wallets=p.trader_wallets, reason=reason,
                  pnl_sol=round(pnl, 6), win=win)
        logger.info("scout bench PAPER close %s %s %+.4f SOL (%s)", p.mint[:8], reason, pnl, "win" if win else "loss")
        for w in p.trader_wallets or []:
            s = self._w(w)
            s["paper_wins"] = s.get("paper_wins", 0) + 1 if win else 0
            if c["unbench_basis"] != "realistic" and s.get("benched") and s["paper_wins"] >= c["unbench_wins"]:
                self.unbench(w, f"{s['paper_wins']} paper wins in a row")
        self.paper.pop(pid, None)
        if pid not in self.paper_real:
            self.paper_meta.pop(pid, None)
        self.save()

    # ---------------- summary ----------------
    def summary(self, short: bool = False) -> Dict[str, Any]:
        c = cfg()
        if c["enabled"]:
            self._ensure()
        benched = [{"wallet": _short(w), "wallet_full": w, "since": _ct(s.get("benched_ms")), "why": s.get("benched_why"),
                    "paper_wins_in_row": s.get("paper_wins", 0)} for w, s in self.wallets.items() if s.get("benched")]
        out: Dict[str, Any] = {"enabled": c["enabled"], "loss_streak": c["loss_streak"],
                               "unbench_paper_wins": c["unbench_wins"], "started": _ct(self.started_ms),
                               "benched": benched, "benched_count": len(benched), "paper_open": len(self.paper),
                               "unbench_basis": c["unbench_basis"], "paper_open_realistic": len(self.paper_real)}
        if not short:
            out.update(streaks={_short(w): {k: s.get(k) for k in ("streak", "live_wins", "live_losses", "benched",
                                                                    "paper_wins", "paper_wins_real",
                                                                    "would_unbench_realistic_ms")}
                                for w, s in self.wallets.items()},
                       paper_realistic=[{"id": pid, "mint": p.mint, **p.pnl()} for pid, p in self.paper_real.items()],
                       paper=[{"id": pid, "mint": p.mint, "age_s": round((time.time() * 1000 - p.entry_ms) / 1000),
                               **p.pnl()} for pid, p in self.paper.items()],
                       stats=dict(self.stats),
                       note="Benched wallets are copied on paper only (no orders, no signing, no wallet).")
        return out


bench = ScoutBench()
