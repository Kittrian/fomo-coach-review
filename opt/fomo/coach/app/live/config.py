"""Env-driven live-trading config (read at call time so tests / restarts pick changes up)."""
from __future__ import annotations

import os

import app.config  # noqa: F401  (loads .env into os.environ, same as the rest of the coach)
from pathlib import Path
from typing import Any, Dict, List

ARMS = ("fluffy_pump_dump", "fomo_top_trader", "scout_solo", "scout_confirmed", "smugcalls_control", "zyrox",
        "kol_confluence")   # v1.35 kol_confluence live lane (Andrew approved Oct 16, 2026; KOL_CONFLUENCE_LIVE_ENABLED)
# v1.15: Telegram-sourced arms — every live buy must pass the blocking pre-buy gate (app/live/tg_gate.py)
TG_ARMS = ("fluffy_pump_dump", "zyrox")
DEFAULT_RULES = ("tp2x_trail", "min10_check", "hard_stop", "trader_sells", "max_hold")
ALLOWED_RULES = DEFAULT_RULES + ("ladder",)
# v1.15: Fluffy/Zyrox restore the paper ladder (50%@2x, 25%@3x, 12.5%@5x → trail the rest, hard stop)
TG_DEFAULT_RULES = ("ladder", "hard_stop", "max_hold")
ALWAYS_RULES = ("hard_stop", "max_hold")          # never removable: every position always has an exit
APP_SUPPORT = Path(os.path.expanduser("~/Library/Application Support/FOMO-Signal-Coach"))
from app.paths import data_dir  # noqa: E402
DATA_DIR = data_dir()


def _truthy(name: str, d: str = "false") -> bool:
    return os.getenv(name, d).strip().lower() in {"1", "true", "yes", "on"}


def _f(name: str, d: float) -> float:
    try:
        return float(os.getenv(name, str(d)).strip())
    except ValueError:
        return d


def _i(name: str, d: int) -> int:
    return int(_f(name, d))


def arm_env(arm: str) -> str:
    return arm.upper()


def arm_size_sol(arm: str) -> float:
    return _f(f"LIVE_SIZE_SOL_{arm_env(arm)}", _f("LIVE_DEFAULT_SIZE_SOL", 0.10))


def arm_rules(arm: str) -> List[str]:
    default = TG_DEFAULT_RULES if arm in TG_ARMS else DEFAULT_RULES
    glob = None if arm in TG_ARMS else os.getenv("LIVE_EXIT_RULES")   # TG arms keep the ladder unless overridden per arm
    raw = os.getenv(f"LIVE_EXIT_RULES_{arm_env(arm)}") or glob or ",".join(default)
    rules = [r.strip() for r in raw.split(",") if r.strip() in ALLOWED_RULES]
    return list(dict.fromkeys(rules + list(ALWAYS_RULES)))


def ladder() -> List[tuple]:
    """[(multiple, fraction_of_ORIGINAL_tokens, label)] from LIVE_TG_LADDER="2:0.5,3:0.25,5:0.125"."""
    out = []
    for i, part in enumerate((os.getenv("LIVE_TG_LADDER") or "2:0.5,3:0.25,5:0.125").split(",")):
        try:
            m, f = (float(x) for x in part.split(":"))
        except ValueError:
            continue
        if m > 1 and 0 < f <= 1:
            out.append((m, f, f"tp{i + 1}_{m:g}x"))
    return sorted(out) or [(2.0, 0.5, "tp1_2x"), (3.0, 0.25, "tp2_3x"), (5.0, 0.125, "tp3_5x")]


def _iso_ms(name: str) -> int:
    """v1.21: ISO-8601 timestamp with offset (e.g. 2026-10-09T00:00:00-05:00) → epoch ms; empty/invalid → 0."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return 0
    try:
        from datetime import datetime
        return int(datetime.fromisoformat(raw).timestamp() * 1000)
    except ValueError:
        return 0


def _wallet_sizes(raw: str) -> Dict[str, float]:
    """v1.36: "addr:0.10,addr:0.10" → {addr: 0.10}. Bad / non-positive entries are ignored."""
    out: Dict[str, float] = {}
    for part in (raw or "").split(","):
        if ":" not in part:
            continue
        a, v = part.rsplit(":", 1)
        try:
            f = float(v.strip())
        except ValueError:
            continue
        if a.strip() and f > 0:
            out[a.strip()] = f
    return out


def config() -> Dict[str, Any]:
    arms = [a.strip() for a in os.getenv("LIVE_ARMS", "").split(",") if a.strip() in ARMS]
    steps = [int(x) for x in os.getenv("LIVE_SELL_SLIPPAGE_STEPS", "").split(",") if x.strip().isdigit()]
    base_sell = _i("LIVE_SELL_SLIPPAGE_BPS", 2500)
    if not steps:
        steps = [base_sell, int(base_sell * 1.6), int(base_sell * 2.4), base_sell * 4]
    # v1.15: never sell with more than LIVE_SELL_SLIPPAGE_MAX_BPS (default 5000) — 10000 bps = min_out 0 = sandwich bait
    cap = max(1, min(10_000, _i("LIVE_SELL_SLIPPAGE_MAX_BPS", 5000)))
    steps = list(dict.fromkeys(min(cap, s) for s in steps if s > 0)) or [cap]
    return {
        "exec_enabled": _truthy("JUPITER_EXEC_ENABLED"),
        "autotrade_enabled": _truthy("LIVE_AUTOTRADE_ENABLED"),
        "arms": arms,
        "sizes_sol": {a: arm_size_sol(a) for a in ARMS},
        "rules": {a: arm_rules(a) for a in ARMS},
        "max_sol_per_trade": _f("LIVE_MAX_SOL_PER_TRADE", 0.10),
        "max_open_positions": _i("LIVE_MAX_OPEN_POSITIONS", 3),
        "max_trades_per_day": _i("LIVE_MAX_TRADES_PER_DAY", 10),
        # cap-exempt arms: skip ONLY the trades/day cap (loss limit, reserve, open-pos cap, gates still apply)
        "trade_cap_exempt_arms": [a.strip() for a in os.getenv("LIVE_TRADE_CAP_EXEMPT_ARMS", "").split(",") if a.strip()],
        # v1.35c: arms whose buys are NOT counted toward (nor blocked by) the trades/day cap at all
        "trade_cap_uncounted_arms": [a.strip() for a in os.getenv("LIVE_TRADE_CAP_UNCOUNTED_ARMS", "").split(",")
                                     if a.strip()],
        # v1.35c: arms with their OWN open-position pool (not counted in / not blocked by LIVE_MAX_OPEN_POSITIONS)
        "own_pool_max_open": {a: _i(f"LIVE_MAX_OPEN_POSITIONS_{arm_env(a)}", 7) for a in
                              (x.strip() for x in os.getenv("LIVE_OWN_POOL_ARMS", "").split(",")) if a in ARMS},
        # v1.19 profit-gated extension: past the base cap, keep buying up to LIVE_MAX_TRADES_PER_DAY_PROFIT only
        # while today's REALIZED P&L (same day()["realized_pnl_sol"] the loss limit uses) is >= 0.
        # Default OFF in code → exactly the old behavior unless .env turns it on.
        "profit_extend_enabled": _truthy("LIVE_PROFIT_EXTEND_ENABLED", "false"),
        "max_trades_per_day_profit": _i("LIVE_MAX_TRADES_PER_DAY_PROFIT", 20),
        "daily_loss_limit_sol": _f("LIVE_DAILY_LOSS_LIMIT_SOL", 0.30),
        # v1.35d: arms outside the shared loss limit (their P&L is not counted and the limit never blocks them)
        "loss_limit_exempt_arms": [a.strip() for a in os.getenv("LIVE_LOSS_LIMIT_EXEMPT_ARMS", "").split(",") if a.strip()],
        # v1.36 (Sky approved Oct 17, 2026 9:39 PM CT): per-wallet live size on the scout arms. A scout_solo /
        # scout_confirmed signal whose trigger wallet is listed buys this size instead of the arm size (min with the
        # non-SOL size). Every cap / gate is unchanged. Empty = off.
        "scout_wallet_sizes": _wallet_sizes(os.getenv("LIVE_SCOUT_WALLET_SIZE_SOL", "")),
        "min_sol_reserve": _f("LIVE_MIN_SOL_RESERVE", 0.50),
        "fee_buffer_sol": _f("LIVE_FEE_BUFFER_SOL", 0.01),
        "buy_slippage_bps": _i("LIVE_BUY_SLIPPAGE_BPS", 1500),
        "sell_slippage_steps_bps": steps,
        "max_priority_lamports": _i("LIVE_MAX_PRIORITY_LAMPORTS", 2_000_000),
        "tp_mult": _f("LIVE_TP_MULT", 2.0),
        "tp_frac": _f("LIVE_TP_FRAC", 0.5),
        "trail_pct": _f("LIVE_TRAIL_PCT", 0.30),
        "min10_s": _f("LIVE_MIN10_S", 600),
        "hard_stop_pct": _f("LIVE_HARD_STOP_PCT", 0.35),
        "max_hold_s": _f("LIVE_MAX_HOLD_S", 3600),
        # v1.14.1: a runner (TP1 done / trail armed and not below entry) exits ONLY on the trail.
        # 0 = no time limit for runners.
        "runner_max_hold_s": _f("LIVE_RUNNER_MAX_HOLD_MIN", 0) * 60,
        "mark_s": _f("LIVE_MARK_S", 2.0),
        # v1.17 exits: ~1 s poll when the account watcher is down; gap-down = line crossed in one tick and the price
        # is already this far beyond it → sell at once with the capped sell slippage
        "mark_fast_s": _f("LIVE_MARK_FAST_S", 1.0),
        "gap_pct": _f("LIVE_GAP_PCT", 0.10),
        # v1.17 optional early partial take-profit on a fast pump — OFF (Andrew: keep the trailing stop as coded)
        "fast_tp_enabled": _truthy("LIVE_FAST_TP_ENABLED", "false"),
        "fast_tp_mult": _f("LIVE_FAST_TP_MULT", 1.5),
        "fast_tp_window_s": _f("LIVE_FAST_TP_WINDOW_S", 120),
        "fast_tp_frac": _f("LIVE_FAST_TP_FRAC", 0.25),
        "sell_retry_s": _f("LIVE_SELL_RETRY_S", 5.0),
        "sell_retry_max_s": _f("LIVE_SELL_RETRY_MAX_S", 60.0),         # v1.15 exponential backoff ceiling
        "sell_step_backoff_s": _f("LIVE_SELL_STEP_BACKOFF_S", 1.5),    # v1.15 pause between slippage steps
        "sell_sim_bypass_after": _i("LIVE_SELL_SIM_BYPASS_AFTER", 6),   # v1.15 sim RPC unreachable N times → send
        "ladder": ladder(),
        "tg_max_age_s": _f("LIVE_TG_MAX_AGE_S", 600),
        # v1.15: true = refuse every mint in scout_filters.scheduled_mints. NOTE: the coach tags EVERY fluffy/telegram
        # mint as scheduled (SCHEDULED_SOURCES), so strict mode refuses all Fluffy calls; false = only mints with a
        # pump_at_iso / announced before this call / announced by another source.
        "tg_scheduled_strict": _truthy("LIVE_TG_SCHEDULED_STRICT", "true"),
        "mesh_timeout_s": _f("LIVE_MESH_TIMEOUT_S", 8.0),
        "stale_mark_s": _f("LIVE_STALE_MARK_S", 60),
        "stale_sell_after_s": _f("LIVE_STALE_SELL_AFTER_S", 180),
        "sim_rpc_url": os.getenv("LIVE_SIM_RPC_URL") or os.getenv("LIVE_RPC_URL") or "",
        "kill_file": os.path.expanduser(os.getenv("LIVE_KILL_FILE", str(APP_SUPPORT / "KILL"))),
        "kill_sell_all": _truthy("LIVE_KILL_SELL_ALL"),
        "token_path": os.path.expanduser(os.getenv("LIVE_API_TOKEN_PATH", "~/.config/fomo-coach/live-api-token")),
        "state_path": os.getenv("LIVE_STATE_PATH", str(DATA_DIR / "live_state.json")),
        "ledger_path": os.getenv("LIVE_LEDGER_PATH", str(DATA_DIR / "live_trades.jsonl")),
        "timezone": os.getenv("LIVE_DAY_TZ", "America/Chicago"),
        # v1.21 limit window for the trade cap / profit extension / daily loss limit. calendar (code default) = the
        # old midnight-CT day exactly; rolling24h = [max(now-24h, LIVE_ROLLING_START), now].
        "limit_window": ("rolling24h" if os.getenv("LIVE_LIMIT_WINDOW", "calendar").strip().lower()
                         in {"rolling24h", "rolling", "rolling_24h", "24h"} else "calendar"),
        "rolling_start_ms": _iso_ms("LIVE_ROLLING_START"),
        # v1.21 chase filter: skip a buy whose quoted price is > LIVE_MAX_CHASE_PCT above the trigger wallet's fill
        "chase_filter_enabled": _truthy("LIVE_CHASE_FILTER_ENABLED", "false"),
        "max_chase_pct": _f("LIVE_MAX_CHASE_PCT", 15.0),
        "chase_arms": [a.strip() for a in os.getenv("LIVE_CHASE_ARMS", "scout_solo,scout_confirmed").split(",")
                       if a.strip()],
        "chase_decimals": _i("LIVE_CHASE_TOKEN_DECIMALS", 6),
        # v1.21 fast cut (pre-2x only): down LIVE_FAST_CUT_PCT within LIVE_FAST_CUT_WINDOW_S of entry → sell all
        "fast_cut_enabled": _truthy("LIVE_FAST_CUT_ENABLED", "false"),
        "fast_cut_pct": _f("LIVE_FAST_CUT_PCT", 15.0) / 100,
        "fast_cut_window_s": _f("LIVE_FAST_CUT_WINDOW_S", 30.0),
        # v1.33 (Andrew approved Oct 15, 2026 12:11 PM CT). Telegram arms keep their own trail (unchanged 30%).
        "tg_trail_pct": _f("LIVE_TG_TRAIL_PCT", 0.30),
        # v1.35b kol_confluence: standard live exit → same trail as LIVE_TRAIL_PCT unless overridden
        "kol_confluence_trail_pct": _f("LIVE_TRAIL_PCT_KOL_CONFLUENCE", _f("LIVE_TRAIL_PCT", 0.30)),
        # v1.33 pre-2x candle stop: previous COMPLETED N-s candle low (ratcheting) minus buffer; 35% hard stop stays.
        "candle_stop_enabled": _truthy("LIVE_CANDLE_STOP_ENABLED", "false"),
        "candle_stop_s": _f("LIVE_CANDLE_STOP_S", 15.0),
        "candle_stop_buf_pct": _f("LIVE_CANDLE_STOP_BUF_PCT", 3.0) / 100,
        "candle_stop_arms": [a.strip() for a in os.getenv("LIVE_CANDLE_STOP_ARMS", "scout_solo,scout_confirmed").split(",")
                             if a.strip() and a.strip() not in TG_ARMS],
        # v1.33 follow the trader's sell only if it lands within N s of entry (0 = always); candle-stop arms only
        "trader_sell_window_s": _f("LIVE_TRADER_SELL_WINDOW_S", 0.0),
        # v1.33 night pause for NEW entries (exits unaffected); Telegram arms exempt by default
        "night_pause_enabled": _truthy("LIVE_NIGHT_PAUSE_ENABLED", "false"),
        "night_pause_start_h": _i("LIVE_NIGHT_PAUSE_START_H", 22),
        "night_pause_end_h": _i("LIVE_NIGHT_PAUSE_END_H", 6),
        "night_pause_exempt_arms": [a.strip() for a in os.getenv("LIVE_NIGHT_PAUSE_EXEMPT_ARMS",
                                    "fluffy_pump_dump,zyrox").split(",") if a.strip()],
    }
