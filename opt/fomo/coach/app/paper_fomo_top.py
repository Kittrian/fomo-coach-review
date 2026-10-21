"""v1.19 `fomo_top_paper` — PAPER-ONLY 7-day copy test of two FOMO top wallets (zakum, NotARandomUser).

Sky, 2026-10-08 4:56 PM CT. Hard guarantees:
* PAPER ONLY. This module never imports the signer, never calls Jupiter (no order / quote / execute), never reads
  the keypair, never touches the live engine's positions, day counters, caps, kill switch or wallet.
  Paper trades therefore never count toward the live daily trade cap, the daily loss limit or max open positions.
* Detection: the existing fomo-onchain source (logsSubscribe on the wallets, public mainnet-beta WS + publicnode
  getTransaction) calls `on_wallet_buy` / `on_wallet_sell` for verified FOMO co-signed trades.
* Price: DexScreener public marks (free, batched, cached) → SOL via SOL/USD. Entry = mark at detection
  (fallback: the copied wallet's own fill price) + PAPER_FOMO_TOP_COST_BPS per side.
* Checks: the SAME rug check (own RugChecker instance so live stats are untouched) and the SAME mesh rule as the
  live buy path; verdicts are recorded (`would_live_buy`) but the paper entry is simulated either way.
* Exits: the CURRENT live scout exit (app.live.engine.evaluate_exit + live config: 50% at LIVE_TP_MULT, then
  LIVE_TRAIL_PCT trail, LIVE_HARD_STOP_PCT stop, min-10 and max-hold, runner rule) on two tracks:
    - `live_exit`     : live rules WITHOUT trader-sells (what Sky asked for: 2x half / 30% trail / 35% stop)
    - `follow_wallet` : the same plus "exit when the copied wallet sells" (exactly the live scout rule set)
Off switch: PAPER_FOMO_TOP_ENABLED (default false in code). Window: PAPER_FOMO_TOP_DAYS (7) from the first start.
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
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from app.paths import data_dir

logger = logging.getLogger("coach.paper.fomo_top")

ARM = "fomo_top_paper"
TRACKS = ("live_exit", "follow_wallet")
DEFAULT_WALLETS = ("BXLEfLN8bHsQCn81rFZebcavP1JSory1XbSDoeYQxJPc:zakum,"
                   "2L17Sw85Lh2Ca7ejEzWqyJsZ3yS4DxQhYr52X5H5xRns:NotARandomUser")
TZ = ZoneInfo("America/Chicago")
DECIMALS = 6                      # synthetic token units (paper)


def _f(name: str, d: float) -> float:
    try:
        return float(os.getenv(name, str(d)).strip())
    except ValueError:
        return d


def cfg() -> Dict[str, Any]:
    targets: Dict[str, str] = {}
    for part in os.getenv("PAPER_FOMO_TOP_WALLETS", DEFAULT_WALLETS).split(","):
        a, _, h = part.strip().partition(":")
        if a:
            targets[a.strip()] = (h or a[:6]).strip()
    return {
        "enabled": os.getenv("PAPER_FOMO_TOP_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"},
        "targets": targets,
        "size_sol": _f("PAPER_FOMO_TOP_SIZE_SOL", 0.20),
        "days": _f("PAPER_FOMO_TOP_DAYS", 7),
        "cost_bps": int(_f("PAPER_FOMO_TOP_COST_BPS", 150)),     # per side: fees + slippage assumption
        "mark_s": _f("PAPER_FOMO_TOP_MARK_S", 5),
        "mesh_timeout_s": _f("PAPER_FOMO_TOP_MESH_TIMEOUT_S", 8),
        "max_open": int(_f("PAPER_FOMO_TOP_MAX_OPEN", 40)),        # paper-book safety bound only
        "log_path": Path(os.getenv("PAPER_FOMO_TOP_LOG_PATH", str(data_dir() / "paper_fomo_top.jsonl"))),
        "state_path": Path(os.getenv("PAPER_FOMO_TOP_STATE_PATH", str(data_dir() / "paper_fomo_top_state.json"))),
    }


def _ct(ms: Optional[int]) -> Optional[str]:
    return datetime.fromtimestamp(ms / 1000, TZ).strftime("%a %b %-d %-I:%M %p CT") if ms else None


def _short(a: str) -> str:
    return f"{a[:4]}…{a[-4:]}" if a and len(a) > 10 else a


def _live_exit_cfg_rules() -> Tuple[Dict[str, Any], List[str]]:
    """Current live exit config + the live scout rule set (read at call time → follows live changes)."""
    from app.live.config import DEFAULT_RULES, config as live_config
    c = live_config()
    rules = list((c.get("rules") or {}).get("scout_solo") or DEFAULT_RULES)
    return c, rules


async def mesh_verdict(mint: str, timeout_s: float) -> Dict[str, Any]:
    """Same decision as LiveEngine._mesh_check, but on the normal (non-live) mesh budget so the paper test can't
    eat the live pre-buy rate budget. Read-only."""
    try:
        from app.mesh import enrich_with_timeout
        from app.risk_gate import mesh_rule_decision
        from app.live.tg_gate import mesh_stale
        mesh = await enrich_with_timeout(mint, "solana", timeout=timeout_s, live=False)
        rule = mesh_rule_decision(mesh)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "why": f"mesh check error ({type(exc).__name__})"}
    if rule.get("blocked"):
        return {"ok": False, "why": "mesh hard block: " + ", ".join(map(str, (rule.get("reasons") or [])[:3]))}
    if rule.get("mesh_status") != "ok":
        return {"ok": False, "why": f"mesh {rule.get('mesh_status')}"}
    if mesh_stale(mesh):
        return {"ok": False, "why": "mesh data stale"}
    return {"ok": True, "why": "mesh ok"}


class PaperFomoTop:
    arm = ARM
    tracks = TRACKS

    def cfg(self) -> Dict[str, Any]:            # v1.29: subclasses (paper_v3) override cfg/arm/tracks
        return cfg()

    def _custom_track(self, pid: str, b: Dict[str, Any], track: str, p: Any, now: int) -> bool:
        """v1.29 hook: return True when the subclass handled this track itself."""
        return False

    def __init__(self) -> None:
        self.start_ms: Optional[int] = None
        self.end_ms: Optional[int] = None
        self.book: Dict[str, Dict[str, Any]] = {}          # id -> {"meta":…, "tracks": {track: LivePos}}
        self.seen_mints: Dict[str, str] = {}                # mint -> paper id (one entry per mint for the test)
        self.stats: Dict[str, int] = collections.defaultdict(int)
        self._loop: Optional[asyncio.Task] = None
        self._tasks: set = set()
        self._rug = None
        self.loaded = False
        from app import paper_realism as _pr                 # v1.37 realistic tracks (paper only)
        self.real_ledger = _pr.GateLedger()
        self._drain = _pr.DrainWatch()

    # ---------------- v1.37 realism helpers ----------------
    def real_tracks(self) -> Tuple[str, ...]:
        from app import paper_realism as _pr
        return tuple(_pr.REAL_PREFIX + t for t in self.tracks) if _pr.enabled() else ()

    def record_track(self) -> str:
        """The realistic track that mirrors the live twin's exit rules (headline + gate ledger)."""
        from app import paper_realism as _pr
        P = _pr.params(self.arm)
        rules = (P.get("live_cfg") or {}).get("rules", {}).get(P["live_arm"]) or []
        return _pr.REAL_PREFIX + ("follow_wallet" if "trader_sells" in rules else "live_exit")

    async def _real_onchain(self, mint: str, block_ms: Optional[int]) -> Optional[Dict[str, Any]]:
        """First on-chain price read at ≥ signal + the live p50 signal→land latency. Never raises."""
        try:
            from app import paper_realism as _pr
            if not _pr.enabled() or not _pr.onchain_enabled():
                return None
            P = _pr.params(self.arm)
            t_sig = block_ms or int(time.time() * 1000)
            wait = (t_sig + P["lat_ms"] - time.time() * 1000) / 1000
            if wait > 0:
                await asyncio.sleep(min(wait, 15))
            return await _pr.onchain_price(mint)
        except Exception:  # noqa: BLE001
            return None

    # ---------------- persistence ----------------
    def load(self) -> None:
        c = self.cfg()
        self.loaded = True
        try:
            st = json.loads(c["state_path"].read_text())
        except Exception:  # noqa: BLE001
            st = {}
        from app.live.engine import LivePos
        self.start_ms, self.end_ms = st.get("start_ms"), st.get("end_ms")
        self.seen_mints = dict(st.get("seen_mints") or {})
        try:
            self.real_ledger.load(st.get("real_ledger"))
        except Exception:  # noqa: BLE001
            pass
        self.book = {}
        for pid, b in (st.get("open") or {}).items():
            try:
                self.book[pid] = {"meta": b["meta"], "tracks": {t: LivePos(**v) for t, v in b["tracks"].items()}}
            except Exception:  # noqa: BLE001
                continue
        if c["enabled"] and not self.start_ms:
            self.start_ms = int(time.time() * 1000)
            self.end_ms = self.start_ms + int(c["days"] * 86400 * 1000)
            self._log("start", start_ms=self.start_ms, end_ms=self.end_ms, start_ct=_ct(self.start_ms),
                      end_ct=_ct(self.end_ms), targets={_short(a): h for a, h in c["targets"].items()},
                      size_sol=c["size_sol"], cost_bps=c["cost_bps"])
            logger.info("fomo_top_paper started %s → ends %s", _ct(self.start_ms), _ct(self.end_ms))
        self.save()
        if self.book:
            self.ensure_loop()

    def save(self) -> None:
        c = self.cfg()
        try:
            c["state_path"].parent.mkdir(parents=True, exist_ok=True)
            tmp = c["state_path"].with_suffix(".tmp")
            tmp.write_text(json.dumps({
                "arm": self.arm, "paper_only": True, "start_ms": self.start_ms, "end_ms": self.end_ms,
                "seen_mints": self.seen_mints, "real_ledger": self.real_ledger.to_dict(),
                "open": {pid: {"meta": b["meta"], "tracks": {t: asdict(p) for t, p in b["tracks"].items()}}
                         for pid, b in self.book.items()}}, default=str))
            tmp.replace(c["state_path"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("fomo_top_paper save failed: %s", type(exc).__name__)

    def _log(self, kind: str, **rec: Any) -> None:
        p = self.cfg()["log_path"]
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"ts_ms": int(time.time() * 1000), "kind": kind, "arm": self.arm, "paper": True, **rec},
                                    default=str) + "\n")
        except Exception as exc:  # noqa: BLE001
            logger.warning("fomo_top_paper log failed: %s", type(exc).__name__)

    # ---------------- window ----------------
    def active(self, now_ms: Optional[int] = None) -> bool:
        c = self.cfg()
        if not c["enabled"]:
            return False
        if not self.loaded:
            self.load()
        now_ms = now_ms or int(time.time() * 1000)
        return bool(self.start_ms and self.end_ms and self.start_ms <= now_ms < self.end_ms)

    def _spawn(self, coro: Any) -> None:
        try:
            t = asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            coro.close()
            return
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    # ---------------- events from the fomo-onchain source ----------------
    def on_wallet_buy(self, wallet: str, handle: str, res: Dict[str, Any], *, signature: Optional[str] = None,
                      block_ms: Optional[int] = None, detect_lag_ms: Optional[int] = None) -> Optional[str]:
        """Sync + cheap; spawns the async entry. Returns a short status (for tests / logs)."""
        c = self.cfg()
        if wallet not in c["targets"]:
            return None
        if not self.active():
            return "inactive"
        mint = res.get("mint")
        if not mint:
            return "no mint"
        self.stats["buys_seen"] += 1
        pid = self.seen_mints.get(mint)
        if pid:
            self.stats["repeat_buys"] += 1
            b = self.book.get(pid)
            if b and handle not in b["meta"]["handles"]:
                b["meta"]["handles"].append(handle)
                b["meta"]["wallets"].append(wallet)
            self._log("also_bought", id=pid, mint=mint, handle=handle, usd=res.get("usd"), signature=signature)
            return "repeat"
        if len(self.book) >= c["max_open"]:
            self.stats["skipped_max_open"] += 1
            self._log("skip", mint=mint, handle=handle, reason=f"paper book full ({c['max_open']})")
            return "full"
        pid = f"ft{int(time.time() * 1000):x}{mint[:4]}"
        self.seen_mints[mint] = pid                          # reserve before the await (no double entry)
        self._spawn(self._enter(pid, wallet, handle, mint, res, signature, block_ms, detect_lag_ms))
        return "entering"

    def on_wallet_sell(self, wallet: str, handle: str, mint: str, *, sold_fraction: Optional[float] = None,
                       usd: Optional[float] = None, signature: Optional[str] = None,
                       block_ms: Optional[int] = None) -> Optional[str]:
        if wallet not in self.cfg()["targets"]:
            return None
        pid = self.seen_mints.get(mint)
        b = self.book.get(pid or "")
        if not b or wallet not in b["meta"]["wallets"]:
            return None
        self.stats["wallet_sells"] += 1
        live_t, follow_t = b["tracks"]["live_exit"], b["tracks"]["follow_wallet"]
        px = live_t.last_price_sol
        first = not b["meta"].get("wallet_sold_ms")
        if first:
            b["meta"]["wallet_sold_ms"] = int(time.time() * 1000)
            b["meta"]["wallet_sold_mult"] = round(px / live_t.entry_price_sol, 4) if px and live_t.entry_price_sol else None
        follow_t.trader_sold = True
        rf = b["tracks"].get("real_follow_wallet")
        if rf is not None:
            rf.trader_sold = True
            if rf.meta.get("trader_sold_ms") is None:
                rf.meta["trader_sold_ms"] = int(time.time() * 1000)     # live trader-sell window (candle-stop arms)
        # what the live_exit track would have booked if it had dumped everything at this mark
        if_exit = None
        if px and live_t.status == "open":
            recv = live_t.received_lamports + self._proceeds(live_t.tokens_left, px)
            if_exit = round((recv - live_t.spent_lamports) / 1e9, 6)
        self._log("wallet_sell", id=pid, mint=mint, handle=handle, sold_fraction=sold_fraction, usd=usd,
                  signature=signature, block_ms=block_ms, price_sol=px, first=first,
                  multiple=b["meta"].get("wallet_sold_mult") if first else (round(px / live_t.entry_price_sol, 4) if px else None),
                  live_exit_pnl_if_exit_all_now_sol=if_exit)
        self.save()
        self._spawn(self._manage(pid))
        return "noted"

    # ---------------- entry ----------------
    def _rugchecker(self):
        if self._rug is None:
            from app.live.rugcheck import RugChecker
            self._rug = RugChecker()                         # own instance: live rugcheck stats stay untouched
        return self._rug

    async def _price_sol(self, mint: str, *, force: bool = True) -> Tuple[Optional[float], Dict[str, Any]]:
        from app.fomo_onchain import WSOL_MINT, market
        mk = await market.marks([mint], force=force)
        m = mk.get(mint) or {}
        sol = (mk.get(WSOL_MINT) or {}).get("price_usd") or market.sol_usd()
        if m.get("price_usd") and sol:
            return float(m["price_usd"]) / float(sol), m
        return None, m

    def _proceeds(self, tokens: int, price_sol: float) -> int:
        gross = tokens / 10 ** DECIMALS * price_sol * 1e9
        return int(gross * (1 - self.cfg()["cost_bps"] / 1e4))

    def _entry_gate(self, wallet: str, handle: str, mint: str, res: Dict[str, Any], px: float) -> Optional[str]:
        """v1.30 hook: return a skip reason to refuse a paper entry (subclass logs it). Base arm: never gates."""
        return None

    async def _enter(self, pid: str, wallet: str, handle: str, mint: str, res: Dict[str, Any],
                     signature: Optional[str], block_ms: Optional[int], detect_lag_ms: Optional[int]) -> None:
        from app.fomo_onchain import market
        from app.live.engine import LivePos
        c = self.cfg()
        t0 = time.time()
        try:
            (px, mk), rug, mesh, onchain = await asyncio.gather(
                self._price_sol(mint),
                self._rugchecker().prebuy(mint, self.arm),
                mesh_verdict(mint, c["mesh_timeout_s"]),
                self._real_onchain(mint, block_ms))
        except Exception as exc:  # noqa: BLE001
            self.stats["entry_errors"] += 1
            self.seen_mints.pop(mint, None)
            self._log("skip", mint=mint, handle=handle, reason=f"entry error {type(exc).__name__}")
            return
        src = "dexscreener"
        sol_usd = market.sol_usd()
        if not px and res.get("fill_price_usd") and sol_usd:
            px, src = float(res["fill_price_usd"]) / float(sol_usd), "trader_fill"
        if not px or px <= 0:
            self.stats["skipped_no_price"] += 1
            self._log("skip", mint=mint, handle=handle, reason="no price (DexScreener + trader fill missing)",
                      rug_ok=rug.get("ok"), mesh_ok=mesh.get("ok"))
            self.save()
            return
        gate = self._entry_gate(wallet, handle, mint, res, px)   # v1.30: subclass hook (default: none)
        if gate:
            self.seen_mints.pop(mint, None)
            self.save()
            return
        entry_px = px * (1 + c["cost_bps"] / 1e4)            # we pay the cost on the way in
        spent = int(c["size_sol"] * 1e9)
        tokens = int(c["size_sol"] / entry_px * 10 ** DECIMALS)
        now = int(time.time() * 1000)
        meta = {"id": pid, "mint": mint, "symbol": mk.get("symbol"), "handles": [handle], "wallets": [wallet],
                "opened_ms": now, "entry_src": src, "mark_price_sol": px, "trader_usd": res.get("usd"),
                "trader_fill_price_usd": res.get("fill_price_usd"), "signature": signature, "block_ms": block_ms,
                "detect_lag_ms": detect_lag_ms, "checks_ms": round((time.time() - t0) * 1000),
                "rug": {k: rug.get(k) for k in ("ok", "refuse", "unknown", "na", "elapsed_ms")},
                "mesh": mesh, "would_live_buy": bool(rug.get("ok")) and bool(mesh.get("ok")),
                "liquidity_usd": mk.get("liquidity_usd"), "mcap_usd": mk.get("mcap_usd"), "dex": mk.get("dex")}
        tracks = {t: LivePos(mint=mint, arm=self.arm, size_sol=c["size_sol"], spent_lamports=spent, tokens_initial=tokens,
                             tokens_left=tokens, entry_price_sol=entry_px, entry_ms=now, decimals=DECIMALS,
                             id=f"{pid}:{t}", trader_wallets=[wallet], high_price_sol=entry_px,
                             last_price_sol=px, last_mark_ms=now, mark_source=src, flags=["paper"])
                  for t in self.tracks}
        self.book[pid] = {"meta": meta, "tracks": tracks}
        self.stats["opened"] += 1
        self._log("open", **meta, size_sol=c["size_sol"], entry_price_sol=entry_px, cost_bps=c["cost_bps"])
        try:
            self._open_real(pid, mint, meta, tracks, res, px, src, mk, onchain, now)
        except Exception as exc:  # noqa: BLE001
            self.stats["real_errors"] += 1
            logger.warning("paper realism open %s: %s", mint[:8], type(exc).__name__)
        logger.info("fomo_top_paper OPEN %s %s via %s @ %.3g SOL (rug %s, mesh %s)", handle, mint[:8], src, entry_px,
                    "ok" if rug.get("ok") else "REFUSE", "ok" if mesh.get("ok") else "REFUSE")
        self.save()
        self.ensure_loop()

    def _open_real(self, pid: str, mint: str, meta: Dict[str, Any], tracks: Dict[str, Any], res: Dict[str, Any],
                   px: float, src: str, mk: Dict[str, Any], onchain: Optional[Dict[str, Any]], now: int) -> None:
        """v1.37 realistic tracks: live-calibrated entry, own size/fees, gate tags. Legacy tracks untouched."""
        from app import paper_realism as _pr
        from app.fomo_onchain import market
        from app.live.engine import LivePos
        if not _pr.enabled():
            return
        P = _pr.params(self.arm)
        su = market.sol_usd()
        tp = res.get("trader_price_sol") or (float(res["fill_price_usd"]) / float(su)
                                             if res.get("fill_price_usd") and su else None)
        nm = {"px": px, "ms": mk.get("at_ms") or now, "src": src} if src == "dexscreener" else None
        ef = _pr.entry_fill(self.arm, tp, onchain, nm, P["size_sol"], P)
        info = {k: ef.get(k) for k in ("ok", "px", "src", "mark_px", "sim_px", "floor_px", "trader_px", "lat_ms",
                                         "slip_pct", "route", "rejected", "reason", "floored_to_trader")}
        if not ef.get("ok"):
            self.stats["real_skipped"] += 1
            meta["real"] = {**info, "skipped": True}
            self._log("real_skip", id=pid, mint=mint, handle=meta["handles"][0], **info)
            return
        epx = float(ef["px"])
        spent, tokens = _pr.entry_position(P, epx, P["size_sol"])
        refused = self.real_ledger.refusals(self.arm, now, would_live_buy=meta.get("would_live_buy"), trader_px=tp,
                                            entry_px=epx, P=P)
        gated = not refused
        if gated:
            self.real_ledger.opened(pid, now)
        for t in self.real_tracks():
            tracks[t] = LivePos(mint=mint, arm=P["live_arm"], size_sol=P["size_sol"], spent_lamports=spent,
                                tokens_initial=tokens, tokens_left=tokens, entry_price_sol=epx, entry_ms=now,
                                decimals=DECIMALS, id=f"{pid}:{t}", trader_wallets=list(meta["wallets"]),
                                high_price_sol=epx, last_price_sol=px, last_mark_ms=now, mark_source=src,
                                flags=["paper", "realistic"])
        meta["real"] = {**info, "gates_refused": refused, "gated": gated, "size_sol": P["size_sol"],
                        "spent_lamports": spent, "fill_vs_trader_pct": round((epx / tp - 1) * 100, 2) if tp else None,
                        "fill_vs_legacy_pct": round((epx / tracks[self.tracks[0]].entry_price_sol - 1) * 100, 2)}
        self.stats["real_opened"] += 1
        self.stats["real_gated" if gated else "real_refused"] += 1
        self._log("real_open", id=pid, mint=mint, handle=meta["handles"][0], entry_price_sol=epx,
                  legacy_entry_price_sol=tracks[self.tracks[0]].entry_price_sol, size_sol=P["size_sol"],
                  spent_lamports=spent, gates_refused=refused, gated=gated, live_arm=P["live_arm"],
                  fill_vs_trader_pct=meta["real"]["fill_vs_trader_pct"],
                  fill_vs_legacy_pct=meta["real"]["fill_vs_legacy_pct"], **{k: v for k, v in info.items() if k != "px"})

    # ---------------- marks + exits ----------------
    def ensure_loop(self) -> None:
        if self._loop and not self._loop.done():
            return
        try:
            self._loop = asyncio.get_running_loop().create_task(self._mark_loop())
        except RuntimeError:
            pass

    async def _mark_loop(self) -> None:
        from app.fomo_onchain import WSOL_MINT, market
        while self.book:
            try:
                mints = list({b["meta"]["mint"] for b in self.book.values()})
                mk = await market.marks(mints, force=True)
                sol = (mk.get(WSOL_MINT) or {}).get("price_usd") or market.sol_usd()
                now = int(time.time() * 1000)
                for pid, b in list(self.book.items()):
                    m = mk.get(b["meta"]["mint"]) or {}
                    if m.get("price_usd") and sol:
                        px = float(m["price_usd"]) / float(sol)
                        try:                     # v1.32 absurd-mark guard (paper only): skip, log, use next mark
                            from app.scout.exit_variants import log_anomaly, mark_anomaly
                            ops = [p for p in b["tracks"].values() if p.status == "open"]
                            prev = max((p.last_price_sol or 0) for p in ops) if ops else None
                            bad = mark_anomaly(prev, [p.entry_price_sol for p in ops], px)
                        except Exception:  # noqa: BLE001
                            bad = None
                        if bad:
                            self.stats["marks_rejected"] += 1
                            log_anomaly(self.arm, b["meta"]["mint"], px, bad, prev=prev, id=pid)
                            continue
                        for p in b["tracks"].values():
                            if p.status == "open":
                                p.last_price_sol, p.last_mark_ms, p.mark_source = px, now, "dexscreener"
                                p.high_price_sol = max(p.high_price_sol or px, px)
                        self._real_drain(pid, b, px, now)
                    await self._manage(pid)
                self.save()
            except Exception as exc:  # noqa: BLE001
                logger.warning("fomo_top_paper mark loop: %s", type(exc).__name__)
            await asyncio.sleep(self.cfg()["mark_s"])

    def _real_drain(self, pid: str, b: Dict[str, Any], px: float, now: int) -> None:
        try:
            from app import paper_realism as _pr
            if not any(t.startswith(_pr.REAL_PREFIX) and p.status == "open" for t, p in b["tracks"].items()):
                return
            why = self._drain.check(pid, px, now, _pr.params(self.arm))
            if why and not b["meta"].get("real_drain"):
                b["meta"]["real_drain"] = {"ts_ms": now, "why": why, "price_sol": px}
                self.stats["real_drains"] += 1
        except Exception:  # noqa: BLE001
            pass

    def _real_sell(self, pid: str, b: Dict[str, Any], track: str, p: Any, reason: str, frac_tokens: int, mark_px: float,
                   now: int, *, stale: bool = False) -> Dict[str, Any]:
        """v1.37 realistic fill for one sell on a real_* track (live sell ratio + live fees)."""
        from app import paper_realism as _pr
        P = _pr.params(self.arm)
        sell_px = _pr.exit_px(P, reason, mark_px, p.entry_price_sol, P.get("live_cfg"))
        basis = p.cost_basis(frac_tokens)
        lam = _pr.proceeds_lamports(P, frac_tokens, sell_px)
        p.tokens_left -= frac_tokens
        p.received_lamports += lam
        fill = {"ts_ms": now, "reason": reason, "tokens": frac_tokens, "sol": round(lam / 1e9, 6),
                "pnl_sol": round((lam - basis) / 1e9, 6), "price_sol": sell_px, "mark_price_sol": mark_px,
                "multiple": round(sell_px / p.entry_price_sol, 4) if p.entry_price_sol else None,
                "stale_mark": stale, "realistic": True}
        p.sells.append(fill)
        self._log("sell", id=pid, track=track, mint=p.mint, **fill)
        return fill

    def _real_close(self, pid: str, b: Dict[str, Any], track: str, p: Any, reason: str, now: int) -> None:
        p.status, p.exit_reason, p.closed_ms = "closed", reason, now
        pn = p.pnl()
        self.stats[f"closed_{track}"] += 1
        rm = b["meta"].get("real") or {}
        if track == self.record_track():
            self.real_ledger.closed(pid, now, pn["pnl_sol"])
        self._log("close", id=pid, track=track, mint=p.mint, symbol=b["meta"].get("symbol"),
                  handles=b["meta"]["handles"], reason=reason, pnl_sol=pn["pnl_sol"], pnl_pct=pn["pnl_pct"],
                  hold_s=round((now - p.entry_ms) / 1000), would_live_buy=b["meta"]["would_live_buy"],
                  realistic=True, gated=rm.get("gated"), gates_refused=rm.get("gates_refused"),
                  size_sol=p.size_sol, fill_vs_trader_pct=rm.get("fill_vs_trader_pct"),
                  max_multiple=round((p.high_price_sol or 0) / p.entry_price_sol, 4) if p.entry_price_sol else None)

    async def _manage(self, pid: str) -> None:
        from app.live.engine import evaluate_exit, is_partial
        b = self.book.get(pid)
        if not b:
            return
        lc, rules = _live_exit_cfg_rules()
        now = int(time.time() * 1000)
        stale_ms = float(lc.get("stale_mark_s", 60)) * 1000
        for track, p in b["tracks"].items():
            if p.status != "open":
                continue
            if self._custom_track(pid, b, track, p, now):
                continue
            real = track.startswith("real_")
            base = track[5:] if real else track
            if real:
                rr = list((lc.get("rules") or {}).get(p.arm) or rules)      # the live twin arm's own rule set
                r = [x for x in rr if x != "trader_sells"] if base == "live_exit" else rr
            else:
                r = [x for x in rules if x != "trader_sells"] if track == "live_exit" else list(rules)
            if "trader_sells" not in r and base == "follow_wallet":
                r.append("trader_sells")
            stale = now - (p.last_mark_ms or p.entry_ms) > stale_ms
            px = None if stale else p.last_price_sol
            if real:
                acts = evaluate_exit(p, px, now, lc, r)
                dr = b["meta"].get("real_drain")
                if dr and not acts and p.last_price_sol:
                    acts = [("sell_all", 1.0, "rug_flag_liquidity_drain")]
                for kind, frac, reason in acts:
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
                    self._real_sell(pid, b, track, p, reason, tokens, p.last_price_sol or p.entry_price_sol, now,
                                    stale=stale)
                    if p.tokens_left <= 0 or not is_partial(reason):
                        self._real_close(pid, b, track, p, reason, now)
                        break
                continue
            for kind, frac, reason in evaluate_exit(p, px, now, lc, r):
                sell_px = p.last_price_sol or p.entry_price_sol
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
                lam = self._proceeds(tokens, sell_px)
                p.tokens_left -= tokens
                p.received_lamports += lam
                fill = {"ts_ms": now, "reason": reason, "tokens": tokens, "sol": round(lam / 1e9, 6),
                        "pnl_sol": round((lam - basis) / 1e9, 6), "price_sol": sell_px,
                        "multiple": round(sell_px / p.entry_price_sol, 4) if p.entry_price_sol else None,
                        "stale_mark": stale}
                p.sells.append(fill)
                self._log("sell", id=pid, track=track, mint=p.mint, **fill)
                if p.tokens_left <= 0 or not is_partial(reason):
                    p.status, p.exit_reason, p.closed_ms = "closed", reason, now
                    pn = p.pnl()
                    self.stats[f"closed_{track}"] += 1
                    self._log("close", id=pid, track=track, mint=p.mint, symbol=b["meta"].get("symbol"),
                              handles=b["meta"]["handles"], reason=reason, pnl_sol=pn["pnl_sol"], pnl_pct=pn["pnl_pct"],
                              hold_s=round((now - p.entry_ms) / 1000), would_live_buy=b["meta"]["would_live_buy"],
                              max_multiple=round((p.high_price_sol or 0) / p.entry_price_sol, 4) if p.entry_price_sol else None)
                    logger.info("fomo_top_paper CLOSE %s %s %s %s %+.4f SOL", track, b["meta"]["handles"][0],
                                p.mint[:8], reason, pn["pnl_sol"])
                    break
        if all(p.status == "closed" for p in b["tracks"].values()):
            self.book.pop(pid, None)
            self._drain.drop(pid)
            self.save()

    # ---------------- summary ----------------
    def summary(self, recent: int = 15) -> Dict[str, Any]:
        c = self.cfg()
        if not self.loaded:
            self.load()
        rows: List[Dict[str, Any]] = []
        try:
            with c["log_path"].open() as fh:
                for line in fh:
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        continue
        except FileNotFoundError:
            pass
        opens = {r["id"]: r for r in rows if r.get("kind") == "open"}
        closes = [r for r in rows if r.get("kind") == "close"]

        def agg(rs: List[Dict[str, Any]]) -> Dict[str, Any]:
            n = len(rs)
            tot = sum(r.get("pnl_sol") or 0 for r in rs)
            wins = sum(1 for r in rs if (r.get("pnl_sol") or 0) > 0)
            return {"closed": n, "wins": wins, "win_rate": round(wins / n, 3) if n else None,
                    "pnl_sol": round(tot, 6), "avg_pnl_pct": round(sum(r.get("pnl_pct") or 0 for r in rs) / n, 2) if n else None}

        from app import paper_realism as _pr
        legacy = {t: agg([r for r in closes if r.get("track") == t]) for t in self.tracks}
        real_names = tuple(_pr.REAL_PREFIX + t for t in self.tracks)
        real = {t[len(_pr.REAL_PREFIX):]: agg([r for r in closes if r.get("track") == t]) for t in real_names}
        rec_track = self.record_track()
        rec_rows = [r for r in closes if r.get("track") == rec_track]
        real_gated = agg([r for r in rec_rows if r.get("gated")])
        # v1.37: the REALISTIC track is the headline; legacy (optimistic) kept for comparison
        tracks = real if _pr.enabled() else legacy
        by_handle = {h: {t: agg([r for r in closes if r.get("track") == (_pr.REAL_PREFIX + t if _pr.enabled() else t)
                                 and h in (r.get("handles") or [])[:1]])
                         for t in self.tracks} for h in c["targets"].values()}
        legacy_by_handle = {h: {t: agg([r for r in closes if r.get("track") == t and h in (r.get("handles") or [])[:1]])
                                for t in self.tracks} for h in c["targets"].values()}
        gated = {k: {t: agg([r for r in closes if r.get("track") == t and bool(r.get("would_live_buy")) == v])
                     for t in self.tracks} for k, v in (("checks_passed", True), ("checks_refused", False))}
        ropen = [r for r in rows if r.get("kind") == "real_open"]
        refused_ct = collections.Counter(g.split(" ")[0] for r in ropen for g in (r.get("gates_refused") or []))
        fvt = sorted(r["fill_vs_trader_pct"] for r in ropen if r.get("fill_vs_trader_pct") is not None)
        realism = {**_pr.describe(), "params": {k: v for k, v in _pr.params(self.arm).items() if k != "live_cfg"},
                   "record_track": rec_track, "real_opens": len(ropen),
                   "real_skips": sum(1 for r in rows if r.get("kind") == "real_skip"),
                   "entry_src": dict(collections.Counter(r.get("src") for r in ropen)),
                   "fill_vs_trader_median_pct": fvt[len(fvt) // 2] if fvt else None,
                   "gates_refused_counts": dict(refused_ct), "gated_entries": sum(1 for r in ropen if r.get("gated")),
                   "record_track_all": agg(rec_rows), "record_track_gated": real_gated}
        open_now = []
        for pid, b in self.book.items():
            lt = b["tracks"]["live_exit"]
            open_now.append({"id": pid, "mint": b["meta"]["mint"], "symbol": b["meta"].get("symbol"),
                             "handles": b["meta"]["handles"], "age_s": round((time.time() * 1000 - lt.entry_ms) / 1000),
                             "multiple_now": lt.pnl()["multiple_now"],
                             "tracks": {t: {"status": p.status, **p.pnl()} for t, p in b["tracks"].items()},
                             "would_live_buy": b["meta"]["would_live_buy"]})
        rec = []
        for r in sorted(closes, key=lambda x: -x["ts_ms"])[: recent * 2]:
            o = opens.get(r["id"], {})
            rec.append({"id": r["id"], "track": r["track"], "symbol": r.get("symbol") or (r.get("mint") or "")[:6],
                        "mint": r.get("mint"), "handle": (r.get("handles") or ["?"])[0], "reason": r.get("reason"),
                        "pnl_sol": r.get("pnl_sol"), "pnl_pct": r.get("pnl_pct"), "max_multiple": r.get("max_multiple"),
                        "would_live_buy": r.get("would_live_buy"), "entry_src": o.get("entry_src"),
                        "closed_ct": _ct(r["ts_ms"])})
        return {"ok": True, "arm": self.arm, "paper_only": True, "enabled": c["enabled"], "active": self.active(),
                "start_ms": self.start_ms, "end_ms": self.end_ms, "start_ct": _ct(self.start_ms), "end_ct": _ct(self.end_ms),
                "targets": [{"handle": h, "wallet": _short(a)} for a, h in c["targets"].items()],
                "size_sol": c["size_sol"], "cost_bps_per_side": c["cost_bps"], "mark_s": c["mark_s"],
                "price_source": ("realistic tracks: on-chain curve/pool at signal + live latency (see realism); legacy: "
                                 "DexScreener public marks (entry fallback: copied wallet's fill)"),
                "exit": "current live scout exit (evaluate_exit + live config); follow_wallet adds exit-on-wallet-sell",
                "signals": sum(1 for r in rows if r.get("kind") == "open"),
                "skips": sum(1 for r in rows if r.get("kind") == "skip"),
                "repeat_buys": sum(1 for r in rows if r.get("kind") == "also_bought"),
                "wallet_sells": sum(1 for r in rows if r.get("kind") == "wallet_sell"),
                "would_live_buy": sum(1 for r in opens.values() if r.get("would_live_buy")),
                "headline": {"basis": "realistic" if _pr.enabled() else "legacy", "track": rec_track if _pr.enabled()
                             else self.tracks[0], **(agg(rec_rows) if _pr.enabled() else legacy[self.tracks[0]]),
                             "gated": real_gated if _pr.enabled() else None},
                "tracks": tracks, "legacy_tracks": legacy, "realistic_tracks": real, "realism": realism,
                "by_handle": by_handle, "legacy_by_handle": legacy_by_handle, "by_checks": gated,
                "open": open_now, "recent": rec[: recent * 2], "stats": dict(self.stats),
                "note": "Paper only: no orders, no signing, no Jupiter, not counted in live caps / loss limit / max open."}


paper = PaperFomoTop()
