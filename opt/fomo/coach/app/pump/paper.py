"""Paper-trading engine: simulates the 0.40 SOL `fluffy_pump_dump` ladder on live data.

Fills use LIVE bonding-curve reserves (constant-product math incl. 1.25% curve fee) while the coin
is pre-migration, and a real Jupiter /order quote (no taker → no transaction) or PumpSwap pool math
after migration. Nothing is signed or sent. Every open/fill/close is appended to
data/paper_trades.jsonl.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.pump import exit_signals
from app.pump.curve import buy_tokens_out, sell_sol_out
from app.pump.solana_pda import WSOL_MINT

logger = logging.getLogger("coach.pump.paper")

from app.paths import data_path  # noqa: E402
LOG_PATH = data_path("PAPER_LOG_PATH", "paper_trades.jsonl")
# v1.38 realistic twin rows go to their OWN file so nothing that reads paper_trades.jsonl (live rugcheck rugger list,
# wallet scores, recap, restart-orphan markers) sees a different row set.
REAL_LOG_PATH = data_path("PAPER_REAL_LOG_PATH", "paper_trades_real.jsonl")
LADDER_LABELS = ("TP1", "TP2", "TP3")
CURVE_FEE_BPS = int(os.getenv("PAPER_CURVE_FEE_BPS", "125"))
AMM_FEE_BPS = int(os.getenv("PAPER_AMM_FEE_BPS", "30"))
TICK_S = float(os.getenv("PAPER_TICK_S", "5"))


def enabled() -> bool:
    return os.getenv("PAPER_TRADING_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}


def _plan() -> Dict[str, float]:
    from app.config import settings as s
    return {
        "entry_sol": float(s.plan_entry_sol_default),
        "ladder": [(float(s.plan_tp1_mult), float(s.plan_tp1_frac), "TP1"),
                   (float(s.plan_tp2_mult), float(s.plan_tp2_frac), "TP2"),
                   (float(s.plan_tp3_mult), float(s.plan_tp3_frac), "TP3")],
    }


@dataclass
class PaperPosition:
    mint: str
    source: str
    entry_sol: float
    entry_lamports: int
    tokens_initial: int
    entry_price_sol: float
    entry_venue: str
    entry_ms: int
    profile: str = "fluffy_pump_dump"
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    tokens_left: int = 0
    sol_out_lamports: int = 0
    high_price_sol: Optional[float] = None
    last_price_sol: Optional[float] = None
    ladder_done: List[str] = field(default_factory=list)
    fills: List[Dict[str, Any]] = field(default_factory=list)
    status: str = "open"
    exit_reason: Optional[str] = None
    closed_ms: Optional[int] = None
    migrated: bool = False
    signal_latency_ms: Optional[float] = None
    jupiter_entry_quote: Optional[Dict[str, Any]] = None
    last_cards: List[Dict[str, Any]] = field(default_factory=list)
    paper: bool = True
    # v1.12 — non-Fluffy profiles (fomo_top_trader): stop state + source meta; unused by fluffy_pump_dump
    meta: Dict[str, Any] = field(default_factory=dict)
    stop_price_sol: Optional[float] = None
    stop_kind: Optional[str] = None
    external_signals: List[Dict[str, Any]] = field(default_factory=list)

    def pnl(self) -> Dict[str, Any]:
        realized = self.sol_out_lamports / 1e9
        mark = (self.tokens_left / 1e6) * (self.last_price_sol or 0)
        value = realized + mark
        return {"realized_sol": round(realized, 6), "mark_sol": round(mark, 6),
                "value_sol": round(value, 6), "pnl_sol": round(value - self.entry_sol, 6),
                "pnl_pct": round((value / self.entry_sol - 1) * 100, 2) if self.entry_sol else None,
                "multiple_now": round(self.last_price_sol / self.entry_price_sol, 3)
                if self.last_price_sol and self.entry_price_sol else None}

    def view(self) -> Dict[str, Any]:
        d = asdict(self)
        d["pnl"] = self.pnl()
        d["fills"] = self.fills[-20:]
        return d


def _log(kind: str, pos: PaperPosition, **extra: Any) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        rec = {"ts_ms": int(time.time() * 1000), "kind": kind, "id": pos.id, "mint": pos.mint,
               "profile": pos.profile, "paper": True, "pnl": pos.pnl(), **extra}
        with LOG_PATH.open("a") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")
    except Exception as exc:  # noqa: BLE001
        logger.warning("paper log failed: %s", exc)


def _log_real(kind: str, pos: "PaperPosition", **extra: Any) -> None:
    try:
        REAL_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        rec = {"ts_ms": int(time.time() * 1000), "kind": kind, "id": pos.id, "mint": pos.mint, "profile": pos.profile,
               "paper": True, "realistic": True, "legacy_entry_price_sol": pos.entry_price_sol, **extra}
        with REAL_LOG_PATH.open("a") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")
    except Exception as exc:  # noqa: BLE001
        logger.warning("paper realistic log failed: %s", type(exc).__name__)


def _real_pnl(r: Dict[str, Any]) -> Dict[str, Any]:
    pn = (r["received"] - r["spent"]) / 1e9
    return {"pnl_sol": round(pn, 6), "pnl_pct": round(pn / r["size_sol"] * 100, 2) if r.get("size_sol") else None}


def _reserves_from_stream(s: Any) -> Optional[Dict[str, Any]]:
    """Latest curve reserves: newest of last TradeEvent vs curve poll."""
    ev = s.trades[-1] if getattr(s, "trades", None) else None
    cv = getattr(s, "curve", None) or {}
    ev_ms = ev.get("recv_ms", 0) if ev else 0
    cv_ms = cv.get("read_at_ms", 0) if cv else 0
    if ev and ev_ms >= cv_ms:
        return {"virtual_token_reserves": ev["virtual_token_reserves"], "virtual_quote_reserves": ev["virtual_sol_reserves"],
                "real_token_reserves": ev["real_token_reserves"], "real_quote_reserves": ev["real_sol_reserves"]}
    if cv.get("status") == "ok" and not cv.get("complete"):
        return cv
    return None


def _jupiter_quote(input_mint: str, output_mint: str, amount: int) -> Dict[str, Any]:
    from app.jupiter_exec import fetch_order
    t0 = time.perf_counter()
    r = fetch_order(input_mint=input_mint, output_mint=output_mint, amount=str(int(amount)), timeout=6.0)
    o = r.get("order") or {}
    return {"ok": bool(r.get("ok") and o.get("outAmount")), "out_amount": int(o.get("outAmount") or 0),
            "router": o.get("router"), "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
            "error": r.get("error") or o.get("errorMessage")}


class PaperEngine:
    def __init__(self) -> None:
        self.positions: Dict[str, PaperPosition] = {}   # fluffy_pump_dump book (one slot per mint)
        # other profiles live in their own book keyed "profile:mint" so they never occupy Fluffy's slot
        self.profile_positions: Dict[str, PaperPosition] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self._ticker: Optional[asyncio.Task] = None
        # v1.12.1 dedupe: one Fluffy paper entry per mint, ever (survives restarts via the jsonl log);
        # a dedicated open-lock closes the check→await-quote→insert race that double-logged opens.
        self._open_locks: Dict[str, asyncio.Lock] = {}
        self._real_tasks: set = set()
        self._opened_mints: set = self._load_opened_mints()

    @staticmethod
    def _load_opened_mints() -> set:
        out: set = set()
        try:
            if LOG_PATH.exists():
                with LOG_PATH.open() as fh:
                    for line in fh:
                        if '"open"' not in line:
                            continue
                        try:
                            rec = json.loads(line)
                        except ValueError:
                            continue
                        if rec.get("kind") == "open" and rec.get("profile", "fluffy_pump_dump") == "fluffy_pump_dump" \
                                and rec.get("mint"):
                            out.add(rec["mint"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("paper: could not seed opened mints: %s", exc)
        return out

    @staticmethod
    def mark_restart_orphans(max_age_h: float = 48.0) -> int:
        """v1.16 data hygiene (append-only): paper positions are not restored after a restart, so an `open` with no
        `close` older than 2 h lost its marks. Append one {"kind":"excluded","excluded":"restart_gap"} marker per such
        id so analyses skip it. Never rewrites the log."""
        try:
            if not LOG_PATH.exists():
                return 0
            now = int(time.time() * 1000)
            opened, closed, marked = {}, set(), set()
            with LOG_PATH.open() as fh:
                for line in fh:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    k, i = r.get("kind"), r.get("id")
                    if k == "open":
                        opened[i] = r
                    elif k == "close":
                        closed.add(i)
                    elif k == "excluded" or r.get("excluded"):
                        marked.add(i)
            orphans = [r for i, r in opened.items() if i not in closed and i not in marked
                       and now - int(r.get("ts_ms") or now) > 2 * 3600_000
                       and now - int(r.get("ts_ms") or now) < max_age_h * 3600_000]
            with LOG_PATH.open("a") as fh:
                for r in orphans:
                    fh.write(json.dumps({"ts_ms": now, "kind": "excluded", "excluded": "restart_gap", "id": r["id"],
                                         "mint": r.get("mint"), "profile": r.get("profile"), "paper": True,
                                         "note": "open with no close: marks lost across a restart"}) + "\n")
            return len(orphans)
        except Exception as exc:  # noqa: BLE001
            logger.warning("paper: restart-orphan marking failed: %s", type(exc).__name__)
            return 0

    def already_opened(self, mint: str) -> bool:
        return mint in self.positions or mint in self._opened_mints

    def open_for(self, mint: str) -> Optional[PaperPosition]:
        p = self.positions.get(mint)
        return p if p and p.status == "open" else None

    async def open_from_curve(self, mint: str, curve: Dict[str, Any], *, source: str,
                              signal_latency_ms: Optional[float] = None) -> Optional[PaperPosition]:
        if not enabled() or self.already_opened(mint):
            return None
        lock = self._open_locks.setdefault(mint, asyncio.Lock())
        async with lock:
            if self.already_opened(mint):  # re-check after waiting: a concurrent ingest may have opened it
                logger.info("paper: skip %s (already opened once — dedupe)", mint)
                return None
            return await self._open_from_curve_locked(mint, curve, source=source,
                                                      signal_latency_ms=signal_latency_ms)

    async def _open_from_curve_locked(self, mint: str, curve: Dict[str, Any], *, source: str,
                                      signal_latency_ms: Optional[float] = None) -> Optional[PaperPosition]:
        if curve.get("status") == "ok" and not curve.get("quote_is_sol", True):
            logger.info("paper: skip %s (non-SOL quote curve)", mint)
            return None
        plan = _plan()
        lam = int(plan["entry_sol"] * 1e9)
        if curve.get("status") == "ok" and not curve.get("complete"):
            toks = buy_tokens_out(curve, lam, CURVE_FEE_BPS)
            venue = "pump_curve_sim"
        else:
            q = await asyncio.to_thread(_jupiter_quote, WSOL_MINT, mint, lam)
            if not q["ok"]:
                logger.info("paper: no entry quote for %s (%s)", mint, q.get("error"))
                return None
            toks, venue = q["out_amount"], f"jupiter_quote:{q.get('router')}"
        if toks <= 0:
            return None
        pos = PaperPosition(mint=mint, source=source, entry_sol=plan["entry_sol"], entry_lamports=lam,
                            tokens_initial=toks, tokens_left=toks, entry_price_sol=plan["entry_sol"] / (toks / 1e6),
                            entry_venue=venue, entry_ms=int(time.time() * 1000),
                            migrated=bool(curve.get("complete")), signal_latency_ms=signal_latency_ms)
        pos.last_price_sol = curve.get("price_sol") or pos.entry_price_sol
        pos.high_price_sol = pos.last_price_sol
        self.positions[mint] = pos
        self._opened_mints.add(mint)
        _log("open", pos, venue=venue, tokens=toks, entry_price_sol=pos.entry_price_sol,
             spot_price_sol=curve.get("price_sol"), signal_latency_ms=signal_latency_ms)
        self._ensure_ticker()
        try:                                     # v1.38 realistic twin (live latency + on-chain price, live fees)
            from app import paper_realism as pr
            if pr.enabled():
                t = asyncio.get_running_loop().create_task(self._open_real(pos))
                self._real_tasks.add(t)
                t.add_done_callback(self._real_tasks.discard)
        except Exception as exc:  # noqa: BLE001
            logger.warning("paper realistic schedule failed: %s", type(exc).__name__)
        return pos

    # ---- v1.38 realistic twin (Fluffy book only; legacy position, fills, closes and streams unchanged) ----
    async def _open_real(self, pos: "PaperPosition") -> None:
        try:
            from app import paper_realism as pr
            P = pr.params("fluffy_pump_dump")
            onchain = None
            if pr.onchain_enabled():
                wait = (pos.entry_ms + P["lat_ms"] - time.time() * 1000) / 1000
                if wait > 0:
                    await asyncio.sleep(min(wait, 15))
                onchain = await pr.onchain_price(pos.mint)
            if pos.status != "open":
                _log_real("real_skip", pos, reason="legacy paper closed before the live latency elapsed")
                return
            # the legacy curve fill at signal time is the floor (live never fills better than the signal price)
            ef = pr.entry_fill("fluffy_pump_dump", pos.entry_price_sol, onchain, None, pos.entry_sol, P)
            if not ef.get("ok"):
                _log_real("real_skip", pos, reason=ef.get("reason"), rejected=ef.get("rejected"))
                return
            epx = float(ef["px"])
            spent, tokens = pr.entry_position(P, epx, pos.entry_sol)
            pos.meta["real"] = {"entry_px": epx, "spent": spent, "received": 0, "tokens_initial": tokens,
                                "tokens_left": tokens, "ladder_done": [], "size_sol": pos.entry_sol,
                                "src": ef.get("src"), "status": "open", "entry_ms": int(time.time() * 1000)}
            _log_real("real_open", pos, entry_price_sol=epx, src=ef.get("src"), lat_ms=P["lat_ms"],
                      slip_pct=P["slip_pct"], entry_vs_legacy_pct=round((epx / pos.entry_price_sol - 1) * 100, 2),
                      rejected=ef.get("rejected"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("paper realistic open failed: %s", type(exc).__name__)

    def _real_sell(self, pos: "PaperPosition", tokens: int, px: Optional[float], reason: str) -> None:
        r = pos.meta.get("real")
        if not r or r.get("status") != "open" or not px:
            return
        try:
            from app import paper_realism as pr
            P = pr.params("fluffy_pump_dump")
            tokens = min(int(tokens), r["tokens_left"])
            if tokens <= 0:
                return
            spx = pr.exit_px(P, reason, px, r["entry_px"])
            lam = pr.proceeds_lamports(P, tokens, spx)
            r["tokens_left"] -= tokens
            r["received"] += lam
            _log_real("real_fill", pos, reason=reason, tokens=tokens, sol=round(lam / 1e9, 6), price_sol=spx,
                      multiple=round(spx / r["entry_px"], 4))
            if r["tokens_left"] <= 0:
                r["status"] = "closed"
                r["exit_reason"] = reason
                _log_real("real_close", pos, exit_reason=reason, entry_price_sol=r["entry_px"],
                          held_s=round((time.time() * 1000 - r["entry_ms"]) / 1000), **_real_pnl(r))
        except Exception as exc:  # noqa: BLE001
            logger.warning("paper realistic sell failed: %s", type(exc).__name__)

    def _real_ladder(self, pos: "PaperPosition", price: Optional[float]) -> None:
        r = pos.meta.get("real")
        if not r or r.get("status") != "open" or not price:
            return
        fee = (AMM_FEE_BPS if pos.migrated else CURVE_FEE_BPS) / 1e4
        for mult, frac, label in _plan()["ladder"]:
            if label in r["ladder_done"] or price < r["entry_px"] * mult:
                continue
            r["ladder_done"].append(label)
            self._real_sell(pos, int(r["tokens_initial"] * frac), price * (1 - fee), label)

    async def attach_jupiter_entry_quote(self, mint: str, quote: Dict[str, Any]) -> None:
        p = self.positions.get(mint)
        if p:
            p.jupiter_entry_quote = quote

    async def _sell(self, pos: PaperPosition, tokens: int, reason: str, stream: Any) -> None:
        tokens = min(tokens, pos.tokens_left)
        if tokens <= 0:
            return
        venue, lam, quote_info = None, 0, None
        res = _reserves_from_stream(stream) if stream is not None and not pos.migrated else None
        if res:
            lam, venue = sell_sol_out(res, tokens, CURVE_FEE_BPS), "pump_curve_sim"
        else:
            q = await asyncio.to_thread(_jupiter_quote, pos.mint, WSOL_MINT, tokens)
            if q["ok"]:
                lam, venue, quote_info = q["out_amount"], f"jupiter_quote:{q.get('router')}", q
            else:
                ps = ((getattr(stream, "curve", None) or {}).get("pumpswap") or {}) if stream else {}
                base, eff = ps.get("base_reserve"), ps.get("effective_quote_reserves")
                if base and eff:
                    gross = eff - (base * eff) // (base + tokens)
                    lam, venue = gross * (10_000 - AMM_FEE_BPS) // 10_000, "pumpswap_pool_sim"
                else:
                    pos.fills.append({"ts_ms": int(time.time() * 1000), "reason": reason, "failed": True,
                                      "error": q.get("error")})
                    return
        pos.tokens_left -= tokens
        pos.sol_out_lamports += lam
        fill = {"ts_ms": int(time.time() * 1000), "reason": reason, "tokens": tokens, "sol": lam / 1e9,
                "venue": venue, "price_sol": pos.last_price_sol,
                "multiple": round(pos.last_price_sol / pos.entry_price_sol, 3) if pos.last_price_sol else None}
        if quote_info:
            fill["quote_ms"] = quote_info.get("elapsed_ms")
        pos.fills.append(fill)
        _log("fill", pos, **fill)
        if reason not in LADDER_LABELS and pos.profile == "fluffy_pump_dump":
            # risk exits (exit cards, max hold, close): the realistic twin sells its remainder at the same realised
            # per-token price; ladder TPs are evaluated separately against the twin's own (higher) entry
            self._real_sell(pos, (pos.meta.get("real") or {}).get("tokens_left", 0),
                            (lam / 1e9) / (tokens / 1e6) if tokens else None, reason)
        if pos.tokens_left <= 0:
            self._close(pos, reason)

    def _close(self, pos: PaperPosition, reason: str) -> None:
        pos.status, pos.exit_reason, pos.closed_ms = "closed", reason, int(time.time() * 1000)
        _log("close", pos, exit_reason=reason, held_s=(pos.closed_ms - pos.entry_ms) / 1000)
        r = pos.meta.get("real") if pos.profile == "fluffy_pump_dump" else None
        if r and r.get("status") == "open":       # legacy fully out → twin's remainder out at the last mark
            fee = (AMM_FEE_BPS if pos.migrated else CURVE_FEE_BPS) / 1e4
            self._real_sell(pos, r["tokens_left"], (pos.last_price_sol or r["entry_px"]) * (1 - fee),
                            "legacy_close:" + str(reason))
        if pos.profile != "fluffy_pump_dump":
            return  # never stop a (Fluffy) mint stream on behalf of another profile
        try:
            from app.live.engine import engine as live_engine
            from app.pump.trade_stream import streams
            if pos.mint in streams.pinned or any(lp.mint == pos.mint for lp in live_engine.open_positions()):
                return   # a live position still needs this mint's fast trade stream
            streams.stop(pos.mint, reason="position_closed")
        except Exception:  # noqa: BLE001
            pass

    async def on_stream(self, s: Any) -> None:
        pos = self.open_for(s.mint)
        if not pos:
            return
        lock = self._locks.setdefault(s.mint, asyncio.Lock())
        async with lock:
            await self._evaluate(pos, s)

    async def _evaluate(self, pos: PaperPosition, s: Any) -> None:
        if pos.status != "open":
            return
        if s is not None:
            if s.last_price_sol:
                pos.last_price_sol = s.last_price_sol
                pos.high_price_sol = max(pos.high_price_sol or 0, s.last_price_sol)
            if s.migrated and not pos.migrated:
                pos.migrated = True
                _log("migrated", pos)
        price = pos.last_price_sol
        if pos.profile == "fluffy_pump_dump":
            self._real_ladder(pos, price)
        for mult, frac, label in _plan()["ladder"]:
            if label in pos.ladder_done or not price:
                continue
            if price >= pos.entry_price_sol * mult:
                pos.ladder_done.append(label)
                await self._sell(pos, int(pos.tokens_initial * frac), label, s)
                if pos.status != "open":
                    return
        trades = [t for t in (s.trades if s is not None else []) if t.get("recv_ms", 0) >= pos.entry_ms]
        cards = exit_signals.evaluate(
            trades=trades, creator=getattr(s, "creator", None), price=price, high=pos.high_price_sol,
            entry_price=pos.entry_price_sol, entry_ms=pos.entry_ms,
            real_sol=(s.real_sol / 1e9) if s is not None and s.real_sol is not None else None,
            peak_real_sol=(s.peak_real_sol / 1e9) if s is not None and s.peak_real_sol is not None else None,
            migrated=pos.migrated, tp1_hit="TP1" in pos.ladder_done,
        )
        pos.last_cards = cards
        for card in exit_signals.fired(cards):
            if card["action"] in ("exit_all", "exit_remainder"):
                await self._sell(pos, pos.tokens_left, card["kind"], s)
                return

    def _ensure_ticker(self) -> None:
        if self._ticker and not self._ticker.done():
            return
        try:
            self._ticker = asyncio.get_running_loop().create_task(self._tick_loop())
        except RuntimeError:
            pass

    async def _tick_loop(self) -> None:
        """Price-independent tick so max-hold fires even when the stream/RPC is dead."""
        from app.pump.trade_stream import streams
        while any(p.status == "open" for p in self.positions.values()):
            for p in list(self.positions.values()):
                if p.status == "open":
                    lock = self._locks.setdefault(p.mint, asyncio.Lock())
                    async with lock:
                        try:
                            await self._evaluate(p, streams.get(p.mint))
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("paper tick error: %s", exc)
            await asyncio.sleep(TICK_S)

    # ---- profile book (v1.12): mark-price simulation, separate size, never touches Fluffy's book ----
    def profile_open_for(self, profile: str, mint: str) -> Optional[PaperPosition]:
        p = self.profile_positions.get(f"{profile}:{mint}")
        return p if p and p.status == "open" else None

    def profile_open(self, profile: str) -> List[PaperPosition]:
        return [p for p in self.profile_positions.values() if p.profile == profile and p.status == "open"]

    def open_profile_position(self, *, profile: str, mint: str, source: str, entry_sol: float,
                              price_sol: float, cost_bps: int, venue: str,
                              meta: Optional[Dict[str, Any]] = None,
                              signal_latency_ms: Optional[float] = None) -> Optional[PaperPosition]:
        """Simulated entry at mark × (1 + cost). Tokens use the engine's 1e6 base-unit convention."""
        if not enabled() or self.profile_open_for(profile, mint) or not price_sol or price_sol <= 0 or entry_sol <= 0:
            return None
        fill_px = price_sol * (1 + cost_bps / 10_000)
        toks = int(entry_sol / fill_px * 1e6)
        if toks <= 0:
            return None
        pos = PaperPosition(mint=mint, source=source, entry_sol=entry_sol, entry_lamports=int(entry_sol * 1e9),
                            tokens_initial=toks, tokens_left=toks, entry_price_sol=fill_px, entry_venue=venue,
                            entry_ms=int(time.time() * 1000), profile=profile, signal_latency_ms=signal_latency_ms,
                            meta=dict(meta or {}))
        pos.last_price_sol = price_sol
        pos.high_price_sol = price_sol
        self.profile_positions[f"{profile}:{mint}"] = pos
        _log("open", pos, venue=venue, tokens=toks, entry_price_sol=fill_px, spot_price_sol=price_sol,
             cost_bps=cost_bps, meta=pos.meta)
        return pos

    def sell_profile(self, pos: PaperPosition, tokens: int, reason: str, *, price_sol: Optional[float],
                     cost_bps: int, venue: str) -> Optional[Dict[str, Any]]:
        """Simulated exit at the CURRENT mark (not the stop level) × (1 − cost): gaps hurt, as in reality."""
        tokens = min(int(tokens), pos.tokens_left)
        if pos.status != "open" or tokens <= 0:
            return None
        if not price_sol:
            pos.fills.append({"ts_ms": int(time.time() * 1000), "reason": reason, "failed": True, "error": "no_mark"})
            return None
        lam = int((tokens / 1e6) * price_sol * (1 - cost_bps / 10_000) * 1e9)
        pos.tokens_left -= tokens
        pos.sol_out_lamports += lam
        fill = {"ts_ms": int(time.time() * 1000), "reason": reason, "tokens": tokens, "sol": lam / 1e9,
                "venue": venue, "price_sol": price_sol,
                "multiple": round(price_sol / pos.entry_price_sol, 3) if pos.entry_price_sol else None}
        pos.fills.append(fill)
        _log("fill", pos, **fill)
        if pos.tokens_left <= 0:
            self._close(pos, reason)
        return fill

    def profile_summary(self, profile: str) -> Dict[str, Any]:
        ps = [p for p in self.profile_positions.values() if p.profile == profile]
        closed = [p for p in ps if p.status == "closed"]
        pnl = [p.pnl()["pnl_sol"] for p in closed]
        opn = [p.pnl()["pnl_sol"] for p in ps if p.status == "open"]
        return {"profile": profile, "open": len(ps) - len(closed), "closed": len(closed),
                "wins": sum(1 for x in pnl if x > 0), "realized_pnl_sol": round(sum(pnl), 6),
                "open_mark_pnl_sol": round(sum(opn), 6)}

    def realistic_summary(self) -> Dict[str, Any]:
        rs = [p.meta["real"] for p in self.positions.values() if (p.meta or {}).get("real")]
        closed = [r for r in rs if r.get("status") == "closed"]
        pn = [_real_pnl(r)["pnl_sol"] for r in closed]
        return {"basis": "realistic (live latency + on-chain entry never below the signal fill, live fees; own "
                         "ladder vs its own entry, risk exits shared)", "log_path": str(REAL_LOG_PATH.name),
                "open": len(rs) - len(closed), "closed": len(closed), "wins": sum(1 for x in pn if x > 0),
                "total_pnl_sol": round(sum(pn), 6), "since": "v1.38 restart (in-memory; full history in the log)"}

    def summary(self) -> Dict[str, Any]:
        closed = [p for p in self.positions.values() if p.status == "closed"]
        pnl = [p.pnl()["pnl_sol"] for p in closed]
        return {
            "headline": self.realistic_summary(), "legacy_note": "top-level totals = legacy fill model",
            "enabled": enabled(), "paper": True, "live_exec": False, "signing": False,
            "open": sum(1 for p in self.positions.values() if p.status == "open"),
            "closed": len(closed), "wins": sum(1 for x in pnl if x > 0),
            "total_pnl_sol": round(sum(pnl), 6), "log_path": str(LOG_PATH.name),
            "fees": {"curve_fee_bps": CURVE_FEE_BPS, "amm_fee_bps_fallback": AMM_FEE_BPS},
            "profiles": {pr: self.profile_summary(pr) for pr in sorted({p.profile for p in self.profile_positions.values()})},
        }

    def view(self) -> Dict[str, Any]:
        return {**self.summary(),
                "positions": [p.view() for p in sorted(self.positions.values(), key=lambda x: -x.entry_ms)][:50],
                "profile_positions": [p.view() for p in sorted(self.profile_positions.values(),
                                                               key=lambda x: -x.entry_ms)][:50]}


paper = PaperEngine()
