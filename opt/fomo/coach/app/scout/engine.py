"""Scout-wallet paper engine: arms A/B/C × fill latency (+2 s, +5 s) × four exit rules, all in parallel.

PAPER ONLY. Its own book and log (data/scout_trades.jsonl) — never touches the Fluffy paper book, the CA fast
lane, mesh, JEV or Jupiter.

Fill realism: an entry reads the bonding curve at detection +2 s and +5 s (own RPC) and simulates the buy with
constant-product curve math incl. the pump fee; every sell is simulated against the curve read at that moment
(after migration: DexScreener mark minus AMM fee + slippage allowance). A per-transaction cost (priority + base
fee) is charged on every simulated buy and sell. "Edge gap" = our fill price vs the scout's own fill price
(arm C: vs the curve price when the call was received).

Exit rules (each simulated separately on the same entry):
  tp2x_trail   — sell 50% at 2×, trail the rest 30% off the high
  min10_check  — at minute 10, exit if below entry
  hard_stop_35 — exit at −35%
  scout_sells  — exit when the scout that triggered the entry sells (arms A/B only)
All rules also close at max hold (60 min). No-loss is not guaranteed: stops fill at the next 2 s mark.
"""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import statistics
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

from app.learning import candidates as learn
from app.pump.curve import buy_tokens_out, curve_price_sol, sell_sol_out
from app.pump.events import event_price_sol
from app.scout import ARM_A, ARM_B, ARM_C, ARMS, RULES
from app.scout.filters import DUMP_K, filters

logger = logging.getLogger("coach.scout.engine")
from app.paths import data_dir  # noqa: E402
DATA_DIR = data_dir()
LOG_PATH = Path(os.getenv("SCOUT_LOG_PATH", str(DATA_DIR / "scout_trades.jsonl")))


def _f(name: str, d: float) -> float:
    try:
        return float(os.getenv(name, str(d)))
    except ValueError:
        return d


def config() -> Dict[str, Any]:
    return {
        "entry_sol": _f("SCOUT_ENTRY_SOL", 0.10),
        "min_scout_buy_sol": _f("SCOUT_MIN_BUY_SOL", 0.5),
        "max_mcap_usd": _f("SCOUT_MAX_MCAP_USD", 20_000),
        "max_age_s": _f("SCOUT_MAX_AGE_S", 900),
        "confirm_window_s": _f("SCOUT_CONFIRM_WINDOW_S", 60),
        "latencies_s": [int(x) for x in os.getenv("SCOUT_LATENCIES", "2,5").split(",") if x.strip()],
        "tp_mult": _f("SCOUT_TP_MULT", 2.0), "tp_frac": _f("SCOUT_TP_FRAC", 0.5),
        "trail_pct": _f("SCOUT_TRAIL_PCT", 0.30), "min10_s": _f("SCOUT_MIN10_S", 600),
        "hard_stop_pct": _f("SCOUT_HARD_STOP_PCT", 0.35), "max_hold_s": _f("SCOUT_MAX_HOLD_S", 3600),
        # v1.14.1 mirror of live: a tp2x_trail runner (TP1 done, not below entry) exits only on the trail
        "runner_max_hold_s": _f("SCOUT_RUNNER_MAX_HOLD_MIN", _f("LIVE_RUNNER_MAX_HOLD_MIN", 0)) * 60,
        "curve_fee_bps": int(_f("SCOUT_CURVE_FEE_BPS", _f("PAPER_CURVE_FEE_BPS", 125))),
        "amm_cost_bps": int(_f("SCOUT_AMM_COST_BPS", 130)),
        "tx_cost_sol": _f("SCOUT_TX_COST_SOL", 0.0005),
        "mark_s": _f("SCOUT_MARK_S", 2.0),
        "target_signals": int(_f("SCOUT_TARGET_SIGNALS", 200)), "target_days": _f("SCOUT_TARGET_DAYS", 14),
        "disclaimer": "Paper only. Fills are simulated from on-chain curve reads at +2 s/+5 s; stops fill at the "
                      "next 2 s mark, so gaps and rugs can exceed any stop. No-loss is not guaranteed.",
    }


@dataclass
class VPos:
    arm: str
    rule: str
    lat_s: int
    mint: str
    signal_id: str
    entry_ms: int
    entry_sol: float
    tokens: int
    entry_price_sol: float
    ref_price_sol: Optional[float]
    scout_wallet: Optional[str]
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    tokens_left: int = 0
    sol_out: float = 0.0
    fees_sol: float = 0.0
    high_price: Optional[float] = None
    last_price: Optional[float] = None
    last_mark_ms: Optional[int] = None
    tp_done: bool = False
    min10_done: bool = False
    scout_sold: bool = False
    status: str = "open"
    exit_reason: Optional[str] = None
    exit_ms: Optional[int] = None
    migrated: bool = False
    fill_offset_ms: Optional[int] = None
    edge_gap_pct: Optional[float] = None
    dump_k: Optional[int] = None
    flags: List[str] = field(default_factory=list)
    fills: List[Dict[str, Any]] = field(default_factory=list)

    def pnl(self) -> Dict[str, Any]:
        mark = (self.tokens_left / 1e6) * (self.last_price or 0) if self.status == "open" else 0.0
        val = self.sol_out + mark - self.fees_sol
        return {"pnl_sol": round(val - self.entry_sol, 6),
                "pnl_pct": round((val / self.entry_sol - 1) * 100, 2) if self.entry_sol else None,
                "multiple_now": round(self.last_price / self.entry_price_sol, 3)
                if self.last_price and self.entry_price_sol else None}


def _log(kind: str, rec: Dict[str, Any]) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a") as fh:
            fh.write(json.dumps({"ts_ms": int(time.time() * 1000), "kind": kind, "paper": True, **rec},
                                default=str) + "\n")
    except Exception as exc:  # noqa: BLE001
        logger.warning("scout log failed: %s", exc)


async def _mcap_usd(spot_sol: float) -> Optional[float]:
    from app.fomo_onchain import market
    if market.sol_usd() is None:
        try:
            await market.marks([])
        except Exception:  # noqa: BLE001
            return None
    px = market.sol_usd()
    return spot_sol * 1e9 * px if spot_sol and px else None


# v1.16: skip reasons caused by a transient gap (price feed, RPC, decode) — the mint is NOT locked out by them;
# the next scout buy of the same mint is evaluated again (at most SCOUT_MAX_RETRIES times).
RETRYABLE = ("no SOL/USD price yet", "age unknown: rpc", "age unknown: no history", "decode incomplete",
             "curve read failed", "quote price unavailable", "non-SOL: no pump.fun curve (or curve read failed)")


def _retryable(reasons: List[str]) -> bool:
    return bool(reasons) and all(any(r.startswith(x) for x in RETRYABLE) for r in reasons)


def _excluded(r: Dict[str, Any]) -> bool:
    """Rows dropped on load (test fixtures, recap posts, …). restart_gap rows are kept but flagged, so the
    position still shows in /api/scout while the report leaves it out of win rate / PnL."""
    ex = r.get("excluded") or (r.get("pos") or {}).get("excluded")
    return bool(ex) and ex != "restart_gap"


class ScoutEngine:
    def __init__(self) -> None:
        self.positions: Dict[str, VPos] = {}
        self.signals: Deque[Dict[str, Any]] = collections.deque(maxlen=500)
        self.decided: Dict[Tuple[str, str], str] = {}          # (arm, mint) -> decision (one per arm+mint)
        self.qualified_a: Dict[str, Dict[str, Any]] = {}       # mint -> qualified scout signal (for arm B)
        self.buys: Dict[str, Deque[Dict[str, Any]]] = collections.defaultdict(lambda: collections.deque(maxlen=50))
        self.mint_info: Dict[str, Dict[str, Any]] = {}         # creator, created_ms, peak, symbol
        self.alerts: Deque[Dict[str, Any]] = collections.deque(maxlen=200)
        self.counts: Dict[str, Dict[str, int]] = {a: collections.defaultdict(int) for a in ARMS}
        self.skip_reasons: Dict[str, Dict[str, int]] = {a: collections.defaultdict(int) for a in ARMS}
        self.scout_buys_seen = 0
        self.confirmer_buys_seen = 0
        self.smug_received = 0
        self.first_ms: Optional[int] = None
        self._tasks: set = set()
        self._mark_task: Optional[asyncio.Task] = None
        self._price_task: Optional[asyncio.Task] = None
        self._scanned: set = set()
        self.loaded = False
        self.retries: Dict[str, int] = collections.defaultdict(int)
        self.stats: Dict[str, int] = collections.defaultdict(int)
        self.excluded_rows: Dict[str, int] = collections.defaultdict(int)
        self.alerts_recap: Deque[Dict[str, Any]] = collections.deque(maxlen=50)

    # ---------------- persistence ----------------
    def load(self) -> None:
        if self.loaded:
            return
        self.loaded = True
        try:
            if not LOG_PATH.exists():
                return
            with LOG_PATH.open() as fh:
                for line in fh:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if _excluded(r):          # v1.16 data hygiene: test fixtures / recap posts / … never count
                        self.excluded_rows[str(r.get("excluded") or (r.get("pos") or {}).get("excluded"))] += 1
                        continue
                    self.first_ms = self.first_ms or r.get("ts_ms")
                    if r.get("kind") == "signal":
                        arm, mint = r.get("arm"), r.get("mint")
                        self.decided[(arm, mint)] = r.get("decision")
                        if arm in self.counts:
                            self.counts[arm]["signals"] += 1
                            self.counts[arm][r.get("decision") or "?"] += 1
                    elif r.get("kind") == "pos":
                        d = {k: v for k, v in (r.get("pos") or {}).items() if k in VPos.__dataclass_fields__}
                        if r.get("excluded") == "restart_gap" and "restart_gap" not in (d.get("flags") or []):
                            d["flags"] = list(d.get("flags") or []) + ["restart_gap"]
                        try:
                            self.positions[d["id"]] = VPos(**d)
                        except Exception:  # noqa: BLE001
                            continue
            for p in self.positions.values():
                if p.status == "open" and "restart_gap" not in p.flags:
                    p.flags.append("restart_gap")
        except Exception as exc:  # noqa: BLE001
            logger.warning("scout load failed: %s", exc)
        try:                                   # v1.18 exit-variants paper add-on (own state file)
            from app.scout.exit_variants import variants
            variants.load()
        except Exception as exc:  # noqa: BLE001
            logger.warning("exit variants load failed: %s", exc)

    def _snap(self, p: VPos) -> None:
        _log("pos", {"pos": asdict(p), "pnl": p.pnl()})

    # ---------------- task helpers ----------------
    def _spawn(self, coro: Any) -> None:
        try:
            t = asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            coro.close()
            return
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    def ensure_mark_loop(self) -> None:
        if self._mark_task and not self._mark_task.done():
            return
        try:
            self._mark_task = asyncio.get_running_loop().create_task(self._mark_loop())
        except RuntimeError:
            pass

    # ---------------- SOL/USD (DexScreener, free; shared cache with fomo-onchain) ----------------
    def start_price_loop(self) -> None:
        from app.scout.source import enabled
        if not enabled():
            return
        if self._price_task is None or self._price_task.done():
            self._price_task = asyncio.get_running_loop().create_task(self._price_loop())

    async def _price_loop(self) -> None:
        from app.fomo_onchain import market
        while True:
            try:
                await market.marks([])
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(30)

    # ---------------- inputs ----------------
    def on_trade(self, w: Any, ev: Dict[str, Any], sig: str, recv_ms: int) -> None:
        """Websocket listener (sync, cheap): records the trade and spawns arm logic.

        v1.16: `ev` is either a SOL-quoted pump TradeEvent decoded from logs, or a wallet trade resolved from the
        full transaction (router / multi-hop / PumpSwap / non-SOL quote) carrying `price_sol`, `spot_sol`,
        `quote_is_sol`, `venue` and `decode_path='tx'`."""
        from app.fomo_onchain import market
        mint = ev["mint"]
        sol = (ev.get("sol_amount") or 0) / 1e9
        fill_px = ev.get("price_sol") or ((sol / (ev["token_amount"] / 1e6)) if ev.get("token_amount") and sol else None)
        spot = event_price_sol(ev) or ev.get("spot_sol")
        sol_usd = market.sol_usd()
        mcap_usd = spot * 1e9 * sol_usd if spot and sol_usd else None
        try:                                    # v1.34 kol_confluence (PAPER ONLY): every watched role, before routing
            from app.paper_kol_confluence import paper as _kc
            _kc.tracker(w.wallet, w.label, w.role, bool(ev["is_buy"]), mint, price_sol=fill_px, sol=sol, sig=sig,
                        recv_ms=recv_ms, venue=ev.get("venue"))
        except Exception:  # noqa: BLE001
            self.stats["kol_confluence_errors"] += 1
        if w.role == "cryptohub":               # v1.34 paper/log only: never a scout trigger, confirmer or live input
            self.stats["cryptohub_buys" if ev["is_buy"] else "cryptohub_sells"] += 1
            return
        if w.role in ("paper_v3", "watch"):     # v1.29 paper_v3_new arm / watch-only: never a scout trigger or confirmer
            try:
                from app.paper_v3 import paper as _v3
                _v3.on_scout_trade(w.wallet, w.label, bool(ev["is_buy"]), mint, price_sol=fill_px, sol=sol, sig=sig,
                                   recv_ms=recv_ms, venue=ev.get("venue"))
            except Exception:  # noqa: BLE001
                self.stats["paper_v3_errors"] += 1
            if w.role == "paper_v3":            # v1.32 paper_top4 (PAPER ONLY): same events, 4 best paper_v3 wallets
                try:
                    from app.paper_top4 import paper as _t4
                    _t4.on_scout_trade(w.wallet, w.label, bool(ev["is_buy"]), mint, price_sol=fill_px, sol=sol,
                                       sig=sig, recv_ms=recv_ms, venue=ev.get("venue"))
                except Exception:  # noqa: BLE001
                    self.stats["paper_top4_errors"] += 1
            return
        if w.role == "scout":                   # v1.36 promoted paper_v3 wallets keep feeding paper_v3/top4 (PAPER ONLY)
            try:
                from app.paper_v3 import cfg as _v3cfg
                if w.wallet in _v3cfg()["targets"]:
                    from app.paper_v3 import paper as _v3
                    from app.paper_top4 import paper as _t4
                    for _pp in (_v3, _t4):
                        _pp.on_scout_trade(w.wallet, w.label, bool(ev["is_buy"]), mint, price_sol=fill_px, sol=sol,
                                           sig=sig, recv_ms=recv_ms, venue=ev.get("venue"))
            except Exception:  # noqa: BLE001
                self.stats["paper_v3_errors"] += 1
        info = self.mint_info.setdefault(mint, {})
        if ev.get("creator"):
            info["creator"] = ev["creator"]
            filters.note_token(ev["creator"], mint)
        rec = {"wallet": w.wallet, "label": w.label, "role": w.role, "ms": recv_ms, "sol": sol, "price": fill_px,
               "mcap_usd": mcap_usd, "spot": spot, "is_buy": ev["is_buy"], "sig": sig,
               "complete": ev.get("real_token_reserves", 1) == 0,
               "quote_mint": ev.get("quote_mint"), "quote_is_sol": ev.get("quote_is_sol", True),
               "venue": ev.get("venue") or ("pump_curve" if ev.get("kind") == "pump_trade" else None),
               "decode_path": ev.get("decode_path") or "logs", "ix_name": ev.get("ix_name"),
               "event_version": ev.get("event_version"), "spot_from_fill": bool(ev.get("spot_from_fill"))}
        if ev["is_buy"]:                       # v1.22 momentum_paper cluster input (PAPER ONLY, sync + cheap)
            try:
                from app.paper_momentum import paper as _mom
                _mom.on_wallet_buy(w.wallet, mint, source="scout", label=w.label, ts_ms=recv_ms)
            except Exception:  # noqa: BLE001
                pass
        if ev["is_buy"] and w.role in ("scout", "confirmer"):   # v1.39b live confluence gate-skip count
            try:
                from app.live.engine import engine as live_engine
                live_engine.note_tracked_buy(w.wallet, mint, recv_ms)
            except Exception:  # noqa: BLE001
                pass
        if not ev["is_buy"]:
            try:
                from app.live.engine import engine as live_engine
                live_engine.note_trader_sell(w.wallet, mint)
            except Exception:  # noqa: BLE001
                pass
            for p in self.positions.values():
                if p.status == "open" and p.mint == mint and p.scout_wallet == w.wallet and p.rule == "scout_sells":
                    p.scout_sold = True
                    self._spawn(self.mark_mints([mint]))
            try:                               # v1.18 exit-variants paper add-on: trader_sells pre-2x rule
                from app.scout.exit_variants import variants
                variants.on_scout_sell(w.wallet, mint)
            except Exception:  # noqa: BLE001
                pass
            try:                               # v1.37 realistic scout positions: live trader-sell rule
                from app.scout.real import real as _real
                _real.on_trader_sell(w.wallet, mint)
            except Exception:  # noqa: BLE001
                pass
            return
        if sol <= 0 and (ev.get("token_amount") or 0) > 0:
            # v1.16: a buy we could not size (should not happen after the tx fallback). Never lock the mint on it.
            self.stats["decode_incomplete"] += 1
            return
        self.buys[mint].append(rec)
        if w.role == "scout":
            self.scout_buys_seen += 1
            if (ARM_A, mint) not in self.decided:
                self.decided[(ARM_A, mint)] = "pending"
                self._spawn(self.signal_a(w, ev, rec, recv_ms))
        else:
            self.confirmer_buys_seen += 1
        self._check_b(mint, rec)

    def _check_b(self, mint: str, rec: Dict[str, Any]) -> None:
        q = self.qualified_a.get(mint)
        if not q or (ARM_B, mint) in self.decided:
            return
        win = config()["confirm_window_s"] * 1000
        others = [b for b in self.buys[mint] if b["wallet"] != q["scout_wallet"] and abs(b["ms"] - q["scout_ms"]) <= win]
        if not others:
            return
        conf = others[0]
        self.decided[(ARM_B, mint)] = "enter"
        detect = max(conf["ms"], q["detect_ms"])
        sig = self._record_signal(ARM_B, mint, "enter", [f"{q['scout_label']} + {conf['label']} within "
                                                         f"{abs(conf['ms'] - q['scout_ms']) / 1000:.0f}s"],
                                  scout=q["scout_label"], scout_wallet=q["scout_wallet"], confirmer=conf["label"],
                                  confirmer_wallet=conf.get("wallet"),
                                  confirmer_role=conf["role"], scout_price=q["scout_price"],
                                  mcap_usd=q.get("mcap_usd"), detect_ms=detect,
                                  scout_sol=q.get("scout_sol"), quote_mint=q.get("quote_mint"),
                                  nonsol_gates_passed=q.get("nonsol_gates_passed", False),
                                  _features={**(q.get("features") or {}), "confirmer": conf["label"],
                                             "confirmer_role": conf["role"], "confirmer_sol": conf.get("sol"),
                                             "confirm_gap_s": round(abs(conf["ms"] - q["scout_ms"]) / 1000, 1),
                                             "confirmer_venue": conf.get("venue")})
        self._spawn(self.enter(ARM_B, mint, sig, detect, q["scout_price"], q["scout_wallet"]))

    def note_recap(self, mint: str, reason: str, *, meta: Dict[str, Any]) -> None:
        """v1.16: SmugCalls recap post filtered at ingest. Counted + logged as an excluded candidate, never traded."""
        self.stats["smug_recap_filtered"] += 1
        self.alerts_recap.appendleft({"ts_ms": int(time.time() * 1000), "mint": mint, "reason": reason,
                                      "snippet": str((meta or {}).get("snippet", ""))[:100]})
        learn.log_candidate(arm=ARM_C, source="smugcalls/telegram", mint=mint, decision="skip", reasons=[reason],
                            features={"post_type": "recap"}, excluded="recap_post", track=False)

    def on_smugcall(self, mint: str, *, recv_ms: int, message_ms: Optional[int], meta: Dict[str, Any]) -> Dict[str, Any]:
        self.smug_received += 1
        if (ARM_C, mint) in self.decided:
            return {"queued": False, "reason": "already decided for this mint"}
        self.decided[(ARM_C, mint)] = "pending"
        self._spawn(self.signal_c(mint, recv_ms, message_ms, meta))
        return {"queued": True, "arm": ARM_C}

    # ---------------- v1.17.2 processed-commitment trial: did the triggering tx really land? ----------------
    def _maybe_verify_trigger(self, arm: str, mint: str, sig: Optional[str], live: Optional[str]) -> None:
        """While SCOUT_WS_COMMITMENT=processed, every 'enter' signal's scout tx is re-checked later (publicnode,
        read-only): confirmed / failed on-chain / dropped. Rows → data/processed_trial.jsonl; counts in status."""
        try:
            from app.scout.source import _ws_commitment
            if _ws_commitment() != "processed" or not sig:
                return
        except Exception:  # noqa: BLE001
            return
        self.stats["trial_tracked"] += 1
        self._spawn(self._verify_trigger(arm, mint, sig, live))

    async def _verify_trigger(self, arm: str, mint: str, sig: str, live: Optional[str]) -> str:
        from app.scout.source import source
        verdict, status = "dropped", None
        for wait in (_f("SCOUT_TRIAL_CHECK1_S", 15), _f("SCOUT_TRIAL_CHECK2_S", 25)):
            await asyncio.sleep(wait)
            res = await source.call("getSignatureStatuses", [[sig], {"searchTransactionHistory": False}], bg=True)
            status = ((res or {}).get("value") or [None])[0] if isinstance(res, dict) else None
            if status:
                verdict = "failed_onchain" if status.get("err") else "landed"
                break
        if verdict == "dropped":
            await asyncio.sleep(_f("SCOUT_TRIAL_CHECK3_S", 50))
            tx = await source.call("getTransaction", [sig, {"encoding": "json", "commitment": "confirmed",
                                                            "maxSupportedTransactionVersion": 1}], bg=True, timeout=8.0)
            if tx:
                verdict = "failed_onchain" if ((tx.get("meta") or {}).get("err")) else "landed"
        self.stats[f"trial_{verdict}"] += 1
        bought = False
        if str(live or "").lower() == "buy":                       # a live attempt started — did it fill?
            try:
                from app.live.engine import engine as live_engine
                bought = any(getattr(p, "mint", None) == mint for p in list(live_engine.positions.values()))
            except Exception:  # noqa: BLE001
                bought = True                                      # unknown → count it (conservative)
        if verdict != "landed" and bought:
            self.stats["trial_bad_trigger_live_buys"] += 1
            logger.warning("processed trial: live %s buy on %s was triggered by a %s scout tx", arm, mint, verdict)
        row = {"ts_ms": int(time.time() * 1000), "arm": arm, "mint": mint, "scout_sig": sig, "live": live,
               "live_filled": bought,
               "verdict": verdict, "confirmation": (status or {}).get("confirmationStatus"), "commitment": "processed"}
        try:
            path = Path(os.getenv("SCOUT_TRIAL_LOG", str(DATA_DIR / "processed_trial.jsonl")))
            with path.open("a") as f:
                f.write(json.dumps(row) + "\n")
        except Exception:  # noqa: BLE001
            pass
        return verdict

    def _record_signal(self, arm: str, mint: str, decision: str, reasons: List[str],
                       _features: Optional[Dict[str, Any]] = None, **extra: Any) -> Dict[str, Any]:
        s = {"id": uuid.uuid4().hex[:10], "arm": arm, "mint": mint, "decision": decision, "reasons": reasons,
             "ts_ms": int(time.time() * 1000), **extra}
        self.decided[(arm, mint)] = decision
        self.signals.appendleft(s)
        self.counts[arm]["signals"] += 1
        self.counts[arm][decision] += 1
        if decision == "skip":
            for r in reasons:
                self.skip_reasons[arm][r.split(":")[0].split("(")[0].strip()[:40]] += 1
        self.first_ms = self.first_ms or s["ts_ms"]
        _log("signal", s)
        if decision == "enter":     # v1.14 live router (refuses unless flags + LIVE_ARMS + caps allow)
            try:
                from app.live.engine import engine as live_engine
                s["live"] = live_engine.on_signal(arm, mint, detect_ms=extra.get("detect_ms"),
                                                  trader_wallets=[extra["scout_wallet"]] if extra.get("scout_wallet") else [],
                                                  ref_price_sol=extra.get("scout_price"), meta=extra)["decision"]
            except Exception as exc:  # noqa: BLE001
                logger.warning("live hook error: %s", type(exc).__name__)
            self._maybe_verify_trigger(arm, mint, extra.get("scout_sig"), s.get("live"))
        # v1.16 learning log: every decision, entered or skipped, with its decision-time features
        feats = {k: extra.get(k) for k in ("scout", "scout_wallet", "scout_sol", "mcap_usd", "age_s", "creator",
                                          "confirmer", "message_ms") if extra.get(k) is not None}
        feats.update(_features or {})
        feats.update(learn.sol_usd_features())
        if s.get("live"):
            feats["live_decision"] = s["live"]
        learn.log_candidate(arm=arm, source="scout" if arm != ARM_C else "smugcalls/telegram", mint=mint,
                            decision=decision, reasons=reasons, features=feats,
                            ref_price_sol=extra.get("scout_price"), detect_ms=extra.get("detect_ms"),
                            decision_ms=s["ts_ms"], entered=decision == "enter")
        return s

    # ---------------- arm A ----------------
    async def signal_a(self, w: Any, ev: Dict[str, Any], rec: Dict[str, Any], recv_ms: int) -> None:
        from app.scout.source import source
        cfg = config()
        mint = ev["mint"]
        reasons: List[str] = []
        curve: Optional[Dict[str, Any]] = None
        t_sig0 = time.perf_counter()
        age_task: Optional["asyncio.Future"] = None
        if rec.get("decode_path") == "tx" and rec.get("quote_is_sol") is not False:
            # v1.40: the age lookup overlaps the curve read (it was serial: curve, then age)
            age_task = asyncio.ensure_future(source.token_age(mint))
            age_task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
            # v1.16: router / PumpSwap / multi-hop buy — no reliable curve state in the event: read the curve
            curve = await source.read_curve(mint)
            if curve is None:
                reasons.append(f"not on a pump.fun curve ({rec.get('venue') or 'router'} buy)")
            elif curve.get("complete"):
                rec["complete"] = True
            elif not curve.get("quote_is_sol", True):
                rec["quote_is_sol"], rec["quote_mint"] = False, curve.get("quote_mint")
            else:
                rec["spot"] = curve_price_sol(curve) or rec.get("spot")
                rec["spot_from_fill"] = False
                rec["mcap_usd"] = None
                if curve.get("creator"):
                    self.mint_info.setdefault(mint, {})["creator"] = curve["creator"]
                    ev = {**ev, "creator": ev.get("creator") or curve["creator"]}
        if rec["mcap_usd"] is None and rec.get("spot"):
            rec["mcap_usd"] = await _mcap_usd(rec["spot"])
        if rec["sol"] < cfg["min_scout_buy_sol"]:
            reasons.append(f"scout buy {rec['sol']:.2f} SOL < {cfg['min_scout_buy_sol']}")
        if rec["complete"]:
            reasons.append("migrated: curve complete")
        nonsol_facts: Dict[str, Any] = {}
        if rec.get("quote_is_sol") is False:
            # v1.16.1: non-SOL-quote coin (Andrew approved 2026-10-07 8:32 PM CT for the scout arms). Same scout rule,
            # with mcap in USD from the curve's quote reserves × a verified quote-asset USD price. Fail closed.
            from app.fomo_onchain import market
            from app.live import nonsol
            if curve is None or curve.get("quote_mint") != rec.get("quote_mint"):
                curve = await source.read_curve(mint)
            if curve and not rec.get("quote_mint"):
                rec["quote_mint"] = curve.get("quote_mint")       # event lacked it: take the on-chain curve's
            if curve and curve.get("creator"):
                self.mint_info.setdefault(mint, {})["creator"] = curve["creator"]
                ev = {**ev, "creator": ev.get("creator") or curve["creator"]}
            if market.sol_usd() is None:
                reasons.append("no SOL/USD price yet")
            nr, nonsol_facts = await nonsol.decision_gates(curve, rec.get("quote_mint"), max_mcap_usd=cfg["max_mcap_usd"])
            reasons += [r for r in nr if r not in reasons]
            rec["mcap_usd"] = nonsol_facts.get("mcap_usd")
            if nonsol_facts.get("spot_sol"):
                rec["spot"] = nonsol_facts["spot_sol"]
        elif rec["mcap_usd"] is None:
            reasons.append("no SOL/USD price yet")
        elif rec["mcap_usd"] >= cfg["max_mcap_usd"]:
            reasons.append(f"mcap ${rec['mcap_usd']:,.0f} ≥ ${cfg['max_mcap_usd']:,.0f}")
        if mint in filters.scheduled_mints:
            reasons.append("scheduled-launch channel: " + str(filters.scheduled_mints[mint].get("source")))
        reasons += filters.deployer_flags(ev.get("creator"))
        age_s = None
        age: Dict[str, Any] = {}
        if not reasons:
            # fast path: start the live pre-buy reads (mesh + rug/bundle facts) while the age lookup runs.
            # Read-only; the live engine still awaits and applies every gate before any buy.
            try:
                from app.live.engine import engine as live_engine
                live_engine.prewarm(ARM_A, mint, creator=ev.get("creator"))
            except Exception as exc:  # noqa: BLE001
                logger.debug("live prewarm: %s", type(exc).__name__)
            t_age = time.perf_counter()
            age = await (age_task if age_task is not None else source.token_age(mint))
            rec["age_wait_ms"] = round((time.perf_counter() - t_age) * 1000)
            self.mint_info[mint]["age"] = age
            if age.get("created_s"):
                age_s = time.time() - age["created_s"]
                self.mint_info[mint]["created_ms"] = int(age["created_s"] * 1000)
                if age_s > cfg["max_age_s"]:
                    reasons.append(f"age {age_s / 60:.1f} min > {cfg['max_age_s'] / 60:.0f}")
            elif age.get("older_than_s"):
                reasons.append(f"age > {age['older_than_s'] / 60:.0f} min")
            else:
                reasons.append("age unknown: " + str(age.get("reason") or f">{age.get('tx_count_gt')} txs"))
        decision = "skip" if reasons else "enter"
        feats: Dict[str, Any] = {
            "scout_role": w.role, "trigger_path": rec.get("decode_path"), "venue": rec.get("venue"),
            "quote_mint": rec.get("quote_mint"), "quote_is_sol": rec.get("quote_is_sol"), "ix_name": rec.get("ix_name"),
            "event_version": rec.get("event_version"), "spot_sol": rec.get("spot"),
            "spot_from_fill": rec.get("spot_from_fill"), "scout_fill_price_sol": rec.get("price"),
            "tx_count": age.get("tx_count") or age.get("tx_count_gt"),
            "watched_buys_on_mint": len(self.buys.get(mint) or []),
            "deployer_flags": filters.deployer_flags(ev.get("creator")) or None,
            "retry": self.retries.get(mint, 0),
            "age_wait_ms": rec.get("age_wait_ms"), "signal_build_ms": round((time.perf_counter() - t_sig0) * 1000),
            "recv_to_signal_ms": int(time.time() * 1000) - int(recv_ms),
            **({"nonsol_" + k: v for k, v in nonsol_facts.items() if k in ("quote_symbol", "usd", "mcap_usd", "spot_quote",
                                                                          "route_dev_pct")} if nonsol_facts else {}),
            **learn.curve_features(curve or (ev if ev.get("kind") == "pump_trade" else None)),
            **learn.flow_features(mint),
        }
        feats.update(quote_mint=rec.get("quote_mint"), quote_is_sol=rec.get("quote_is_sol"))   # the signal's, not the curve dict's
        nonsol_ok = rec.get("quote_is_sol") is False and decision == "enter"
        s = self._record_signal(ARM_A, mint, decision, reasons or [f"{w.label} {rec['sol']:.2f} SOL @ "
                                                                  f"${rec['mcap_usd']:,.0f} mcap, age {age_s or 0:.0f}s"
                                                                  + (f" (quote {nonsol_facts.get('quote_symbol')})"
                                                                     if nonsol_ok else "")],
                                _features=feats,
                                scout=w.label, scout_wallet=w.wallet, scout_sol=rec["sol"], scout_price=rec["price"],
                                mcap_usd=rec["mcap_usd"], age_s=age_s, creator=ev.get("creator"), detect_ms=recv_ms,
                                scout_sig=rec["sig"], venue=rec.get("venue"), decode_path=rec.get("decode_path"),
                                quote_mint=rec.get("quote_mint") if rec.get("quote_is_sol") is False else None,
                                nonsol_gates_passed=nonsol_ok)
        if decision == "enter" and age.get("via") == "gtfa":     # v1.40d: would the old pager have said 'unknown'?
            self._spawn(self._legacy_age_tag(mint, s.get("id")))
        if decision != "enter":
            if _retryable(reasons) and self.retries[mint] < int(_f("SCOUT_MAX_RETRIES", 3)):
                self.retries[mint] += 1
                self.decided.pop((ARM_A, mint), None)     # v1.16: transient gap — let the next scout buy re-evaluate
                self.stats["retryable_skips"] += 1
            return
        self.qualified_a[mint] = {"scout_wallet": w.wallet, "scout_label": w.label, "scout_ms": recv_ms,
                                  "detect_ms": int(time.time() * 1000), "scout_price": rec["price"],
                                  "mcap_usd": rec["mcap_usd"], "scout_sol": rec["sol"],
                                  "quote_mint": rec.get("quote_mint") if rec.get("quote_is_sol") is False else None,
                                  "nonsol_gates_passed": nonsol_ok,
                                  "features": {k: feats.get(k) for k in ("venue", "trigger_path", "curve_progress_pct",
                                                                         "real_sol", "tx_count", "spot_sol")}}
        self._spawn(self.enter(ARM_A, mint, s, recv_ms, rec["price"], w.wallet))
        self._check_b(mint, rec)

    # ---------------- arm C ----------------
    async def signal_c(self, mint: str, recv_ms: int, message_ms: Optional[int], meta: Dict[str, Any]) -> None:
        from app.scout.source import source
        c = await source.read_curve(mint)
        reasons: List[str] = []
        if c is None:
            reasons.append("not a pump.fun curve (or RPC miss)")
        elif c.get("complete"):
            reasons.append("migrated: curve complete")
        elif not c.get("quote_is_sol", True):
            reasons.append("non-SOL quote curve")
        if c and c.get("creator"):
            self.mint_info.setdefault(mint, {})["creator"] = c["creator"]
            filters.note_token(c["creator"], mint)
            reasons += filters.deployer_flags(c["creator"])
        if mint in filters.scheduled_mints:
            reasons.append("scheduled-launch channel: " + str(filters.scheduled_mints[mint].get("source")))
        ref = curve_price_sol(c) if c and not reasons else None
        mcap = await _mcap_usd(ref) if ref else None
        if ref and mcap is None:
            reasons.append("no SOL/USD price yet")          # v1.16: no entry without a SOL/USD price
        snippet = str((meta or {}).get("snippet", ""))
        feats = {"post_type": (snippet.strip().splitlines() or [""])[0][:40], **learn.curve_features(c),
                 **learn.flow_features(mint)}
        s = self._record_signal(ARM_C, mint, "skip" if reasons else "enter", reasons or ["SmugCalls CA post"],
                                _features=feats,
                                detect_ms=recv_ms, message_ms=message_ms, mcap_usd=mcap, scout_price=ref,
                                snippet=snippet[:120])
        if not reasons:
            self._spawn(self.enter(ARM_C, mint, s, recv_ms, ref, None))

    async def _legacy_age_tag(self, mint: str, signal_id: Optional[str]) -> None:
        """v1.40d: off the hot path, re-run the old 3-page age pager; tag the realistic entries it could not have aged."""
        from app.pump.solana_pda import bonding_curve_pda
        from app.scout.source import source
        try:
            r = await source._token_age_pager(bonding_curve_pda(mint))
        except Exception as exc:  # noqa: BLE001
            r = {"ok": False, "reason": type(exc).__name__}
        tag = ("age_unknown_legacy_gt3000tx" if r.get("unknown") else
               "age_legacy_older_than_1h" if r.get("older_than_s") else None)
        row = {"ts_ms": int(time.time() * 1000), "mint": mint, "signal_id": signal_id, "tag": tag,
               "legacy": {k: r.get(k) for k in ("ok", "tx_count", "tx_count_gt", "older_than_s", "unknown", "reason")}}
        try:
            with (DATA_DIR / "scout_tags.jsonl").open("a") as fh:
                fh.write(json.dumps(row) + "\n")
        except Exception:  # noqa: BLE001
            pass
        self.stats["legacy_age_checked"] += 1
        if tag:
            self.stats["legacy_age_" + tag] += 1
            try:
                from app.scout.real import real as _real
                _real.tag(mint, tag, signal_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("legacy age tag failed: %s", type(exc).__name__)

    # ---------------- fills ----------------
    async def enter(self, arm: str, mint: str, sig: Dict[str, Any], detect_ms: int, ref_price: Optional[float],
                    scout_wallet: Optional[str]) -> None:
        try:                                   # v1.37 realistic twin (live latency / slippage / rules / fees)
            from app.scout.real import real as _real
            self._spawn(_real.open(arm, mint, sig["id"], detect_ms, ref_price, scout_wallet))
        except Exception as exc:  # noqa: BLE001
            logger.warning("scout realistic open failed: %s", type(exc).__name__)
        await asyncio.gather(*[self._fill(arm, mint, sig, detect_ms, lat, ref_price, scout_wallet)
                               for lat in config()["latencies_s"]])
        if mint not in self._scanned:
            self._scanned.add(mint)
            self._spawn(self.scan_first_buyers(mint))

    async def _fill(self, arm: str, mint: str, sig: Dict[str, Any], detect_ms: int, lat: int,
                    ref_price: Optional[float], scout_wallet: Optional[str]) -> None:
        from app.scout.source import source
        cfg = config()
        wait = (detect_ms + lat * 1000 - time.time() * 1000) / 1000
        if wait > 0:
            await asyncio.sleep(wait)
        c = await source.read_curve(mint)
        if not c or c.get("complete") or not c.get("quote_is_sol", True):
            _log("fill_fail", {"arm": arm, "mint": mint, "lat_s": lat, "signal_id": sig["id"],
                               "reason": ("non-SOL quote curve (paper sim is SOL-curve only; live may still buy)"
                                          if c and not c.get("quote_is_sol", True) and not c.get("complete")
                                          else "migrated/not on curve at fill time" if c else "curve read failed")})
            self.counts[arm][f"fill_fail_{lat}s"] += 1
            return
        lam = int(cfg["entry_sol"] * 1e9)
        toks = buy_tokens_out(c, lam, cfg["curve_fee_bps"])
        if toks <= 0:
            return
        px = cfg["entry_sol"] / (toks / 1e6)
        spot = curve_price_sol(c)
        gap = round((px / ref_price - 1) * 100, 2) if ref_price else None
        info = self.mint_info.setdefault(mint, {})
        info.setdefault("peak", spot)
        info.setdefault("peak_ms", c["read_at_ms"])
        for rule in RULES:
            if rule == "scout_sells" and not scout_wallet:
                continue
            p = VPos(arm=arm, rule=rule, lat_s=lat, mint=mint, signal_id=sig["id"], entry_ms=c["read_at_ms"],
                     entry_sol=cfg["entry_sol"], tokens=toks, tokens_left=toks, entry_price_sol=px,
                     ref_price_sol=ref_price, scout_wallet=scout_wallet, fees_sol=cfg["tx_cost_sol"],
                     high_price=spot, last_price=spot, last_mark_ms=c["read_at_ms"],
                     fill_offset_ms=c["read_at_ms"] - detect_ms, edge_gap_pct=gap)
            self.positions[p.id] = p
            self._snap(p)
        if lat == min(cfg["latencies_s"]):
            try:                               # v1.18 exit-variants paper add-on: same entry, no extra RPC
                from app.scout.exit_variants import variants
                variants.open_signal(arm=arm, mint=mint, signal_id=sig["id"], lat_s=lat, entry_ms=c["read_at_ms"],
                                     entry_price_sol=px, scout_wallet=scout_wallet, chase_pct=gap,
                                     ref_price_sol=ref_price)
            except Exception as exc:  # noqa: BLE001
                logger.warning("exit variants open failed: %s", exc)
        self.counts[arm][f"filled_{lat}s"] += 1
        if arm == ARM_B and lat == min(cfg["latencies_s"]):
            self.alerts.appendleft({"id": sig["id"], "ts_ms": int(time.time() * 1000), "mint": mint,
                                    "scout": sig.get("scout"), "confirmer": sig.get("confirmer"),
                                    "mcap_usd": sig.get("mcap_usd"), "edge_gap_pct": gap,
                                    "entry_price_sol": px})
        self.ensure_mark_loop()

    # ---------------- marks + exits ----------------
    def _open_mints(self) -> set:
        mints = {p.mint for p in self.positions.values() if p.status == "open"}
        try:                                   # v1.18: keep marking while an exit-variant tracker is still open
            from app.scout.exit_variants import variants
            mints |= variants.open_mints()
        except Exception:  # noqa: BLE001
            pass
        try:                                   # v1.37: and while a realistic position is open
            from app.scout.real import real as _real
            mints |= _real.open_mints()
        except Exception:  # noqa: BLE001
            pass
        return mints

    async def _mark_loop(self) -> None:
        while self._open_mints():
            mints = sorted(self._open_mints())
            try:
                await self.mark_mints(mints)
            except Exception as exc:  # noqa: BLE001
                logger.warning("scout mark error: %s", exc)
            filters.save()
            await asyncio.sleep(config()["mark_s"])

    async def mark_mints(self, mints: List[str]) -> None:
        from app.fomo_onchain import market
        from app.scout.source import source
        cfg = config()
        curves = await source.read_curves(mints)
        migrated = [m for m in mints if curves.get(m) is None or (curves.get(m) or {}).get("complete")]
        dex = await market.marks(migrated) if migrated else {}
        sol_usd = market.sol_usd()
        now = int(time.time() * 1000)
        for m in mints:
            c = curves.get(m)
            on_curve = bool(c) and not c.get("complete")
            if on_curve:
                price = curve_price_sol(c)
            else:
                pu = (dex.get(m) or {}).get("price_usd")
                price = pu / sol_usd if pu and sol_usd else None
            if price is None:
                continue
            info = self.mint_info.setdefault(m, {})
            if price > (info.get("peak") or 0):
                info["peak"], info["peak_ms"] = price, now
            filters.note_outcome(creator=info.get("creator"), mint=m, peak_price=info.get("peak") or price,
                                 price=price, peak_ms=info.get("peak_ms") or now, created_ms=info.get("created_ms"),
                                 now_ms=now)
            for p in [p for p in self.positions.values() if p.status == "open" and p.mint == m]:
                p.last_price, p.last_mark_ms = price, now
                p.high_price = max(p.high_price or 0, price)
                if not on_curve and not p.migrated:
                    p.migrated = True
                self._evaluate(p, c if on_curve else None, price, now, cfg)
            try:                               # v1.18 exit-variants paper add-on: same mark, after the paper rules
                from app.scout.exit_variants import variants
                variants.on_mark(m, price, now)
            except Exception as exc:  # noqa: BLE001
                logger.warning("exit variants mark failed: %s", exc)
            try:                               # v1.37 realistic scout positions: same mark
                from app.scout.real import real as _real
                _real.on_mark(m, price, now)
            except Exception as exc:  # noqa: BLE001
                logger.warning("scout realistic mark failed: %s", type(exc).__name__)

    def _sell(self, p: VPos, tokens: int, reason: str, curve: Optional[Dict[str, Any]], price: float,
              cfg: Dict[str, Any]) -> None:
        tokens = min(tokens, p.tokens_left)
        if tokens <= 0:
            return
        if curve:
            sol = sell_sol_out(curve, tokens, cfg["curve_fee_bps"]) / 1e9
            venue = "curve"
        else:
            sol = (tokens / 1e6) * price * (1 - cfg["amm_cost_bps"] / 10_000)
            venue = "amm_mark"
        p.tokens_left -= tokens
        p.sol_out += sol
        p.fees_sol += cfg["tx_cost_sol"]
        p.fills.append({"ts_ms": int(time.time() * 1000), "reason": reason, "tokens": tokens, "sol": round(sol, 6),
                        "venue": venue, "multiple": round(price / p.entry_price_sol, 3)})
        if p.tokens_left <= 0:
            p.status, p.exit_reason, p.exit_ms = "closed", reason, int(time.time() * 1000)
            p.dump_k = filters.dump_wallets_k(p.mint)
        self._snap(p)

    def _evaluate(self, p: VPos, curve: Optional[Dict[str, Any]], price: float, now: int, cfg: Dict[str, Any]) -> None:
        held = (now - p.entry_ms) / 1000
        if p.rule == "tp2x_trail":
            if not p.tp_done and price >= p.entry_price_sol * cfg["tp_mult"]:
                p.tp_done = True
                self._sell(p, int(p.tokens * cfg["tp_frac"]), "tp_2x", curve, price, cfg)
            elif p.tp_done and p.high_price and price <= p.high_price * (1 - cfg["trail_pct"]):
                self._sell(p, p.tokens_left, "trail", curve, price, cfg)
        elif p.rule == "min10_check":
            if not p.min10_done and held >= cfg["min10_s"]:
                p.min10_done = True
                if price < p.entry_price_sol:
                    self._sell(p, p.tokens_left, "below_entry_min10", curve, price, cfg)
        elif p.rule == "hard_stop_35":
            if price <= p.entry_price_sol * (1 - cfg["hard_stop_pct"]):
                self._sell(p, p.tokens_left, "hard_stop", curve, price, cfg)
        elif p.rule == "scout_sells":
            if p.scout_sold:
                self._sell(p, p.tokens_left, "scout_sold", curve, price, cfg)
        runner = p.rule == "tp2x_trail" and p.tp_done and price >= p.entry_price_sol
        if p.status == "open" and runner:
            if cfg.get("runner_max_hold_s") and held >= cfg["runner_max_hold_s"]:
                self._sell(p, p.tokens_left, "runner_max_hold", curve, price, cfg)
            return                                   # runner: only the trail (above) can close it
        if p.status == "open" and held >= cfg["max_hold_s"]:
            self._sell(p, p.tokens_left, "max_hold", curve, price, cfg)

    # ---------------- first-20 buyers (background, free RPC) ----------------
    async def scan_first_buyers(self, mint: str) -> None:
        from app.scout.source import source
        if mint in filters.first_buyers:
            return
        age = self.mint_info.get(mint, {}).get("age") or await source.token_age(mint, max_pages=1)
        sigs = (age or {}).get("first_sigs") or []
        if len(sigs) < 2:
            return
        buyers: List[str] = []
        creator = self.mint_info.get(mint, {}).get("creator")
        for s in sigs[1:]:
            signer = await source.tx_signer(s)
            if signer and signer != creator and signer not in buyers:
                buyers.append(signer)
            if len(buyers) >= 20:
                break
        filters.first_buyers[mint] = buyers
        filters._dirty = True
        k = filters.dump_wallets_k(mint)
        for p in self.positions.values():
            if p.mint == mint:
                p.dump_k = k

    # ---------------- report ----------------
    def report(self) -> Dict[str, Any]:
        cfg = config()
        rows = []
        for arm in ARMS:
            for rule in RULES:
                for lat in cfg["latencies_s"]:
                    allps = [p for p in self.positions.values() if p.arm == arm and p.rule == rule and p.lat_s == lat]
                    if not allps and rule == "scout_sells" and arm == ARM_C:
                        continue
                    # v1.16: positions whose marks were interrupted by a restart are excluded from the stats
                    ps = [p for p in allps if "restart_gap" not in p.flags]
                    closed = [p for p in ps if p.status == "closed"]
                    pnl = [p.pnl()["pnl_pct"] for p in closed]
                    pnl_sol = [p.pnl()["pnl_sol"] for p in closed]
                    gaps = [p.edge_gap_pct for p in ps if p.edge_gap_pct is not None]
                    clean = [p.pnl()["pnl_pct"] for p in closed if (p.dump_k or 0) < DUMP_K]
                    rows.append({
                        "arm": arm, "rule": rule, "lat_s": lat, "entries": len(ps), "open": len(ps) - len(closed),
                        "closed": len(closed),
                        "win_rate": round(sum(1 for x in pnl_sol if x > 0) / len(closed), 3) if closed else None,
                        "avg_pnl_pct": round(statistics.mean(pnl), 2) if pnl else None,
                        "median_pnl_pct": round(statistics.median(pnl), 2) if pnl else None,
                        "avg_pnl_sol": round(statistics.mean(pnl_sol), 5) if pnl_sol else None,
                        "total_pnl_sol": round(sum(pnl_sol), 5),
                        "avg_edge_gap_pct": round(statistics.mean(gaps), 2) if gaps else None,
                        "median_edge_gap_pct": round(statistics.median(gaps), 2) if gaps else None,
                        "avg_pnl_pct_excl_dump_k": round(statistics.mean(clean), 2) if clean else None,
                        "restart_gap_excluded": sum(1 for p in allps if "restart_gap" in p.flags),
                    })
        days = (time.time() * 1000 - self.first_ms) / 86_400_000 if self.first_ms else 0.0
        arms = {a: {**dict(self.counts[a]), "entries_mints": len({p.mint for p in self.positions.values() if p.arm == a}),
                    "progress": f"{len({p.mint for p in self.positions.values() if p.arm == a})}/{cfg['target_signals']} "
                                f"signals · {days:.1f}/{cfg['target_days']:.0f} days",
                    "skip_reasons": dict(self.skip_reasons[a])} for a in ARMS}
        try:                                   # v1.37 realistic headline (legacy rows kept below for comparison)
            from app.scout.real import real as _real
            realistic = _real.report()
        except Exception as exc:  # noqa: BLE001
            realistic = {"error": type(exc).__name__}
        return {"recommend_only": True, "paper_only": True, "config": cfg, "arms": arms,
                "headline": {"basis": "realistic", **realistic}, "legacy_rows_note":
                "rows = legacy per-rule paper (+2 s/+5 s curve fills, isolated rules); headline = realistic",
                "rows": rows,
                "fee_model": "pump curve fee (SCOUT_CURVE_FEE_BPS) inside the curve sim + SCOUT_TX_COST_SOL per tx; "
                             "after migration: mark − SCOUT_AMM_COST_BPS",
                "seen": {"scout_buys": self.scout_buys_seen, "confirmer_buys": self.confirmer_buys_seen,
                         "smugcalls_received": self.smug_received},
                "hygiene": {"excluded_rows_on_load": dict(self.excluded_rows), "stats": dict(self.stats),
                            "restart_gap_note": "positions flagged restart_gap are excluded from win rate / PnL"}}

    def view(self) -> Dict[str, Any]:
        recent = sorted(self.positions.values(), key=lambda p: -p.entry_ms)[:60]
        return {"signals": list(self.signals)[:60],
                "positions": [{**asdict(p), "pnl": p.pnl()} for p in recent],
                "open": sum(1 for p in self.positions.values() if p.status == "open")}


engine = ScoutEngine()
