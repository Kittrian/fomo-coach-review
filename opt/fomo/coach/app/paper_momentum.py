"""v1.22 `momentum_paper` — PAPER-ONLY 7-day momentum arm (Sky, 2026-10-09 3:17 AM CT).

Goal: anticipate pumps on coins minutes→hours old (bonding curve AND graduated) from four signals:
  1. cluster  — ≥ MOMENTUM_CLUSTER_N ranked wallets (data/wallet_scores.json: FOMO + scouts + confirmers) buy the
                same coin within MOMENTUM_CLUSTER_WINDOW_MIN, weighted by wallet score. Fed by the EXISTING wallet
                feeds (fomo-onchain source + scout engine) — no new subscriptions.
  2. chatter  — Telegram CA / $TICKER mention spike (posted by the fomo-telegram-pump watcher to
                POST /api/paper/momentum/mentions) and, only if X_BEARER_TOKEN is set and MOMENTUM_X_ENABLED=true,
                X recent post counts (GET /2/tweets/counts/recent — $0.005 per request, capped per day).
  3. flow     — buys / volume per minute accelerating (DexScreener m5 vs h1) + unique buyers accelerating
                (GeckoTerminal pool trades), while the price has NOT already run more than MOMENTUM_MAX_RUNUP_PCT.
  4. health   — the live rug check (own RugChecker instance on a PUBLIC RPC → 0 Helius credits) + liquidity
                trending up / not being pulled (DexScreener liquidity snapshots).
Score = Σ wᵢ·sᵢ / Σ wᵢ (MOMENTUM_W_*), paper entry when score ≥ MOMENTUM_THRESHOLD and no veto.

Hard guarantees: PAPER ONLY. Never imports the signer or any swap / direct-trade module, never reads the wallet key, never touches
the live engine's positions, counters, caps, loss limit, kill switch or wallet. Off switch PAPER_MOMENTUM_ENABLED
(default false in code). Every scored coin is logged (data/momentum_scores.jsonl, throttled per coin) so the
thresholds can be tuned later; paper trades → data/paper_momentum.jsonl.
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
from typing import Any, Deque, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import httpx

from app.paths import data_dir

logger = logging.getLogger("coach.paper.momentum")

ARM = "momentum_paper"
TRACKS = ("live_exit", "t30")
TZ = ZoneInfo("America/Chicago")
DECIMALS = 6
PUBLIC_RPC = "https://solana-rpc.publicnode.com"
DEX_URL = "https://api.dexscreener.com/tokens/v1/solana/"
GT_URL = "https://api.geckoterminal.com/api/v2/networks/solana/pools/{pool}/trades"
X_COUNTS_URL = "https://api.x.com/2/tweets/counts/recent"
TWITTERAPI_IO_URL = "https://api.twitterapi.io/twitter/tweet/advanced_search"
# v1.23: tickers too generic to search on their own (CA is always searched)
COMMON_TICKERS = {
    "THE", "AND", "FOR", "YOU", "CAT", "DOG", "PEPE", "MOON", "PUMP", "SOL", "SOLANA", "BONK", "WIF", "TRUMP", "ELON",
    "DOGE", "INU", "APE", "GEM", "BUY", "SELL", "NEW", "CEO", "USA", "BTC", "ETH", "USDC", "USDT", "USD1", "MEME",
    "COIN", "TOKEN", "BASED", "CHAD", "FROG", "BABY", "KING", "GOD", "LOL", "WTF", "HODL", "SEND", "RICH", "CASH",
    "MONEY", "BULL", "BEAR", "PUMPFUN", "AI", "GM", "GN", "TEST", "LIVE", "NEWS", "ALPHA", "CALL", "CALLS",
}


def _f(name: str, d: float) -> float:
    try:
        return float(os.getenv(name, str(d)).strip())
    except ValueError:
        return d


def _on(name: str, d: str = "false") -> bool:
    return os.getenv(name, d).strip().lower() in {"1", "true", "yes", "on"}


def cfg() -> Dict[str, Any]:
    return {
        "enabled": _on("PAPER_MOMENTUM_ENABLED"),
        "days": _f("PAPER_MOMENTUM_DAYS", 7),
        "size_sol": _f("PAPER_MOMENTUM_SIZE_SOL", 0.20),
        "cost_bps": int(_f("PAPER_MOMENTUM_COST_BPS", 150)),            # per side
        "max_open": int(_f("PAPER_MOMENTUM_MAX_OPEN", 20)),               # paper-book bound only
        # signal 1: smart-money cluster
        "cluster_n": int(_f("MOMENTUM_CLUSTER_N", 3)),
        "cluster_window_min": _f("MOMENTUM_CLUSTER_WINDOW_MIN", 10),
        "wallet_score_weight": _f("MOMENTUM_WALLET_SCORE_WEIGHT", 4.0),  # weight = 1 + this × max(score, 0)
        # signal 2: chatter
        "tg_enabled": _on("MOMENTUM_TG_ENABLED", "true"),
        "chatter_window_min": _f("MOMENTUM_CHATTER_WINDOW_MIN", 10),
        "chatter_baseline_min": _f("MOMENTUM_CHATTER_BASELINE_MIN", 60),
        "tg_min_mentions": int(_f("MOMENTUM_TG_MIN_MENTIONS", 2)),
        "tg_full_chats": _f("MOMENTUM_TG_FULL_CHATS", 4),                 # distinct chats in window for s=1
        "x_enabled": _on("MOMENTUM_X_ENABLED") and bool(os.getenv("TWITTERAPI_IO_KEY", "").strip()
                                                        or os.getenv("X_BEARER_TOKEN", "").strip()),
        "x_flag": _on("MOMENTUM_X_ENABLED"),
        "x_token_set": bool(os.getenv("X_BEARER_TOKEN", "").strip()),
        "x_twitterapi_set": bool(os.getenv("TWITTERAPI_IO_KEY", "").strip()),
        # v1.23: twitterapi.io advanced_search (preferred when its key is set) — one page, since_time window
        "x_provider": ("twitterapi_io" if os.getenv("TWITTERAPI_IO_KEY", "").strip() else
                       "x_counts" if os.getenv("X_BEARER_TOKEN", "").strip() else None),
        "x_window_min": _f("MOMENTUM_X_WINDOW_MIN", 30),                   # twitterapi.io since_time lookback
        # v1.23: Telegram PUBLIC search (watcher's user session does the searching; coach only queues + scores)
        "tg_global_enabled": _on("MOMENTUM_TG_GLOBAL_ENABLED"),
        "tg_global_pre": _f("MOMENTUM_TG_GLOBAL_PRE", 0.40),               # pre-score needed before a search
        "tg_global_window_min": _f("MOMENTUM_TG_GLOBAL_WINDOW_MIN", 30),
        "tg_global_recheck_min": _f("MOMENTUM_TG_GLOBAL_RECHECK_MIN", 10),  # per coin
        "tg_global_stale_min": _f("MOMENTUM_TG_GLOBAL_STALE_MIN", 20),      # older result → unknown
        "tg_global_queue_max": int(_f("MOMENTUM_TG_GLOBAL_QUEUE_MAX", 20)),
        "tg_global_full_channels": _f("MOMENTUM_TG_GLOBAL_FULL_CHANNELS", 5),
        "tg_global_full_msgs": _f("MOMENTUM_TG_GLOBAL_FULL_MSGS", 15),
        "ticker_min_len": int(_f("MOMENTUM_TICKER_MIN_LEN", 4)),
        # chatter = capped evidence sum of the KNOWN sources (unknown adds nothing and is logged as unknown)
        "chw_private": _f("MOMENTUM_CHATTER_W_PRIVATE", 1.0),
        "chw_public": _f("MOMENTUM_CHATTER_W_PUBLIC", 0.8),
        "chw_x": _f("MOMENTUM_CHATTER_W_X", 1.0),
        "x_max_calls_day": int(_f("MOMENTUM_X_MAX_CALLS_DAY", 150)),      # × $0.005 = $0.75/day max
        "x_min_interval_min": _f("MOMENTUM_X_MIN_INTERVAL_MIN", 10),       # per coin
        "x_full_posts": _f("MOMENTUM_X_FULL_POSTS", 20),                   # posts in window for s=1
        # signal 3: flow
        "min_buys_m5": _f("MOMENTUM_MIN_BUYS_M5", 10),
        "max_runup_pct": _f("MOMENTUM_MAX_RUNUP_PCT", 150),
        "gt_enabled": _on("MOMENTUM_GT_ENABLED", "true"),
        "gt_max_per_min": int(_f("MOMENTUM_GT_MAX_PER_MIN", 12)),          # GeckoTerminal free limit is 30/min
        # signal 4: health
        "rug_enabled": _on("MOMENTUM_RUG_ENABLED", "true"),
        "rug_rpc_url": os.getenv("MOMENTUM_RUG_RPC_URL", PUBLIC_RPC).strip() or PUBLIC_RPC,
        "rug_max_per_hour": int(_f("MOMENTUM_RUG_MAX_PER_HOUR", 60)),
        "liq_pull_pct": _f("MOMENTUM_LIQ_PULL_PCT", 25),
        "min_liq_usd": _f("MOMENTUM_MIN_LIQ_USD", 3000),
        "max_age_h": _f("MOMENTUM_MAX_AGE_H", 24),
        # combine
        "w_cluster": _f("MOMENTUM_W_CLUSTER", 0.35),
        "w_chatter": _f("MOMENTUM_W_CHATTER", 0.15),
        "w_flow": _f("MOMENTUM_W_FLOW", 0.30),
        "w_health": _f("MOMENTUM_W_HEALTH", 0.20),
        "threshold": _f("MOMENTUM_THRESHOLD", 0.60),
        "need_core": _on("MOMENTUM_REQUIRE_CLUSTER_OR_FLOW", "true"),     # chatter + health alone never enter
        "core_min": _f("MOMENTUM_CORE_MIN", 0.6),
        # loop / cost caps
        "score_s": _f("MOMENTUM_SCORE_S", 30),
        "mark_s": _f("MOMENTUM_MARK_S", 5),
        "max_candidates": int(_f("MOMENTUM_MAX_CANDIDATES", 60)),
        "candidate_ttl_min": _f("MOMENTUM_CANDIDATE_TTL_MIN", 120),
        "score_log_every_s": _f("MOMENTUM_SCORE_LOG_EVERY_S", 120),
        "log_path": Path(os.getenv("PAPER_MOMENTUM_LOG_PATH", str(data_dir() / "paper_momentum.jsonl"))),
        "score_path": Path(os.getenv("MOMENTUM_SCORE_LOG_PATH", str(data_dir() / "momentum_scores.jsonl"))),
        "state_path": Path(os.getenv("PAPER_MOMENTUM_STATE_PATH", str(data_dir() / "paper_momentum_state.json"))),
        "wallet_scores_path": Path(os.getenv("WALLET_SCORES_PATH", str(data_dir() / "wallet_scores.json"))),
    }


def _ct(ms: Optional[int]) -> Optional[str]:
    return datetime.fromtimestamp(ms / 1000, TZ).strftime("%a %b %-d %-I:%M %p CT") if ms else None


def _clip(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


# ======================= pure signal functions (tests + replay) =======================
def cluster_signal(buys: List[Dict[str, Any]], now_ms: int, c: Dict[str, Any]) -> Dict[str, Any]:
    """buys: [{"wallet","ts","score"}]. Unique ranked wallets inside the window, score-weighted."""
    win = c["cluster_window_min"] * 60_000
    last: Dict[str, Dict[str, Any]] = {}
    for b in buys:
        if now_ms - win <= b["ts"] <= now_ms:
            last[b["wallet"]] = b
    n = len(last)
    weighted = sum(1 + c["wallet_score_weight"] * max(0.0, float(b.get("score") or 0)) for b in last.values())
    s = _clip(weighted / (max(1, c["cluster_n"]) * 1.5))
    return {"s": round(s, 4), "wallets": n, "weighted": round(weighted, 3), "met": n >= c["cluster_n"],
            "labels": [b.get("label") or b["wallet"][:6] for b in last.values()][:8]}


def distinctive_ticker(sym: Optional[str], min_len: int = 4) -> Optional[str]:
    """$TICKER is only searched when it is distinctive (long enough, not a common word / major coin)."""
    t = (sym or "").strip().upper().lstrip("$")
    if not (min_len <= len(t) <= 12) or not t.isalnum() or t.isdigit() or t in COMMON_TICKERS:
        return None
    return t


def public_signal(pub: Optional[Dict[str, Any]], now_ms: int, c: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Telegram PUBLIC search result → sub-signal, or None when unknown (failed / stale / never searched)."""
    if not pub or pub.get("unknown") or now_ms - int(pub.get("ts") or 0) > c["tg_global_stale_min"] * 60_000:
        return None
    ch, msgs = float(pub.get("channels") or 0), float(pub.get("messages") or 0)
    s = 0.7 * _clip(ch / max(1.0, c["tg_global_full_channels"])) + 0.3 * _clip(msgs / max(1.0, c["tg_global_full_msgs"]))
    return {"s": round(s, 4), "channels": int(ch), "messages": int(msgs), "method": pub.get("method"),
            "capped": bool(pub.get("capped"))}


def chatter_signal(tg: List[Dict[str, Any]], x: Optional[Dict[str, Any]], now_ms: int,
                   c: Dict[str, Any], pub: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """tg: [{"ts","chat"}] private-chat mentions (CA or ticker). x: {"window","baseline"|"baseline_per_window"} or
    {"unknown": True}. pub: Telegram public-search result. Chatter = capped weighted evidence sum of KNOWN sources."""
    win, base = c["chatter_window_min"] * 60_000, c["chatter_baseline_min"] * 60_000
    w = [m for m in tg if now_ms - win <= m["ts"] <= now_ms]
    b = [m for m in tg if now_ms - win - base <= m["ts"] < now_ms - win]
    chats = len({m.get("chat") for m in w})
    base_per_win = len(b) * win / base if base else 0.0
    spike = len(w) >= c["tg_min_mentions"] and len(w) >= 2 * base_per_win + 1
    s_tg = _clip(chats / max(1.0, c["tg_full_chats"])) * (1.0 if spike else 0.4) if w else 0.0
    s_x, xs = 0.0, None
    if x and x.get("unknown"):
        xs = {"unknown": True, "error": x.get("error")}
    elif x and x.get("window") is not None:
        xw = float(x["window"])
        if x.get("baseline_per_window") is not None:
            xb = float(x["baseline_per_window"])
        else:
            xb = float(x.get("baseline") or 0) * win / base if base else 0.0
        x_spike = xw >= 3 and xw >= 2 * xb + 1
        s_x = _clip(xw / max(1.0, c["x_full_posts"])) * (1.0 if x_spike else 0.4)
        xs = {"window": xw, "baseline_per_window": round(xb, 2), "spike": x_spike, "s": round(s_x, 4),
              "authors": x.get("authors"), "provider": x.get("provider"), "capped": x.get("capped")}
    ps = public_signal(pub, now_ms, c)
    total = c["chw_private"] * s_tg + c["chw_x"] * s_x + (c["chw_public"] * ps["s"] if ps else 0.0)
    pub_out = ps if ps else ({"unknown": True, "error": (pub or {}).get("error") or
                              ("stale" if pub and not pub.get("unknown") else None)} if pub else None)
    return {"s": round(_clip(total), 4), "tg_mentions": len(w), "tg_chats": chats, "s_private": round(s_tg, 4),
            "tg_baseline_per_window": round(base_per_win, 2), "tg_spike": spike, "x": xs, "tg_public": pub_out}


def flow_signal(dx: Optional[Dict[str, Any]], gt: Optional[Dict[str, Any]], c: Dict[str, Any],
                runup_pct: Optional[float] = None) -> Dict[str, Any]:
    """dx: DexScreener pair snapshot; gt: {"ub_5m","ub_prev15"} unique buyers from GeckoTerminal trades."""
    if not dx:
        return {"s": 0.0, "why": "no market data", "veto": None}
    t = dx.get("txns") or {}
    v = dx.get("volume") or {}
    b5, b60 = float((t.get("m5") or {}).get("buys") or 0), float((t.get("h1") or {}).get("buys") or 0)
    v5, v60 = float(v.get("m5") or 0), float(v.get("h1") or 0)
    acc_b = (b5 / 5) / max(b60 / 60, 0.1)
    acc_v = (v5 / 5) / max(v60 / 60, 1.0)
    parts = [(0.4, _clip((min(acc_b, 4) - 1) / 2)), (0.3, _clip((min(acc_v, 4) - 1) / 2))]
    ub_acc = None
    if gt and gt.get("ub_5m") is not None:
        ub_acc = gt["ub_5m"] / max(gt.get("ub_prev15", 0) / 3, 1.0)
        parts.append((0.3, _clip((min(ub_acc, 4) - 1) / 2)))
    s = sum(w * x for w, x in parts) / sum(w for w, _ in parts)
    if b5 < c["min_buys_m5"]:
        s *= b5 / max(1.0, c["min_buys_m5"])
    pc = (dx.get("priceChange") or {})
    run = max([x for x in (runup_pct, pc.get("h1")) if x is not None] or [0.0])
    veto = f"already ran +{run:.0f}% (max {c['max_runup_pct']:g}%)" if run > c["max_runup_pct"] else None
    return {"s": round(s, 4), "buys_m5": b5, "buys_h1": b60, "acc_buys": round(acc_b, 2), "acc_vol": round(acc_v, 2),
            "ub_acc": round(ub_acc, 2) if ub_acc is not None else None, "runup_pct": round(run, 1), "veto": veto}


def health_signal(rug: Optional[Dict[str, Any]], liq_hist: List[Tuple[int, float]], now_ms: int,
                  c: Dict[str, Any]) -> Dict[str, Any]:
    """rug: RugChecker.prebuy result or None (not checked yet). liq_hist: [(ms, liquidity_usd)]."""
    veto = None
    if rug is None:
        s_rug, rug_state = 0.5, "not_checked"
    elif rug.get("refuse"):
        s_rug, rug_state, veto = 0.0, "fail", "rug: " + "; ".join(rug["refuse"])[:160]
    elif rug.get("unknown"):
        s_rug, rug_state = 0.6, "unknown"
    else:
        s_rug, rug_state = 1.0, "pass"
    liq_now = liq_hist[-1][1] if liq_hist else None
    recent = [l for ts, l in liq_hist if now_ms - 15 * 60_000 <= ts]
    peak = max(recent) if recent else None
    first = recent[0] if recent else None
    if liq_now is None:
        s_liq, trend = 0.5, "unknown"
    elif peak and liq_now < peak * (1 - c["liq_pull_pct"] / 100):
        s_liq, trend = 0.0, "pulled"
        veto = veto or f"liquidity pulled {100 * (1 - liq_now / peak):.0f}% from its 15-min peak"
    elif liq_now < c["min_liq_usd"]:
        s_liq, trend = 0.2, "thin"
    elif first and liq_now >= first * 1.05:
        s_liq, trend = 1.0, "up"
    else:
        s_liq, trend = 0.7, "flat"
    return {"s": round(0.5 * s_rug + 0.5 * s_liq, 4), "rug": rug_state, "liq_usd": liq_now, "liq_trend": trend,
            "veto": veto}


def combine(sig: Dict[str, Dict[str, Any]], c: Dict[str, Any]) -> Dict[str, Any]:
    w = {"cluster": c["w_cluster"], "chatter": c["w_chatter"], "flow": c["w_flow"], "health": c["w_health"]}
    tot = sum(w.values()) or 1.0
    score = sum(w[k] * float(sig[k]["s"]) for k in w) / tot
    vetoes = [v for v in (sig["flow"].get("veto"), sig["health"].get("veto")) if v]
    core = max(float(sig["cluster"]["s"]), float(sig["flow"]["s"]))
    why = []
    if vetoes:
        why += vetoes
    if c["need_core"] and core < c["core_min"]:
        why.append(f"no core signal (cluster/flow max {core:.2f} < {c['core_min']:g})")
    if score < c["threshold"]:
        why.append(f"score {score:.3f} < {c['threshold']:g}")
    return {"score": round(score, 4), "trigger": not why, "why": why, "weights": w}


# ======================= the arm =======================
class PaperMomentum:
    def __init__(self) -> None:
        self.start_ms: Optional[int] = None
        self.end_ms: Optional[int] = None
        self.book: Dict[str, Dict[str, Any]] = {}
        self.entered: Dict[str, str] = {}                       # mint -> paper id (one entry per coin)
        self.cands: Dict[str, Dict[str, Any]] = {}             # mint -> candidate state
        self.mentions: Dict[str, Deque[Dict[str, Any]]] = collections.defaultdict(lambda: collections.deque(maxlen=500))
        self.stats: Dict[str, int] = collections.defaultdict(int)
        self.wallet_scores: Dict[str, Dict[str, Any]] = {}
        self._ws_loaded = 0.0
        self._gt_calls: Deque[float] = collections.deque()
        self._rug_calls: Deque[float] = collections.deque()
        self._x_day: Tuple[str, int] = ("", 0)
        self._last_log: Dict[str, Tuple[float, float]] = {}
        self._rug = None
        self._http: Optional[httpx.AsyncClient] = None
        self._loop: Optional[asyncio.Task] = None
        self._mark_task: Optional[asyncio.Task] = None
        self.loaded = False
        self.last_tg_ms: Optional[int] = None
        self.tg_chats_seen: set = set()
        self.tgq: Dict[str, Dict[str, Any]] = {}               # v1.23 public-search queue (mint -> request)
        self.tg_last_result_ms: Optional[int] = None
        self.tg_watcher: Dict[str, Any] = {}                    # limiter status reported by the watcher

    # ---------------- persistence / window ----------------
    def load(self) -> None:
        c = cfg()
        self.loaded = True
        try:
            st = json.loads(c["state_path"].read_text())
        except Exception:  # noqa: BLE001
            st = {}
        from app.live.engine import LivePos
        self.start_ms, self.end_ms = st.get("start_ms"), st.get("end_ms")
        self.entered = dict(st.get("entered") or {})
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
                      end_ct=_ct(self.end_ms), size_sol=c["size_sol"], cost_bps=c["cost_bps"])
            logger.info("momentum_paper started %s → ends %s", _ct(self.start_ms), _ct(self.end_ms))
        self.save()

    def save(self) -> None:
        c = cfg()
        try:
            c["state_path"].parent.mkdir(parents=True, exist_ok=True)
            tmp = c["state_path"].with_suffix(".tmp")
            tmp.write_text(json.dumps({
                "arm": ARM, "paper_only": True, "start_ms": self.start_ms, "end_ms": self.end_ms,
                "entered": self.entered,
                "open": {pid: {"meta": b["meta"], "tracks": {t: asdict(p) for t, p in b["tracks"].items()}}
                         for pid, b in self.book.items()}}, default=str))
            tmp.replace(c["state_path"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("momentum_paper save failed: %s", type(exc).__name__)

    def _append(self, path: Path, rec: Dict[str, Any]) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, default=str) + "\n")
        except Exception as exc:  # noqa: BLE001
            logger.warning("momentum_paper log failed: %s", type(exc).__name__)

    def _log(self, kind: str, **rec: Any) -> None:
        self._append(cfg()["log_path"], {"ts_ms": int(time.time() * 1000), "kind": kind, "arm": ARM, "paper": True, **rec})

    def active(self, now_ms: Optional[int] = None) -> bool:
        c = cfg()
        if not c["enabled"]:
            return False
        if not self.loaded:
            self.load()
        now_ms = now_ms or int(time.time() * 1000)
        return bool(self.start_ms and self.end_ms and self.start_ms <= now_ms < self.end_ms)

    # ---------------- wallet scores ----------------
    def _scores(self) -> Dict[str, Dict[str, Any]]:
        if time.time() - self._ws_loaded > 600:
            self._ws_loaded = time.time()
            try:
                d = json.loads(cfg()["wallet_scores_path"].read_text())
                self.wallet_scores = {w["wallet"]: w for w in d.get("wallets") or [] if w.get("wallet")}
            except Exception:  # noqa: BLE001
                pass
        return self.wallet_scores

    # ---------------- inputs ----------------
    def on_wallet_buy(self, wallet: str, mint: str, *, source: str, label: Optional[str] = None,
                      ts_ms: Optional[int] = None, symbol: Optional[str] = None) -> Optional[str]:
        """Called by the existing wallet feeds (fomo-onchain source, scout engine). Sync + cheap."""
        if not mint or not self.active():
            return None
        sc = self._scores()
        if wallet not in sc:
            return "unranked"
        now = int(ts_ms or time.time() * 1000)
        cd = self._cand(mint, now, f"wallet:{source}")
        if symbol and not cd.get("symbol"):
            cd["symbol"] = symbol
        cd["buys"].append({"wallet": wallet, "ts": now, "score": sc[wallet].get("score"),
                           "label": label or sc[wallet].get("label"), "src": source})
        self.stats["wallet_buys"] += 1
        return "noted"

    def on_mentions(self, items: List[Dict[str, Any]]) -> Dict[str, int]:
        """Telegram mentions from the watcher: [{"mint"?, "symbol"?, "chat", "ts_ms"}]. No message text is stored."""
        if not cfg()["enabled"]:
            return {"accepted": 0, "ignored": len(items)}
        now = int(time.time() * 1000)
        n = 0
        for it in items[:500]:
            ts = int(it.get("ts_ms") or now)
            chat = str(it.get("chat") or "?")[:80]
            mint, sym = it.get("mint"), (it.get("symbol") or "").upper().lstrip("$")[:16] or None
            if not mint and not sym:
                continue
            rec = {"ts": ts, "chat": chat, "mint": mint, "symbol": sym}
            if mint:
                self.mentions[mint].append(rec)
                if self.active():
                    cd = self._cand(mint, ts, "telegram")
                    if sym and not cd.get("symbol"):
                        cd["symbol"] = sym
            if sym:
                self.mentions["$" + sym].append(rec)
            self.tg_chats_seen.add(chat)
            n += 1
        self.stats["tg_mentions"] += n
        self.last_tg_ms = now if n else self.last_tg_ms
        return {"accepted": n, "ignored": len(items) - n}

    def _cand(self, mint: str, now: int, why: str) -> Dict[str, Any]:
        cd = self.cands.get(mint)
        if cd is None:
            cd = {"mint": mint, "first_ms": now, "last_ms": now, "via": why, "buys": [], "liq": [], "dx": None,
                  "gt": None, "rug": None, "rug_ms": 0, "x": None, "x_ms": 0, "first_px": None, "symbol": None}
            self.cands[mint] = cd
            self.stats["candidates"] += 1
        cd["last_ms"] = max(cd["last_ms"], now)
        return cd

    def _tg_for(self, cd: Dict[str, Any]) -> List[Dict[str, Any]]:
        seen, out = set(), []
        keys = [cd["mint"]] + (["$" + cd["symbol"].upper()] if cd.get("symbol") else [])
        for k in keys:
            for m in self.mentions.get(k, ()):
                key = (m["ts"], m["chat"])
                if key not in seen:
                    seen.add(key)
                    out.append(m)
        return out

    # ---------------- scoring ----------------
    def score(self, cd: Dict[str, Any], now_ms: int) -> Dict[str, Any]:
        c = cfg()
        runup = None
        px = self._px_usd(cd.get("dx"))
        if px and cd.get("first_px"):
            runup = (px / cd["first_px"] - 1) * 100
        sig = {"cluster": cluster_signal(cd["buys"], now_ms, c),
               "chatter": chatter_signal(self._tg_for(cd) if c["tg_enabled"] else [], cd.get("x"), now_ms, c,
                                          cd.get("tg_pub") if c["tg_global_enabled"] else None),
               "flow": flow_signal(cd.get("dx"), cd.get("gt"), c, runup),
               "health": health_signal(cd.get("rug"), cd["liq"], now_ms, c)}
        dx = cd.get("dx") or {}
        if dx.get("pairCreatedAt") and now_ms - int(dx["pairCreatedAt"]) > c["max_age_h"] * 3600_000:
            sig["flow"]["veto"] = sig["flow"].get("veto") or f"pair older than {c['max_age_h']:g} h"
        out = combine(sig, c)
        return {**out, "signals": sig}

    def _px_usd(self, dx: Optional[Dict[str, Any]]) -> Optional[float]:
        try:
            return float(dx["priceUsd"]) if dx and dx.get("priceUsd") else None
        except (TypeError, ValueError):
            return None

    def _log_score(self, cd: Dict[str, Any], res: Dict[str, Any], now_ms: int, force: bool = False) -> None:
        c = cfg()
        last = self._last_log.get(cd["mint"])
        if not force and last and now_ms / 1000 - last[0] < c["score_log_every_s"] and abs(res["score"] - last[1]) < 0.05:
            return
        self._last_log[cd["mint"]] = (now_ms / 1000, res["score"])
        dx = cd.get("dx") or {}
        self._append(c["score_path"], {
            "ts_ms": now_ms, "mint": cd["mint"], "symbol": cd.get("symbol"), "via": cd.get("via"),
            "age_min": round((now_ms - int(dx["pairCreatedAt"])) / 60_000, 1) if dx.get("pairCreatedAt") else None,
            "dex": dx.get("dexId"), "mcap_usd": dx.get("marketCap"), "price_usd": dx.get("priceUsd"),
            "score": res["score"], "trigger": res["trigger"], "why": res["why"],
            "s": {k: v["s"] for k, v in res["signals"].items()},
            "chatter_counts": {"private_mentions": res["signals"]["chatter"].get("tg_mentions"),
                               "private_chats": res["signals"]["chatter"].get("tg_chats"),
                               "tg_public": res["signals"]["chatter"].get("tg_public"),
                               "tg_public_usernames": ((cd.get("tg_pub") or {}).get("usernames") or [])[:10] or None,
                               "x": res["signals"]["chatter"].get("x")},
            "signals": res["signals"], "paper": True})
        self.stats["scores_logged"] += 1

    # ---------------- network (free sources; public RPC for rug) ----------------
    async def _client(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(timeout=8.0, headers={"accept": "application/json"})
        return self._http

    async def fetch_dex(self, mints: List[str]) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        cl = await self._client()
        for i in range(0, len(mints), 30):
            chunk = mints[i:i + 30]
            try:
                self.stats["dex_calls"] += 1
                r = await cl.get(DEX_URL + ",".join(chunk))
                r.raise_for_status()
                for p in r.json() or []:
                    m = (p.get("baseToken") or {}).get("address")
                    if m in chunk and (p.get("quoteToken") or {}).get("symbol") in ("SOL", "WSOL", "USDC", "USD1"):
                        best = out.get(m)
                        if not best or float((p.get("liquidity") or {}).get("usd") or 0) > \
                                float((best.get("liquidity") or {}).get("usd") or 0):
                            out[m] = p
            except Exception as exc:  # noqa: BLE001
                self.stats["dex_errors"] += 1
                logger.debug("momentum dex: %s", type(exc).__name__)
            if i + 30 < len(mints):
                await asyncio.sleep(1.0)
        return out

    def _gt_budget(self) -> bool:
        now = time.time()
        while self._gt_calls and now - self._gt_calls[0] > 60:
            self._gt_calls.popleft()
        if len(self._gt_calls) >= cfg()["gt_max_per_min"]:
            return False
        self._gt_calls.append(now)
        return True

    async def fetch_gt(self, pool: str, now_ms: int) -> Optional[Dict[str, Any]]:
        if not pool or not self._gt_budget():
            return None
        try:
            self.stats["gt_calls"] += 1
            r = await (await self._client()).get(GT_URL.format(pool=pool), params={"trade_volume_in_usd_greater_than": 0})
            r.raise_for_status()
            rows = (r.json() or {}).get("data") or []
        except Exception as exc:  # noqa: BLE001
            self.stats["gt_errors"] += 1
            logger.debug("momentum gt: %s", type(exc).__name__)
            return None
        return unique_buyers(rows, now_ms)

    async def rug(self, mint: str) -> Optional[Dict[str, Any]]:
        c = cfg()
        if not c["rug_enabled"]:
            return {"ok": True, "refuse": [], "unknown": [], "disabled": True}
        now = time.time()
        while self._rug_calls and now - self._rug_calls[0] > 3600:
            self._rug_calls.popleft()
        if len(self._rug_calls) >= c["rug_max_per_hour"]:
            self.stats["rug_capped"] += 1
            return None
        self._rug_calls.append(now)
        if self._rug is None:
            from app.live.rugcheck import RugChecker
            rc = RugChecker()

            async def public_rpc(method: str, params: List[Any]) -> Any:     # never the Helius URL
                from app.pump import rpc as _rpc
                try:
                    return await _rpc.call(method, params, client=await rc.client(), url=cfg()["rug_rpc_url"], timeout=4.0)
                except Exception:  # noqa: BLE001
                    rc.stats["rpc_errors"] += 1
                    return None
            rc.rpc = public_rpc                                      # type: ignore[method-assign]
            self._rug = rc
        self.stats["rug_checks"] += 1
        try:
            return await self._rug.prebuy(mint, ARM)
        except Exception:  # noqa: BLE001
            return None

    async def fetch_x(self, cd: Dict[str, Any], now_ms: int) -> Optional[Dict[str, Any]]:
        """X chatter. twitterapi.io advanced_search (one page) when TWITTERAPI_IO_KEY is set, else official X recent
        post COUNTS. Capped per day and per coin. A failed call = unknown (not zero)."""
        c = cfg()
        if not c["x_enabled"] or now_ms - cd.get("x_ms", 0) < c["x_min_interval_min"] * 60_000:
            return cd.get("x")
        day = datetime.now(TZ).strftime("%Y-%m-%d")
        if self._x_day[0] != day:
            self._x_day = (day, 0)
        if self._x_day[1] >= c["x_max_calls_day"]:
            self.stats["x_capped"] += 1
            return cd.get("x")
        self._x_day = (day, self._x_day[1] + 1)
        cd["x_ms"] = now_ms
        if c["x_provider"] == "twitterapi_io":
            cd["x"] = await self._fetch_twitterapi(cd, now_ms, c)
            return cd["x"]
        q = cd["mint"] + (f" OR ${cd['symbol']}" if cd.get("symbol") and len(cd["symbol"]) >= 3 else "")
        from datetime import timezone as _tz
        start = datetime.fromtimestamp((now_ms - (c["chatter_window_min"] + c["chatter_baseline_min"]) * 60_000) / 1000,
                                       _tz.utc)
        try:
            self.stats["x_calls"] += 1
            r = await (await self._client()).get(
                X_COUNTS_URL, params={"query": q, "granularity": "minute",
                                      "start_time": start.strftime("%Y-%m-%dT%H:%M:%SZ")},
                headers={"Authorization": "Bearer " + os.getenv("X_BEARER_TOKEN", "").strip()})
            if r.status_code != 200:
                self.stats[f"x_http_{r.status_code}"] += 1
                cd["x"] = {"unknown": True, "error": f"http {r.status_code}"}
                return cd["x"]
            data = (r.json() or {}).get("data") or []
        except Exception as exc:  # noqa: BLE001
            self.stats["x_errors"] += 1
            logger.debug("momentum x: %s", type(exc).__name__)
            cd["x"] = {"unknown": True, "error": type(exc).__name__}
            return cd["x"]
        cut = now_ms - c["chatter_window_min"] * 60_000
        w = b = 0
        for row in data:
            try:
                ts = int(datetime.fromisoformat(row["start"].replace("Z", "+00:00")).timestamp() * 1000)
            except Exception:  # noqa: BLE001
                continue
            if ts >= cut:
                w += int(row.get("tweet_count") or 0)
            else:
                b += int(row.get("tweet_count") or 0)
        cd["x"] = {"window": w, "baseline": b, "provider": "x_counts"}
        return cd["x"]

    async def _fetch_twitterapi(self, cd: Dict[str, Any], now_ms: int, c: Dict[str, Any]) -> Dict[str, Any]:
        """twitterapi.io advanced_search: quoted CA (+ quoted $TICKER if distinctive), since_time, ONE page (≤20 posts,
        ~$0.003 max per call). Only counts / authors are kept — never post text. Failure → unknown."""
        tick = distinctive_ticker(cd.get("symbol"), c["ticker_min_len"])
        since_s = int((now_ms - c["x_window_min"] * 60_000) / 1000)
        terms = f'"{cd["mint"]}"' + (f' OR "${tick}"' if tick else "")
        q = f"({terms}) since_time:{since_s}" if tick else f"{terms} since_time:{since_s}"
        try:
            self.stats["x_calls"] += 1
            r = await (await self._client()).get(
                TWITTERAPI_IO_URL, params={"query": q, "queryType": "Latest", "cursor": ""},
                headers={"X-API-Key": os.getenv("TWITTERAPI_IO_KEY", "").strip()})
            if r.status_code != 200:
                self.stats[f"x_http_{r.status_code}"] += 1
                return {"unknown": True, "error": f"http {r.status_code}", "provider": "twitterapi_io"}
            body = r.json() or {}
            if body.get("status") == "error":
                self.stats["x_errors"] += 1
                return {"unknown": True, "error": "api error", "provider": "twitterapi_io"}
            tweets = body.get("tweets") or []
        except Exception as exc:  # noqa: BLE001
            self.stats["x_errors"] += 1
            logger.debug("momentum twitterapi: %s", type(exc).__name__)
            return {"unknown": True, "error": type(exc).__name__, "provider": "twitterapi_io"}
        cut = now_ms - c["chatter_window_min"] * 60_000
        w = b = 0
        authors: set = set()
        for t in tweets:
            try:
                ts = int(datetime.strptime(str(t.get("createdAt")), "%a %b %d %H:%M:%S %z %Y").timestamp() * 1000)
            except Exception:  # noqa: BLE001
                continue
            if ts < since_s * 1000:
                continue
            authors.add(((t.get("author") or {}).get("userName") or "?").lower())
            if ts >= cut:
                w += 1
            else:
                b += 1
        older_min = max(1.0, c["x_window_min"] - c["chatter_window_min"])
        self.stats["x_ok"] += 1
        return {"window": w, "baseline": b, "baseline_per_window": round(b * c["chatter_window_min"] / older_min, 3),
                "authors": len(authors), "capped": bool(body.get("has_next_page")), "provider": "twitterapi_io"}

    # ---------------- v1.23 Telegram PUBLIC search queue (searches run in the watcher's user session) ----------------
    def maybe_queue_tg(self, cd: Dict[str, Any], pre: Dict[str, Any], now_ms: int) -> bool:
        c = cfg()
        if not c["tg_global_enabled"] or pre["score"] < c["tg_global_pre"]:
            return False
        if pre["signals"]["flow"].get("veto") or pre["signals"]["health"].get("veto"):
            return False
        mint = cd["mint"]
        if mint in self.tgq or now_ms - int(cd.get("tg_req_ms") or 0) < c["tg_global_recheck_min"] * 60_000:
            return False
        if len(self.tgq) >= c["tg_global_queue_max"]:
            low = min(self.tgq.values(), key=lambda r: r["score"])
            if low["state"] != "queued" or low["score"] >= pre["score"]:
                self.stats["tg_global_queue_full"] += 1
                return False
            self.tgq.pop(low["mint"], None)
        tick = distinctive_ticker(cd.get("symbol"), c["ticker_min_len"])
        self.tgq[mint] = {"mint": mint, "queries": [mint] + (["$" + tick] if tick else []), "score": pre["score"],
                          "symbol": cd.get("symbol"), "req_ms": now_ms, "state": "queued", "sent_ms": None}
        cd["tg_req_ms"] = now_ms
        self.stats["tg_global_queued"] += 1
        return True

    def queue_manual(self, mint: str, symbol: Optional[str] = None) -> Dict[str, Any]:
        """Loopback-only manual request (verification). Same queue, same watcher-side rate limits."""
        if not cfg()["tg_global_enabled"] or not mint:
            return {"ok": False, "reason": "MOMENTUM_TG_GLOBAL_ENABLED is off"}
        now = int(time.time() * 1000)
        cd = self.cands.get(mint) or {"mint": mint, "symbol": symbol}
        tick = distinctive_ticker(symbol or cd.get("symbol"), cfg()["ticker_min_len"])
        self.tgq[mint] = {"mint": mint, "queries": [mint] + (["$" + tick] if tick else []), "score": 9.0,
                          "symbol": symbol or cd.get("symbol"), "req_ms": now, "state": "queued", "sent_ms": None,
                          "manual": True}
        return {"ok": True, "queued": self.tgq[mint]["queries"]}

    def tg_next(self, n: int = 1, status: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        c = cfg()
        now = int(time.time() * 1000)
        if status:
            self.tg_watcher = {**{k: status.get(k) for k in ("searches_hour", "searches_day", "max_hour", "max_day",
                                                             "min_interval_s", "disabled_until", "next_ok_ms",
                                                             "posts_remaining", "posts_daily", "posts_off_until",
                                                             "floods")}, "seen_ms": now}
        for m, r in list(self.tgq.items()):            # watcher never answered → unknown
            if r["state"] == "sent" and now - (r["sent_ms"] or now) > 5 * 60_000:
                self.tg_result([{"mint": m, "ok": False, "error": "timeout"}])
            elif r["state"] == "queued" and now - r["req_ms"] > 15 * 60_000:     # watcher limiter / off
                self.tg_result([{"mint": m, "ok": False, "error": "not searched (watcher limit or off)"}])
        if not c["tg_global_enabled"]:
            return {"ok": True, "enabled": False, "requests": []}
        out = []
        for r in sorted((r for r in self.tgq.values() if r["state"] == "queued"), key=lambda r: -r["score"])[:max(0, n)]:
            r["state"], r["sent_ms"] = "sent", now
            out.append({"mint": r["mint"], "queries": r["queries"], "window_min": c["tg_global_window_min"]})
        return {"ok": True, "enabled": True, "requests": out}

    def tg_result(self, items: List[Dict[str, Any]]) -> Dict[str, int]:
        """Watcher → counts only: {mint, ok, channels, messages, usernames[], method, capped, error}. Never text."""
        now = int(time.time() * 1000)
        n = 0
        for it in items[:50]:
            mint = str(it.get("mint") or "")
            req = self.tgq.pop(mint, None)
            if not mint:
                continue
            if it.get("ok"):
                rec = {"ts": now, "channels": int(it.get("channels") or 0), "messages": int(it.get("messages") or 0),
                       "usernames": [str(u)[:64] for u in (it.get("usernames") or [])][:20],
                       "method": str(it.get("method") or "")[:20], "capped": bool(it.get("capped")),
                       "partial": bool(it.get("partial")), "queries": len((req or {}).get("queries") or [])}
                self.stats["tg_global_ok"] += 1
            else:
                rec = {"ts": now, "unknown": True, "error": str(it.get("error") or "failed")[:80]}
                self.stats["tg_global_unknown"] += 1
            cd = self.cands.get(mint)
            if cd is not None:
                cd["tg_pub"] = rec
            self._append(cfg()["score_path"], {"ts_ms": now, "kind": "tg_public_search", "mint": mint,
                                               "symbol": (req or {}).get("symbol"), "manual": bool((req or {}).get("manual")),
                                               "result": rec, "paper": True})
            self.tg_last_result_ms = now
            n += 1
        return {"accepted": n}

    # ---------------- main loop ----------------
    def ensure_loop(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if not self._loop or self._loop.done():
            self._loop = loop.create_task(self._score_loop())
        if self.book and (not self._mark_task or self._mark_task.done()):
            self._mark_task = loop.create_task(self._mark_loop())

    async def _score_loop(self) -> None:
        while cfg()["enabled"]:
            try:
                if self.active():
                    await self.tick()
            except Exception as exc:  # noqa: BLE001
                self.stats["tick_errors"] += 1
                logger.warning("momentum tick: %s", type(exc).__name__)
            await asyncio.sleep(cfg()["score_s"])

    async def tick(self, now_ms: Optional[int] = None) -> List[str]:
        """One scoring pass over live candidates. Returns paper ids opened."""
        c = cfg()
        now = int(now_ms or time.time() * 1000)
        ttl = c["candidate_ttl_min"] * 60_000
        for m in [m for m, cd in self.cands.items() if now - cd["last_ms"] > ttl or m in self.entered]:
            self.cands.pop(m, None)
        live = sorted(self.cands.values(), key=lambda cd: -cd["last_ms"])[: c["max_candidates"]]
        if not live:
            return []
        dx = await self.fetch_dex([cd["mint"] for cd in live])
        opened: List[str] = []
        for cd in live:
            p = dx.get(cd["mint"])
            if p:
                cd["dx"] = p
                cd["symbol"] = cd.get("symbol") or (p.get("baseToken") or {}).get("symbol")
                liq = float((p.get("liquidity") or {}).get("usd") or 0)
                cd["liq"] = [x for x in cd["liq"] if now - x[0] <= 30 * 60_000] + [(now, liq)]
                if cd.get("first_px") is None:
                    cd["first_px"] = self._px_usd(p)
            pre = self.score(cd, now)
            hopeful = pre["score"] >= c["threshold"] - 0.15 and not any(
                v for v in (pre["signals"]["flow"].get("veto"),) if v)
            if hopeful and p and c["gt_enabled"]:
                cd["gt"] = await self.fetch_gt(p.get("pairAddress"), now) or cd.get("gt")
            if hopeful and c["x_enabled"]:
                await self.fetch_x(cd, now)
            self.maybe_queue_tg(cd, pre, now)
            if hopeful and (cd.get("rug") is None or now - cd.get("rug_ms", 0) > 10 * 60_000):
                r = await self.rug(cd["mint"])
                if r is not None:
                    cd["rug"], cd["rug_ms"] = r, now
            res = self.score(cd, now) if hopeful else pre
            self.stats["scored"] += 1
            self._log_score(cd, res, now, force=res["trigger"])
            if res["trigger"]:
                pid = self._enter(cd, res, now)
                if pid:
                    opened.append(pid)
        return opened

    # ---------------- paper book ----------------
    def _enter(self, cd: Dict[str, Any], res: Dict[str, Any], now: int) -> Optional[str]:
        c = cfg()
        mint = cd["mint"]
        if mint in self.entered or len(self.book) >= c["max_open"]:
            self.stats["entry_skipped"] += 1
            return None
        from app.fomo_onchain import market
        from app.live.engine import LivePos
        px_usd, sol = self._px_usd(cd.get("dx")), market.sol_usd()
        if not px_usd or not sol:
            self.stats["entry_no_price"] += 1
            return None
        px = px_usd / sol
        entry = px * (1 + c["cost_bps"] / 1e4)
        tokens = int(c["size_sol"] / entry * 10 ** DECIMALS)
        pid = f"mom-{mint[:6]}-{now % 10**8}"
        meta = {"mint": mint, "symbol": cd.get("symbol"), "score": res["score"],
                "s": {k: v["s"] for k, v in res["signals"].items()}, "signals": res["signals"],
                "dex": (cd.get("dx") or {}).get("dexId"), "via": cd.get("via"), "entry_ct": _ct(now)}
        self.book[pid] = {"meta": meta, "tracks": {
            t: LivePos(mint=mint, arm=ARM, size_sol=c["size_sol"], spent_lamports=int(c["size_sol"] * 1e9),
                       tokens_initial=tokens, tokens_left=tokens, entry_price_sol=entry, entry_ms=now, decimals=DECIMALS,
                       id=f"{pid}:{t}", high_price_sol=entry, last_price_sol=px, last_mark_ms=now,
                       mark_source="dexscreener", flags=["paper"]) for t in TRACKS}}
        self.entered[mint] = pid
        self.stats["opened"] += 1
        self._log("open", id=pid, **meta, size_sol=c["size_sol"], entry_price_sol=entry, cost_bps=c["cost_bps"])
        logger.info("momentum_paper OPEN %s %s score %.3f (cluster %.2f chatter %.2f flow %.2f health %.2f)",
                    cd.get("symbol") or "", mint[:8], res["score"], *(res["signals"][k]["s"] for k in
                                                                       ("cluster", "chatter", "flow", "health")))
        self.save()
        self.ensure_loop()
        try:                                                  # v1.37 realistic twin (on-chain entry after live latency)
            from app import paper_realism as _pr
            if _pr.enabled() and _pr.onchain_enabled():
                t = asyncio.get_running_loop().create_task(self._open_real(pid, mint, px, now))
                self._real_tasks = getattr(self, "_real_tasks", set())
                self._real_tasks.add(t)
                t.add_done_callback(self._real_tasks.discard)
        except Exception:  # noqa: BLE001
            pass
        return pid

    async def _open_real(self, pid: str, mint: str, mark_px: float, signal_ms: int) -> None:
        """v1.37: no trader fill on this arm → realistic entry only from an on-chain read at signal + live latency."""
        try:
            from app import paper_realism as _pr
            from app.live.engine import LivePos
            P = _pr.params(ARM)
            wait = (signal_ms + P["lat_ms"] - time.time() * 1000) / 1000
            if wait > 0:
                await asyncio.sleep(min(wait, 15))
            onchain = await _pr.onchain_price(mint)
            b = self.book.get(pid)
            if not b:
                return
            ef = _pr.entry_fill(ARM, None, onchain, None, P["size_sol"], P)
            info = {k: ef.get(k) for k in ("ok", "px", "src", "mark_px", "sim_px", "lat_ms", "route", "reason")}
            if not ef.get("ok"):
                self.stats["real_skipped"] += 1
                b["meta"]["real"] = {**info, "skipped": True}
                self._log("real_skip", id=pid, mint=mint, **info)
                return
            now = int(time.time() * 1000)
            epx = float(ef["px"])
            spent, tokens = _pr.entry_position(P, epx, P["size_sol"])
            if not hasattr(self, "real_ledger"):
                self.real_ledger = _pr.GateLedger()
            refused = self.real_ledger.refusals(ARM, now, would_live_buy=None, trader_px=None, entry_px=epx, P=P)
            if not refused:
                self.real_ledger.opened(pid, now)
            for t in TRACKS:
                b["tracks"][_pr.REAL_PREFIX + t] = LivePos(
                    mint=mint, arm=P["live_arm"], size_sol=P["size_sol"], spent_lamports=spent, tokens_initial=tokens,
                    tokens_left=tokens, entry_price_sol=epx, entry_ms=now, decimals=DECIMALS,
                    id=f"{pid}:{_pr.REAL_PREFIX}{t}", high_price_sol=epx, last_price_sol=onchain["px"] if onchain else mark_px,
                    last_mark_ms=now, mark_source=ef.get("src"), flags=["paper", "realistic"])
            b["meta"]["real"] = {**info, "gates_refused": refused, "gated": not refused, "size_sol": P["size_sol"],
                                 "fill_vs_legacy_pct": round((epx / b["tracks"]["live_exit"].entry_price_sol - 1) * 100, 2)}
            self.stats["real_opened"] += 1
            self._log("real_open", id=pid, mint=mint, entry_price_sol=epx, size_sol=P["size_sol"], gates_refused=refused,
                      gated=not refused, fill_vs_legacy_pct=b["meta"]["real"]["fill_vs_legacy_pct"],
                      **{k: v for k, v in info.items() if k != "px"})
            self.save()
        except Exception as exc:  # noqa: BLE001
            self.stats["real_errors"] += 1
            logger.warning("momentum realism open %s: %s", mint[:8], type(exc).__name__)

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
                        self.mark(pid, float(m["price_usd"]) / float(sol), now)
                self.save()
            except Exception as exc:  # noqa: BLE001
                logger.warning("momentum mark loop: %s", type(exc).__name__)
            await asyncio.sleep(cfg()["mark_s"])

    def mark(self, pid: str, px: float, now: int) -> None:
        b = self.book.get(pid)
        if not b:
            return
        for p in b["tracks"].values():
            if p.status == "open":
                p.last_price_sol, p.last_mark_ms = px, now
                p.high_price_sol = max(p.high_price_sol or px, px)
        self.manage(pid, now)

    def manage(self, pid: str, now: Optional[int] = None) -> None:
        from app.live.config import DEFAULT_RULES, config as live_config
        from app.live.engine import evaluate_exit, is_partial
        b = self.book.get(pid)
        if not b:
            return
        lc = live_config()
        rules = [r for r in ((lc.get("rules") or {}).get("scout_solo") or DEFAULT_RULES) if r != "trader_sells"]
        now = now or int(time.time() * 1000)
        stale = float(lc.get("stale_mark_s", 60)) * 1000
        c = cfg()
        for track, p in b["tracks"].items():
            if p.status != "open":
                continue
            real = track.startswith("real_")
            base = track[5:] if real else track
            px = None if now - (p.last_mark_ms or p.entry_ms) > stale else p.last_price_sol
            acts = []
            if base == "t30" and px and not p.tp_done and (p.high_price_sol or 0) < p.entry_price_sol * lc["tp_mult"] \
                    and px > p.entry_price_sol * (1 - lc["hard_stop_pct"]) and px <= (p.high_price_sol or px) * 0.70:
                acts = [("sell_all", 1.0, "pre2x_trail_30")]        # T30: 30% peak trail from entry before 2x
            if not acts:
                acts = evaluate_exit(p, px, now, lc, rules)
            for kind, frac, reason in acts:
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
                if real:                                          # v1.37 live sell ratio + live fees
                    from app import paper_realism as _pr
                    P = _pr.params(ARM)
                    sell_px = _pr.exit_px(P, reason, sell_px, p.entry_price_sol, lc)
                    lam = _pr.proceeds_lamports(P, tokens, sell_px)
                else:
                    lam = int(tokens / 10 ** DECIMALS * sell_px * 1e9 * (1 - c["cost_bps"] / 1e4))
                p.tokens_left -= tokens
                p.received_lamports += lam
                fill = {"ts_ms": now, "reason": reason, "tokens": tokens, "sol": round(lam / 1e9, 6),
                        "pnl_sol": round((lam - basis) / 1e9, 6), "price_sol": sell_px,
                        "multiple": round(sell_px / p.entry_price_sol, 4) if p.entry_price_sol else None}
                p.sells.append(fill)
                self._log("sell", id=pid, track=track, mint=p.mint, **fill)
                if p.tokens_left <= 0 or not is_partial(reason):
                    p.status, p.exit_reason, p.closed_ms = "closed", reason, now
                    pn = p.pnl()
                    self.stats[f"closed_{track}"] += 1
                    if real and track == "real_live_exit" and hasattr(self, "real_ledger"):
                        self.real_ledger.closed(pid, now, pn["pnl_sol"])
                    self._log("close", id=pid, track=track, mint=p.mint, symbol=b["meta"].get("symbol"), reason=reason,
                              realistic=real or None, gated=(b["meta"].get("real") or {}).get("gated") if real else None,
                              pnl_sol=pn["pnl_sol"], pnl_pct=pn["pnl_pct"], hold_s=round((now - p.entry_ms) / 1000),
                              score=b["meta"].get("score"), s=b["meta"].get("s"),
                              max_multiple=round((p.high_price_sol or 0) / p.entry_price_sol, 4) if p.entry_price_sol else None)
                    logger.info("momentum_paper CLOSE %s %s %s %+.4f SOL", track, p.mint[:8], reason, pn["pnl_sol"])
                    break
        if all(p.status == "closed" for p in b["tracks"].values()):
            self.book.pop(pid, None)
            self.save()

    # ---------------- summary ----------------
    def sources(self) -> Dict[str, Any]:
        c = cfg()
        now = int(time.time() * 1000)
        return {
            "cluster": {"wallets_ranked": len(self._scores()), "wallet_buys_seen": self.stats.get("wallet_buys", 0),
                        "n": c["cluster_n"], "window_min": c["cluster_window_min"],
                        "feeds": "fomo-onchain source (FOMO wallets) + scout engine (scouts/confirmers)"},
            "telegram": {"enabled": c["tg_enabled"], "mentions_received": self.stats.get("tg_mentions", 0),
                         "chats_seen": len(self.tg_chats_seen), "last_mention": _ct(self.last_tg_ms),
                         "feed": "fomo-telegram-pump watcher (Telethon user session) → POST /api/paper/momentum/mentions",
                         "status": ("receiving" if self.last_tg_ms and now - self.last_tg_ms < 3600_000 else
                                    "no mentions in the last hour")},
            "telegram_public": {
                "enabled": c["tg_global_enabled"], "pre_threshold": c["tg_global_pre"],
                "window_min": c["tg_global_window_min"], "recheck_min": c["tg_global_recheck_min"],
                "queued": sum(1 for r in self.tgq.values() if r["state"] == "queued"),
                "in_flight": sum(1 for r in self.tgq.values() if r["state"] == "sent"),
                "results_ok": self.stats.get("tg_global_ok", 0), "results_unknown": self.stats.get("tg_global_unknown", 0),
                "last_result": _ct(self.tg_last_result_ms), "watcher_limiter": self.tg_watcher or None,
                "status": ("off: MOMENTUM_TG_GLOBAL_ENABLED=false" if not c["tg_global_enabled"] else
                           "watcher polling" if self.tg_watcher.get("seen_ms") and now - self.tg_watcher["seen_ms"] < 120_000
                           else "waiting for watcher"),
                "feed": "watcher user session: channels.searchPosts (public channels, free daily slots only) → "
                        "messages.searchGlobal fallback; counts + channel usernames only"},
            "x": {"enabled": c["x_enabled"], "flag": c["x_flag"], "token_set": c["x_token_set"],
                  "twitterapi_io_key_set": c["x_twitterapi_set"], "provider": c["x_provider"],
                  "calls_today": self._x_day[1], "max_calls_day": c["x_max_calls_day"],
                  "status": "on (" + str(c["x_provider"]) + ")" if c["x_enabled"] else (
                      "off: no TWITTERAPI_IO_KEY / X_BEARER_TOKEN" if not c["x_provider"]
                      else "off: MOMENTUM_X_ENABLED=false (key present)"),
                  "pricing": ("twitterapi.io advanced_search: $0.15/1k posts, 1 page ≤20 posts ≈ ≤$0.003/call, "
                              "min $0.00015") if c["x_provider"] != "x_counts" else
                             "GET /2/tweets/counts/recent, pay-per-use $0.005/request"},
            "flow": {"dex_calls": self.stats.get("dex_calls", 0), "gt_calls": self.stats.get("gt_calls", 0),
                     "gt_enabled": c["gt_enabled"], "gt_max_per_min": c["gt_max_per_min"]},
            "health": {"rug_checks": self.stats.get("rug_checks", 0), "rug_rpc": "public (publicnode) — 0 Helius credits"
                       if c["rug_rpc_url"] == PUBLIC_RPC else "custom MOMENTUM_RUG_RPC_URL",
                       "rug_max_per_hour": c["rug_max_per_hour"]},
        }

    def summary(self, recent: int = 15) -> Dict[str, Any]:
        c = cfg()
        if c["enabled"] and not self.loaded:
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
        closes = [r for r in rows if r.get("kind") == "close"]

        def agg(rs: List[Dict[str, Any]]) -> Dict[str, Any]:
            n = len(rs)
            tot = sum(r.get("pnl_sol") or 0 for r in rs)
            wins = sum(1 for r in rs if (r.get("pnl_sol") or 0) > 0)
            return {"closed": n, "wins": wins, "win_rate": round(wins / n, 3) if n else None, "pnl_sol": round(tot, 6)}

        open_now = []
        for pid, b in self.book.items():
            lt = b["tracks"]["live_exit"]
            open_now.append({"id": pid, "mint": b["meta"]["mint"], "symbol": b["meta"].get("symbol"),
                             "score": b["meta"].get("score"), "age_s": round((time.time() * 1000 - lt.entry_ms) / 1000),
                             "tracks": {t: {"status": p.status, **p.pnl()} for t, p in b["tracks"].items()}})
        rec = [{"id": r["id"], "track": r["track"], "symbol": r.get("symbol") or (r.get("mint") or "")[:6],
                "mint": r.get("mint"), "reason": r.get("reason"), "pnl_sol": r.get("pnl_sol"), "pnl_pct": r.get("pnl_pct"),
                "score": r.get("score"), "max_multiple": r.get("max_multiple"), "closed_ct": _ct(r["ts_ms"])}
               for r in sorted(closes, key=lambda x: -x["ts_ms"])[: recent * 2]]
        return {"ok": True, "arm": ARM, "paper_only": True, "enabled": c["enabled"], "active": self.active(),
                "start_ct": _ct(self.start_ms), "end_ct": _ct(self.end_ms), "start_ms": self.start_ms, "end_ms": self.end_ms,
                "size_sol": c["size_sol"], "cost_bps_per_side": c["cost_bps"], "threshold": c["threshold"],
                "weights": {"cluster": c["w_cluster"], "chatter": c["w_chatter"], "flow": c["w_flow"], "health": c["w_health"]},
                "exit": "live_exit = live scout exit without trader-sells (2x half, 30% trail, 35% stop); "
                        "t30 = + 30% peak trail from entry before 2x",
                "entries": sum(1 for r in rows if r.get("kind") == "open"),
                "candidates_now": len(self.cands), "scored": self.stats.get("scored", 0),
                "scores_logged": self.stats.get("scores_logged", 0),
                "tracks": ({t: agg([r for r in closes if r.get("track") == "real_" + t]) for t in TRACKS}
                           if _realism_on() else {t: agg([r for r in closes if r.get("track") == t]) for t in TRACKS}),
                "legacy_tracks": {t: agg([r for r in closes if r.get("track") == t]) for t in TRACKS},
                "headline": {"basis": "realistic" if _realism_on() else "legacy",
                             **agg([r for r in closes if r.get("track") == ("real_live_exit" if _realism_on() else "live_exit")])},
                "realism": _realism_desc(),
                "open": open_now, "recent": rec, "sources": self.sources(), "stats": dict(self.stats),
                "note": "Paper only: no orders, no signing, not counted in live caps / loss limit / max open."}

    def health(self) -> Dict[str, Any]:
        c = cfg()
        s = self.sources() if c["enabled"] else {}
        return {"enabled": c["enabled"], "active": self.active() if c["enabled"] else False,
                "start_ms": self.start_ms, "end_ms": self.end_ms, "end_ct": _ct(self.end_ms), "open": len(self.book),
                "candidates": len(self.cands), "telegram": (s.get("telegram") or {}).get("status"),
                "telegram_public": (s.get("telegram_public") or {}).get("status"),
                "x": (s.get("x") or {}).get("status"), "stats": dict(self.stats),
                "endpoint": "/api/paper/momentum/summary"}


def _realism_on() -> bool:
    from app import paper_realism as _pr
    return _pr.enabled()


def _realism_desc() -> Dict[str, Any]:
    from app import paper_realism as _pr
    return {**_pr.describe(), "params": {k: v for k, v in _pr.params(ARM).items() if k != "live_cfg"},
            "note": "momentum has no trader fill: realistic entry needs an on-chain read, else it is skipped"}


def unique_buyers(rows: List[Dict[str, Any]], now_ms: int) -> Dict[str, Any]:
    """GeckoTerminal trades → unique buyers in the last 5 min vs the 15 min before."""
    last5, prev15 = set(), set()
    for r in rows:
        a = r.get("attributes") or {}
        if a.get("kind") != "buy":
            continue
        try:
            ts = int(datetime.fromisoformat(str(a["block_timestamp"]).replace("Z", "+00:00")).timestamp() * 1000)
        except Exception:  # noqa: BLE001
            continue
        who = a.get("tx_from_address")
        if now_ms - 5 * 60_000 <= ts <= now_ms:
            last5.add(who)
        elif now_ms - 20 * 60_000 <= ts < now_ms - 5 * 60_000:
            prev15.add(who)
    return {"ub_5m": len(last5), "ub_prev15": len(prev15)}


paper = PaperMomentum()
