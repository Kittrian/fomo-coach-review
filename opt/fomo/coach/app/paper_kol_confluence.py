"""v1.34 `kol_confluence` — PAPER-ONLY multi-wallet confluence arm (Andrew approved Oct 15, 2026, PAPER ONLY).

Fires when ≥ KOL_CONFLUENCE_N (3) DISTINCT watched wallets whose role is in KOL_CONFLUENCE_ROLES (scouts, confirmers,
kept FOMO wallets, paper_v3 / watch wallets and the cryptohub Discord wallets) buy the same mint within
KOL_CONFLUENCE_WINDOW_MIN (30) minutes. One paper entry per mint, recorded with the existing paper exit ladder
(paper_v3 tracks: live_exit, follow_wallet, half2x_c30) — same machinery and guarantees as paper_v3: never imports the
signer, never calls Jupiter, never touches live positions / sizes / caps / loss limit / kill switch / wallet weights.

Feature `pumped_since_first_alert_pct`: % the price is above the FIRST watched-wallet buy of that mint (any role),
logged on every signal (kind=signal), on the paper entry (open row, meta) and on its fill row, so the daily learner
can look for a skip threshold. It is a FEATURE ONLY — never a filter here (KOL_CONFLUENCE_PUMP_FILTER does not exist).

Feed: app.scout.engine.on_trade calls `tracker(...)` for every watched-wallet trade before any arm routing.
Log data/kol_confluence.jsonl, state data/kol_confluence_state.json. Off switch KOL_CONFLUENCE_ENABLED.
Real-money promotion of any wallet seen here is a PROPOSAL for Andrew only (scripts/learn_daily.py → live_proposals).
"""
from __future__ import annotations

import asyncio
import collections
import logging
import os
import time
from pathlib import Path
from typing import Any, Deque, Dict, Optional, Tuple

from app.paper_fomo_top import _f
from app.paper_v3 import PaperV3, cfg as v3cfg
from app.paths import data_dir

ARM = "kol_confluence"
DEFAULT_ROLES = "scout,confirmer,fomo,paper_v3,watch,cryptohub"
FIRST_ALERT_TTL_MS = 24 * 3600 * 1000
logger = logging.getLogger("coach.kol_confluence")


def live_cfg() -> Dict[str, Any]:
    """v1.35 LIVE routing (Andrew approved Oct 16, 2026). Off switch KOL_CONFLUENCE_LIVE_ENABLED (code default false).
    Size = LIVE_SIZE_SOL_KOL_CONFLUENCE through the normal live engine (same caps, kill switch, rugcheck/mesh,
    reserve, dedupe and exits as every live lane); the arm must also be in LIVE_ARMS. Normal signals count INSIDE
    the trades/day cap. STRONG = ≥ KOL_CONFLUENCE_CAP_BYPASS_MIN_WALLETS distinct wallets within the window
    (counted by KOL_CONFLUENCE_CAP_BYPASS_COUNT: cryptohub = cryptohub-role wallets only, all = every role) AND a
    passing Jev verdict → only the trades/day cap is skipped (logged as cap_bypass on the trade)."""
    return {"enabled": _on("KOL_CONFLUENCE_LIVE_ENABLED", "false"),
            "bypass_enabled": _on("KOL_CONFLUENCE_CAP_BYPASS_ENABLED", "true"),
            "bypass_require_jev": _on("KOL_CONFLUENCE_CAP_BYPASS_REQUIRE_JEV", "false"),   # v1.35b: off (Andrew)
            "bypass_min": max(2, int(_f("KOL_CONFLUENCE_CAP_BYPASS_MIN_WALLETS", 5))),
            "bypass_count": ("all" if os.getenv("KOL_CONFLUENCE_CAP_BYPASS_COUNT", "cryptohub").strip().lower() == "all"
                             else "cryptohub")}


def _on(name: str, d: str) -> bool:
    return os.getenv(name, d).strip().lower() in {"1", "true", "yes", "on"}


def cfg() -> Dict[str, Any]:
    base = v3cfg()
    return {**base,
            "enabled": _on("KOL_CONFLUENCE_ENABLED", "true"),
            "n": max(2, int(_f("KOL_CONFLUENCE_N", 3))),
            "window_min": _f("KOL_CONFLUENCE_WINDOW_MIN", 30),
            "roles": {r.strip() for r in os.getenv("KOL_CONFLUENCE_ROLES", DEFAULT_ROLES).split(",") if r.strip()},
            "size_sol": _f("KOL_CONFLUENCE_SIZE_SOL", base["size_sol"]),   # paper-only simulated size
            "days": _f("KOL_CONFLUENCE_DAYS", 30),
            "max_open": int(_f("KOL_CONFLUENCE_MAX_OPEN", 40)),            # paper-book bound only
            "targets": {}, "whales": {}, "watch": {},
            "log_path": Path(os.getenv("KOL_CONFLUENCE_LOG_PATH", str(data_dir() / "kol_confluence.jsonl"))),
            "state_path": Path(os.getenv("KOL_CONFLUENCE_STATE_PATH", str(data_dir() / "kol_confluence_state.json")))}


class KolConfluence(PaperV3):
    arm = ARM

    def __init__(self) -> None:
        super().__init__()
        self.labels: Dict[str, str] = {}                       # wallet -> label, for wallets in a fired confluence
        self.buys: Dict[str, Deque[Tuple[int, str, str, str, Optional[float]]]] = collections.defaultdict(collections.deque)
        self.first_alert: Dict[str, Dict[str, Any]] = {}        # mint -> first watched-wallet buy
        self.pending: Dict[str, Dict[str, Any]] = {}            # mint -> signal context for _enter
        self.live_routed: Dict[str, str] = {}                   # v1.35 mint -> live decision of the confluence event
        self.strong_tried: set = set()                          # v1.35 one strong (cap-bypass) attempt per event
        self._tasks: set = set()

    # ---------------- v1.35 live routing ----------------
    @staticmethod
    def _strong_count(distinct: Dict[str, Tuple[str, str]], lc: Dict[str, Any]) -> int:
        if lc["bypass_count"] == "all":
            return len(distinct)
        return sum(1 for v in distinct.values() if v[1] == "cryptohub")

    def _live_signal(self, mint: str, wallets: list, price_sol: Optional[float], ctx: Dict[str, Any],
                     bypass: Optional[Dict[str, Any]] = None) -> Optional[str]:
        from app.live.engine import engine as live_engine
        meta = {"lane": ARM, "confluence_n": ctx.get("confluence_n"), "cryptohub_wallets": ctx.get("cryptohub_wallets"),
                "cap_bypass": bypass}
        d = live_engine.on_signal(ARM, mint, detect_ms=int(time.time() * 1000), trader_wallets=list(wallets),
                                  ref_price_sol=price_sol, meta=meta)
        dec = d.get("decision")
        if self.live_routed.get(mint) != "buy":
            self.live_routed[mint] = dec
        self.stats["live_" + str(dec)] += 1
        self._log("live_route", mint=mint, decision=dec, reasons=(d.get("reasons") or [])[:6], size_sol=d.get("size_sol"),
                  strong=bool(bypass), cap_bypass=d.get("cap_bypass"), confluence_n=ctx.get("confluence_n"),
                  cryptohub_wallets=ctx.get("cryptohub_wallets"))
        return dec

    def _route_live(self, mint: str, distinct: Dict[str, Tuple[str, str]], price_sol: Optional[float],
                    ctx: Dict[str, Any], *, at_signal: bool) -> None:
        """Never raises; paper logging continues whatever happens here."""
        try:
            lc = live_cfg()
            if not lc["enabled"]:
                return
            n_strong = self._strong_count(distinct, lc)
            strong = lc["bypass_enabled"] and n_strong >= lc["bypass_min"]
            if strong and mint not in self.strong_tried and self.live_routed.get(mint) != "buy":
                self.strong_tried.add(mint)
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None
                if loop is not None:
                    t = loop.create_task(self._strong(mint, dict(distinct), price_sol, dict(ctx), n_strong, lc))
                    self._tasks.add(t)
                    t.add_done_callback(self._tasks.discard)
                    return
            if at_signal and mint not in self.live_routed:
                self._live_signal(mint, list(distinct), price_sol, ctx)
        except Exception as exc:  # noqa: BLE001
            self.stats["live_errors"] += 1
            logger.warning("kol_confluence live route error %s: %s", mint, type(exc).__name__)

    async def _strong(self, mint: str, distinct: Dict[str, Tuple[str, str]], price_sol: Optional[float],
                      ctx: Dict[str, Any], n_strong: int, lc: Dict[str, Any]) -> None:
        try:
            from app.live.engine import engine as live_engine
            if mint in live_engine.bought_mints or mint in live_engine.inflight:
                return
            if lc.get("bypass_require_jev"):
                jev = await asyncio.to_thread(self._jev, mint, distinct, n_strong, lc)
                ok = jev.get("jev_status") == "ok" and jev.get("action") == "show" and bool(jev.get("safe_to_surface"))
            else:
                jev, ok = {"jev_status": "not_required", "action": None}, True
            ctx = {**ctx, "confluence_n": len(distinct),
                   "cryptohub_wallets": sum(1 for v in distinct.values() if v[1] == "cryptohub")}
            self._log("strong_signal", mint=mint, strong_wallets=n_strong, count_basis=lc["bypass_count"],
                      min_wallets=lc["bypass_min"], jev_status=jev.get("jev_status"), jev_action=jev.get("action"),
                      jev_safe=jev.get("safe_to_surface"), jev_pass=ok)
            if not ok:
                self.stats["strong_jev_refused"] += 1
                if self.live_routed.get(mint) is None:          # no attempt yet → normal path (inside the cap)
                    self._live_signal(mint, list(distinct), price_sol, ctx)
                return
            reason = (f"strong kol_confluence: {n_strong} distinct {lc['bypass_count']} wallets within "
                      f"{ctx.get('window_min')} min (>= KOL_CONFLUENCE_CAP_BYPASS_MIN_WALLETS={lc['bypass_min']}) "
                      + (f"+ Jev {jev.get('action')}/safe " if lc.get('bypass_require_jev') else "")
                      + "→ trades/day cap bypassed (rugcheck/mesh, reserve, kill, loss limit, max open, night pause still apply)")
            self.stats["strong_signals"] += 1
            self._live_signal(mint, list(distinct), price_sol, ctx,
                              bypass={"reason": reason, "strong_wallets": n_strong, "count_basis": lc["bypass_count"],
                                      "min_wallets": lc["bypass_min"], "jev_action": jev.get("action"),
                                      "jev_noul": jev.get("safe_to_surface_noul")})
        except Exception as exc:  # noqa: BLE001
            self.stats["live_errors"] += 1
            logger.warning("kol_confluence strong route error %s: %s %s", mint, type(exc).__name__, exc)

    def _jev(self, mint: str, distinct: Dict[str, Tuple[str, str]], n_strong: int, lc: Dict[str, Any]) -> Dict[str, Any]:
        from types import SimpleNamespace
        from app.jev_gate import evaluate_signal
        sig = SimpleNamespace(symbol=None, token_address=mint, chain="solana", meta={"source": "kol_confluence"},
                              score=None, unique_traders=len(distinct), buy_count=len(self.buys.get(mint) or []),
                              trader_handles=[v[0] for v in distinct.values()],
                              reasons=[f"{len(distinct)} watched wallets bought within 30 min",
                                       f"{n_strong} distinct {lc['bypass_count']} wallets"],
                              total_usd=None, avg_market_cap=None, mesh=None)
        return evaluate_signal(sig, use_cache=False).to_dict()

    def cfg(self) -> Dict[str, Any]:
        c = cfg()
        c["targets"] = self.labels
        return c

    # ---------------- feature ----------------
    def pumped_since_first_alert(self, mint: str, price_sol: Optional[float]) -> Optional[float]:
        fa = self.first_alert.get(mint)
        if not fa or not fa.get("price_sol") or not price_sol:
            return None
        return round((price_sol / fa["price_sol"] - 1) * 100, 2)

    def _note_first(self, mint: str, wallet: str, label: str, role: str, price_sol: Optional[float], ms: int) -> None:
        fa = self.first_alert.get(mint)
        if fa is None:
            if len(self.first_alert) > 20000:                   # bound memory: drop entries older than the TTL
                cut = ms - FIRST_ALERT_TTL_MS
                self.first_alert = {m: v for m, v in self.first_alert.items() if v["ms"] >= cut}
            self.first_alert[mint] = {"ms": ms, "price_sol": price_sol, "wallet": wallet, "label": label, "role": role}
        elif not fa.get("price_sol") and price_sol:
            fa["price_sol"] = price_sol                          # first buy had no price: take the next priced one

    # ---------------- feed (sync + cheap) ----------------
    def tracker(self, wallet: str, label: str, role: str, is_buy: bool, mint: str, *, price_sol: Optional[float],
                sol: float, sig: str, recv_ms: int, venue: Optional[str] = None) -> Optional[str]:
        c = cfg()
        if not c["enabled"] or not mint:
            return None
        if not is_buy:
            try:                                    # v1.35 follow_wallet on live kol_confluence positions
                from app.live.engine import engine as live_engine
                if any(p.arm == ARM and p.mint == mint for p in live_engine.open_positions()) or mint in live_engine.inflight:
                    live_engine.note_trader_sell(wallet, mint)
            except Exception:  # noqa: BLE001
                pass
            if wallet in self.labels:
                return self.on_wallet_sell(wallet, self.labels[wallet], mint, signature=sig, block_ms=recv_ms)
            return None
        self._note_first(mint, wallet, label, role, price_sol, recv_ms)
        if role not in c["roles"]:
            return None
        dq = self.buys[mint]
        cut = recv_ms - int(c["window_min"] * 60_000)
        while dq and dq[0][0] < cut:
            dq.popleft()
        dq.append((recv_ms, wallet, label, role, price_sol))
        if len(self.buys) > 5000:
            for m in [m for m, d in self.buys.items() if not d or d[-1][0] < cut]:
                self.buys.pop(m, None)
        distinct: Dict[str, Tuple[str, str]] = {}
        for _, w, lb, rl, _px in dq:
            distinct.setdefault(w, (lb, rl))
        if mint in self.live_routed and len(distinct) >= c["n"]:         # v1.35 strong check on later buys
            self._route_live(mint, distinct, price_sol, {"window_min": c["window_min"]}, at_signal=False)
        if len(distinct) < c["n"] or mint in self.seen_mints:
            if mint in self.seen_mints and wallet not in self.labels and len(distinct) >= c["n"]:
                self.labels[wallet] = label
                return self.on_wallet_buy(wallet, label, {"mint": mint}, signature=sig, block_ms=recv_ms)
            return None
        if not self.active():
            return "inactive"
        fa = self.first_alert.get(mint) or {}
        ctx = {"confluence_n": len(distinct), "n_required": c["n"], "window_min": c["window_min"],
               "confluence_wallets": [w[:6] for w in distinct], "confluence_labels": [v[0] for v in distinct.values()],
               "confluence_roles": sorted({v[1] for v in distinct.values()}),
               "cryptohub_wallets": sum(1 for v in distinct.values() if v[1] == "cryptohub"),
               "first_buy_ms": dq[0][0], "span_s": round((recv_ms - dq[0][0]) / 1000, 1),
               "first_alert_ms": fa.get("ms"), "first_alert_label": fa.get("label"), "first_alert_role": fa.get("role"),
               "first_alert_price_sol": fa.get("price_sol"),
               "min_since_first_alert": round((recv_ms - fa["ms"]) / 60000, 2) if fa.get("ms") else None,
               "trigger_price_sol": price_sol,
               "pumped_since_first_alert_pct": self.pumped_since_first_alert(mint, price_sol)}
        self.stats["signals"] += 1
        self._log("signal", mint=mint, handle=label, wallet=wallet[:6], signature=sig, venue=venue, **ctx)
        for w, (lb, _rl) in distinct.items():
            self.labels[w] = lb
        self.pending[mint] = (ctx, list(distinct))
        from app.fomo_onchain import market
        su = market.sol_usd()
        res = {"mint": mint, "usd": round(sol * su, 2) if su else None,
               "fill_price_usd": price_sol * su if price_sol and su else None, "trader_price_sol": price_sol,
               "trader_sol": round(sol, 6), "venue": venue}
        out = self.on_wallet_buy(wallet, label, res, signature=sig, block_ms=recv_ms,
                                 detect_lag_ms=max(0, int(time.time() * 1000) - recv_ms))
        if out not in (None, "inactive"):                                  # v1.35 live routing (one per event)
            self._route_live(mint, distinct, price_sol, ctx, at_signal=True)
        return out

    async def _enter(self, pid, wallet, handle, mint, res, signature, block_ms, detect_lag_ms) -> None:  # type: ignore[override]
        await super()._enter(pid, wallet, handle, mint, res, signature, block_ms, detect_lag_ms)
        ctx, full = self.pending.pop(mint, ({}, []))
        b = self.book.get(pid)
        if not b:
            return
        px = b["meta"].get("mark_price_sol")
        b["meta"].update(ctx)
        b["meta"]["wallets"] = list(dict.fromkeys(b["meta"]["wallets"] + full))
        b["meta"]["handles"] = list(dict.fromkeys([handle] + ctx.get("confluence_labels", [])))
        b["meta"]["pumped_since_first_alert_pct_at_entry"] = self.pumped_since_first_alert(mint, px)
        self._log("confluence_entry", id=pid, mint=mint, handles=b["meta"]["handles"],
                  pumped_since_first_alert_pct=ctx.get("pumped_since_first_alert_pct"),
                  pumped_since_first_alert_pct_at_entry=b["meta"]["pumped_since_first_alert_pct_at_entry"],
                  confluence_n=ctx.get("confluence_n"), cryptohub_wallets=ctx.get("cryptohub_wallets"))
        self.save()

    def summary(self, recent: int = 15) -> Dict[str, Any]:  # type: ignore[override]
        s = super().summary(recent)
        c = cfg()
        lc = live_cfg()
        s.update(live={"enabled": lc["enabled"], "routed": len(self.live_routed), "strong_tried": len(self.strong_tried),
                       "cap_bypass_min_wallets": lc["bypass_min"], "cap_bypass_count": lc["bypass_count"],
                       "cap_bypass_enabled": lc["bypass_enabled"]})
        s.update(arm=ARM, paper_only=not lc["enabled"], n_required=c["n"], window_min=c["window_min"], roles=sorted(c["roles"]),
                 signals=self.stats.get("signals", 0), tracked_mints=len(self.buys), first_alerts=len(self.first_alert),
                 feature="pumped_since_first_alert_pct (logged on every signal; NOT a filter)",
                 detection="scout websocket (every watched role) → app.scout.engine.on_trade → tracker",
                 promotion_rule="learner proposals only (live_proposals.jsonl, status awaiting_andrew); never automatic")
        return s


paper = KolConfluence()
