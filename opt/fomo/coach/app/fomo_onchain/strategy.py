"""`fomo_top_trader` paper profile — "buy low, sell high": follow verified FOMO top wallets EARLY,
never chase, protect gains with a ratcheting stop. PAPER ONLY; recommend-only; nothing is signed.

Why anti-chase: fomo-flow finding — crowd buys tend to follow a ~+7.7% 4h run-up and the median
token is ~−22% the next day. So we skip/flag tokens already up big, prefer on-curve/young tokens and
prefer ≥2 independent top wallets converging on the same mint inside a short window.

Exits (see exit_signals.fomo_stop/evaluate_fomo): hard stop (−20% default) → breakeven once +15% →
trailing (15% off high) once +25%; TP ladder; follow-the-trader (source wallet sells); max hold.
NO-LOSS IS NOT GUARANTEED: paper fills use the NEXT observed DexScreener mark (≈15 s), so gaps,
rugs and slippage can fill well below the stop — same as reality.
"""
from __future__ import annotations

import asyncio
import collections
import logging
import os
import time
from typing import Any, Deque, Dict, List, Optional

from app.fomo_onchain import PROFILE, SOURCE
from app.fomo_onchain import market
from app.pump import exit_signals

logger = logging.getLogger("coach.fomo.strategy")


def _f(name: str, d: float) -> float:
    try:
        return float(os.getenv(name, str(d)))
    except ValueError:
        return d


def config() -> Dict[str, Any]:
    return {
        "entry_sol": _f("FOMO_PAPER_ENTRY_SOL", 0.10),       # separate from Fluffy's PLAN_ENTRY_SOL (0.40)
        "cost_bps": int(_f("FOMO_PAPER_COST_BPS", 150)),     # per side: fee + slippage assumption
        "max_open": int(_f("FOMO_MAX_OPEN", 5)),
        "mint_cooldown_s": _f("FOMO_MINT_COOLDOWN_S", 3600),
        "min_wallet_quality": _f("FOMO_MIN_WALLET_QUALITY", 40),
        "min_buy_usd": _f("FOMO_MIN_BUY_USD", 50),
        "min_liq_usd": _f("FOMO_MIN_LIQ_USD", 8000),
        "converge_window_s": _f("FOMO_CONVERGE_WINDOW_S", 1800),
        "converge_min": int(_f("FOMO_CONVERGE_MIN", 2)),
        "solo_min_quality": _f("FOMO_SOLO_MIN_QUALITY", 75),
        "early_max_mcap_usd": _f("FOMO_EARLY_MAX_MCAP_USD", 1_500_000),
        "early_max_pair_age_h": _f("FOMO_EARLY_MAX_PAIR_AGE_H", 12),
        "skip_m5_pct": _f("FOMO_SKIP_M5_PCT", 15),
        "skip_h1_pct": _f("FOMO_SKIP_H1_PCT", 30),
        "skip_h6_pct": _f("FOMO_SKIP_H6_PCT", 80),
        "skip_h24_pct": _f("FOMO_SKIP_H24_PCT", 300),
        "flag_h1_pct": _f("FOMO_FLAG_H1_PCT", 7.7),
        "flag_h6_pct": _f("FOMO_FLAG_H6_PCT", 15),
        "max_above_first_fill_pct": _f("FOMO_MAX_ABOVE_FIRST_FILL_PCT", 15),
        "mark_poll_s": _f("FOMO_MARK_POLL_S", 15),
        "yield_max_s": _f("FOMO_YIELD_TO_FLUFFY_MAX_S", 15),
        "disclaimer": "No-loss is NOT guaranteed: stops fill at the next observed mark; gaps, rugs and "
                      "slippage can exceed the stop. Paper only.",
    }


class Strategy:
    def __init__(self) -> None:
        self.candidates: Dict[str, Dict[str, Any]] = {}
        self.decisions: Deque[Dict[str, Any]] = collections.deque(maxlen=200)
        self.trader_sells: Deque[Dict[str, Any]] = collections.deque(maxlen=200)
        self.advisories: Deque[Dict[str, Any]] = collections.deque(maxlen=100)
        self.theses: Dict[str, List[Dict[str, Any]]] = {}
        self.closed_at: Dict[str, float] = {}
        self._tasks: set = set()
        self._loop_task: Optional[asyncio.Task] = None
        self.stats = {"buys_in": 0, "sells_in": 0, "entries": 0, "skips": 0, "watching": 0, "exits": 0}

    # ---------------- inputs ----------------
    def on_ingested_buy(self, ev: Any) -> Dict[str, Any]:
        """Called by /api/ingest/buy for low-priority sources. Never blocks; returns immediately."""
        if ev.source != SOURCE:
            return {"queued": False, "reason": "annotation-only source"}
        m = ev.meta or {}
        mint = ev.token_address
        self.stats["buys_in"] += 1
        c = self.candidates.setdefault(mint, {"mint": mint, "first_seen_ms": int(time.time() * 1000), "buys": [],
                                              "decision": "pending", "reasons": [], "flags": []})
        c["buys"].append({"wallet": m.get("wallet") or ev.trader_id, "handle": ev.trader_handle,
                          "quality": m.get("wallet_quality"), "usd": ev.usd_amount,
                          "fill_price_usd": m.get("fill_price_usd"), "signature": m.get("signature"),
                          "block_ms": ev.occurred_at_ms, "on_curve_hint": m.get("on_curve_hint")})
        c["buys"] = c["buys"][-50:]
        self._spawn(self.decide(mint))
        return {"queued": True, "profile": PROFILE}

    def on_trader_sell(self, *, wallet: str, handle: str, mint: str, sold_fraction: Optional[float],
                       usd: Optional[float], signature: Optional[str], block_ms: int) -> Dict[str, Any]:
        from app.pump.paper import paper
        self.stats["sells_in"] += 1
        rec = {"wallet": wallet, "handle": handle, "mint": mint, "sold_fraction": sold_fraction, "usd": usd,
               "signature": signature, "block_ms": block_ms, "seen_ms": int(time.time() * 1000)}
        self.trader_sells.appendleft(rec)
        try:
            from app.live.engine import engine as live_engine
            live_engine.note_trader_sell(wallet, mint)
        except Exception:  # noqa: BLE001
            pass
        hit = []
        pos = paper.profile_open_for(PROFILE, mint)
        if pos and wallet in (pos.meta.get("source_wallets") or []):
            pos.meta.setdefault("pending_trader_sells", []).append(rec)
            hit.append("fomo_top_trader")
            self._spawn(self.manage_position(pos, force=True))
        fl = paper.open_for(mint)
        if fl:   # Fluffy position: advisory note only — never auto-acts on the Fluffy plan
            fl.external_signals.append({**rec, "kind": "fomo_trader_sell", "advisory_only": True})
            self.advisories.appendleft({**rec, "note": "FOMO top trader sold a mint held in the Fluffy paper book"})
            hit.append("fluffy_advisory")
        return {"applied_to": hit}

    def annotate(self, mint: str, thesis: Dict[str, Any]) -> None:
        lst = self.theses.setdefault(mint, [])
        lst.append(thesis)
        del lst[:-10]

    # ---------------- entry ----------------
    def _spawn(self, coro: Any) -> None:
        try:
            t = asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            coro.close()
            return
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def _yield_to_fluffy(self, max_s: float) -> float:
        """Wait while a Telegram/Fluffy CA pipeline is running so we never compete for the loop/RPC."""
        from app.pump import fast_lane
        t0 = time.time()
        while fast_lane._tasks and time.time() - t0 < max_s:
            await asyncio.sleep(0.25)
        return time.time() - t0

    def _record(self, c: Dict[str, Any], decision: str, reasons: List[str], **extra: Any) -> Dict[str, Any]:
        c["decision"], c["reasons"] = decision, reasons
        c.update(extra)
        d = {"ts_ms": int(time.time() * 1000), "mint": c["mint"], "symbol": c.get("symbol"), "decision": decision,
             "reasons": reasons, "wallets": sorted({b["handle"] for b in c["buys"]}), "flags": c.get("flags", []),
             **{k: v for k, v in extra.items() if k in ("mark", "converging")}}
        self.decisions.appendleft(d)
        key = "entries" if decision == "enter" else "watching" if decision == "watching" else "skips"
        self.stats[key] += 1
        # v1.16 learning log: entries + skips, and only the first "watching" per mint (it repeats on every buy)
        if decision != "watching" or not c.get("_learn_watch_logged"):
            if decision == "watching":
                c["_learn_watch_logged"] = True
            try:
                from app.learning import candidates as learn
                mk = extra.get("mark") or {}
                sol_usd = learn.sol_usd_features().get("sol_usd")
                feats = {"wallets": len({b["wallet"] for b in c["buys"]}), "converging": extra.get("converging"),
                         "flags": c.get("flags") or None, "dex": mk.get("dex"), "mcap_usd": mk.get("mcap_usd"),
                         "liquidity_usd": mk.get("liquidity_usd"), "chg_m5": mk.get("m5"), "chg_h1": mk.get("h1"),
                         "chg_h6": mk.get("h6"), "pair_created_ms": mk.get("pair_created_ms"),
                         "first_buy_usd": max((b.get("usd") or 0) for b in c["buys"]) if c["buys"] else None,
                         **learn.sol_usd_features()}
                ref = (mk["price_usd"] / sol_usd) if mk.get("price_usd") and sol_usd else None
                learn.log_candidate(arm=PROFILE, source=SOURCE, mint=c["mint"], decision=decision, reasons=reasons,
                                    features=feats, ref_price_sol=ref,
                                    detect_ms=max((b["block_ms"] for b in c["buys"]), default=None),
                                    entered=decision == "enter")
            except Exception:  # noqa: BLE001
                pass
        return d

    async def decide(self, mint: str) -> Dict[str, Any]:
        from app.fomo_onchain.source import manager
        from app.fomo_onchain.wallets import watchlist
        from app.pump.paper import paper
        cfg = config()
        c = self.candidates[mint]
        if paper.profile_open_for(PROFILE, mint):
            return {"decision": "holding"}
        await self._yield_to_fluffy(cfg["yield_max_s"])
        now = time.time()
        if paper.profile_open_for(PROFILE, mint):
            return self._record(c, "skip", ["already holding"])
        if now - self.closed_at.get(mint, 0) < cfg["mint_cooldown_s"]:
            return self._record(c, "skip", ["mint cooldown after recent exit"])
        if len(paper.profile_open(PROFILE)) >= cfg["max_open"]:
            return self._record(c, "skip", [f"max open positions ({cfg['max_open']})"])

        win_ms = cfg["converge_window_s"] * 1000
        recent = [b for b in c["buys"] if b["block_ms"] >= now * 1000 - win_ms]
        good = {}
        for b in recent:
            w = watchlist.get(b["wallet"])
            q = w.quality if w else (b.get("quality") or 0)
            if q >= cfg["min_wallet_quality"] and (b.get("usd") or 0) >= cfg["min_buy_usd"]:
                good.setdefault(b["wallet"], {**b, "quality": q})
        if not good:
            return self._record(c, "watching", [f"no qualifying wallet buy (quality ≥{cfg['min_wallet_quality']:.0f}, "
                                                 f"≥${cfg['min_buy_usd']:.0f}) in window"])

        mk = (await market.marks([mint], client=manager.http)).get(mint) or {}
        sol_usd = market.sol_usd()
        c["symbol"] = mk.get("symbol") or c.get("symbol")
        if not mk.get("price_usd") or not sol_usd:
            return self._record(c, "watching", ["no DexScreener mark yet"], mark=mk)
        reasons: List[str] = []
        flags: List[str] = []
        ch = {k: (mk.get(k) or 0) for k in ("m5", "h1", "h6", "h24")}
        if ch["m5"] >= cfg["skip_m5_pct"] or ch["h1"] >= cfg["skip_h1_pct"] or ch["h6"] >= cfg["skip_h6_pct"] \
                or ch["h24"] >= cfg["skip_h24_pct"]:
            reasons.append(f"anti-chase: already up m5 {ch['m5']:+.0f}% h1 {ch['h1']:+.0f}% h6 {ch['h6']:+.0f}% "
                           f"h24 {ch['h24']:+.0f}%")
        elif ch["h1"] >= cfg["flag_h1_pct"] or ch["h6"] >= cfg["flag_h6_pct"]:
            flags.append(f"run-up flag (h1 {ch['h1']:+.1f}%, h6 {ch['h6']:+.1f}%) — needs convergence")
        first_fill = min((b["fill_price_usd"] for b in good.values() if b.get("fill_price_usd")), default=None)
        if first_fill and mk["price_usd"] > first_fill * (1 + cfg["max_above_first_fill_pct"] / 100):
            reasons.append(f"price {mk['price_usd'] / first_fill - 1:+.0%} above first top-wallet fill — too late")
        liq = mk.get("liquidity_usd") or 0
        if liq < cfg["min_liq_usd"]:
            reasons.append(f"liquidity ${liq:,.0f} < ${cfg['min_liq_usd']:,.0f}")
        age_h = (now * 1000 - mk["pair_created_ms"]) / 3.6e6 if mk.get("pair_created_ms") else None
        early = (mk.get("dex") == "pumpfun") or (age_h is not None and age_h <= cfg["early_max_pair_age_h"]) \
            or ((mk.get("mcap_usd") or 9e18) <= cfg["early_max_mcap_usd"])
        if early:
            flags.append("early (on-curve/young/low-mcap)")
        n = len(good)
        best_q = max(b["quality"] for b in good.values())
        converging = n >= cfg["converge_min"]
        if not converging:
            if flags and flags[0].startswith("run-up"):
                reasons.append("solo buy into a run-up — wait for a 2nd top wallet")
            elif not (early and best_q >= cfg["solo_min_quality"]):
                reasons.append(f"solo wallet (q {best_q:.0f}); need {cfg['converge_min']} wallets in "
                               f"{cfg['converge_window_s'] / 60:.0f}m, or q≥{cfg['solo_min_quality']:.0f} on an early token")
        c["flags"] = flags
        if reasons:
            hard = [r for r in reasons if not r.startswith("solo")]
            return self._record(c, "skip" if hard else "watching", reasons, mark=mk, converging=n)
        auth = await market.mint_authorities(mint, url=manager.rpc_url, client=manager.http)
        if not auth.get("ok"):
            return self._record(c, "watching", ["mint authority check failed — retry on next buy"], mark=mk)
        if auth.get("mint_authority") or auth.get("freeze_authority"):
            return self._record(c, "skip", ["mint or freeze authority still set"], mark=mk)
        price_sol = mk["price_usd"] / sol_usd
        pos = paper.open_profile_position(
            profile=PROFILE, mint=mint, source=SOURCE, entry_sol=cfg["entry_sol"], price_sol=price_sol,
            cost_bps=cfg["cost_bps"], venue=f"mark:{mk.get('dex')}",
            meta={"source_wallets": list(good), "handles": [b["handle"] for b in good.values()],
                  "symbol": mk.get("symbol"), "entry_mark": mk, "flags": flags, "first_fill_usd": first_fill,
                  "signal_lag_s": round(now - max(b["block_ms"] for b in good.values()) / 1000, 1),
                  "theses": self.theses.get(mint, [])[-3:]},
        )
        if not pos:
            return self._record(c, "skip", ["paper engine refused (disabled or duplicate)"], mark=mk)
        try:   # v1.14 live router (fomo_top_trader is paper-only unless listed in LIVE_ARMS)
            from app.live.engine import engine as live_engine
            live_engine.on_signal(PROFILE, mint, trader_wallets=list(good), ref_price_sol=price_sol)
        except Exception:  # noqa: BLE001
            pass
        st = exit_signals.fomo_stop(pos.entry_price_sol, pos.high_price_sol, None)
        pos.stop_price_sol, pos.stop_kind = st["stop_price"], st.get("stop_kind")
        pos.meta["mark_ms"] = mk.get("at_ms")
        self.ensure_loop()
        return self._record(c, "enter", [f"{n} top wallet(s), best q {best_q:.0f}"], mark=mk, converging=n,
                            position_id=pos.id)

    # ---------------- exits ----------------
    def ensure_loop(self) -> None:
        if self._loop_task and not self._loop_task.done():
            return
        try:
            self._loop_task = asyncio.get_running_loop().create_task(self._mark_loop())
        except RuntimeError:
            pass

    async def _mark_loop(self) -> None:
        from app.pump.paper import paper
        while paper.profile_open(PROFILE):
            for pos in list(paper.profile_open(PROFILE)):
                try:
                    await self.manage_position(pos)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("fomo manage error: %s", exc)
            await asyncio.sleep(config()["mark_poll_s"])

    async def manage_position(self, pos: Any, *, force: bool = False) -> None:
        from app.fomo_onchain.source import manager
        from app.pump.paper import paper
        if pos.status != "open":
            return
        cfg = config()
        mk = (await market.marks([pos.mint], client=manager.http, force=force)).get(pos.mint) or {}
        sol_usd = market.sol_usd()
        if mk.get("price_usd") and sol_usd:
            pos.last_price_sol = mk["price_usd"] / sol_usd
            pos.high_price_sol = max(pos.high_price_sol or 0, pos.last_price_sol)
            pos.meta["mark_ms"] = mk.get("at_ms")
        st = exit_signals.fomo_stop(pos.entry_price_sol, pos.high_price_sol, pos.stop_price_sol)
        if st.get("ratcheted"):
            pos.stop_price_sol, pos.stop_kind = st["stop_price"], st.get("stop_kind", pos.stop_kind)
        price = pos.last_price_sol
        venue = f"mark:{mk.get('dex')}"

        def sell(tokens: int, reason: str) -> None:
            if paper.sell_profile(pos, tokens, reason, price_sol=price, cost_bps=cfg["cost_bps"], venue=venue) \
                    and pos.status != "open":
                self.closed_at[pos.mint] = time.time()
                self.stats["exits"] += 1

        for mult, frac, label in exit_signals.fomo_config()["ladder"]:
            if pos.status == "open" and price and label not in pos.ladder_done and price >= pos.entry_price_sol * mult:
                pos.ladder_done.append(label)
                sell(int(pos.tokens_initial * frac), label)
        if pos.status != "open":
            return
        pending = pos.meta.pop("pending_trader_sells", [])
        age = (time.time() * 1000 - pos.meta["mark_ms"]) / 1000 if pos.meta.get("mark_ms") else None
        cards = exit_signals.evaluate_fomo(entry=pos.entry_price_sol, price=price, high=pos.high_price_sol,
                                           stop_price=pos.stop_price_sol, stop_kind=pos.stop_kind,
                                           entry_ms=pos.entry_ms, trader_sells=pending, mark_age_s=age)
        pos.last_cards = cards
        pos.external_signals.extend(pending)
        for card in exit_signals.fired(cards):
            if pos.status != "open":
                break
            if card["action"] == "exit_all":
                sell(pos.tokens_left, card["kind"])
            elif card["action"] == "exit_fraction":
                sell(int(pos.tokens_left * card["fraction"]), "follow_trader_partial")

    # ---------------- views ----------------
    def view(self) -> Dict[str, Any]:
        from app.pump.paper import paper
        cands = sorted(self.candidates.values(), key=lambda c: -c["first_seen_ms"])[:40]
        return {"profile": PROFILE, "config": config(), "exit_config": {k: v for k, v in exit_signals.fomo_config().items()},
                "stats": dict(self.stats), "summary": paper.profile_summary(PROFILE),
                "positions": [p.view() for p in sorted(paper.profile_positions.values(), key=lambda x: -x.entry_ms)
                              if p.profile == PROFILE][:30],
                "candidates": [{**{k: v for k, v in c.items() if k != "buys"}, "buys": c["buys"][-8:],
                                "theses": self.theses.get(c["mint"], [])[-3:]} for c in cands],
                "decisions": list(self.decisions)[:50], "trader_sells": list(self.trader_sells)[:50],
                "fluffy_advisories": list(self.advisories)[:20]}


strategy = Strategy()
on_ingested_buy = strategy.on_ingested_buy
