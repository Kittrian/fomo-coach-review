"""Live auto-trade engine: caps → real Jupiter buy → live exit engine (persisted, resumes on restart).

Buys need JUPITER_EXEC_ENABLED + LIVE_AUTOTRADE_ENABLED + arm in LIVE_ARMS + every cap passing.
Exits for already-open live positions run whenever JUPITER_EXEC_ENABLED is true (signing master switch), even if
LIVE_AUTOTRADE_ENABLED is later turned off, so a position is never orphaned. Nothing here logs key material.
"""
from __future__ import annotations

import asyncio
import re
import collections
import json
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import httpx
from urllib.parse import urlsplit

from app.live import fastfill
from app.live import jupiter
from app.live import nonsol
from app.live import pumpdirect
from app.live import bench as scout_bench
from app.live import rugcheck
from app.live.watch import watch as acct_watch
from app.live.config import ARMS, TG_ARMS, config


def _csv_env(name: str, default: str = "") -> List[str]:
    return [x.strip() for x in os.getenv(name, default).split(",") if x.strip()]


def scout_paper_only_wallets() -> set:
    """v1.39: scout wallets that stay watched (paper books, learner) but never trigger a LIVE scout buy."""
    return set(_csv_env("LIVE_SCOUT_PAPER_ONLY_WALLETS"))


def budget_reserve_cfg() -> Dict[str, Any]:
    """v1.39: keep LIVE_SCOUT_BUDGET_RESERVE_SOL of the signer's 24h buy budget (LIVE_SIGNER_BUDGET_SOL, mirrors
    SIGNER_MAX_BUY_LAMPORTS_24H) for the priority arms. Non-priority arms are refused once their buy would eat into it.
    0 = off. Coach-side only: the signer's own cap is unchanged."""
    def f(n, d):
        try:
            return float(os.getenv(n, str(d)).strip())
        except ValueError:
            return d
    return {"budget": f("LIVE_SIGNER_BUDGET_SOL", 6.0), "reserve": f("LIVE_SCOUT_BUDGET_RESERVE_SOL", 0.0),
            "arms": set(_csv_env("LIVE_BUDGET_PRIORITY_ARMS", "scout_solo,scout_confirmed"))}


_SPEND_CACHE: Dict[str, Any] = {"key": None, "ev": []}


def buy_spend_24h(ledger_path: str, now_ms: Optional[int] = None) -> float:
    """SOL of live buys in the last 24h from the ledger (landed `buy` + `buy_failed`; conservative vs the signer)."""
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    try:
        st = os.stat(ledger_path)
        key = (ledger_path, st.st_mtime_ns, st.st_size)
    except OSError:
        return 0.0
    if _SPEND_CACHE["key"] != key:
        ev = []
        try:
            with open(ledger_path, encoding="utf-8") as fh:
                for line in fh:
                    if '"buy' not in line:
                        continue
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    k = r.get("kind")
                    if k == "buy":
                        sol = (r.get("spent_lamports") or 0) / 1e9 or float(r.get("size_sol") or 0)
                    elif k == "buy_failed":
                        sol = float(r.get("size_sol") or 0)
                    else:
                        continue
                    ev.append((int(r.get("ts_ms") or 0), sol))
        except OSError:
            ev = []
        _SPEND_CACHE.update(key=key, ev=ev)
    return round(sum(s for t, s in _SPEND_CACHE["ev"] if now - t <= 86400_000), 6)
from app.pump import rpc
from app.pump.curve import curve_price_sol, decode_curve
from app.pump.solana_pda import PUMP_PROGRAM_ID, bonding_curve_pda

logger = logging.getLogger("coach.live")
WSOL = jupiter.WSOL


@dataclass
class LivePos:
    mint: str
    arm: str
    size_sol: float
    spent_lamports: int
    tokens_initial: int
    tokens_left: int
    entry_price_sol: float               # SOL per whole token (actual fill)
    entry_ms: int
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    decimals: int = 6
    buy_sig: Optional[str] = None
    router: Optional[str] = None
    trader_wallets: List[str] = field(default_factory=list)
    trader_sold: bool = False
    high_price_sol: Optional[float] = None
    last_price_sol: Optional[float] = None
    last_mark_ms: Optional[int] = None
    mark_source: Optional[str] = None
    migrated: bool = False
    tp_done: bool = False
    min10_checked: bool = False
    status: str = "open"
    exit_reason: Optional[str] = None
    exit_pending: Optional[str] = None
    sell_failures: int = 0
    last_sell_try_ms: int = 0
    received_lamports: int = 0
    sells: List[Dict[str, Any]] = field(default_factory=list)
    flags: List[str] = field(default_factory=list)
    closed_ms: Optional[int] = None
    meta: Dict[str, Any] = field(default_factory=dict)
    # v1.15
    ladder_done: List[str] = field(default_factory=list)       # TP levels already sold (ladder / tp_2x)
    mark_src_ms: Optional[int] = None                           # source timestamp of the last stream price
    quote_fail_since_ms: Optional[int] = None                   # stale mark + Jupiter quote failing since
    ambiguous_sell: Optional[Dict[str, Any]] = None             # partial TP sent but outcome unknown

    def cost_basis(self, tokens: int) -> float:
        return self.spent_lamports * tokens / self.tokens_initial if self.tokens_initial else 0.0

    def pnl(self) -> Dict[str, Any]:
        held_val = (self.last_price_sol or 0) * self.tokens_left / 10 ** self.decimals * 1e9
        realized = self.received_lamports - self.cost_basis(self.tokens_initial - self.tokens_left)
        unreal = held_val - self.cost_basis(self.tokens_left) if self.tokens_left else 0.0
        tot = realized + unreal
        return {"realized_sol": round(realized / 1e9, 6), "unrealized_sol": round(unreal / 1e9, 6),
                "pnl_sol": round(tot / 1e9, 6), "pnl_pct": round(tot / self.spent_lamports * 100, 2) if self.spent_lamports else None,
                "multiple_now": round(self.last_price_sol / self.entry_price_sol, 3)
                if self.last_price_sol and self.entry_price_sol else None}


def is_partial(reason: str) -> bool:
    """TP sells (tp_2x / ladder tp1_2x, tp2_3x, …) are partials; everything else is a full exit."""
    return str(reason).startswith("tp")


def tp_levels(cfg: Dict[str, Any], rules: List[str]) -> List[Tuple[float, Optional[float], str]]:
    """[(multiple, fraction_of_original or None, label)]. ladder → LIVE_TG_LADDER; tp2x_trail → one level."""
    if "ladder" in rules:
        return list(cfg.get("ladder") or [(2.0, 0.5, "tp1_2x"), (3.0, 0.25, "tp2_3x"), (5.0, 0.125, "tp3_5x")])
    if "tp2x_trail" in rules:
        return [(cfg["tp_mult"], None, "tp_2x")]
    return []


def trail_armed(p: "LivePos", cfg: Dict[str, Any], rules: Optional[List[str]] = None) -> bool:
    """Trail is armed once TP1 sold, or once the high reached the first TP multiple (e.g. TP sell still retrying)."""
    lv = tp_levels(cfg, rules if rules is not None else ["tp2x_trail"])
    first = lv[0][0] if lv else cfg["tp_mult"]
    e = p.entry_price_sol
    return bool(p.tp_done or (e and p.high_price_sol and p.high_price_sol >= e * first))


def is_runner(p: "LivePos", price: Optional[float], cfg: Dict[str, Any], rules: List[str]) -> bool:
    """v1.14.1 runner: trail armed AND not below entry. Runners exit only on the trailing stop (+ ladder TPs)."""
    if not tp_levels(cfg, rules) or not trail_armed(p, cfg, rules):
        return False
    px = price if price else p.last_price_sol
    return not (px and p.entry_price_sol and px < p.entry_price_sol)


def next_tp(p: "LivePos", price: float, cfg: Dict[str, Any], rules: List[str]) -> Optional[Tuple[str, float, str]]:
    """The next un-sold TP level reached by `price` → ("sell_frac", fraction_of_tokens_left, label)."""
    e = p.entry_price_sol
    if not e or not price or p.tokens_left <= 0:
        return None
    for i, (mult, frac, label) in enumerate(tp_levels(cfg, rules)):
        # legacy (pre-v1.15) rows: tp_done=True with no ladder_done → the first level (the 2x partial) was sold
        if label in p.ladder_done or (p.tp_done and (label == "tp_2x" or (i == 0 and not p.ladder_done))):
            continue
        if price < e * mult:
            return None                                   # levels ascend: the next one isn't reached yet
        if frac is None:                                  # tp2x_trail: fraction of what is left
            return ("sell_frac", cfg["tp_frac"], label)
        want = p.tokens_initial * frac                    # ladder: fraction of the ORIGINAL position
        return ("sell_frac", round(min(1.0, want / p.tokens_left), 6), label)
    return None


def night_paused(arm: str, cfg: Dict[str, Any], now_ms: Optional[int] = None) -> bool:
    """v1.33: no NEW entries in [start_h, end_h) local (CT) for non-exempt arms. Exits are never affected."""
    if not cfg.get("night_pause_enabled") or arm in (cfg.get("night_pause_exempt_arms") or []):
        return False
    from datetime import datetime
    from zoneinfo import ZoneInfo
    ts = (now_ms / 1000) if now_ms else time.time()
    h = datetime.fromtimestamp(ts, ZoneInfo(cfg.get("timezone") or "America/Chicago")).hour
    s, en = int(cfg.get("night_pause_start_h", 22)), int(cfg.get("night_pause_end_h", 6))
    return (h >= s or h < en) if s > en else (s <= h < en)


def trail_for(p: "LivePos", cfg: Dict[str, Any]) -> float:
    """v1.33: Telegram arms keep LIVE_TG_TRAIL_PCT (30%); every other arm uses LIVE_TRAIL_PCT."""
    if p.arm == "kol_confluence":                       # v1.35: this lane only (LIVE_TRAIL_PCT_KOL_CONFLUENCE, 30%)
        return float(cfg.get("kol_confluence_trail_pct", cfg["trail_pct"]))
    return float(cfg.get("tg_trail_pct", cfg["trail_pct"])) if p.arm in TG_ARMS else float(cfg["trail_pct"])


def candle_arm(p: "LivePos", cfg: Dict[str, Any]) -> bool:
    return bool(cfg.get("candle_stop_enabled")) and p.arm in (cfg.get("candle_stop_arms") or []) and p.arm not in TG_ARMS


def candle_line(p: "LivePos", price: float, now_ms: int, cfg: Dict[str, Any]) -> Optional[float]:
    """v1.33: track N-s candles from entry in p.meta["cstop"]; return prev completed candle low (ratcheting) - buf."""
    sec = max(1.0, float(cfg.get("candle_stop_s", 15)))
    b = int(max(0, now_ms - p.entry_ms) / 1000 // sec)
    st = p.meta.setdefault("cstop", {})
    if st.get("b") != b:
        if st.get("lo") is not None:
            st["prev"] = max(st.get("prev") or 0.0, st["lo"])
        st["b"], st["lo"] = b, price
    else:
        st["lo"] = min(st["lo"], price)
    return st["prev"] * (1 - float(cfg.get("candle_stop_buf_pct", 0.03))) if st.get("prev") else None


def trader_sell_honored(p: "LivePos", cfg: Dict[str, Any]) -> bool:
    """v1.33: on candle-stop arms follow the trader's sell only if it came within LIVE_TRADER_SELL_WINDOW_S."""
    w = float(cfg.get("trader_sell_window_s") or 0)
    if w <= 0 or not candle_arm(p, cfg):
        return True
    ts = (p.meta or {}).get("trader_sold_ms")
    if ts is None:
        return True
    return (ts - p.entry_ms) / 1000 < w


def evaluate_exit(p: LivePos, price: Optional[float], now_ms: int, cfg: Dict[str, Any], rules: List[str],
                  kill_sell_all: bool = False) -> List[Tuple[str, float, str]]:
    """Pure exit decision → [(kind, fraction_of_tokens_left, reason)]. kind ∈ {sell_frac, sell_all}.

    Runner (TP1 hit / trail armed, not below entry): ONLY the trail closes it (plus ladder TPs, the explicit kill
    switch sell-all and the optional LIVE_RUNNER_MAX_HOLD_MIN). Max hold, the minute-10 rule and trader
    sells are ignored. Everything else (losers / flat) keeps hard stop, min-10, trader sells and max hold.
    `price=None` means no fresh mark (stale): only time-based rules can fire."""
    if p.status != "open" or p.tokens_left <= 0:
        return []
    if kill_sell_all:
        return [("sell_all", 1.0, "kill_switch_sell_all")]
    age_s = (now_ms - p.entry_ms) / 1000
    e = p.entry_price_sol
    if is_runner(p, price, cfg, rules):
        if cfg.get("runner_max_hold_s") and age_s >= cfg["runner_max_hold_s"]:
            return [("sell_all", 1.0, "runner_max_hold")]
        if not price:
            return []                                   # keep monitoring; never time out a runner
        tp = next_tp(p, price, cfg, rules)
        if tp:
            return [tp]
        hi = max(p.high_price_sol or price, price)
        if price <= hi * (1 - trail_for(p, cfg)):
            return [("sell_all", 1.0, "trail")]
        return []
    if "max_hold" in rules and age_s >= cfg["max_hold_s"]:
        return [("sell_all", 1.0, "max_hold")]
    if "trader_sells" in rules and p.trader_sold and trader_sell_honored(p, cfg):
        return [("sell_all", 1.0, "trader_sold")]
    if not price:
        return []
    if not e or e <= 0:
        return []
    if "hard_stop" in rules and price <= e * (1 - cfg["hard_stop_pct"]):
        return [("sell_all", 1.0, "hard_stop")]
    if candle_arm(p, cfg) and not p.tp_done and not trail_armed(p, cfg, rules):
        line = candle_line(p, price, now_ms, cfg)            # v1.33 pre-2x candle stop (35% hard stop above stays)
        if line and price <= line:
            p.meta["candle_stop"] = {"age_s": round(age_s, 1), "mult": round(price / e, 4), "line_mult": round(line / e, 4)}
            return [("sell_all", 1.0, "candle_stop")]
    if (cfg.get("fast_cut_enabled") and not trail_armed(p, cfg, rules) and not p.tp_done
            and age_s <= cfg.get("fast_cut_window_s", 30) and price <= e * (1 - cfg.get("fast_cut_pct", 0.15))):
        # v1.21 fast cut (pre-2x only; the 35% hard stop and the trail are unchanged)
        p.meta["fast_cut"] = {"age_s": round(age_s, 1), "mult": round(price / e, 4), "pct": cfg.get("fast_cut_pct")}
        return [("sell_all", 1.0, "fast_cut")]
    if "min10_check" in rules and not p.min10_checked and age_s >= cfg["min10_s"]:
        p.min10_checked = True
        if price < e:
            return [("sell_all", 1.0, "below_entry_min10")]
    ft = fast_tp(p, price, age_s, cfg)
    if ft:
        return [ft]
    if tp_levels(cfg, rules):
        tp = next_tp(p, price, cfg, rules)
        if tp:
            return [tp]
        if p.tp_done and p.high_price_sol and price <= p.high_price_sol * (1 - trail_for(p, cfg)):
            return [("sell_all", 1.0, "trail")]
    return []


def fast_tp(p: "LivePos", price: float, age_s: float, cfg: Dict[str, Any]) -> Optional[Tuple[str, float, str]]:
    """v1.17 optional early partial take-profit on a fast pump (LIVE_FAST_TP_ENABLED, default OFF). It never arms
    the trail and never sets tp_done, so the 30% trail and the TP ladder behave exactly as coded."""
    if not cfg.get("fast_tp_enabled") or not p.entry_price_sol or p.tokens_left <= 0:
        return None
    label = f"tpfast_{cfg['fast_tp_mult']:g}x"
    if label in p.ladder_done or age_s > cfg["fast_tp_window_s"] or price < p.entry_price_sol * cfg["fast_tp_mult"]:
        return None
    return ("sell_frac", cfg["fast_tp_frac"], label)


def gap_down(prev: Optional[float], price: Optional[float], high: Optional[float], entry: Optional[float],
             reason: str, cfg: Dict[str, Any], trail_pct: Optional[float] = None) -> bool:
    """v1.17: the trail / hard-stop line was crossed in ONE price update and the new price is already
    LIVE_GAP_PCT (default 10%) beyond the line → sell at once with the capped sell slippage (no stepping)."""
    if not prev or not price:
        return False
    if reason == "trail" and high:
        line = high * (1 - (cfg["trail_pct"] if trail_pct is None else trail_pct))
    elif reason == "hard_stop" and entry:
        line = entry * (1 - cfg["hard_stop_pct"])
    else:
        return False
    return prev > line and price <= line * (1 - cfg.get("gap_pct", 0.10))


RUG_WORDS = ("honeypot", "rug")
# v1.15: mesh block reasons that force-close even a runner
FORCE_CLOSE_WORDS = RUG_WORDS + ("mint authority live", "freeze authority live", "sell tax",
                                 "verdict high", "verdict critical", "verdict danger")


_TRANSIENT_SIGN_RE = re.compile(r"(http (429|5\d\d))|too many requests|rpc error (-32429|429)", re.I)


def _transient_sign_error(err: Any) -> bool:
    """v1.37: a signer refusal that only means its RPC was rate-limited / down (nothing evaluated, nothing signed).
    Policy refusals (program / destination / limits) never match."""
    s = str(err or "")
    return "evaluation error" in s and bool(_TRANSIENT_SIGN_RE.search(s))


def _sell_sign_retries() -> int:
    try:
        return max(0, min(6, int(os.getenv("LIVE_SELL_SIGN_RPC_RETRIES", "3"))))
    except ValueError:
        return 3


def _sell_sign_backoff_s() -> float:
    try:
        return max(0.05, float(os.getenv("LIVE_SELL_SIGN_RPC_BACKOFF_S", "0.4")))
    except ValueError:
        return 0.4


def _rug_overlap_enabled() -> bool:
    """v1.37: run the rug/bundle check alongside the unsigned order build; sign only after it passes."""
    return os.getenv("LIVE_RUG_OVERLAP_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}


def _prewarm_max_age_s() -> float:
    try:
        return float(os.getenv("LIVE_PREWARM_MAX_AGE_S", "15"))
    except ValueError:
        return 15.0


def _mesh_overlap_enabled() -> bool:
    """v1.17.2: build+simulate the buy while the mesh check runs; /execute waits for it. Off → old serial order."""
    return os.getenv("LIVE_MESH_OVERLAP_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}


def _sell_skip_balance_enabled() -> bool:
    """v1.17.2: build the sell from the known position size while the balance read runs; the read is awaited in
    the pre-send gate (never sends on an unknown-but-wrong amount). Off → old serial balance-then-order."""
    return os.getenv("LIVE_SELL_SKIP_BALANCE", "true").strip().lower() in {"1", "true", "yes", "on"}


TOKEN_2022_PROGRAM = "REDACTED"
PUMP_MINT_AUTHORITY = "TSLvdd1pWpHVjahSpsvCXUbgwsL3JAcvokwaKt1eokM"
BAD_2022_EXT = {"transferHook", "permanentDelegate", "nonTransferable", "pausableConfig", "defaultAccountState",
                "confidentialTransferMint"}


def _confluence_skip_min() -> int:
    try:
        return max(0, int(os.getenv("LIVE_CONFLUENCE_SKIP_GATE_MIN", "0")))
    except ValueError:
        return 0


def instant_mint_verdict(acct: Optional[Dict[str, Any]]) -> Tuple[Optional[bool], str, Dict[str, Any]]:
    """jsonParsed getAccountInfo value of the MINT → (True ok | False reject | None unknown, why, facts)."""
    try:
        info = acct["data"]["parsed"]["info"]
        owner = acct.get("owner")
    except Exception:  # noqa: BLE001
        return None, "mint account not parseable", {}
    facts = {"owner": owner, "mint_authority": info.get("mintAuthority"), "freeze_authority": info.get("freezeAuthority"),
             "extensions": [e.get("extension") for e in info.get("extensions") or []]}
    if info.get("freezeAuthority"):
        return False, "freeze authority live", facts
    if info.get("mintAuthority") not in (None, PUMP_MINT_AUTHORITY):
        return False, "mint authority live (not pump-standard)", facts
    for e in info.get("extensions") or []:
        name, st = e.get("extension"), e.get("state") or {}
        if name == "transferHook" and not st.get("programId"):
            continue
        if name == "permanentDelegate" and not st.get("delegate"):
            continue
        if name in BAD_2022_EXT:
            return False, f"Token-2022 {name}", facts
        if name == "transferFeeConfig":
            bps = max(int(((st.get(k) or {}).get("transferFeeBasisPoints")) or 0)
                      for k in ("olderTransferFee", "newerTransferFee"))
            if bps > 0:
                return False, f"Token-2022 transfer fee {bps} bps", facts
    return True, "instant checks ok", facts


def _sell_prefetch_s() -> float:
    try:
        return float(os.getenv("LIVE_SELL_PREFETCH_S", "0"))
    except ValueError:
        return 0.0


def _sell_prefetch_max_age() -> float:
    try:
        return float(os.getenv("LIVE_SELL_PREFETCH_MAX_AGE_S", "3.5"))
    except ValueError:
        return 3.5


def _direct_sell_ambig_wait() -> float:
    try:
        return float(os.getenv("LIVE_DIRECT_SELL_AMBIG_WAIT_S", "0.8"))
    except ValueError:
        return 0.8


def _keepwarm_s() -> float:
    try:
        return float(os.getenv("LIVE_KEEPWARM_S", "0"))   # .env sets 15; unset (tests) = off
    except ValueError:
        return 0.0


def _post_buy_check_arms() -> set:
    return {a.strip() for a in os.getenv("LIVE_POST_BUY_CHECK_ARMS", "").split(",") if a.strip()}


def _direct_mesh_max_wait_s() -> float:
    try:
        return max(0.0, float(os.getenv("LIVE_DIRECT_MESH_MAX_WAIT_S", "0")))
    except ValueError:
        return 0.0


def _max_signal_age_ms() -> int:
    """v1.41 late-entry filter (LIVE_MAX_SIGNAL_AGE_MS, 0/unset = off): refuse BEFORE signing when the copied wallet's
    trade reached us more than this long ago (detect_ms = our receipt of that trade)."""
    try:
        return max(0, int(float(os.getenv("LIVE_MAX_SIGNAL_AGE_MS", "0"))))
    except ValueError:
        return 0


def _signal_age_arms() -> set:
    return {a.strip() for a in os.getenv("LIVE_SIGNAL_AGE_ARMS", "scout_solo,scout_confirmed").split(",") if a.strip()}


def _mesh_requote_s() -> float:
    """If the mesh answer arrives later than this after the Jupiter order was built, rebuild the order (fresh price)."""
    try:
        return float(os.getenv("LIVE_MESH_GATE_REQUOTE_S", "4"))
    except ValueError:
        return 4.0


def force_close_signal(signals: Optional[List[Any]]) -> bool:
    return any(w in str(x).lower() for x in (signals or []) for w in FORCE_CLOSE_WORDS)



def _peak_info(p: "LivePos") -> Dict[str, Any]:
    """v1.32: true live peak for the close ledger row. Logging only; any error → {}."""
    try:
        e = p.entry_price_sol
        hi = p.high_price_sol
        return {"high_price_sol": hi, "entry_price_sol": e,
                "peak_multiple": round(hi / e, 4) if hi and e else None,
                "hold_s": round(((p.closed_ms or int(time.time() * 1000)) - p.entry_ms) / 1000, 1),
                "mark_source": p.mark_source}
    except Exception:  # noqa: BLE001
        return {}


def _latency_row(p: "LivePos") -> None:
    """v1.32: one latency_timelines.jsonl row per live buy (the Telegram fast lane that used to write it is off on
    the backend). Same shape as app.pump.fast_lane timelines. Logging only; never raises."""
    try:
        from app.pump import fast_lane
        m = p.meta or {}
        tm = m.get("timing") if isinstance(m.get("timing"), dict) else {}
        det = m.get("detect_ms") or p.entry_ms
        fast_lane._persist({"mint": p.mint, "source": f"live:{p.arm}", "event_id": p.id, "status": "live_buy",
                            "started_ms": det, "stages": {"ingest": det, "live": p.entry_ms},
                            "durations_ms": {k: v for k, v in tm.items() if k.endswith("_ms") and isinstance(v, (int, float))},
                            "info": {"chase_pct": (m.get("chase") or {}).get("premium_pct"),
                                     "edge_gap_pct": m.get("edge_gap_pct"), "path": tm.get("path"),
                                     "detect_to_open_ms": p.entry_ms - det if det else None}})
    except Exception:  # noqa: BLE001
        pass

class LiveEngine:
    def __init__(self) -> None:
        self.positions: Dict[str, LivePos] = {}
        self.bought_mints: Dict[str, Dict[str, Any]] = {}     # mint → first attempt (dedupe forever)
        self.daily: Dict[str, Dict[str, float]] = {}
        self.api_kill: bool = False
        self.inflight: set = set()
        self.decisions: Deque[Dict[str, Any]] = collections.deque(maxlen=200)
        self.alerts: Deque[Dict[str, Any]] = collections.deque(maxlen=200)
        self.stats: Dict[str, int] = collections.defaultdict(int)
        self._locks: Dict[str, asyncio.Lock] = {}
        self._tasks: set = set()
        self._loop: Optional[asyncio.Task] = None
        self._balance: Dict[str, Any] = {}
        self.http: Optional[httpx.AsyncClient] = None
        self.loaded = False
        # v1.15
        self.inflight_sizes: Dict[str, float] = {}               # mint → SOL reserved by an in-flight buy
        self.pending_buys: Dict[str, Dict[str, Any]] = {}       # mint → reserved/sending buy (persisted)
        self.pending_blocks: Dict[str, Dict[str, Any]] = {}     # mesh block that arrived while the buy was inflight
        self.state_error: Optional[str] = None                  # live_state.json unreadable → buys refused
        self.bad_rows: List[Any] = []
        self._save_path: Optional[str] = None                   # recovery path while state_error is set
        self._managing: set = set()
        self._reconcile_task: Optional[asyncio.Task] = None
        self.last_reconcile: Dict[str, Any] = {}
        # v1.17 (in memory only — the ledger / state formats are unchanged)
        self.rug_state: Dict[str, Dict[str, Any]] = {}          # pos id → creator, linked, baseline balances
        self._prev_px: Dict[str, float] = {}                    # pos id → mark before the latest one
        self._reserves: Dict[str, Deque[Tuple[int, int]]] = {}  # pos id → recent (ms, quote reserves)
        self._last_watch_ms: float = 0.0
        self._trade_idx: Dict[str, int] = {}                    # pos id → trades already scanned in the stream
        self._vault_cache: Dict[str, Tuple] = {}                # mint → PumpSwap (base, quote[, sol_quote]) vaults
        self._vault_miss: Dict[str, float] = {}                  # v1.37 mint → last failed pool lookup (s)
        self._pool_amts: Dict[str, Dict[str, int]] = {}
        self._urgent_retry: set = set()                         # pos ids whose pending exit retries at the cap
        # fast-path (staging): read-only pre-buy work started at detection time, reused by _buy
        self._prewarm: Dict[str, Dict[str, Any]] = {}           # mint → {"ts", "arm", "rug": task}
        self.pending_trader_sells: Dict[str, set] = {}          # mint → trader wallets that sold while our buy was inflight
        # v1.21 rolling limit window: {"k": "t"|"p", "ts": ms, "m": mint, "pnl": SOL} (persisted, pruned to 48 h)
        self.window_events: List[Dict[str, Any]] = []
        scout_bench.bench.curve_reader = self._read_curve_raw   # v1.21 bench paper marks (read-only RPC)

    # ---------------- persistence ----------------
    def load(self) -> None:
        """Fail closed (v1.15): if the state file exists but can't be parsed, or any row fails to load, new buys
        are refused and saves go to `<state>.recovery.json` so the original file is never overwritten/deleted."""
        p = Path(config()["state_path"])
        self.loaded = True
        if not p.exists():
            return
        try:
            d = json.loads(p.read_text())
            if not isinstance(d, dict):
                raise ValueError("state is not an object")
        except Exception as exc:  # noqa: BLE001
            self._state_failed(p, f"live state unreadable ({type(exc).__name__})")
            return
        for raw in d.get("positions", []):
            try:
                lp = LivePos(**raw)
            except (TypeError, ValueError):
                self.bad_rows.append(raw)
                continue
            if lp.status != "closed":
                lp.flags = list(dict.fromkeys(lp.flags + ["resumed_after_restart"]))
            self.positions[lp.id] = lp
        self.bought_mints = d.get("bought_mints", {})
        self.daily = d.get("daily", {})
        self.api_kill = bool(d.get("api_kill"))
        self.api_kill_sell_all = bool(d.get("api_kill_sell_all"))
        self.pending_buys = d.get("pending_buys", {}) or {}
        if isinstance(d.get("window_events"), list):
            self.window_events = [e for e in d["window_events"] if isinstance(e, dict) and "ts" in e]
        else:                                                   # first v1.21 start: backfill from existing state
            self.window_events = self._backfill_window_events()
        self.bad_rows += list(d.get("bad_rows") or [])
        if self.bad_rows:
            self._state_failed(p, f"{len(self.bad_rows)} live position row(s) failed to load")

    def _state_failed(self, p: Path, why: str) -> None:
        self.state_error = f"{why} — new buys refused; {p.name} left untouched, saving to {p.name}.recovery.json"
        self._save_path = str(p) + ".recovery.json"
        logger.warning("live state: %s", self.state_error)

    def save(self) -> None:
        cfg = config()
        p = Path(self._save_path or cfg["state_path"])
        p.parent.mkdir(parents=True, exist_ok=True)
        keep = [asdict(x) for x in self.positions.values()
                if x.status != "closed" or (x.closed_ms or 0) > time.time() * 1000 - 7 * 86400_000]
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps({"positions": keep, "bought_mints": self.bought_mints, "daily": self.daily,
                                   "api_kill": self.api_kill, "api_kill_sell_all": self.api_kill_sell_all,
                                   "pending_buys": self.pending_buys, "bad_rows": self.bad_rows,
                                   "window_events": self._pruned_window_events(),
                                   "saved_ms": int(time.time() * 1000)}, default=str))
        tmp.replace(p)

    def _ledger(self, kind: str, **rec: Any) -> None:
        p = Path(config()["ledger_path"])
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts_ms": int(time.time() * 1000), "kind": kind, "live": True, **rec}, default=str) + "\n")

    # ---------------- day / caps ----------------
    def today(self) -> str:
        return datetime.now(ZoneInfo(config()["timezone"])).strftime("%Y-%m-%d")

    def day(self) -> Dict[str, float]:
        return self.daily.setdefault(self.today(), {"trades": 0, "realized_pnl_sol": 0.0})

    def open_positions(self) -> List[LivePos]:
        return [p for p in self.positions.values() if p.status != "closed"]

    def _arm_of(self, mint: str) -> Optional[str]:
        return (self.pending_buys.get(mint) or {}).get("arm") or (self.bought_mints.get(mint) or {}).get("arm")

    def open_count(self, *, arm: Optional[str] = None, exclude: Optional[set] = None) -> int:
        """v1.35c open + inflight positions, optionally only one arm or excluding own-pool arms."""
        ex = exclude or set()
        ok = (lambda a: a == arm) if arm else (lambda a: a not in ex)
        return (sum(1 for p in self.open_positions() if ok(p.arm))
                + sum(1 for m in self.inflight if ok(self._arm_of(m))))

    def _loss_counted(self, arm: Optional[str]) -> bool:
        return not arm or arm not in config().get("loss_limit_exempt_arms", [])

    def _counted(self, arm: Optional[str]) -> bool:
        return not arm or arm not in config().get("trade_cap_uncounted_arms", [])

    # ---------------- v1.21 rolling limit window ----------------
    def _backfill_window_events(self) -> List[Dict[str, Any]]:
        ev: List[Dict[str, Any]] = []
        for m, b in (self.bought_mints or {}).items():          # refunded buys are popped from bought_mints
            if isinstance(b, dict) and b.get("ts_ms") and self._counted(b.get("arm")):
                ev.append({"k": "t", "ts": int(b["ts_ms"]), "m": m})
        for p in self.positions.values():
            if not self._loss_counted(p.arm):                    # v1.35d loss-limit-exempt lane
                continue
            for s in p.sells or []:
                if s.get("ts_ms") and s.get("pnl_sol") is not None:
                    ev.append({"k": "p", "ts": int(s["ts_ms"]), "m": p.mint, "pnl": float(s["pnl_sol"])})
        cut = time.time() * 1000 - 48 * 3600_000
        return sorted([e for e in ev if e["ts"] >= cut], key=lambda e: e["ts"])

    def _pruned_window_events(self) -> List[Dict[str, Any]]:
        cut = time.time() * 1000 - 48 * 3600_000
        self.window_events = [e for e in self.window_events if e.get("ts", 0) >= cut]
        return self.window_events

    def _window_trade(self, mint: str, ts_ms: int) -> None:
        self.window_events.append({"k": "t", "ts": int(ts_ms), "m": mint})

    def _window_untrade(self, mint: str) -> None:
        for i in range(len(self.window_events) - 1, -1, -1):
            e = self.window_events[i]
            if e.get("k") == "t" and e.get("m") == mint:
                del self.window_events[i]
                return

    def limit_window(self, cfg: Optional[Dict[str, Any]] = None, now_ms: Optional[int] = None) -> Dict[str, Any]:
        """Counts the trade cap / profit extension / loss limit use. calendar → today's day() numbers (old behavior);
        rolling24h → trades opened + realized sell P&L with ts in [max(now-24h, LIVE_ROLLING_START), now]."""
        cfg = cfg or config()
        if cfg.get("limit_window") != "rolling24h":
            d = self.day()
            return {"window": "calendar", "trades": int(d.get("trades") or 0),
                    "realized_pnl_sol": float(d.get("realized_pnl_sol") or 0.0), "start_ms": None,
                    "next_slot_frees_ms": None}
        now = int(now_ms if now_ms is not None else time.time() * 1000)
        start = max(now - 86400_000, int(cfg.get("rolling_start_ms") or 0))
        tr = [e["ts"] for e in self.window_events if e.get("k") == "t" and start <= e["ts"] <= now]
        pnl = sum(float(e.get("pnl") or 0.0) for e in self.window_events if e.get("k") == "p" and start <= e["ts"] <= now)
        return {"window": "rolling24h", "trades": len(tr), "realized_pnl_sol": round(pnl, 6), "start_ms": start,
                "next_slot_frees_ms": (min(tr) + 86400_000) if tr else None}

    def trade_cap(self, cfg: Optional[Dict[str, Any]] = None, d: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
        """v1.19 effective daily trade cap. Base LIVE_MAX_TRADES_PER_DAY; once reached, up to
        LIVE_MAX_TRADES_PER_DAY_PROFIT while today's REALIZED P&L >= 0 (unrealized ignored). Pure (no logging)."""
        cfg = cfg or config()
        d = d if d is not None else self.limit_window(cfg)
        base = int(cfg["max_trades_per_day"])
        ext = int(cfg.get("max_trades_per_day_profit") or base)
        n = int(d.get("trades") or 0)
        pnl = float(d.get("realized_pnl_sol") or 0.0)
        enabled = bool(cfg.get("profit_extend_enabled")) and ext > base
        if not enabled:
            eff, state = base, "off"
            why = ("LIVE_PROFIT_EXTEND_ENABLED=false" if not cfg.get("profit_extend_enabled")
                   else f"LIVE_MAX_TRADES_PER_DAY_PROFIT {ext} <= base {base}")
        elif pnl >= 0:
            if n < base:
                eff, state = base, "eligible"
                why = f"under base cap; realized today {pnl:+.4f} SOL >= 0 → can extend to {ext} at {base}"
            elif n < ext:
                eff, state = ext, "unlocked"
                why = f"realized today {pnl:+.4f} SOL >= 0 → cap extended {base}→{ext}"
            else:
                eff, state = ext, "max_reached"
                why = f"extended cap {ext} reached (realized today {pnl:+.4f} SOL)"
        else:
            eff = base
            state = "blocked" if n >= base else "not_eligible"
            why = f"realized today {pnl:+.4f} SOL < 0 → no extension past {base}"
        out = {"enabled": enabled, "base": base, "profit_cap": ext, "effective": eff, "trades_today": n,
               "display": f"{n}/{eff}", "extension": state, "reason": why, "realized_pnl_today_sol": round(pnl, 6),
               "basis": "realized P&L today (sell fills: SOL out − cost basis; Jupiter swap amounts, same as loss limit)"}
        if d.get("window") == "rolling24h":                      # v1.21
            tz = ZoneInfo(cfg.get("timezone") or "America/Chicago")

            def ct(ms):
                return datetime.fromtimestamp(ms / 1000, tz).strftime("%a %b %-d %-I:%M %p CT") if ms else None
            out.update(window="rolling24h", window_start=ct(d.get("start_ms")), window_start_ms=d.get("start_ms"),
                       next_slot_frees=ct(d.get("next_slot_frees_ms")), next_slot_frees_ms=d.get("next_slot_frees_ms"),
                       basis="rolling 24h window (start = max(now − 24h, LIVE_ROLLING_START)): trades opened + realized "
                             "sell P&L in the window; same numbers the loss limit uses",
                       reason=why.replace("today", "in window"))
        else:
            out["window"] = "calendar"
        return out

    def _log_trade_cap(self, tc: Dict[str, Any]) -> None:
        """One INFO line when the extension state changes at/above the base cap (unlock / block / max)."""
        if tc["trades_today"] < tc["base"] or not tc["enabled"]:
            return
        key = (self.today() if tc.get("window") != "rolling24h" else "rolling24h", tc["extension"])
        if getattr(self, "_trade_cap_logged", None) == key:
            return
        self._trade_cap_logged = key
        logger.info("live trade cap [%s]: profit extension %s — trades %s (base %s, profit cap %s): %s%s",
                    tc.get("window", "calendar"), tc["extension"].upper(), tc["display"], tc["base"], tc["profit_cap"],
                    tc["reason"], f" — next slot frees {tc['next_slot_frees']}" if tc.get("next_slot_frees") else "")

    def unrealized_today_sol(self) -> float:
        return sum(min(0.0, x.pnl()["unrealized_sol"]) for x in self.open_positions() if self._loss_counted(x.arm))

    def killed(self) -> Dict[str, Any]:
        f = os.path.exists(config()["kill_file"])
        return {"active": self.api_kill or f, "api": self.api_kill, "file": f, "file_path": config()["kill_file"],
                "sell_all": bool(self.api_kill_sell_all), "state_error": self.state_error}

    def can_sign(self) -> bool:
        return config()["exec_enabled"] and jupiter.pubkey() is not None

    def is_nonsol(self, p: "LivePos") -> bool:
        return bool((p.meta or {}).get("nonsol")) and nonsol.is_nonsol((p.meta or {}).get("quote_mint"))

    def check_caps(self, arm: str, mint: str, size_override: Optional[float] = None, *,
                   cap_bypass: bool = False) -> Tuple[bool, List[str], float]:
        """cap_bypass (v1.35): kol_confluence STRONG signals only — skips ONLY the trades/day cap; kill switch, dedupe,
        max open, loss limit, night pause, reserve and every pre-buy gate still apply."""
        cfg = config()
        why: List[str] = []
        self._cap_bypass_applied = False
        cap_bypass = bool(cap_bypass) and arm == "kol_confluence"
        if not cfg["exec_enabled"]:
            why.append("JUPITER_EXEC_ENABLED=false")
        if not cfg["autotrade_enabled"]:
            why.append("LIVE_AUTOTRADE_ENABLED=false")
        if arm not in cfg["arms"]:
            why.append(f"arm {arm} not in LIVE_ARMS")
        if self.killed()["active"]:
            why.append("kill switch active")
        if night_paused(arm, cfg):
            why.append(f"v1.33 night pause {cfg['night_pause_start_h']:02d}:00-{cfg['night_pause_end_h']:02d}:00 CT (new entries)")
        if self.state_error:
            why.append("live state file problem — buys refused until fixed (see /health)")
        if mint in self.bought_mints or mint in self.inflight:
            why.append("already bought this mint once (dedupe forever)")
        pools = cfg.get("own_pool_max_open") or {}
        if arm in pools:                                 # v1.35c separate pool for this lane
            n = self.open_count(arm=arm)
            if n >= pools[arm]:
                why.append(f"max open positions for {arm} {pools[arm]}")
        elif self.open_count(exclude=set(pools)) >= cfg["max_open_positions"]:
            why.append(f"max open positions {cfg['max_open_positions']}")
        d = self.limit_window(cfg)                      # v1.21: calendar day (old) or rolling 24h
        rolling = d["window"] == "rolling24h"
        tc = self.trade_cap(cfg, d)                     # v1.19 profit-gated extension (off → base cap only)
        self._log_trade_cap(tc)
        uncounted = arm in cfg.get("trade_cap_uncounted_arms", [])
        if uncounted:
            pass                                          # v1.35c: no trades/day cap for this lane
        elif d["trades"] >= tc["effective"] and arm not in cfg.get("trade_cap_exempt_arms", []) and cap_bypass:
            self._cap_bypass_applied = True
        elif d["trades"] >= tc["effective"] and arm not in cfg.get("trade_cap_exempt_arms", []):
            why.append(f"max trades/day {tc['effective']}" + (" (rolling 24h)" if rolling else "")
                       + (f" (profit extension {tc['extension']}: {tc['reason']})" if tc["enabled"] else "")
                       + (f" — next slot frees {tc['next_slot_frees']}" if rolling and tc.get("next_slot_frees") else ""))
        loss = d["realized_pnl_sol"] + self.unrealized_today_sol()
        if loss <= -cfg["daily_loss_limit_sol"] and arm not in cfg.get("loss_limit_exempt_arms", []):
            why.append(f"daily loss limit hit ({loss:.3f} ≤ -{cfg['daily_loss_limit_sol']} SOL) — "
                       + ("halted until window P&L recovers / losses age out (rolling 24h)" if rolling else "halted for today"))
        size = min(cfg["sizes_sol"].get(arm, 0.0), cfg["max_sol_per_trade"])
        if size_override is not None:                   # v1.16.1 non-SOL scout sizing (already capped)
            size = min(size_override, cfg["max_sol_per_trade"])
        if size <= 0:
            why.append("size 0")
        br = budget_reserve_cfg()                        # v1.39: scouts get the signer budget first
        if br["reserve"] > 0 and arm not in br["arms"] and size > 0:
            used = buy_spend_24h(cfg["ledger_path"])
            if used + size > br["budget"] - br["reserve"]:
                why.append(f"v1.39 signer budget reserved for scouts: 24h buys {used:.2f} + {size:.2f} SOL > "
                           f"{br['budget']:g} - {br['reserve']:g} reserve")
        if not why and jupiter.pubkey() is None:
            why.append("keypair not loadable")
        return (not why), why, size

    # ---------------- entry ----------------
    def on_signal(self, arm: str, mint: str, *, detect_ms: Optional[int] = None, trader_wallets: Optional[List[str]] = None,
                  ref_price_sol: Optional[float] = None, meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Sync + cheap (called from the paper gates). Spawns the real buy only when every check passes."""
        if not self.loaded:
            self.load()
        meta = meta or {}
        qm = meta.get("quote_mint")
        ns = nonsol.is_nonsol(qm)
        pre: List[str] = []
        size_override: Optional[float] = None
        if ns:
            # v1.16.1: non-SOL-quote coin → scout arms only, flag on, whitelisted quote, decision gates passed,
            # size = fraction × the triggering scout's SOL-equivalent buy (capped). Anything missing → refuse.
            ncfg = nonsol.cfg()
            if arm not in nonsol.SCOUT_LIVE_ARMS:
                pre.append(f"non-SOL quote coin — live only for scout arms (not {arm})")
            if not ncfg["enabled"]:
                pre.append("non-SOL quote coin — LIVE_SCOUT_NONSOL_ENABLED=false")
            if not nonsol.allowed(qm):
                pre.append(f"non-SOL quote {nonsol.quote_symbol(qm)} not whitelisted")
            if not meta.get("nonsol_gates_passed"):
                pre.append("non-SOL decision gates not passed")
            size_override, size_note = nonsol.size_for(meta.get("scout_sol"),
                                                       max_sol_per_trade=config()["max_sol_per_trade"])
            if size_override is None:
                pre.append(size_note)
                size_override = 0.0
        wsz = config().get("scout_wallet_sizes") or {}
        if wsz and arm in ("scout_solo", "scout_confirmed"):        # v1.36 per-wallet scout size (e.g. 0.10)
            hits = [wsz[w] for w in (trader_wallets or []) if w in wsz]
            if hits:
                ws = min(hits)
                size_override = ws if size_override is None else min(size_override, ws)
                meta = {**meta, "wallet_size_sol": ws}
        po = scout_paper_only_wallets()                   # v1.39: paper-only scout wallets (still watched)
        if po and arm in ("scout_solo", "scout_confirmed"):
            po_hit = [w for w in (trader_wallets or []) if w in po]
            if po_hit:
                pre.append("v1.39 paper-only scout wallet: " + ", ".join(f"{w[:6]}…" for w in po_hit))
        bench_hit: List[str] = []
        if arm in scout_bench.SCOUT_ARMS and scout_bench.cfg()["enabled"]:   # v1.21 scout benching
            bench_hit = scout_bench.bench.benched_of(list(trader_wallets or []) + [meta.get("confirmer_wallet")])
            if bench_hit:
                pre.append("scout bench: " + ", ".join(f"{w[:6]}…" for w in bench_hit)
                           + f" benched ({scout_bench.cfg()['loss_streak']}+ losing live copies in a row) — paper only")
        bypass = (meta.get("cap_bypass") or None) if arm == "kol_confluence" else None
        ok, why, size = self.check_caps(arm, mint, size_override, cap_bypass=bool(bypass))
        if bypass:
            bypass = {**bypass, "applied": bool(getattr(self, "_cap_bypass_applied", False))}
            meta = {**meta, "cap_bypass": bypass}
        if pre:
            ok, why = False, why + pre
        if bench_hit:
            self.stats["bench_refused"] += 1
            logger.info("live bench skip %s %s: wallets %s benched → paper", arm, mint, ",".join(w[:6] for w in bench_hit))
            scout_bench.bench.paper_open(arm, mint, list(trader_wallets or []) + [meta.get("confirmer_wallet")],
                                         ref_price_sol, meta)
        if arm in TG_ARMS and not (meta or {}).get("tg_gate_passed"):
            ok, why = False, why + ["telegram pre-buy gate not passed (v1.15: fast lane must run tg_gate first)"]
        if ok:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                ok, why = False, ["no running event loop"]
        d = {"ts_ms": int(time.time() * 1000), "arm": arm, "mint": mint, "decision": "buy" if ok else "refused",
             "reasons": why, "size_sol": size, "detect_ms": detect_ms}
        if bypass:
            d["cap_bypass"] = bypass
        if ns:
            d.update(nonsol=True, quote_mint=qm, quote_symbol=nonsol.quote_symbol(qm), scout_sol=meta.get("scout_sol"))
        self.decisions.appendleft(d)
        self.stats["signals"] += 1
        if not ok:
            # annotate a held position when another arm confirms it (e.g. scout_confirmed on a scout_solo buy)
            for p in self.open_positions():
                if p.mint == mint and arm not in p.flags and arm != p.arm:
                    p.flags.append(f"also:{arm}")
                    p.trader_wallets = list(dict.fromkeys(p.trader_wallets + (trader_wallets or [])))
            self.stats["refused"] += 1
            if ns:
                self.stats["nonsol_refused"] += 1      # (no new ledger row kinds: live_trades.jsonl format unchanged)
            logger.info("live refused %s %s: %s", arm, mint, "; ".join(why)[:400])
            try:                                               # v1.24 VPS signer shadow (off unless configured)
                from app.live import signer_shadow
                signer_shadow.on_refused(arm, mint)
            except Exception:  # noqa: BLE001
                pass
            return d
        self.inflight.add(mint)
        self.inflight_sizes[mint] = size
        if self._counted(arm):                          # v1.35c uncounted lanes never use the trades/day cap
            self.day()["trades"] += 1
            self._window_trade(mint, d["ts_ms"])
        self.bought_mints[mint] = {"arm": arm, "ts_ms": d["ts_ms"], "size_sol": size}
        self.pending_buys[mint] = {"arm": arm, "ts_ms": d["ts_ms"], "size_sol": size, "stage": "reserved",
                                   "signature": None, "traders": list(trader_wallets or []),
                                   "ref_price_sol": ref_price_sol,
                                   "meta": {k: (meta or {}).get(k) for k in ("source_group", "symbol", "quote_mint", "scout_sol",
                                                                             "confirmer_wallet", "lane", "cap_bypass",
                                                                             "confluence_n", "cryptohub_wallets")
                                            if k in (meta or {}) and (k != "quote_mint" or ns)}}
        if bypass:
            self._ledger("cap_bypass", arm=arm, mint=mint, **bypass)
            logger.info("live cap bypass %s %s: %s (applied=%s)", arm, mint, bypass.get("reason"), bypass.get("applied"))
        if ns:
            self.pending_buys[mint]["meta"]["nonsol"] = True
            self.stats["nonsol_signals_buy"] += 1
        self.save()
        self._spawn(self._buy(arm, mint, size, detect_ms, trader_wallets or [], ref_price_sol,
                              {**meta, "nonsol": True} if ns else meta))
        return d

    def refuse(self, arm: str, mint: str, reasons: List[str], *, detect_ms: Optional[int] = None) -> Dict[str, Any]:
        """Record a pre-buy gate refusal (v1.15 Telegram gate) with a clear, logged reason. Nothing is reserved."""
        d = {"ts_ms": int(time.time() * 1000), "arm": arm, "mint": mint, "decision": "refused", "reasons": list(reasons),
             "size_sol": 0.0, "detect_ms": detect_ms, "gate": "tg_pre_buy"}
        self.decisions.appendleft(d)
        self.stats["signals"] += 1
        self.stats["refused"] += 1
        self.stats["tg_gate_refused"] += 1
        logger.info("live TG gate skip %s %s: %s", arm, mint, "; ".join(reasons)[:400])
        try:
            self._ledger("tg_buy_refused", arm=arm, mint=mint, reasons=list(reasons)[:8])
        except Exception:  # noqa: BLE001
            pass
        return d

    # ---------------- fast path: prewarm (read-only, never reserves or signs) ----------------
    def prewarm(self, arm: str, mint: str, *, creator: Optional[str] = None) -> bool:
        """Start the mesh enrichment + rug/bundle facts for a likely live candidate NOW (e.g. while the scout engine
        still runs its token-age lookup), so _buy finds them in flight or done. Purely read-only: nothing is reserved,
        no gate is skipped — _buy still awaits both answers and refuses exactly as before. Off: LIVE_PREWARM_ENABLED=false."""
        if os.getenv("LIVE_PREWARM_ENABLED", "true").strip().lower() not in {"1", "true", "yes", "on"}:
            return False
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return False
        if not self.loaded:
            self.load()
        ok, _why, _size = self.check_caps(arm, mint)
        if not ok:
            return False
        old = self._prewarm.get(mint)
        if old and time.time() - old["ts"] < _prewarm_max_age_s():
            return True
        try:
            from app.mesh import schedule_enrichment
            schedule_enrichment(mint, "solana", live=True)
        except Exception:  # noqa: BLE001
            pass
        task = asyncio.ensure_future(rugcheck.checker.prebuy(mint, arm, creator=creator))
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)   # never "exception never retrieved"
        self._prewarm[mint] = {"ts": time.time(), "arm": arm, "rug": task}
        for m in [m for m, v in self._prewarm.items() if time.time() - v["ts"] > 120]:
            self._prewarm.pop(m, None)
        self.stats["prewarm_started"] += 1
        return True

    def _take_prewarm_rug(self, mint: str, arm: str) -> Optional["asyncio.Future"]:
        pre = self._prewarm.pop(mint, None)
        if not pre or time.time() - pre["ts"] > _prewarm_max_age_s():
            return None
        # strictness (Telegram arms fail closed on unknowns) must match the arm that is buying
        if (pre["arm"] in rugcheck.TG_FAIL_CLOSED_ARMS) != (arm in rugcheck.TG_FAIL_CLOSED_ARMS):
            return None
        self.stats["prewarm_used"] += 1
        return pre["rug"]

    async def _mesh_check(self, mint: str) -> Tuple[bool, str]:
        """v1.15: no live buy without a mesh rug/honeypot answer. Down / timeout / stale / blocked → no buy."""
        if os.getenv("LIVE_MESH_GATE_ENABLED", "true").strip().lower() in {"0", "false", "no", "off"}:
            return True, "mesh gate off (LIVE_MESH_GATE_ENABLED=false, FOMOANT sniper mode)"
        try:
            from app.mesh import enrich_with_timeout
            from app.risk_gate import mesh_rule_decision
            mesh = await enrich_with_timeout(mint, "solana", timeout=config()["mesh_timeout_s"], live=True)
            rule = mesh_rule_decision(mesh)
        except Exception as exc:  # noqa: BLE001
            return False, f"mesh check error ({type(exc).__name__}) — no buy"
        if rule.get("blocked"):
            return False, "mesh hard block: " + ", ".join(map(str, (rule.get("reasons") or [])[:3]))
        if rule.get("mesh_status") != "ok":
            return False, f"mesh {rule.get('mesh_status')} — no buy without the rug/honeypot check"
        from app.live.tg_gate import mesh_stale
        if mesh_stale(mesh):
            return False, "mesh data stale (timed out / stale risk source) — no buy"
        return True, "mesh ok"

    async def _client(self) -> httpx.AsyncClient:
        if self.http is None or self.http.is_closed:      # v1.40b: keep TLS warm (cold handshake ~60-70 ms)
            self.http = httpx.AsyncClient(timeout=20.0, limits=httpx.Limits(max_connections=8, max_keepalive_connections=8,
                                                                            keepalive_expiry=300))
        t = self.__dict__.get("_warm_task")
        if (t is None or t.done()) and _keepwarm_s() > 0:
            self.__dict__["_warm_task"] = asyncio.ensure_future(self._keep_warm())
        return self.http

    async def _sell_prefetch_loop(self) -> None:
        """v1.40e: keep a fresh Jupiter sell order (full size, first slippage step) per open position, so an exit trigger
        skips the order round trip. Read-only (nothing signed); used only if amount/slippage match and it is fresh."""
        while _sell_prefetch_s() > 0:
            try:
                cfg = config()
                slip = int((cfg.get("sell_slippage_steps_bps") or [0])[0])
                pf = self.__dict__.setdefault("sell_prefetch", {})
                live_ids = set()
                for p in list(self.open_positions()):
                    live_ids.add(p.id)
                    lock = self._locks.get(p.id)
                    if p.tokens_left <= 0 or (lock is not None and lock.locked()):
                        continue
                    o = await jupiter.order(input_mint=p.mint, output_mint=WSOL, amount=int(p.tokens_left),
                                            slippage_bps=slip,
                                            max_priority_lamports=fastfill.priority_lamports(cfg["max_priority_lamports"]),
                                            client=await self._client())
                    if o.get("ok"):
                        o["_fetched_at"] = time.time()
                        pf[p.id] = {"t": o["_fetched_at"], "order": o, "amount": int(p.tokens_left), "slip": slip}
                        self.stats["sell_prefetch_refresh"] += 1
                for k in [k for k in pf if k not in live_ids]:
                    pf.pop(k, None)
            except Exception:  # noqa: BLE001
                self.stats["sell_prefetch_errors"] += 1
            await asyncio.sleep(_sell_prefetch_s())

    async def _keep_warm(self) -> None:
        """v1.40b: every LIVE_KEEPWARM_S (15) touch the buy-path hosts on the shared client so a buy never pays a cold
        TLS handshake. Errors ignored; URLs never logged (the RPC URL carries the key)."""
        while _keepwarm_s() > 0:
            cl = self.http
            if cl is None or cl.is_closed:
                return
            try:
                from app.live import pumpdirect as _pd
                sender = urlsplit(_pd.cfg()["sender_url"])
                await asyncio.gather(
                    cl.post(_pd._rpc_url(), json={"jsonrpc": "2.0", "id": 1, "method": "getHealth"}, timeout=5.0),
                    cl.get(f"{sender.scheme}://{sender.netloc}/ping", timeout=5.0),
                    cl.head(os.getenv("LIVE_KEEPWARM_JUP_URL", "https://api.jup.ag/"), timeout=5.0),
                    return_exceptions=True)
                self.stats["keepwarm_ticks"] += 1
            except Exception:  # noqa: BLE001
                self.stats["keepwarm_errors"] += 1
            await asyncio.sleep(_keepwarm_s())

    async def _rpc(self, method: str, params: List[Any]) -> Any:
        return await rpc.call(method, params, client=await self._client(), url=os.getenv("LIVE_RPC_URL") or None, timeout=8.0)

    async def sol_balance(self, *, cached_s: float = 0) -> Optional[float]:
        if cached_s and self._balance.get("t", 0) > time.time() - cached_s:
            return self._balance.get("sol")
        pk = jupiter.pubkey()
        if not pk:
            return None
        try:
            v = (await self._rpc("getBalance", [pk, {"commitment": "confirmed"}]) or {}).get("value")
            self._balance = {"sol": v / 1e9 if v is not None else None, "t": time.time()}
            return self._balance["sol"]
        except Exception:  # noqa: BLE001
            return None

    async def _read_curve_raw(self, mint: str) -> Optional[Dict[str, Any]]:
        """Fresh bonding-curve decode (None = no pump curve). Raises on RPC failure (caller refuses)."""
        res = await self._rpc("getAccountInfo", [bonding_curve_pda(mint), {"encoding": "base64", "commitment": "processed"}])
        acc = rpc._decode_account((res or {}).get("value"))
        if not acc or acc.get("owner") != PUMP_PROGRAM_ID:
            return None
        return decode_curve(acc["data"])

    async def _jup_quote(self, input_mint: str, output_mint: str, amount: int) -> Optional[int]:
        """Jupiter quote only (no taker → no transaction). Raw output units or None."""
        from app.pump.paper import _jupiter_quote
        q = await asyncio.wait_for(asyncio.to_thread(_jupiter_quote, input_mint, output_mint, int(amount)), timeout=10)
        return int(q["out_amount"]) if q.get("ok") and q.get("out_amount") else None

    async def token_balance(self, mint: str) -> Tuple[Optional[int], Optional[int]]:
        """(raw amount, decimals) summed over our token accounts for `mint`; (None, None) if RPC failed."""
        pk = jupiter.pubkey()
        try:
            res = await self._rpc("getTokenAccountsByOwner", [pk, {"mint": mint},
                                                              {"encoding": "jsonParsed", "commitment": "confirmed"}])
        except Exception:  # noqa: BLE001
            return None, None
        amt, dec = 0, None
        for a in (res or {}).get("value") or []:
            ta = (((a.get("account") or {}).get("data") or {}).get("parsed") or {}).get("info", {}).get("tokenAmount") or {}
            amt += int(ta.get("amount") or 0)
            dec = ta.get("decimals", dec)
        return amt, dec

    async def _buy(self, arm: str, mint: str, size: float, detect_ms: Optional[int], traders: List[str],
                   ref_price: Optional[float], meta: Dict[str, Any]) -> None:
        cfg = config()
        lam = int(size * 1e9)
        sent = False
        meta = dict(meta or {})
        tm: Dict[str, Any] = {}                                       # stage timings (ms) → position meta
        t_start = time.time()
        bal_task: Optional[asyncio.Future] = None
        mesh_task: Optional[asyncio.Future] = None
        try:
            try:                                                      # v1.43 prefetch: state fetch overlaps the gates
                if not meta.get("nonsol") and pumpdirect.mode_for("buy", arm) == "live":
                    pumpdirect.prefetch(mint, await self._client())
            except Exception:  # noqa: BLE001
                pass
            # FOMOANT 2026-10-19: fast local creator-rug-history block (in-memory, no network; unknown creator → no block)
            if os.getenv("LIVE_CREATOR_RUG_BLOCK", "false").strip().lower() in {"1", "true", "yes", "on"}:
                _cr = (meta or {}).get("creator")
                if _cr and _cr in rugcheck.ruggers(max_age_s=300.0):
                    self.stats["creator_rug_refused"] += 1
                    tm["creator_rug_block"] = True
                    self._refund(mint, f"creator rug history: creator {_cr[:6]}… on local rugger list", arm=arm)
                    return
            # v1.17: rug / bundle checks run concurrently with the mesh check (they only add refusals)
            rug_task = self._take_prewarm_rug(mint, arm)
            tm["prewarmed"] = rug_task is not None
            if rug_task is None:
                rug_task = asyncio.ensure_future(rugcheck.checker.prebuy(mint, arm, creator=(meta or {}).get("creator")))
            # fast path: the SOL balance read runs alongside the gates (its value is only used after they pass)
            post = arm in _post_buy_check_arms()               # v1.39c: checks run with/after the send
            tm["post_buy_checks"] = post
            bal_task = asyncio.ensure_future(self.sol_balance(cached_s=120 if post else 0))
            overlap = _mesh_overlap_enabled()
            tm["mesh_overlap"] = overlap
            conf = self._confluence_wallets(mint)              # v1.39b: 3+ tracked whales → skip the slow mesh gate
            if conf:
                tm["mesh_skipped_confluence"] = len(conf)
                mesh_task = asyncio.ensure_future(self._confluence_instant(mint, arm, conf, tm))
            else:
                mesh_task = asyncio.ensure_future(self._mesh_check(mint))
            mesh_task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

            def _mesh_done() -> None:
                tm.setdefault("mesh_ms", round((time.time() - t_start) * 1000))

            ok_mesh, why_mesh = True, ""
            if not overlap:                                            # old serial order: mesh answer first
                ok_mesh, why_mesh = await mesh_task
                _mesh_done()
            rug_deadline = time.time() + rugcheck.cfg()["timeout_s"] + 1.0     # same budget as before

            async def _await_rug() -> Dict[str, Any]:
                """The rug answer, exactly as before: same deadline, same fail-closed fallback. Cached once."""
                if "_rug" in meta:
                    return meta["_rug"]
                try:
                    r_ = await asyncio.wait_for(asyncio.shield(rug_task), timeout=max(0.0, rug_deadline - time.time()))
                except Exception:  # noqa: BLE001
                    rug_task.cancel()
                    r_ = {"ok": arm not in rugcheck.TG_FAIL_CLOSED_ARMS, "unknown": list(rugcheck.CHECKS),
                          "refuse": [] if arm not in rugcheck.TG_FAIL_CLOSED_ARMS else
                          ["rugcheck: no answer in time — Telegram arm fails closed"], "checks": {}}
                meta["_rug"] = r_
                tm["rug_ms"] = r_.get("elapsed_ms")
                return r_

            dmode_pre = "off" if meta.get("nonsol") else pumpdirect.mode_for("buy", arm)
            # v1.37: only the Jupiter path takes the pre-sign hook; direct / non-SOL buys keep the serial order
            rug_overlap = _rug_overlap_enabled() and dmode_pre != "live" and not meta.get("nonsol") and not post
            if rug_overlap:
                for _ in range(2):
                    await asyncio.sleep(0)                     # let answers that are already in (prewarm / mesh) land
                if rug_task.done():
                    rug_overlap = False                        # nothing to overlap: use the serial order unchanged
            tm["rug_overlap"] = rug_overlap
            rug: Dict[str, Any] = {"ok": True, "unknown": [], "refuse": [], "checks": {}, "_deferred": True}
            if post:
                if rug_task.done():                            # answer already in hand → same refusal, 0 ms
                    rug = await _await_rug()
                else:
                    self._rug_deferred(mint, arm, rug_task, meta)
                    tm["rug_deferred"] = True
            elif not rug_overlap:
                rug = await _await_rug()
            tm["gates_ms"] = round((time.time() - t_start) * 1000)
            if overlap and mesh_task.done():                           # mesh already answered → same precedence as before
                ok_mesh, why_mesh = mesh_task.result()
                _mesh_done()
            if not ok_mesh:
                self.stats["mesh_refused"] += 1
                self._refund(mint, why_mesh, arm=arm, rug=rug)
                return
            if not rug_overlap:
                if not rug.get("ok"):
                    self.stats["rug_refused"] += 1
                    self._refund(mint, "; ".join(rug.get("refuse") or ["rugcheck refused"])[:400], arm=arm, rug=rug)
                    return
                if rug.get("unknown"):
                    self.stats["rug_unknown_pass"] += 1
                    self._learn_stage(arm, mint, "pass", [f"rugcheck unknown (skipped for scout/other arm): "
                                                          f"{', '.join(rug['unknown'])}"], rug)
            if self.killed()["active"] or self.state_error:          # kill may have landed while we waited
                self._refund(mint, "kill switch / state error became active before signing")
                return
            if meta.get("nonsol"):                                     # v1.16.1 fresh non-SOL gates (fail closed)
                from app.scout.engine import config as scout_config
                ok_ns, why_ns, facts = await nonsol.prebuy(mint, meta.get("quote_mint"), size,
                                                           max_mcap_usd=scout_config()["max_mcap_usd"],
                                                           read_curve=self._read_curve_raw, quote=self._jup_quote)
                meta["nonsol_prebuy"] = facts
                if not ok_ns:
                    self.stats["nonsol_prebuy_refused"] += 1
                    self._refund(mint, why_ns, arm=arm, rug=meta.get("_rug"))
                    return
            bal = await bal_task
            bal_task = None
            tm["pre_swap_ms"] = round((time.time() - t_start) * 1000)
            others = sum(v for m, v in self.inflight_sizes.items() if m != mint)   # v1.15 reserve lock
            if bal is None or bal - others - size - cfg["fee_buffer_sol"] < cfg["min_sol_reserve"]:
                if rug_overlap:                                # v1.37: same reason precedence as the serial path
                    r_ = await _await_rug()
                    if overlap and mesh_task.done() and not mesh_task.cancelled() and not mesh_task.result()[0]:
                        _mesh_done()
                        self.stats["mesh_refused"] += 1
                        self._refund(mint, mesh_task.result()[1], arm=arm, rug=r_)
                        return
                    if not r_.get("ok"):
                        self.stats["rug_refused"] += 1
                        self._refund(mint, "; ".join(r_.get("refuse") or ["rugcheck refused"])[:400], arm=arm, rug=r_)
                        return
                self._refund(mint, f"SOL reserve: balance {bal} − inflight {others:.2f} − {size} would go below "
                                   f"{cfg['min_sol_reserve']}", arm=arm, rug=meta.get("_rug"))
                return
            pb = self.pending_buys.setdefault(mint, {"arm": arm, "ts_ms": int(time.time() * 1000), "size_sol": size})

            def _signed(sig: Optional[str]) -> None:                  # persisted BEFORE /execute
                pb.update(stage="sending", signature=sig, sent_ms=int(time.time() * 1000))
                self.save()

            gate_state: Dict[str, Any] = {"calls": 0}

            async def _gate(order_age_s: float, quote: Optional[Dict[str, Any]] = None) -> Tuple[bool, str, bool]:
                """v1.17.2 pre-execute gate: the mesh answer (v1.15 rule unchanged) + a fresh kill/state check.
                Runs after order+sign+simulate, before anything is persisted as sent or sent.
                v1.21: + the chase filter on the quote about to be sent (Jupiter order or direct curve quote)."""
                gate_state["calls"] += 1
                q_key = ((quote or {}).get("in_amount"), (quote or {}).get("out_amount"), (quote or {}).get("path"))
                ch = (None if gate_state.get("chase_pre_key") == q_key      # v1.40: same quote already checked pre-sign
                      else self._chase_check(arm, mint, ref_price, None if meta.get("nonsol") else quote, cfg))
                if ch is not None:
                    tm["chase_premium_pct"] = ch.get("premium_pct")
                    meta["chase"] = ch
                    if not ch["ok"]:
                        gate_state["chase_refused"] = True
                        return False, ch["why"], False
                t_w = time.time()
                mw = _direct_mesh_max_wait_s()
                post_now = arm in _post_buy_check_arms()
                if ((mw > 0 and (quote or {}).get("path") == "pump_direct") or post_now) and not mesh_task.done():
                    try:                                             # v1.39 bounded wait; v1.39c post-buy: no wait
                        g_ok, g_why = await asyncio.wait_for(asyncio.shield(mesh_task), timeout=0 if post_now else mw)
                    except asyncio.TimeoutError:
                        g_ok, g_why = True, ""
                        tm["mesh_deferred"] = True
                        self._mesh_deferred(mint, mesh_task)
                else:
                    g_ok, g_why = await mesh_task
                if not tm.get("mesh_deferred"):
                    _mesh_done()
                tm["mesh_wait_ms"] = tm.get("mesh_wait_ms", 0) + round((time.time() - t_w) * 1000)
                if not g_ok:
                    gate_state["mesh_refused"] = True
                    return False, g_why, False
                if self.killed()["active"] or self.state_error:
                    return False, "kill switch / state error became active before sending", False
                waited = time.time() - t_w                           # requote only if WE made the order wait
                return True, "", bool(waited > 0.05 and order_age_s + waited > _mesh_requote_s())
            _gate.wants_quote = True                                   # type: ignore[attr-defined]

            def _chase_pre(quote: Dict[str, Any]) -> Tuple[bool, str]:
                """v1.40: chase filter on the local quote BEFORE the signer sees the tx (refusal = no budget burn)."""
                cap_ms = _max_signal_age_ms()
                if cap_ms and detect_ms and arm in _signal_age_arms():    # v1.41 late-entry filter (local, ~0 ms)
                    age_ms = int(time.time() * 1000) - int(detect_ms)
                    tm["signal_age_ms"] = age_ms
                    if age_ms > cap_ms:
                        gate_state["chase_refused"] = True
                        self.stats["late_entry_refused_pre_sign"] += 1
                        return False, f"late entry: copied trade seen {age_ms} ms ago (max {cap_ms} ms)"
                ch = self._chase_check(arm, mint, ref_price, None if meta.get("nonsol") else quote, cfg)
                gate_state["chase_pre_key"] = (quote.get("in_amount"), quote.get("out_amount"), quote.get("path"))
                if ch is None:
                    return True, ""
                tm["chase_premium_pct"] = ch.get("premium_pct")
                tm["chase_pre_sign"] = True
                meta["chase"] = ch
                if not ch["ok"]:
                    gate_state["chase_refused"] = True
                    self.stats["chase_refused_pre_sign"] += 1
                    return False, ch["why"]
                return True, ""

            dmode = "off" if meta.get("nonsol") else pumpdirect.mode_for("buy", arm)   # v1.20/v1.36 per-arm trial
            tm["direct_mode"] = dmode

            async def _pre_sign(order_age_s: float) -> Tuple[bool, str, bool]:
                """v1.37: awaited after the unsigned order is built and BEFORE it goes to the signer. No pass → nothing
                is signed (so a refused coin never touches the signer's buy budget) and nothing is sent."""
                t_w = time.time()
                r_ = await _await_rug()
                waited = time.time() - t_w
                tm["rug_wait_ms"] = tm.get("rug_wait_ms", 0) + round(waited * 1000)
                if mesh_task.done() and not mesh_task.cancelled() and not mesh_task.result()[0]:
                    gate_state["mesh_refused"] = True          # mesh already said no → never reaches the signer
                    return False, mesh_task.result()[1], False
                if not r_.get("ok"):
                    gate_state["rug_refused"] = True
                    return False, "; ".join(r_.get("refuse") or ["rugcheck refused"])[:400], False
                if self.killed()["active"] or self.state_error:
                    return False, "kill switch / state error became active before signing", False
                return True, "", bool(waited > 0.05 and order_age_s + waited > _mesh_requote_s())

            async def _do_swap() -> Dict[str, Any]:
                if dmode == "live":
                    jq = None
                    try:                                       # v1.36: never-signed Jupiter quote for comparison
                        jq = asyncio.ensure_future(jupiter.order(input_mint=WSOL, output_mint=mint, amount=lam,
                                                                 slippage_bps=cfg["buy_slippage_bps"],
                                                                 max_priority_lamports=0, client=await self._client()))
                    except Exception:  # noqa: BLE001
                        jq = None
                    d = await pumpdirect.swap(side="buy", mint=mint, amount=lam, client=await self._client(),
                                              on_signed=_signed, pre_execute=_gate, pre_sign_quote=_chase_pre,
                                              **({"ref_price": ref_price}             # v1.45 price bound
                                                 if (ref_price and cfg.get("chase_filter_enabled")
                                                     and arm in (cfg.get("chase_arms") or [])
                                                     and not meta.get("nonsol")) else {}))
                    self._direct_live_log("buy", arm, mint, d, tm)
                    if jq is not None:
                        self._direct_jup_compare(jq, d, arm, mint)
                    if not d.get("fallback"):
                        tm["path"] = "pump_direct"
                        return d
                    self.stats["direct_fallback_buy"] += 1
                    if d.get("presigned_signature"):
                        pb.update(signature=None, stage="direct_refused")   # refused, nothing landed → Jupiter
                t_j = time.time()
                r = await jupiter.swap(input_mint=WSOL, output_mint=mint, amount=lam,
                                       slippage_bps=cfg["buy_slippage_bps"],
                                       max_priority_lamports=fastfill.priority_lamports(cfg["max_priority_lamports"]),
                                       client=await self._client(), sim_rpc_url=cfg["sim_rpc_url"], on_signed=_signed,
                                       pre_execute=_gate, pre_sign_quote=_chase_pre,
                                       **({"pre_sign": _pre_sign} if rug_overlap else {}))
                tm["path"] = "jupiter"
                tm["jupiter_ms"] = round((time.time() - t_j) * 1000)
                return r

            shadow_task: Optional[asyncio.Future] = None
            if dmode == "shadow":                                      # build+simulate only; never signs/sends
                shadow_task = asyncio.ensure_future(pumpdirect.shadow("buy", mint, lam, client=await self._client()))
            t0 = time.time()
            res = await _do_swap()
            if shadow_task is not None:
                self._direct_shadow_finish(shadow_task, res, "buy", mint, arm, round((time.time() - t0) * 1000),
                                           ref_price)
            if res.get("stage") == "requote":                         # mesh was slow → rebuild with a fresh price once
                self.stats["mesh_gate_requotes"] += 1
                tm["requoted"] = True
                res = await _do_swap()
                if res.get("stage") == "requote":
                    res = {**res, "stage": "gate", "error": "order aged twice while waiting for the gate"}
            took = round((time.time() - t0) * 1000)
            tm["swap_ms"] = took
            if rug_overlap and "_rug" not in meta and res.get("stage") in ("order", "sign", "simulate", "gate", "execute"):
                await _await_rug()                              # order failed before the hook: still log the answer
            if rug_overlap and meta.get("_rug") is not None:
                rug = meta["_rug"]
            if res.get("stage") in ("order", "sign", "simulate", "gate"):
                if gate_state.get("chase_refused"):
                    self._refund(mint, res.get("error") or "chase filter", arm=arm, rug=meta.get("_rug"))
                    return
                if mesh_task.done() and not mesh_task.cancelled() and not mesh_task.result()[0]:
                    _mesh_done()                                       # mesh said no → report the mesh reason (v1.15)
                    self.stats["mesh_refused"] += 1
                    if res.get("stage") == "gate":
                        self.stats["mesh_gate_refused"] += 1
                    self._refund(mint, mesh_task.result()[1], arm=arm, rug=meta.get("_rug"))
                    return
                if gate_state.get("rug_refused"):
                    self.stats["rug_refused"] += 1
                    self._refund(mint, "; ".join(rug.get("refuse") or ["rugcheck refused"])[:400], arm=arm, rug=rug)
                    return
                if res.get("stage") == "simulate":
                    self._alert("live_buy_sim_failed", arm=arm, mint=mint, sol=size, error=res.get("error"))
                self._refund(mint, f"buy {res.get('stage')} failed: {res.get('error')}", arm=arm, rug=meta.get("_rug"))
                return
            if mesh_task.done() and not mesh_task.cancelled():
                _mesh_done()
            elif not gate_state["calls"]:
                # defensive: a swap implementation that ignored pre_execute (never the real jupiter.swap)
                logger.warning("live buy %s: swap returned without calling the mesh gate", mint)
            sent = True
            tokens, spent, sig, flags = res.get("total_out", 0), res.get("total_in", 0) or lam, res.get("signature"), []
            pb.update(signature=sig or pb.get("signature"))
            if not res.get("ok"):
                # execute failed/ambiguous: the tx may still have landed → check the chain before giving up
                bal_tok = None
                for wait in (8, 12, 20):
                    await asyncio.sleep(wait)
                    bal_tok, _ = await self.token_balance(mint)
                    if bal_tok:
                        break
                if bal_tok:
                    tokens, spent, flags = bal_tok, lam, ["reconciled_from_chain"]
                elif bal_tok is None and sig:
                    # RPC unreadable and a tx was sent: track it anyway so exits still run (sell re-checks balance)
                    tokens = int((res.get("order") or {}).get("out_amount") or 0)
                    spent, flags = lam, ["unverified_fill"]
                    if not tokens:
                        bal_tok = 0
                if not tokens:
                    self.stats["buy_failed"] += 1
                    self._ledger("buy_failed", arm=arm, mint=mint, size_sol=size, error=res.get("error"),
                                 code=res.get("code"), signature=sig)
                    self._alert("live_buy_failed", arm=arm, mint=mint, sol=size, signature=sig, error=res.get("error"))
                    if bal_tok == 0:
                        pb.update(stage="failed_no_tokens")              # reconcile re-checks once later
                    return
            raw_bal, dec = await self.token_balance(mint)
            dec = dec if dec is not None else 6
            if not tokens and raw_bal:
                tokens, flags = raw_bal, flags + ["tokens_from_chain"]
            if not tokens:
                pb.update(stage="unknown_fill")                          # startup/periodic reconcile picks it up
                self._ledger("buy_unknown_fill", arm=arm, mint=mint, signature=sig)
                self._alert("live_buy_failed", arm=arm, mint=mint, sol=size, signature=sig, error="fill size unknown — check wallet")
                self.start_reconcile(delay_s=30)
                return
            pos = self._open_position(arm, mint, size, int(spent), int(tokens), dec, sig,
                                      (res.get("order") or {}).get("router"), traders, flags,
                                      {"detect_ms": detect_ms, "buy_ms": took, "ref_price_sol": ref_price,
                                       "order": res.get("order"),
                                       "timing": {**tm, "post_fill_ms": round((time.time() - t0) * 1000) - took,
                                                  "detect_to_open_ms": (int(time.time() * 1000) - detect_ms)
                                                  if detect_ms else None},
                                       **{k: meta.get(k) for k in ("source_group", "symbol", "lane", "quote_mint", "nonsol",
                                                                   "scout_sol", "nonsol_prebuy", "confirmer_wallet",
                                                                   "chase", "cap_bypass", "confluence_n",
                                                                   "cryptohub_wallets") if k in meta}})
            self._rug_baseline(pos, meta.get("_rug"))
            self._alert("live_buy", arm=arm, mint=mint, sol=round(spent / 1e9, 4), tokens=tokens, signature=sig,
                        edge_gap_pct=pos.meta.get("edge_gap_pct"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("live buy error %s: %s", mint, type(exc).__name__)
            self._ledger("buy_error", arm=arm, mint=mint, error=type(exc).__name__)
            pb = self.pending_buys.get(mint)
            if pb and pb.get("stage") == "reserved" and not sent:
                self._refund(mint, f"buy error before signing ({type(exc).__name__})", arm=arm)
            elif pb:
                pb.update(stage="error")
                self.start_reconcile(delay_s=30)
        finally:
            if bal_task is not None and not bal_task.done():
                bal_task.cancel()
            if mesh_task is not None and not mesh_task.done():
                mesh_task.cancel()
            self.inflight.discard(mint)
            self.inflight_sizes.pop(mint, None)
            if mint not in {p.mint for p in self.open_positions()}:
                self.pending_trader_sells.pop(mint, None)
            self.save()

    def _open_position(self, arm: str, mint: str, size: float, spent: int, tokens: int, dec: int, sig: Optional[str],
                       router: Optional[str], traders: List[str], flags: List[str], meta: Dict[str, Any]) -> LivePos:
        entry_px = (spent / 1e9) / (tokens / 10 ** dec) if tokens else 0.0
        ref = meta.get("ref_price_sol")
        meta = {**meta, "edge_gap_pct": round((entry_px / ref - 1) * 100, 2) if ref else None}
        if isinstance(meta.get("timing"), dict):                       # v1.20: fill vs scout price next to timings
            meta["timing"] = {**meta["timing"], "fill_vs_scout_pct": meta["edge_gap_pct"]}
        now = int(time.time() * 1000)
        pos = LivePos(mint=mint, arm=arm, size_sol=size, spent_lamports=spent, tokens_initial=tokens,
                      tokens_left=tokens, entry_price_sol=entry_px, entry_ms=now, decimals=dec, buy_sig=sig,
                      router=router, trader_wallets=traders, high_price_sol=entry_px, last_price_sol=entry_px,
                      last_mark_ms=now, mark_source="fill", flags=flags, meta=meta)
        self.positions[pos.id] = pos
        self.pending_buys.pop(mint, None)
        self.stats["buys"] += 1
        self._ledger("buy", **{k: v for k, v in asdict(pos).items() if k not in ("sells",)})
        _latency_row(pos)                                          # v1.32 logging only
        self._start_stream(mint)
        self.ensure_loop()
        sold = self.pending_trader_sells.pop(mint, None)           # trader sold while the buy was in flight
        if sold and set(traders) & sold and not pos.trader_sold:
            pos.trader_sold = True
            pos.meta["trader_sold_ms"] = pos.entry_ms                # v1.33: sold before we opened → age 0
            pos.flags.append("trader_sold_while_inflight")
            self._spawn(self.manage(pos))
        blk = self.pending_blocks.pop(mint, None)                  # v1.15: block that arrived mid-buy
        if blk:
            self.force_exit(mint, f"{blk['reason']}_after_fill", signals=blk.get("signals"))
        self.save()
        return pos

    # ---------------- reconcile (v1.15) ----------------
    def start_reconcile(self, *, delay_s: float = 0.0) -> None:
        if _sell_prefetch_s() > 0 and not self.__dict__.get("_prefetch_task"):   # v1.40e warm sell routes
            try:
                self.__dict__["_prefetch_task"] = asyncio.get_running_loop().create_task(self._sell_prefetch_loop())
            except RuntimeError:
                pass
        if _keepwarm_s() > 0 and not self.__dict__.get("_warm_task"):   # v1.40b2: warm the buy path from startup
            try:
                asyncio.get_running_loop().create_task(self._client())
            except RuntimeError:
                pass
        if self._reconcile_task and not self._reconcile_task.done():
            return
        try:
            self._reconcile_task = asyncio.get_running_loop().create_task(self._reconcile_loop(delay_s))
        except RuntimeError:
            pass

    async def _reconcile_loop(self, delay_s: float) -> None:
        if delay_s:
            await asyncio.sleep(delay_s)
        for _ in range(20):
            try:
                r = await self.reconcile()
            except Exception as exc:  # noqa: BLE001
                logger.warning("live reconcile: %s", type(exc).__name__)
                r = {"unresolved": 1}
            if not r.get("unresolved"):
                return
            await asyncio.sleep(30)

    async def reconcile(self) -> Dict[str, Any]:
        """Pending buys + recent bought mints with NO position record vs on-chain token balances.
        Held tokens → a tracked position (flag reconciled_on_startup) so exits run. Nothing is bought or sold."""
        now = int(time.time() * 1000)
        known = {p.mint for p in self.positions.values()}
        cands: Dict[str, Dict[str, Any]] = {m: r for m, r in self.pending_buys.items() if m not in self.inflight}
        for m, r in self.bought_mints.items():
            if m in known or m in cands or m in self.inflight:
                continue
            if now - int((r or {}).get("ts_ms") or 0) <= 7 * 86400_000:
                cands[m] = r or {}
        out = {"checked": 0, "created": [], "cleared": [], "unresolved": 0, "ts_ms": now}
        for mint, rec in cands.items():
            if mint in self.inflight or any(p.mint == mint for p in self.positions.values()):
                continue
            out["checked"] += 1
            bal, dec = await self.token_balance(mint)
            if bal is None:
                out["unresolved"] += 1
                continue
            if bal > 0:
                size = float(rec.get("size_sol") or 0.0)
                pos = self._open_position(rec.get("arm") or "unknown", mint, size, int(size * 1e9), int(bal),
                                          dec if dec is not None else 6, rec.get("signature"), None,
                                          list(rec.get("traders") or []), ["reconciled_on_startup"],
                                          {"reconciled": True, "ref_price_sol": rec.get("ref_price_sol"),
                                           **(rec.get("meta") or {})})
                self._ledger("buy_reconciled", id=pos.id, arm=pos.arm, mint=mint, tokens=bal, signature=rec.get("signature"))
                self._alert("live_buy", arm=pos.arm, mint=mint, sol=size, tokens=bal, signature=rec.get("signature"),
                            note="reconciled from chain (no position record)")
                out["created"].append(mint)
            elif mint in self.pending_buys:
                stage = self.pending_buys[mint].get("stage")
                age = now - int(self.pending_buys[mint].get("sent_ms") or self.pending_buys[mint].get("ts_ms") or 0)
                if stage == "reserved" or age > 120_000:
                    self.pending_buys.pop(mint, None)
                    self._ledger("pending_buy_cleared", mint=mint, stage=stage, reason="no tokens on-chain")
                    out["cleared"].append(mint)
                else:
                    out["unresolved"] += 1                         # tx may still land; re-check later
        self.last_reconcile = out
        self.save()
        return out

    def _learn_stage(self, arm: str, mint: str, decision: str, reasons: List[str],
                     rug: Optional[Dict[str, Any]] = None) -> None:
        """v1.17: every live pre-buy refusal (and every rug check that passed with an `unknown`) gets a learning-log
        row (stage=live_prebuy) with its skip reason. Never raises."""
        try:
            from app.learning import candidates as learn
            feats: Dict[str, Any] = {}
            if rug:
                feats = {"rug_unknown": rug.get("unknown") or None, "rug_strict": rug.get("strict"),
                         "rug_elapsed_ms": rug.get("elapsed_ms"),
                         "rug_checks": {k: v.get("status") for k, v in (rug.get("checks") or {}).items()},
                         "rug_values": {k: v.get("value") for k, v in (rug.get("checks") or {}).items()
                                        if v.get("value") is not None and k != "rugger"}}
            learn.log_candidate(arm=arm, source="live_prebuy", mint=mint, decision=decision, reasons=reasons,
                                features=feats, entered=decision == "pass", stage="live_prebuy")
        except Exception:  # noqa: BLE001
            pass

    def _refund(self, mint: str, why: str, *, arm: Optional[str] = None, rug: Optional[Dict[str, Any]] = None) -> None:
        """No transaction was sent → release the dedupe + trade-count reservation."""
        arm = arm or (self.pending_buys.get(mint) or {}).get("arm") or (self.bought_mints.get(mint) or {}).get("arm")
        if arm:
            self._learn_stage(arm, mint, "skip", [why], rug)
        self.bought_mints.pop(mint, None)
        self.pending_buys.pop(mint, None)
        self.pending_blocks.pop(mint, None)
        if self._counted(arm):
            self.day()["trades"] = max(0, self.day()["trades"] - 1)
            self._window_untrade(mint)
        self.decisions.appendleft({"ts_ms": int(time.time() * 1000), "mint": mint, "decision": "aborted", "reasons": [why]})
        self.stats["aborted"] += 1
        self._ledger("buy_aborted", mint=mint, reason=why)
        logger.info("live buy aborted %s: %s", mint, why)

    def _start_stream(self, mint: str) -> None:
        """Pin + (re)start the mint's trade stream. Pinned streams never hit the 30-min TTL or eviction, so live
        runners are watched indefinitely; if a stream still dies, mark() falls back to curve/Jupiter polling."""
        try:
            from app.pump.trade_stream import streams
            streams.pinned.add(mint)
            s = streams.get(mint)
            if s is None or s.status in ("stopped", "expired", "evicted", "no_loop"):
                streams.start(mint)
                self.stats["stream_restarts"] += 1
        except Exception:  # noqa: BLE001
            pass

    def _unpin_stream(self, mint: str) -> None:
        try:
            from app.pump.trade_stream import streams
            if not any(p.mint == mint for p in self.open_positions()):
                streams.pinned.discard(mint)
        except Exception:  # noqa: BLE001
            pass

    # ---------------- exits ----------------
    def note_trader_sell(self, wallet: str, mint: str) -> None:
        try:
            scout_bench.bench.note_trader_sell(wallet, mint)    # v1.21 benched-wallet paper copies
        except Exception:  # noqa: BLE001
            pass
        hit = False
        for p in self.open_positions():
            if p.mint == mint and wallet in p.trader_wallets and not p.trader_sold:
                p.trader_sold = True
                p.meta["trader_sold_ms"] = int(time.time() * 1000)   # v1.33 trader-sell window
                hit = True
                self._spawn(self.manage(p))
        if not hit and mint in self.inflight and wallet in ((self.pending_buys.get(mint) or {}).get("traders") or []):
            # fast path: the trader sold while our buy was still in flight → apply as soon as the position opens
            self.pending_trader_sells.setdefault(mint, set()).add(wallet)
            self.stats["trader_sell_while_inflight"] += 1

    def note_tracked_buy(self, wallet: str, mint: str, ms: Optional[int] = None) -> None:
        """v1.39b: scout/confirmer buys (fed by app.scout.engine.on_trade before arm routing), for the confluence skip."""
        tb = self.__dict__.setdefault("tracked_buys", {})
        now = int(ms or time.time() * 1000)
        dq = tb.setdefault(mint, collections.deque(maxlen=64))
        dq.append((now, wallet))
        if len(tb) > 5000:
            cut = now - 3600_000
            for m in [m for m, d in tb.items() if not d or d[-1][0] < cut]:
                tb.pop(m, None)

    def _confluence_wallets(self, mint: str) -> List[str]:
        """Distinct scout/confirmer wallets that bought `mint` in the last LIVE_CONFLUENCE_WINDOW_MIN (30) minutes.
        [] unless >= LIVE_CONFLUENCE_SKIP_GATE_MIN (0 = off)."""
        n_min = _confluence_skip_min()
        if n_min <= 0:
            return []
        try:
            win = float(os.getenv("LIVE_CONFLUENCE_WINDOW_MIN", "30"))
        except ValueError:
            win = 30.0
        cut = int(time.time() * 1000) - int(win * 60_000)
        ws = sorted({w for ms, w in list((self.__dict__.get("tracked_buys") or {}).get(mint) or []) if ms >= cut})
        return ws if len(ws) >= n_min else []

    async def _confluence_instant(self, mint: str, arm: str, wallets: List[str], tm: Dict[str, Any]) -> Tuple[bool, str]:
        """Instant mint checks instead of the mesh. Unknown (RPC/parse) → the normal mesh gate (fail-safe)."""
        t0 = time.time()
        acct = None
        try:
            res = await rpc.call("getAccountInfo", [mint, {"encoding": "jsonParsed", "commitment": "processed"}],
                                 client=await self._client(), timeout=1.0)
            acct = (res or {}).get("value")
        except Exception:  # noqa: BLE001
            acct = None
        ok, why, facts = instant_mint_verdict(acct)
        tm["instant_check_ms"] = round((time.time() - t0) * 1000)
        self._ledger("gate_skipped_confluence", mint=mint, arm=arm, n=len(wallets), wallets=[w[:6] for w in wallets],
                     verdict=ok, why=why, facts=facts, ms=tm["instant_check_ms"])
        if ok is None:
            self.stats["confluence_skip_fallback_mesh"] += 1
            return await self._mesh_check(mint)
        self.stats["confluence_gate_skipped" if ok else "confluence_instant_refused"] += 1
        return (True, f"mesh skipped: {len(wallets)} tracked wallets; {why}") if ok else (False, f"instant check: {why}")

    def _rug_deferred(self, mint: str, arm: str, task: "asyncio.Future", meta: Dict[str, Any]) -> None:
        """v1.39c: the on-chain rugcheck answers after the send. Fail → immediate auto-sell (urgent); its facts become
        the open position's rug-watch baseline either way."""
        self.stats["rug_deferred"] += 1

        def _late(t: "asyncio.Future") -> None:
            try:
                r_ = t.result()
            except Exception as exc:  # noqa: BLE001
                r_ = {"ok": True, "unknown": ["error"], "refuse": [], "error": type(exc).__name__}
            meta["_rug"] = r_
            held = [p for p in self.open_positions() if p.mint == mint]
            for p in held:
                try:
                    self._rug_baseline(p, r_)
                except Exception:  # noqa: BLE001
                    pass
            if not r_.get("ok"):
                self.stats["post_buy_check_fail"] += 1
                why = "; ".join(r_.get("refuse") or ["rugcheck refused"])[:300]
                self._ledger("post_buy_check_fail", mint=mint, arm=arm, check="rugcheck", why=why,
                             had_position=bool(held), inflight=mint in self.inflight)
                self.force_exit(mint, "rugcheck_post_buy", signals=["rug", why], urgent=True)
        task.add_done_callback(_late)

    def _mesh_deferred(self, mint: str, task: "asyncio.Future") -> None:
        """v1.39: the buy went ahead before the mesh answered. A late HARD block force-sells (inflight → applied after
        the fill); mesh down / stale / error late is only logged (the on-chain rug check already passed)."""
        self.stats["mesh_deferred"] += 1

        def _late(t: "asyncio.Future") -> None:
            try:
                ok_, why_ = t.result()
            except Exception as exc:  # noqa: BLE001
                ok_, why_ = False, f"mesh check error ({type(exc).__name__})"
            self._ledger("mesh_late_answer", mint=mint, ok=ok_, why=str(why_)[:200])
            if not ok_ and str(why_).startswith(("mesh hard block", "instant check")):
                self.stats["mesh_late_block"] += 1
                self.stats["post_buy_check_fail"] += 1
                self._ledger("post_buy_check_fail", mint=mint, check="mesh", why=str(why_)[:200],
                             had_position=any(p.mint == mint for p in self.open_positions()),
                             inflight=mint in self.inflight)
                self.force_exit(mint, "mesh_block_late", signals=[str(why_)], urgent=True)
        task.add_done_callback(_late)

    def force_exit(self, mint: str, reason: str, *, signals: Optional[List[str]] = None, urgent: bool = False) -> None:
        """Forced exit (mesh hard block). A runner is only force-closed on rug/honeypot/mint-or-freeze authority live/
        sell tax/high-critical verdict. v1.15: a block that lands while the buy is inflight is applied once the
        position exists."""
        cfg = config()
        if str(reason).startswith("rug_flag"):            # v1.27 paper exit-variants hook; never affects live exits
            try:
                from app.scout.exit_variants import variants as _xv
                _xv.on_rug(mint, reason)
            except Exception:  # noqa: BLE001
                pass
        if mint in self.inflight and not any(p.mint == mint for p in self.open_positions()):
            self.pending_blocks[mint] = {"reason": reason, "signals": list(signals or []), "ts_ms": int(time.time() * 1000)}
            self._ledger("force_exit_deferred_inflight", mint=mint, reason=reason, signals=signals)
        severe = force_close_signal(signals)
        for p in self.open_positions():
            if p.mint != mint:
                continue
            if is_runner(p, p.last_price_sol, cfg, cfg["rules"].get(p.arm, [])) and not severe:
                p.flags = list(dict.fromkeys(p.flags + [f"ignored:{reason}(runner, no rug/honeypot)"]))
                self._ledger("force_exit_ignored_runner", id=p.id, mint=mint, reason=reason, signals=signals)
                continue
            # v1.17: only the new open-position rug triggers (creator dump / liquidity drain) sell straight at the
            # capped slippage; every older exit keeps its stepped slippage exactly as before
            self._spawn(self._sell(p, 1.0, reason, **({"urgent": True} if urgent else {})))

    # ---------------- v1.17 rug watch (open positions) ----------------
    def _rug_baseline(self, p: LivePos, rug: Optional[Dict[str, Any]]) -> None:
        """Creator + linked wallets and their balances right before our buy (from the pre-buy rug check)."""
        rug = rug or {}
        owners = [o for o in [rug.get("creator")] + list(rug.get("linked") or []) if o]
        base = {o: int(v) for o, v in (rug.get("launch_holdings") or {}).items() if o in owners}
        self.rug_state[p.id] = {"creator": rug.get("creator"), "linked": list(rug.get("linked") or []),
                                "owners": owners, "baseline": base if base else None,
                                "baseline_total": sum(base.values()) if base else None, "sold_stream": 0}

    def _dump_check(self, p: LivePos, sold_raw: int, source: str) -> bool:
        """Creator/linked wallets sold ≥ LIVE_RUG_DUMP_FRAC of what they held at entry AND ≥ the supply floor."""
        rc = rugcheck.cfg()
        st = self.rug_state.get(p.id) or {}
        base = st.get("baseline_total") or 0
        pct = sold_raw / rugcheck.PUMP_SUPPLY_RAW * 100
        if not rc["watch_enabled"] or sold_raw <= 0 or pct < rc["dump_min_pct"]:
            return False
        if base and sold_raw < base * rc["dump_frac"]:
            return False
        self.stats["rug_watch_dump"] += 1
        self.force_exit(p.mint, "rug_flag_creator_dump", urgent=True,
                        signals=[f"rug: creator/linked wallets sold {pct:.1f}% of supply ({source})"])
        return True

    def _scan_stream_dumps(self, p: LivePos, s: Any) -> None:
        st = self.rug_state.get(p.id)
        if not st or not st.get("owners"):
            return
        trades = list(getattr(s, "trades", []) or [])
        start = self._trade_idx.get(p.id)
        if start is None or start > len(trades):
            start = max(0, len(trades) - 50)
        owners = set(st["owners"])
        for t in trades[start:]:
            if not t.get("is_buy") and t.get("user") in owners and int(t.get("recv_ms") or 0) >= p.entry_ms:
                st["sold_stream"] = st.get("sold_stream", 0) + int(t.get("token_amount") or 0)
        self._trade_idx[p.id] = len(trades)
        if st.get("sold_stream"):
            self._dump_check(p, st["sold_stream"], "trade stream")

    def _drain_check(self, p: LivePos, quote_reserves: Optional[int]) -> bool:
        """Curve / pool quote reserves fell ≥ LIVE_DRAIN_FRAC below their max of the last LIVE_DRAIN_WINDOW_S."""
        rc = rugcheck.cfg()
        if quote_reserves is None or not rc["watch_enabled"]:
            return False
        now = int(time.time() * 1000)
        dq = self._reserves.setdefault(p.id, collections.deque(maxlen=200))
        dq.append((now, int(quote_reserves)))
        win = float(os.getenv("LIVE_DRAIN_WINDOW_S", "30")) * 1000
        while dq and now - dq[0][0] > win:
            dq.popleft()
        peak = max(v for _, v in dq)
        if peak > 0 and quote_reserves <= peak * (1 - rc["drain_frac"]) and p.status != "closed" and not p.exit_pending:
            self.stats["rug_watch_drain"] += 1
            dq.clear()
            self.force_exit(p.mint, "rug_flag_liquidity_drain", urgent=True,
                            signals=[f"rug: curve/pool liquidity fell {1 - quote_reserves / peak:.0%} within "
                                     f"{win / 1000:.0f}s"])
            return True
        return False

    async def rug_watch(self, ps: List[LivePos]) -> None:
        """Every LIVE_RUG_WATCH_S: creator/linked balances vs the entry baseline (catches PumpSwap sells and
        transfers the trade stream can't see). No baseline (check was unknown / restart) → take one now."""
        rc = rugcheck.cfg()
        if not rc["watch_enabled"] or time.time() - self._last_watch_ms < rc["watch_s"]:
            return
        self._last_watch_ms = time.time()
        for p in ps:
            if p.status == "closed":
                continue
            st = self.rug_state.get(p.id)
            if st is None:
                st = self.rug_state[p.id] = {"creator": None, "linked": [], "owners": [], "baseline": None,
                                             "baseline_total": None, "sold_stream": 0}
            if not st["owners"]:
                try:
                    c = await self._read_curve_raw(p.mint)
                except Exception:  # noqa: BLE001
                    c = None
                if c and c.get("creator"):
                    st["creator"], st["owners"] = c["creator"], [c["creator"]]
                else:
                    continue
            try:
                bal = await rugcheck.holdings_now(p.mint, st["owners"], self._rpc_any)
            except Exception:  # noqa: BLE001
                bal = None
            if bal is None:
                self.stats["rug_watch_errors"] += 1
                continue
            tot = sum(bal.values())
            if st["baseline_total"] is None:
                st["baseline"], st["baseline_total"] = dict(bal), tot
                continue
            dropped = st["baseline_total"] - tot
            if dropped > 0:
                self._dump_check(p, dropped, "balances")

    async def _rpc_any(self, method: str, params: List[Any]) -> Any:
        try:
            return await self._rpc(method, params)
        except Exception:  # noqa: BLE001
            return None

    # ---------------- v1.17 account watcher (accountSubscribe) ----------------
    async def _sync_watch(self, ps: List[LivePos]) -> None:
        from app.live.watch import enabled as watch_enabled
        if not watch_enabled():
            return
        wanted: Dict[str, Tuple[str, str]] = {}
        unwatched = 0
        for p in ps:
            if p.status == "closed":
                continue
            if not p.migrated:
                wanted[bonding_curve_pda(p.mint)] = (p.mint, "curve")
            else:
                hit = self._vault_cache.get(p.mint)
                vaults = (p.meta or {}).get("_vaults") or (hit[:2] if hit else None)
                sol_quote = (not self.is_nonsol(p)) if not hit else hit[2] if len(hit) > 2 else True
                neg = self._vault_miss.get(p.mint, 0)
                if not vaults and time.time() - neg >= float(os.getenv("LIVE_WATCH_POOL_RETRY_S", "60")):
                    # v1.37: one lookup per minute at most (was every mark loop), and the pool is looked up with
                    # the coin's real quote mint (PUMP / USDC-quoted coins have no WSOL canonical pool).
                    try:
                        from app.pump.curve import decode_pool
                        from app.pump.solana_pda import canonical_pumpswap_pool
                        qm = (p.meta or {}).get("quote_mint")
                        if not qm or qm in nonsol.SOL_QUOTE_ALIASES:
                            qm = WSOL
                        cands = [qm]
                        if qm == WSOL:
                            try:
                                c = await self._read_curve_raw(p.mint)
                            except Exception:  # noqa: BLE001
                                c = None
                            cq = (c or {}).get("quote_mint")
                            if c is not None and not c.get("quote_is_sol", True) and cq and cq != WSOL:
                                cands = [cq]
                        for q in cands:
                            res = await self._rpc("getAccountInfo", [canonical_pumpswap_pool(p.mint, q),
                                                                     {"encoding": "base64", "commitment": "confirmed"}])
                            acc = rpc._decode_account((res or {}).get("value"))
                            if acc:
                                pool = decode_pool(acc["data"])
                                vaults = (pool["base_vault"], pool["quote_vault"])
                                sol_quote = q == WSOL
                                self._vault_cache[p.mint] = (vaults[0], vaults[1], sol_quote)
                                break
                    except Exception:  # noqa: BLE001
                        vaults = None
                    if not vaults:
                        self._vault_miss[p.mint] = time.time()
                        self.stats["watch_pool_miss"] += 1
                if vaults:
                    sfx = "" if sol_quote else "_x"                # _x: quote is not SOL → drain check only
                    wanted[vaults[0]] = (p.mint, "pool_base" + sfx)
                    wanted[vaults[1]] = (p.mint, "pool_quote" + sfx)
                else:
                    unwatched += 1
        acct_watch.unwatched = unwatched
        acct_watch.sync(wanted, self.on_account)

    async def on_account(self, mint: str, kind: str, data: bytes, slot: Optional[int]) -> None:
        """accountSubscribe push → fresh mark, drain check, exit evaluation (same rules, sooner)."""
        for p in self.open_positions():
            if p.mint != mint:
                continue
            px, quote_res = None, None
            if kind == "curve":
                try:
                    c = decode_curve(data)
                except ValueError:
                    continue
                if c.get("complete"):
                    p.migrated = True
                    continue
                quote_res = c.get("real_quote_reserves", c.get("real_sol_reserves"))
                px = nonsol.mark_price_sol(c, p.meta.get("quote_mint")) if self.is_nonsol(p) else \
                    (curve_price_sol(c) if c.get("quote_is_sol", True) else None)
                src = "curve_ws"
            elif kind.endswith("_x"):                          # v1.37 non-SOL-quoted pool: liquidity-pull check only
                from app.pump.curve import token_amount
                try:
                    amt = token_amount(data)
                except Exception:  # noqa: BLE001
                    continue
                self.stats["watch_marks_x"] += 1
                if kind == "pool_quote_x":
                    self._drain_check(p, amt)
                continue
            else:
                from app.pump.curve import token_amount
                try:
                    amt = token_amount(data)
                except Exception:  # noqa: BLE001
                    continue
                d = self._pool_amts.setdefault(mint, {})
                d["base" if kind == "pool_base" else "quote"] = amt
                if kind == "pool_quote":
                    quote_res = amt
                if d.get("base") and d.get("quote"):
                    px = (d["quote"] / 1e9) / (d["base"] / 10 ** p.decimals)
                src = "pool_ws"
            self.stats["watch_marks"] += 1
            if quote_res is not None and self._drain_check(p, quote_res):
                continue
            if px:
                self._prev_px[p.id] = p.last_price_sol or px
                self._apply_price(p, px, src)
                self._spawn(self._manage_once(p, fresh=True))

    async def on_stream(self, s: Any) -> None:
        """Trade-stream listener: update the mark inline (cheap) and evaluate exits in a separate task so a
        multi-second Jupiter sell never blocks the shared websocket reader (paper lane / other mints).
        v1.15: a price is accepted only when its SOURCE timestamp advanced (a re-notified stale price is ignored)."""
        src_ms = getattr(s, "price_ms", None)
        for p in self.open_positions():
            if p.mint == s.mint and s.last_price_sol:
                if s.migrated:
                    p.migrated = True
                if self.is_nonsol(p):
                    self._scan_stream_dumps(p, s)
                    continue                  # v1.16.1: marked from curve × verified quote price / Jupiter sell quote
                self._scan_stream_dumps(p, s)                 # v1.17 creator / linked wallets dumping
                if src_ms is not None and p.mark_src_ms is not None and src_ms <= p.mark_src_ms:
                    continue
                self._prev_px[p.id] = p.last_price_sol or s.last_price_sol
                self._apply_price(p, s.last_price_sol, "trade_stream", src_ms=src_ms)
                if getattr(s, "real_sol", None) is not None and not p.migrated:
                    self._drain_check(p, s.real_sol)
                self._spawn(self._manage_once(p, fresh=True))

    def _apply_price(self, p: LivePos, price: float, src: str, *, src_ms: Optional[int] = None) -> None:
        p.last_price_sol, p.mark_source, p.last_mark_ms = price, src, int(time.time() * 1000)
        if src_ms is not None:
            p.mark_src_ms = src_ms
        p.quote_fail_since_ms = None
        p.high_price_sol = max(p.high_price_sol or price, price)

    def mark_stale(self, p: LivePos, now_ms: Optional[int] = None) -> bool:
        now_ms = now_ms or int(time.time() * 1000)
        return now_ms - (p.last_mark_ms or p.entry_ms) > config()["stale_mark_s"] * 1000

    async def mark(self, ps: List[LivePos]) -> None:
        cfg = config()
        for qm in {p.meta.get("quote_mint") for p in ps if self.is_nonsol(p)}:   # v1.16.1 keep quote prices fresh
            if not nonsol.cached(qm, 30.0):
                try:
                    await nonsol.refresh(qm)
                except Exception:  # noqa: BLE001
                    self.stats["nonsol_price_errors"] += 1
        curve_ps = [p for p in ps if not p.migrated]
        if curve_ps:
            try:
                res = await self._rpc("getMultipleAccounts", [[bonding_curve_pda(p.mint) for p in curve_ps],
                                                              {"encoding": "base64", "commitment": "processed"}])
                for p, v in zip(curve_ps, (res or {}).get("value") or []):
                    acc = rpc._decode_account(v)
                    if not acc or acc.get("owner") != PUMP_PROGRAM_ID:
                        p.migrated = True
                        continue
                    c = decode_curve(acc["data"])
                    if c.get("complete"):
                        p.migrated = True
                        continue
                    if self._drain_check(p, c.get("real_quote_reserves", c.get("real_sol_reserves"))):
                        continue
                    stale_ms = time.time() * 1000 - float(os.getenv("LIVE_POLL_MIN_AGE_MS", "900"))
                    if self.is_nonsol(p):
                        if (p.last_mark_ms or 0) < stale_ms:
                            px = nonsol.mark_price_sol(c, p.meta.get("quote_mint"))
                            if px:
                                self._prev_px[p.id] = p.last_price_sol or px
                                self._apply_price(p, px, "curve_quote")
                    elif c.get("quote_is_sol", True) and (p.last_mark_ms or 0) < stale_ms:
                        px = curve_price_sol(c)
                        if px:
                            self._prev_px[p.id] = p.last_price_sol or px
                            self._apply_price(p, px, "curve")
            except Exception:  # noqa: BLE001
                self.stats["mark_errors"] += 1
        now = time.time() * 1000
        for p in ps:
            if not p.tokens_left:
                continue
            age = now - (p.last_mark_ms or p.entry_ms)
            need = ((p.migrated or self.is_nonsol(p)) and age > max(cfg["mark_s"], 8) * 1000) \
                or age > cfg["stale_mark_s"] * 1000
            if not need:
                continue
            try:
                q = await jupiter.order(input_mint=p.mint, output_mint=WSOL, amount=p.tokens_left,
                                        slippage_bps=cfg["sell_slippage_steps_bps"][0], max_priority_lamports=0,
                                        client=await self._client())
            except Exception:  # noqa: BLE001
                q = {}
            if q.get("out_amount"):
                px = (q["out_amount"] / 1e9) / (p.tokens_left / 10 ** p.decimals)
                self._prev_px[p.id] = p.last_price_sol or px
                self._apply_price(p, px, "jupiter_quote")
            elif age > cfg["stale_mark_s"] * 1000 and not p.quote_fail_since_ms:
                p.quote_fail_since_ms = int(now)
                self.stats["stale_quote_fail"] += 1

    async def _manage_once(self, p: LivePos, *, fresh: bool = False) -> None:
        """One manage() per position at a time; never lets an exception escape a spawned task."""
        if p.id in self._managing:
            return
        self._managing.add(p.id)
        try:
            await self.manage(p, fresh=fresh)
        except Exception as exc:  # noqa: BLE001
            logger.warning("live manage %s: %s", p.mint, type(exc).__name__)
        finally:
            self._managing.discard(p.id)

    async def manage(self, p: LivePos, *, fresh: bool = False) -> None:
        if p.status == "closed":
            return
        cfg = config()
        now = int(time.time() * 1000)
        if p.ambiguous_sell:
            await self._reconcile_ambiguous(p)
            if p.status == "closed":
                return
        if p.exit_pending:
            f = max(0, p.sell_failures - 1)
            delay = min(cfg["sell_retry_s"] * (2 ** min(f, 6)), max(cfg["sell_retry_max_s"], cfg["sell_retry_s"]))
            if now - p.last_sell_try_ms >= delay * 1000:
                await self._sell(p, 1.0, p.exit_pending, **({"urgent": True} if p.id in self._urgent_retry else {}))
            return
        rules = cfg["rules"].get(p.arm) or ["hard_stop", "max_hold"]
        stale = self.mark_stale(p, now)
        if stale and p.quote_fail_since_ms and now - p.quote_fail_since_ms >= cfg["stale_sell_after_s"] * 1000:
            p.flags = list(dict.fromkeys(p.flags + ["stale_mark_exit"]))
            await self._sell(p, 1.0, "stale_mark_no_quote")
            return
        k = self.killed()
        acts = evaluate_exit(p, None if stale else p.last_price_sol, now, cfg, rules,
                             kill_sell_all=k["active"] and (cfg["kill_sell_all"] or self.api_kill_sell_all))
        for kind, frac, reason in acts:
            if kind == "sell_frac":
                if p.ambiguous_sell:
                    continue                                   # an earlier partial is still unresolved
                if not reason.startswith("tpfast"):            # v1.17 fast-pump partial never arms the trail
                    p.tp_done = True
                if reason not in p.ladder_done:
                    p.ladder_done.append(reason)
            urgent = kind == "sell_all" and gap_down(self._prev_px.get(p.id), None if stale else p.last_price_sol,
                                                     p.high_price_sol, p.entry_price_sol, reason, cfg,
                                                     trail_for(p, cfg))
            if urgent:
                self.stats["gap_down_sells"] += 1
                p.flags = list(dict.fromkeys(p.flags + ["gap_down"]))
            await self._sell(p, frac, reason, **({"urgent": True} if urgent else {}))

    api_kill_sell_all: bool = False

    def _undo_partial(self, p: LivePos, reason: str) -> None:
        if reason in p.ladder_done:
            p.ladder_done.remove(reason)
        p.tp_done = any(not x.startswith("tpfast") for x in p.ladder_done)

    async def _reconcile_ambiguous(self, p: LivePos) -> None:
        """A partial TP whose /execute outcome was unknown: credit it if the balance dropped, else (after 60 s)
        forget it so the level can be retried. Never sends anything."""
        lock = self._locks.setdefault(p.id, asyncio.Lock())
        if lock.locked():
            return
        async with lock:
            amb = p.ambiguous_sell
            if not amb or p.status == "closed":
                return
            onchain, _ = await self.token_balance(p.mint)
            if onchain is not None and onchain < amb["before"]:
                p.ambiguous_sell = None
                self._record_sell(p, amb["before"] - onchain, int(amb.get("est") or 0), amb["reason"], amb.get("slip", 0),
                                  amb.get("sig"), {"order": {"router": amb.get("router")}},
                                  flags=["reconciled_from_chain", "ambiguous_next_cycle"])
            elif onchain is not None and time.time() * 1000 - amb["ts_ms"] > 60_000:
                p.ambiguous_sell = None
                self._undo_partial(p, amb["reason"])
                self._ledger("sell_ambiguous_cleared", id=p.id, mint=p.mint, reason=amb["reason"])
                self.save()

    async def _sell(self, p: LivePos, frac: float, reason: str, *, urgent: bool = False) -> None:
        lock = self._locks.setdefault(p.id, asyncio.Lock())
        if lock.locked():
            if is_partial(reason):
                self._undo_partial(p, reason)          # another sell holds the lock: re-evaluate the TP later
            return
        async with lock:
            if p.status == "closed":
                return
            cfg = config()
            p.last_sell_try_ms = int(time.time() * 1000)
            t_sell0 = time.time()                              # v1.40b sell timing
            if not self.can_sign():
                if is_partial(reason):
                    self._undo_partial(p, reason)      # re-evaluate the partial later; never turn it into a full exit
                else:
                    p.exit_pending = p.exit_pending or reason
                self.stats["sell_blocked_exec_off"] += 1
                return
            # v1.17.2 fast exit: a clean position (no failed/ambiguous sells) starts the order from its known size;
            # the balance read runs alongside and is checked in the pre-send gate below.
            fast = (_sell_skip_balance_enabled() and p.tokens_left > 0 and not p.ambiguous_sell
                    and p.sell_failures == 0)
            bal_task: Optional[asyncio.Future] = None
            if fast:
                bal_task = asyncio.ensure_future(self.token_balance(p.mint))
                bal_task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
                onchain, dec = None, None
                self.stats["sell_fast_path"] += 1
            else:
                onchain, dec = await self.token_balance(p.mint)
                if onchain == 0:
                    self._close(p, f"{reason} (no balance on-chain)")
                    return
            avail = min(p.tokens_left, onchain) if onchain is not None else p.tokens_left
            if onchain is not None and frac >= 1.0:
                avail = onchain
            tokens = avail if frac >= 1.0 else max(1, int(p.tokens_left * frac))
            tokens = min(tokens, avail)
            bal_state: Dict[str, Any] = {"onchain": onchain, "fix": None}

            def _right_amount(ob: int) -> int:
                if frac >= 1.0:
                    return ob
                return min(tokens, ob)

            async def _take_balance() -> Optional[int]:
                """The pre-sell balance (read started before the order; consumed once)."""
                nonlocal bal_task
                if bal_task is None:
                    return bal_state["onchain"]
                try:
                    ob, _d = await asyncio.wait_for(asyncio.shield(bal_task), timeout=2.0)
                except Exception:  # noqa: BLE001
                    ob = None
                bal_task = None
                bal_state["onchain"] = ob
                return ob

            async def _sell_gate(order_age_s: float) -> Tuple[bool, str, bool]:
                if bal_task is None:
                    return True, "", False
                ob = await _take_balance()
                if ob is None:
                    return True, "", False                     # RPC unreadable → same as the old path (known size)
                if ob == 0:
                    return False, "no balance on-chain", False
                if _right_amount(ob) != tokens:
                    bal_state["fix"] = _right_amount(ob)
                    return False, "amount mismatch", False
                return True, "", False

            dmode = pumpdirect.mode_for("sell", p.arm)         # v1.20 direct path; v1.40e per-arm/side (scout sells)
            if p.id in self.__dict__.setdefault("direct_sell_off", set()):
                dmode = "shadow" if dmode == "live" else dmode  # v1.40e: a direct sell already failed → Jupiter

            sign_retry = {"n": 0}

            async def _swap_once(slip_bps: int) -> Dict[str, Any]:
                nonlocal tokens
                for _attempt in range(2 + _sell_sign_retries()):   # one amount correction + v1.37 sign-RPC retries
                    r = None
                    if dmode == "live" and p.id not in self.__dict__.get("direct_sell_off", ()):
                        r = await pumpdirect.swap(side="sell", mint=p.mint, amount=tokens, slippage_bps=slip_bps,
                                                  client=await self._client(), allow_sim_transport_error=bypass,
                                                  pre_execute=_sell_gate if bal_task is not None else None)
                        self._direct_live_log("sell", p.arm, p.mint, r, None)
                        if r.get("fallback"):
                            self.stats["direct_fallback_sell"] += 1
                            r = None
                    if r is None:
                            pf = self.__dict__.setdefault("sell_prefetch", {}).pop(p.id, None)   # v1.40e warm route
                            pf_ok = bool(pf and pf["amount"] == tokens and pf["slip"] == slip_bps
                                         and time.time() - pf["t"] <= _sell_prefetch_max_age())
                            self.stats["sell_prefetch_used" if pf_ok else "sell_prefetch_miss"] += 1
                            r = await jupiter.swap(input_mint=p.mint, output_mint=WSOL, amount=tokens, slippage_bps=slip_bps,
                                               max_priority_lamports=fastfill.priority_lamports(cfg["max_priority_lamports"]),
                                               client=await self._client(), sim_rpc_url=cfg["sim_rpc_url"],
                                               allow_sim_transport_error=bypass,
                                               pre_execute=_sell_gate if bal_task is not None else None,
                                               prefetched=pf["order"] if pf_ok else None)
                    if (r.get("stage") == "sign" and _transient_sign_error(r.get("error"))
                            and sign_retry["n"] < _sell_sign_retries()):
                        # v1.37: the signer's own RPC said 429/5xx while evaluating → nothing was signed or sent;
                        # back off and rebuild instead of dropping the exit (TP / stop / drain sells).
                        sign_retry["n"] += 1
                        self.stats["sell_sign_rpc_retry"] += 1
                        await asyncio.sleep(_sell_sign_backoff_s() * (2 ** (sign_retry["n"] - 1)))
                        continue
                    if r.get("stage") == "gate" and r.get("error") == "amount mismatch" and bal_state["fix"]:
                        tokens, bal_state["fix"] = bal_state["fix"], None
                        self.stats["sell_amount_fixed"] += 1
                        continue
                    if fast and r.get("stage") in ("simulate", "order") and bal_task is not None:
                        ob = await _take_balance()             # sim failed before the gate: was it the amount?
                        if ob == 0:
                            return {"ok": False, "stage": "gate", "error": "no balance on-chain"}
                        if ob is not None and _right_amount(ob) != tokens:
                            tokens = _right_amount(ob)
                            self.stats["sell_amount_fixed"] += 1
                            continue
                    return r
                return r

            last_err, last_stage, sim_err = None, None, None
            bypass = p.sell_failures >= cfg["sell_sim_bypass_after"]
            # v1.17: gap-down / rug / drain → the existing capped sell slippage at once (never above the cap)
            steps = [max(cfg["sell_slippage_steps_bps"])] if urgent else cfg["sell_slippage_steps_bps"]
            for i, slip in enumerate(steps):
                if i and cfg["sell_step_backoff_s"] > 0:
                    await asyncio.sleep(cfg["sell_step_backoff_s"] * i)          # v1.15 back off between steps
                sh_task: Optional[asyncio.Future] = None
                if i == 0 and dmode == "shadow":               # one shadow build+simulate per exit, never sent
                    sh_task = asyncio.ensure_future(pumpdirect.shadow("sell", p.mint, tokens, slippage_bps=slip,
                                                                      client=await self._client()))
                t_sw = time.time()
                res = await _swap_once(slip)
                if sh_task is not None:
                    self._direct_shadow_finish(sh_task, res, "sell", p.mint, p.arm,
                                               round((time.time() - t_sw) * 1000), None, reason=reason)
                onchain = bal_state["onchain"] if fast else onchain
                if res.get("stage") == "gate" and res.get("error") == "no balance on-chain":
                    self._close(p, f"{reason} (no balance on-chain)")
                    return
                if res.get("ok"):
                    sold = res.get("total_in") or tokens
                    try:
                        self._ledger("sell_timing", id=p.id, mint=p.mint, arm=p.arm, reason=reason, urgent=urgent, step=i,
                                     total_ms=round((time.time() - t_sell0) * 1000), swap_ms=round((time.time() - t_sw) * 1000),
                                     router=(res.get("order") or {}).get("router") or res.get("router"), fast=fast,
                                     timing=res.get("timing"))
                    except Exception:  # noqa: BLE001
                        pass
                    self._record_sell(p, sold, res.get("total_out", 0), reason, slip, res.get("signature"), res)
                    return
                last_err, last_stage = res.get("error"), res.get("stage")
                if last_stage == "simulate":
                    sim_err = res.get("error")
                if ((res.get("order") or {}).get("router") == "pump_direct" and frac >= 1.0
                        and (res.get("ambiguous") or last_stage == "execute")):
                    # v1.40e: direct sell failed / not confirmed in the short window → Jupiter next, never stuck.
                    self.__dict__.setdefault("direct_sell_off", set()).add(p.id)
                    self.stats["direct_sell_fallback_jup"] += 1
                    after = None
                    if res.get("ambiguous"):
                        await asyncio.sleep(_direct_sell_ambig_wait())
                        after, _ = await self.token_balance(p.mint)
                        est = (res.get("order") or {}).get("out_amount") or 0
                        if after is not None and onchain is not None and after < onchain:
                            self._record_sell(p, onchain - after, est, reason, slip, res.get("signature"), res,
                                              flags=["reconciled_from_chain", "direct_late_land"])
                            return
                        if after == 0:
                            self._record_sell(p, tokens, est, reason, slip, res.get("signature"), res,
                                              flags=["reconciled_from_chain", "direct_late_land"])
                            return
                        if after is not None and after < tokens:
                            tokens = after
                    self._ledger("direct_sell_fallback", id=p.id, mint=p.mint, reason=reason, stage=last_stage,
                                 ambiguous=bool(res.get("ambiguous")), error=str(last_err)[:160],
                                 signature=res.get("signature"), timing=res.get("timing"))
                    res = await _swap_once(slip)                  # same slippage step, now via Jupiter
                    if res.get("stage") == "gate" and res.get("error") == "no balance on-chain":
                        self._close(p, f"{reason} (no balance on-chain)")
                        return
                    if res.get("ok"):
                        self._record_sell(p, res.get("total_in") or tokens, res.get("total_out", 0), reason, slip,
                                          res.get("signature"), res)
                        return
                    last_err, last_stage = res.get("error"), res.get("stage")
                if res.get("ambiguous") or (last_stage == "execute" and res.get("signature")
                                            and res.get("signature_from_server", True)):
                    await asyncio.sleep(6)
                    after, _ = await self.token_balance(p.mint)
                    if after is not None and onchain is not None and after < onchain:
                        est = (res.get("order") or {}).get("out_amount") or 0
                        self._record_sell(p, onchain - after, est, reason, slip, res.get("signature"), res,
                                          flags=["reconciled_from_chain"])
                        return
                    if is_partial(reason) and res.get("ambiguous"):
                        # v1.15: never fire a second partial on top of an unknown one — reconcile next cycle
                        p.ambiguous_sell = {"reason": reason, "before": onchain if onchain is not None else p.tokens_left,
                                            "ts_ms": int(time.time() * 1000), "slip": slip, "sig": res.get("signature"),
                                            "est": (res.get("order") or {}).get("out_amount") or 0,
                                            "router": (res.get("order") or {}).get("router")}
                        self._ledger("sell_ambiguous", id=p.id, mint=p.mint, reason=reason, signature=res.get("signature"))
                        self.save()
                        return
                if last_stage in ("order", "sign"):
                    break                              # no route / order error: more slippage won't help
                self.stats["sell_retry"] += 1
            if is_partial(reason):
                self._undo_partial(p, reason)          # retry the partial on the next mark instead of a full exit
            else:
                p.exit_pending = reason
                if urgent:
                    self._urgent_retry.add(p.id)
            p.sell_failures += 1
            self._ledger("sell_failed", id=p.id, mint=p.mint, reason=reason, error=last_err, stage=last_stage,
                         failures=p.sell_failures)
            if p.sell_failures in (1, 5, 20) or (sim_err and p.sell_failures % 10 == 1):
                self._alert("live_sell_failed", arm=p.arm, mint=p.mint, error=last_err, reason=reason,
                            failures=p.sell_failures, stage=last_stage)
            self.save()

    def _chase_check(self, arm: str, mint: str, ref_price: Optional[float], quote: Optional[Dict[str, Any]],
                     cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """v1.21 chase filter. None = not evaluated (off / other arm / no ref price / no quote). Logs every skip."""
        if not cfg.get("chase_filter_enabled") or arm not in (cfg.get("chase_arms") or []) or not ref_price or not quote:
            return None
        try:
            lam, out = int(quote.get("in_amount") or 0), int(quote.get("out_amount") or 0)
        except (TypeError, ValueError):
            return None
        if lam <= 0 or out <= 0:
            return None
        px = (lam / 1e9) / (out / 10 ** int(cfg.get("chase_decimals", 6)))
        prem = (px / float(ref_price) - 1) * 100
        info = {"premium_pct": round(prem, 2), "quote_price_sol": px, "ref_price_sol": ref_price,
                "max_pct": cfg["max_chase_pct"], "path": quote.get("path")}
        if prem < -90 or prem > 2000:                     # decimals mismatch / bogus ref → never block on nonsense
            self.stats["chase_unknown"] += 1
            return {**info, "ok": True, "why": "premium out of sane range — not applied"}
        if prem > cfg["max_chase_pct"]:
            why = f"chase filter: quoted price {prem:+.1f}% vs scout fill (max +{cfg['max_chase_pct']:g}%)"
            self.stats["chase_skipped"] += 1
            logger.info("live chase skip %s %s: premium %+.1f%% > %g%%", arm, mint, prem, cfg["max_chase_pct"])
            try:
                p = Path(os.getenv("LIVE_CHASE_LOG_PATH", str(Path(config()["ledger_path"]).parent / "chase_skips.jsonl")))
                with p.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"ts_ms": int(time.time() * 1000), "arm": arm, "mint": mint, **info}) + "\n")
            except Exception:  # noqa: BLE001
                pass
            return {**info, "ok": False, "why": why}
        self.stats["chase_passed"] += 1
        return {**info, "ok": True, "why": ""}

    def _direct_live_log(self, side: str, arm: Optional[str], mint: str, d: Dict[str, Any],
                         tm: Optional[Dict[str, Any]]) -> None:
        """v1.20: every live direct attempt (incl. fallbacks) → data/direct_pump.jsonl + buy timing."""
        rec = {"kind": "live", "side": side, "arm": arm, "mint": mint, "ts_ms": int(time.time() * 1000),
               "ok": d.get("ok"), "fallback": bool(d.get("fallback")), "stage": d.get("stage"),
               "ambiguous": d.get("ambiguous"), "error": d.get("error"), "signature": d.get("signature"),
               "timing": d.get("timing"), "order": d.get("order"), "total_in": d.get("total_in"),
               "total_out": d.get("total_out")}
        pumpdirect.log(rec)
        self.stats[f"direct_{side}_{'fallback' if rec['fallback'] else 'ok' if rec['ok'] else 'not_ok'}"] += 1
        if tm is not None:
            tm["direct"] = {k: rec[k] for k in ("stage", "fallback", "error")}
            tm["direct"].update(d.get("timing") or {})

    def _direct_jup_compare(self, jq: "asyncio.Future", d: Dict[str, Any], arm: Optional[str], mint: str) -> None:
        """v1.36: Jupiter /order quote taken in parallel with a live direct buy (never signed, never executed) →
        data/direct_pump.jsonl kind=live_jup_quote with the direct fill vs that quote and the landing timings."""
        def _done(t: "asyncio.Future") -> None:
            try:
                o = (t.result() if not t.cancelled() else {}) or {}
            except Exception as exc:  # noqa: BLE001
                o = {"ok": False, "error": type(exc).__name__}
            try:
                jout = int(o.get("out_amount") or 0)
                dout = int(d.get("total_out") or 0) if (d.get("ok") and not d.get("fallback")) else 0
                tmg = d.get("timing") or {}
                pumpdirect.log({
                    "kind": "live_jup_quote", "side": "buy", "arm": arm, "mint": mint, "ts_ms": int(time.time() * 1000),
                    "signature": d.get("signature"), "direct_ok": d.get("ok"), "fallback": bool(d.get("fallback")),
                    "stage": d.get("stage"), "direct_out": dout or None, "jup_quote_out": jout or None,
                    "jup_router": o.get("router"), "jup_quote_ms": o.get("elapsed_ms"),
                    "jup_error": None if o.get("ok") else o.get("error"),
                    "direct_vs_jup_quote_pct": round((dout - jout) / jout * 100, 3) if dout and jout else None,
                    "timing": {k: tmg.get(k) for k in ("fetch_ms", "build_ms", "sign_ms", "sim_ms", "send_ms",
                                                       "confirm_ms", "sender", "sender_ms")}})
            except Exception:  # noqa: BLE001
                pass
        jq.add_done_callback(_done)

    def _direct_shadow_finish(self, task: "asyncio.Future", res: Dict[str, Any], side: str, mint: str,
                              arm: Optional[str], jup_ms: int, ref_price: Optional[float], reason: str = "") -> None:
        def _attach(rec: Dict[str, Any]) -> None:
            if side != "buy":
                return
            pos = next((x for x in self.positions.values() if x.mint == mint and x.status != "closed"), None)
            if pos is not None:
                pos.meta["direct_shadow"] = {k: rec.get(k) for k in (
                    "eligible", "why", "sim_ok", "sim_err", "ready_ms", "sim_ms", "expected_out", "sim_out",
                    "direct_vs_jup_pct", "direct_vs_scout_pct", "jup_vs_scout_pct")}
        fut = asyncio.ensure_future(pumpdirect.shadow_finish(task, res, side=side, mint=mint, arm=arm, jup_ms=jup_ms,
                                                             ref_price_sol=ref_price, on_record=_attach,
                                                             reason=reason))
        fut.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

    def _record_sell(self, p: LivePos, tokens: int, lamports: int, reason: str, slip: int, sig: Optional[str],
                     res: Dict[str, Any], flags: Optional[List[str]] = None) -> None:
        tokens = min(int(tokens), p.tokens_left) if p.tokens_left else int(tokens)
        basis = p.cost_basis(tokens)
        p.tokens_left -= tokens
        p.received_lamports += int(lamports)
        pnl = (int(lamports) - basis) / 1e9
        if self._loss_counted(p.arm):                    # v1.35d exempt lanes stay out of the shared loss limit
            self.day()["realized_pnl_sol"] = round(self.day()["realized_pnl_sol"] + pnl, 6)
            self.window_events.append({"k": "p", "ts": int(time.time() * 1000), "m": p.mint, "pnl": round(pnl, 6)})
        fill = {"ts_ms": int(time.time() * 1000), "reason": reason, "tokens": tokens, "sol": round(lamports / 1e9, 6),
                "pnl_sol": round(pnl, 6), "slippage_bps": slip, "signature": sig, "flags": flags or [],
                "router": (res.get("order") or {}).get("router")}
        p.sells.append(fill)
        p.exit_pending = None
        if not is_partial(reason):
            p.ambiguous_sell = None
        self.stats["sells"] += 1
        self._ledger("sell", id=p.id, arm=p.arm, mint=p.mint, **fill)
        self._alert("live_sell", arm=p.arm, mint=p.mint, sol=fill["sol"], pnl_sol=fill["pnl_sol"], reason=reason,
                    signature=sig)
        if p.tokens_left <= 0 or not is_partial(reason):
            if p.tokens_left > 0 and not is_partial(reason):
                p.exit_pending = reason       # partial fill on a full exit → keep selling the rest
            else:
                self._close(p, reason)
        self.save()

    def _close(self, p: LivePos, reason: str) -> None:
        self._urgent_retry.discard(p.id)
        p.status, p.exit_reason, p.closed_ms, p.exit_pending = "closed", reason, int(time.time() * 1000), None
        self._ledger("close", id=p.id, arm=p.arm, mint=p.mint, reason=reason, pnl=p.pnl(),
                     peak=_peak_info(p))                       # v1.32 logging only (never read by decisions)
        try:                                                     # v1.21 scout benching (live copy outcome)
            scout_bench.bench.record_live(p)
        except Exception as exc:  # noqa: BLE001
            logger.warning("scout bench record failed: %s", type(exc).__name__)
        self._unpin_stream(p.mint)
        self.save()

    def ensure_loop(self) -> None:
        if self._loop and not self._loop.done():
            return
        try:
            self._loop = asyncio.get_running_loop().create_task(self._mark_loop())
        except RuntimeError:
            pass

    async def _mark_loop(self) -> None:
        """Runs as long as ANY live position is open — no time limit (runners are monitored indefinitely)."""
        last_stream_check = 0.0
        while self.open_positions():
            ps = self.open_positions()
            try:
                if time.time() - last_stream_check >= 30:      # re-pin / restart dropped streams
                    last_stream_check = time.time()
                    for p in ps:
                        self._start_stream(p.mint)
                await self._sync_watch(ps)                    # v1.17 accountSubscribe on curve / pool
                await self.mark(ps)
                await self.rug_watch(ps)                      # v1.17 creator / linked balances
                for p in ps:                                   # v1.15: one task per position — a stuck sell
                    self._spawn(self._manage_once(p))          # can't block other positions' stops
                if self.pending_buys:
                    self.start_reconcile()
            except Exception as exc:  # noqa: BLE001
                logger.warning("live mark loop: %s", type(exc).__name__)
            await asyncio.sleep(self.poll_interval())
        await self._sync_watch([])

    def poll_interval(self) -> float:
        """v1.17: LIVE_MARK_S (2 s) while the account watcher is healthy; ~1 s poll (LIVE_MARK_FAST_S) otherwise."""
        cfg = config()
        if acct_watch.healthy():
            return cfg["mark_s"]
        return min(cfg["mark_s"], cfg["mark_fast_s"])

    # ---------------- kill ----------------
    def kill(self, *, sell_all: bool = False) -> Dict[str, Any]:
        self.api_kill = True
        self.api_kill_sell_all = bool(sell_all)
        self.save()
        self._alert("live_kill", sell_all=sell_all)
        if sell_all:
            for p in self.open_positions():
                self._spawn(self._sell(p, 1.0, "kill_switch_sell_all"))
        return self.killed()

    def resume(self) -> Dict[str, Any]:
        self.api_kill = False
        self.api_kill_sell_all = False
        self.save()
        k = self.killed()
        return {**k, "note": "KILL file still present — remove it to resume" if k["file"] else "resumed"}

    # ---------------- misc ----------------
    def _alert(self, kind: str, **info: Any) -> None:
        sig = info.get("signature")
        a = {"id": uuid.uuid4().hex[:10], "ts_ms": int(time.time() * 1000), "kind": kind, **info,
             "solscan": f"https://solscan.io/tx/{sig}" if sig else None}
        self.alerts.appendleft(a)

    def _spawn(self, coro: Any) -> None:
        try:
            t = asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            coro.close()
            return
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    def summary(self) -> Dict[str, Any]:
        """Non-secret summary (safe for /health)."""
        cfg = config()
        d = self.day()
        open_ = self.open_positions()
        warn = []
        if self.state_error:
            warn.append(self.state_error)
        stale = [p for p in open_ if self.mark_stale(p)]
        if stale:
            warn.append(f"{len(stale)} live position(s) with a stale mark (> {cfg['stale_mark_s']:.0f}s): "
                        + ", ".join(p.mint[:6] for p in stale[:5]))
        if self.pending_buys:
            warn.append(f"{len(self.pending_buys)} pending/unresolved live buy(s) — reconcile running")
        if any(p.ambiguous_sell for p in open_):
            warn.append("a live partial sell is ambiguous — reconciling against chain")
        if open_ and not cfg["exec_enabled"]:
            warn.append("open live positions but JUPITER_EXEC_ENABLED=false — exits cannot sign")
        if any(p.exit_pending for p in open_):
            warn.append("a live exit is pending/retrying")
        return {"buys_enabled": cfg["exec_enabled"] and cfg["autotrade_enabled"],
                "exec_enabled": cfg["exec_enabled"], "autotrade_enabled": cfg["autotrade_enabled"],
                "arms": cfg["arms"], "kill": self.killed()["active"], "open_positions": len(open_),
                "trades_today": d["trades"], "realized_pnl_today_sol": d["realized_pnl_sol"],
                "trade_cap": self.trade_cap(cfg),
                "limit_window": {k: v for k, v in self.limit_window(cfg).items()},
                "creator_rug_block": {"enabled": os.getenv("LIVE_CREATOR_RUG_BLOCK", "false").strip().lower() in {"1", "true", "yes", "on"}, "refused": self.stats.get("creator_rug_refused", 0)},
                "chase_filter": {"enabled": cfg["chase_filter_enabled"], "max_chase_pct": cfg["max_chase_pct"],
                                 "arms": cfg["chase_arms"], "skips": self.stats.get("chase_skipped", 0)},
                "fast_cut": {"enabled": cfg["fast_cut_enabled"], "pct": cfg["fast_cut_pct"],
                             "window_s": cfg["fast_cut_window_s"]},
                "scout_bench": scout_bench.bench.summary(short=True),
                "unrealized_open_sol": round(sum(p.pnl()["unrealized_sol"] for p in open_), 6),
                "inflight_buys": len(self.inflight), "pending_buys": len(self.pending_buys),
                "kill_sell_all": bool(self.api_kill_sell_all),
                "tg_rules": {a: cfg["rules"][a] for a in TG_ARMS},
                "rugcheck": {k: v for k, v in rugcheck.checker.summary().items() if k != "recent"},
                "exits": {"watch": acct_watch.status(), "poll_s": self.poll_interval(), "gap_pct": cfg["gap_pct"],
                          "fast_tp_enabled": cfg["fast_tp_enabled"], "trail_pct": cfg["trail_pct"]},
                "v133": {"trail_pct": cfg["trail_pct"], "tg_trail_pct": cfg.get("tg_trail_pct"),
                         "hard_stop_pct": cfg["hard_stop_pct"],
                         "candle_stop": {"enabled": cfg.get("candle_stop_enabled"), "s": cfg.get("candle_stop_s"),
                                         "buf_pct": cfg.get("candle_stop_buf_pct"), "arms": cfg.get("candle_stop_arms")},
                         "trader_sell_window_s": cfg.get("trader_sell_window_s"),
                         "night_pause": {"enabled": cfg.get("night_pause_enabled"), "start_h": cfg.get("night_pause_start_h"),
                                         "end_h": cfg.get("night_pause_end_h"), "exempt": cfg.get("night_pause_exempt_arms"),
                                         "active_now_scout_solo": night_paused("scout_solo", cfg)},
                         "max_chase_pct": cfg["max_chase_pct"]},
                "fastfill": fastfill.summary(),
                "nonsol": {k: v for k, v in nonsol.summary().items() if k in ("enabled", "fraction", "max_sol")}
                | {"quotes_allowed": sorted(nonsol.summary()["quotes_allowed"]),
                   "open": sum(1 for p in open_ if self.is_nonsol(p))},
                "warnings": warn}

    def status(self) -> Dict[str, Any]:
        cfg = config()
        d = self.day()
        return {**self.summary(), "wallet": jupiter.pubkey(), "sol_balance_cached": self._balance.get("sol"),
                "kill": self.killed(),
                "caps": {"max_sol_per_trade": cfg["max_sol_per_trade"],
                         "open": f"{len(self.open_positions())}/{cfg['max_open_positions']}",
                         "trades_today": self.trade_cap(cfg)["display"],
                         "loss_today_sol": f"{self.limit_window(cfg)['realized_pnl_sol'] + self.unrealized_today_sol():.4f} / -{cfg['daily_loss_limit_sol']}",
                         "limit_window": cfg["limit_window"],
                         "min_sol_reserve": cfg["min_sol_reserve"], "buy_slippage_bps": cfg["buy_slippage_bps"],
                         "sell_slippage_steps_bps": cfg["sell_slippage_steps_bps"],
                         "max_priority_lamports": cfg["max_priority_lamports"]},
                "sizes_sol": {a: min(cfg["sizes_sol"][a], cfg["max_sol_per_trade"]) for a in cfg["arms"]},
                "rules": {a: cfg["rules"][a] for a in cfg["arms"]},
                "positions": [{**asdict(p), "pnl": p.pnl(),
                               "runner": is_runner(p, p.last_price_sol, cfg, cfg["rules"].get(p.arm, []))} for p in sorted(self.positions.values(),
                                                                          key=lambda x: -x.entry_ms)][:50],
                "decisions": list(self.decisions)[:40], "stats": dict(self.stats), "bought_mints": len(self.bought_mints)}


engine = LiveEngine()


def arm_for_telegram(source: str, meta: Dict[str, Any]) -> Optional[str]:
    s, g = (source or "").lower(), str((meta or {}).get("source_group") or "").lower()
    if "zyrox" in g or "zyrox" in s or "solanainsider" in g:
        return "zyrox"
    if "fluff" in s or "fluff" in g:
        return "fluffy_pump_dump"
    return None
