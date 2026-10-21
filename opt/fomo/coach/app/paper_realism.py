"""v1.37 paper realism — make every paper arm fill like live (Sky, Oct 18 2026 6:31 PM CT: "have paper act more like live").

PAPER ONLY. Never imports the signer, never calls Jupiter, never touches live positions / caps / loss limit / kill switch.
Calibrated from REAL live fills (scripts/paper_realism_calibrate.py → data/paper_realism_calib.json, built-in defaults
below = the Oct 18 calibration, 14-day window).

Entry (realistic track):
  * price at the first real on-chain price (pump.fun curve, else the PumpSwap pool) read at signal + the measured live
    signal→land delay (p50 of the arm's route); our size is simulated against those reserves (constant product + fee);
  * never below the copied trader's own fill;
  * no reliable on-chain price → fill at max(trader fill × (1 + measured live median slippage), next mark), where a mark
    only counts if it is fresh (≤ PAPER_REALISM_MAX_MARK_AGE_S) and not more than PAPER_REALISM_MAX_BELOW_TRADER_PCT
    below the trader's fill (lagging DexScreener marks are rejected);
  * no trader fill and no on-chain price → the realistic entry is SKIPPED (the legacy track still records it).
Exits: same live rules (app.live.engine.evaluate_exit). Hard stops fill at min(mark, stop line × live fill ratio); other
  exits at mark × live generic sell ratio; a price-proxy liquidity drain (the live LIVE_DRAIN_FRAC on quote reserves ≈
  1-(1-f)² on price, within LIVE_DRAIN_WINDOW_S) exits at the post-drain mark × live ratio. The measured live sell latency
  is inside those ratios (they are realized fill / trigger line from live fills). Fees: live fee_bps per side + live median
  priority lamports + sender tip per transaction, counted on both legs.
Gates: every realistic entry is tagged with the live gates that would have refused it (rugcheck/mesh, chase filter,
  night pause, trades/day cap, loss limit, max open) evaluated on a per-arm virtual live ledger; `gated` = none refused.
Off switch PAPER_REALISM_ENABLED (default true). Legacy (optimistic) tracks are kept unchanged for comparison.
"""
from __future__ import annotations

import collections
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

from app.paths import data_dir

logger = logging.getLogger("coach.paper.realism")

REAL_PREFIX = "real_"

# Oct 18 2026 calibration (live_trades.jsonl + direct_pump.jsonl, 14 days ending Sun Oct 18 6:29 PM CT)
DEFAULTS: Dict[str, Any] = {
    "routes": {
        "kol_confluence": {"jupiter": {"n": 75, "lat_ms_p50": 2628, "lat_ms_p75": 3104, "slip_pct_p50": 12.89,
                                       "slip_pct_p75": 26.84, "priority_lamports_p50": 3692, "fee_bps_p50": 10},
                           "pump_direct": {"n": 36, "lat_ms_p50": 2262, "lat_ms_p75": 2635, "slip_pct_p50": 19.91,
                                           "slip_pct_p75": 40.39, "priority_lamports_p50": 50000, "fee_bps_p50": None},
                           "all": {"n": 111, "lat_ms_p50": 2558, "lat_ms_p75": 2984, "slip_pct_p50": 14.63,
                                   "slip_pct_p75": 29.63, "priority_lamports_p50": 11648, "fee_bps_p50": 10}},
        "scout": {"jupiter": {"n": 57, "lat_ms_p50": 2934, "lat_ms_p75": 3908, "slip_pct_p50": 4.37,
                              "slip_pct_p75": 8.27, "priority_lamports_p50": 38459, "fee_bps_p50": 10},
                  "pump_direct": {"n": 8, "lat_ms_p50": 4989, "lat_ms_p75": 9685, "slip_pct_p50": 12.73,
                                  "slip_pct_p75": 16.77, "priority_lamports_p50": 50000, "fee_bps_p50": None},
                  "all": {"n": 65, "lat_ms_p50": 3071, "lat_ms_p75": 4234, "slip_pct_p50": 5.45,
                          "slip_pct_p75": 9.82, "priority_lamports_p50": 50000, "fee_bps_p50": 10}},
        "all": {"all": {"n": 177, "lat_ms_p50": 2611, "lat_ms_p75": 3162, "slip_pct_p50": 9.9, "slip_pct_p75": 21.76,
                        "priority_lamports_p50": 23193, "fee_bps_p50": 10}},
    },
    "sell": {"hard_stop_ratio_p50": 0.9785, "hard_stop_ratio_p25": 0.9149, "hard_stop_ratio_p10": 0.8248,
             "drain_mult_p50": 0.6934, "drain_mult_p10": 0.4418,
             "generic_ratio_p50": 1.0, "latency_ms_p50": 3727, "latency_ms_p75": 4289},
}

# paper arm → (live route group, live arm whose gates / size / rules it mirrors)
ARM_MAP: Dict[str, Tuple[str, str]] = {
    "kol_confluence": ("kol_confluence", "kol_confluence"),
    "paper_v3_new": ("scout", "scout_solo"),
    "paper_top4": ("scout", "scout_solo"),
    "fomo_top_paper": ("scout", "scout_solo"),
    "momentum_paper": ("all", "scout_solo"),
    "scout_solo": ("scout", "scout_solo"),
    "scout_confirmed": ("scout", "scout_confirmed"),
    "smugcalls_control": ("scout", "scout_solo"),
    "fluffy_pump_dump": ("all", "fluffy_pump_dump"),     # v1.38 fluffy paper ladder twin
}

_cache: Dict[str, Any] = {"mtime": None, "data": None}


def _f(name: str, d: float) -> float:
    try:
        return float(os.getenv(name, str(d)).strip())
    except ValueError:
        return d


def enabled() -> bool:
    return os.getenv("PAPER_REALISM_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}


def onchain_enabled() -> bool:
    """PAPER_REALISM_ONCHAIN_ENABLED (default true): on-chain entry reads; off → trader-fill fallback only (tests)."""
    return os.getenv("PAPER_REALISM_ONCHAIN_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}


def calib_path() -> Path:
    return Path(os.getenv("PAPER_REALISM_CALIB_PATH", str(data_dir() / "paper_realism_calib.json")))


def calib() -> Dict[str, Any]:
    """DEFAULTS overlaid with data/paper_realism_calib.json (mtime-cached; a bad file falls back to DEFAULTS)."""
    p = calib_path()
    try:
        mt = p.stat().st_mtime
    except OSError:
        return DEFAULTS
    if _cache["mtime"] != mt:
        try:
            d = json.loads(p.read_text())
            routes = {g: {**DEFAULTS["routes"].get(g, {}), **(d.get("routes") or {}).get(g, {})}
                      for g in set(DEFAULTS["routes"]) | set(d.get("routes") or {})}
            sell = {**DEFAULTS["sell"], **{k: v for k, v in (d.get("sell") or {}).items() if v is not None}}
            _cache.update(mtime=mt, data={"routes": routes, "sell": sell, "file": str(p),
                                          "generated_ms": d.get("generated_ms"), "window_days": d.get("window_days")})
        except Exception as exc:  # noqa: BLE001
            logger.warning("paper realism calib unreadable (%s) → defaults", type(exc).__name__)
            _cache.update(mtime=mt, data=DEFAULTS)
    return _cache["data"] or DEFAULTS


def mapping(arm: str) -> Tuple[str, str]:
    g, live_arm = ARM_MAP.get(arm, ("all", "scout_solo"))
    return (os.getenv(f"PAPER_REALISM_ROUTE_{arm.upper()}", g).strip() or g,
            os.getenv(f"PAPER_REALISM_LIVE_ARM_{arm.upper()}", live_arm).strip() or live_arm)


def _route_stats(group: str, path: str) -> Dict[str, Any]:
    r = calib()["routes"]
    for g, pth in ((group, path), (group, "all"), ("all", "all")):
        s = (r.get(g) or {}).get(pth)
        if s and s.get("lat_ms_p50") is not None and s.get("slip_pct_p50") is not None:
            return {**s, "group": g, "path": pth}
    return {**DEFAULTS["routes"]["all"]["all"], "group": "all", "path": "all"}


def params(arm: str, path: Optional[str] = None) -> Dict[str, Any]:
    """Everything a paper arm needs to fill like its live twin."""
    P = _params(arm, path)
    if os.getenv("PAPER_REALISM_SNAPSHOT_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}:
        write_snapshot()                       # at most every 10 min; for the offline learner
    return P


def _params(arm: str, path: Optional[str] = None) -> Dict[str, Any]:
    group, live_arm = mapping(arm)
    path = path or os.getenv(f"PAPER_REALISM_PATH_{arm.upper()}", "jupiter").strip() or "jupiter"
    s = _route_stats(group, path)
    sell = calib()["sell"]
    try:
        from app.live.config import config as live_config
        lc = live_config()
        size = float((lc.get("sizes_sol") or {}).get(live_arm) or 0) or None
    except Exception:  # noqa: BLE001
        lc, size = {}, None
    tip = int(_f("PAPER_REALISM_TIP_LAMPORTS", _f("LIVE_SENDER_TIP_LAMPORTS", 5000)))
    return {
        "arm": arm, "group": s["group"], "path": s["path"], "live_arm": live_arm,
        "lat_ms": float(_f("PAPER_REALISM_LAT_MS", s["lat_ms_p50"])), "lat_ms_p75": s.get("lat_ms_p75"),
        "slip_pct": float(_f("PAPER_REALISM_SLIP_PCT", s["slip_pct_p50"])), "slip_pct_p75": s.get("slip_pct_p75"),
        "priority_lamports": int(s.get("priority_lamports_p50") or 0), "tip_lamports": tip,
        "fee_bps": int(s.get("fee_bps_p50") or 0),
        "pool_fee_bps": int(_f("PAPER_REALISM_POOL_FEE_BPS", 30)), "curve_fee_bps": int(_f("PAPER_CURVE_FEE_BPS", 125)),
        "hard_stop_ratio": float(sell["hard_stop_ratio_p50"]), "generic_ratio": float(sell["generic_ratio_p50"]),
        "hard_stop_ratio_p10": float(sell.get("hard_stop_ratio_p10") or DEFAULTS["sell"]["hard_stop_ratio_p10"]),
        "drain_mult": sell.get("drain_mult_p50"),
        "drain_mult_p10": float(sell.get("drain_mult_p10") or DEFAULTS["sell"]["drain_mult_p10"]),
        "sell_latency_ms": sell.get("latency_ms_p50"),
        "max_mark_age_s": _f("PAPER_REALISM_MAX_MARK_AGE_S", 10),
        "max_below_trader_pct": _f("PAPER_REALISM_MAX_BELOW_TRADER_PCT", 2),
        "drain_frac": _drain_frac(), "drain_window_s": _f("LIVE_DRAIN_WINDOW_S", 30),
        "size_sol": _f(f"PAPER_REALISM_SIZE_SOL_{arm.upper()}", size or 0.10),
        "live_cfg": lc,
    }


def _drain_frac() -> float:
    try:
        from app.live import rugcheck
        return float(rugcheck.cfg().get("drain_frac") or 0.5)
    except Exception:  # noqa: BLE001
        return _f("LIVE_DRAIN_FRAC", 0.5)


# ---------------- entry ----------------
def _sim_buy_px(m: Dict[str, Any], size_sol: float, P: Dict[str, Any]) -> Optional[float]:
    """Our average fill price for size_sol against the observed reserves (pump curve or PumpSwap pool)."""
    lam = int(size_sol * 1e9)
    try:
        if m.get("curve"):
            from app.pump.curve import buy_tokens_out
            toks = buy_tokens_out(m["curve"], lam, P["curve_fee_bps"])
            return size_sol / (toks / 1e6) if toks > 0 else None
        pool = m.get("pool") or {}
        base, quote = pool.get("base_reserve"), pool.get("effective_quote_reserves")
        if base and quote:
            net = lam * (1 - (P["pool_fee_bps"] + P["fee_bps"]) / 1e4)
            toks = base * net / (quote + net)
            return size_sol / (toks / 1e6) if toks > 0 else None
    except Exception:  # noqa: BLE001
        return None
    return None


def entry_fill(arm: str, trader_px: Optional[float], onchain: Optional[Dict[str, Any]] = None,
               next_mark: Optional[Dict[str, Any]] = None, size_sol: Optional[float] = None,
               P: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Realistic entry price (SOL per whole token, all-in except per-tx lamports) or a skip.

    onchain   = {"px", "ms", "src": "curve"|"pumpswap", "curve"?, "pool"?} read at ≥ signal + live latency
    next_mark = {"px", "ms", "src": "dexscreener"|…} the next ordinary mark (only used if fresh + not lagging)
    """
    P = P or params(arm)
    size = size_sol or P["size_sol"]
    now = int(time.time() * 1000)
    out: Dict[str, Any] = {"ok": False, "trader_px": trader_px, "lat_ms": P["lat_ms"], "slip_pct": P["slip_pct"],
                           "route": f"{P['group']}/{P['path']}", "rejected": []}
    ref = (onchain or {}).get("px") or (next_mark or {}).get("px")
    if trader_px and ref and not (1 / 5 <= trader_px / ref <= 5):
        # unit mismatch (non-SOL quote / decimals): a trader "fill" 5x+ away from any market price is not a fill
        out["rejected"].append(f"trader fill {trader_px:.3g} vs market {ref:.3g} (>5x apart, ignored)")
        out["trader_px_suspect"] = trader_px
        trader_px = None
        out["trader_px"] = None
    floor = trader_px * (1 + P["slip_pct"] / 100) if trader_px else None
    if onchain and onchain.get("px") and onchain["px"] > 0:
        sim = _sim_buy_px(onchain, size, P) or onchain["px"] * (1 + (P["curve_fee_bps"] if onchain.get("curve")
                                                                     else P["pool_fee_bps"] + P["fee_bps"]) / 1e4)
        px = max(sim, trader_px or 0.0)
        out.update(ok=True, px=px, src=f"onchain_{onchain.get('src') or 'read'}", mark_px=onchain["px"],
                   sim_px=sim, floored_to_trader=bool(trader_px and sim < trader_px), read_ms=onchain.get("ms"))
        return out
    mk_ok = None
    if next_mark and next_mark.get("px"):
        age = (now - int(next_mark.get("ms") or now)) / 1000
        if age > P["max_mark_age_s"]:
            out["rejected"].append(f"mark stale {age:.0f}s > {P['max_mark_age_s']:g}s")
        elif trader_px and next_mark["px"] < trader_px * (1 - P["max_below_trader_pct"] / 100):
            out["rejected"].append(f"mark {(next_mark['px'] / trader_px - 1) * 100:+.1f}% vs trader fill "
                                   f"(lagging, > {P['max_below_trader_pct']:g}% below)")
        else:
            mk_ok = float(next_mark["px"])
    if floor:
        px = max(floor, mk_ok or 0.0)
        out.update(ok=True, px=px, src="trader_fill+live_slip" if px == floor else "mark>trader_floor", floor_px=floor,
                   mark_px=mk_ok)
        return out
    out["reason"] = ("no on-chain price and no usable trader fill → realistic entry skipped"
                     + (" (lagging/stale mark not trusted alone)" if next_mark else ""))
    return out


def entry_position(P: Dict[str, Any], px: float, size_sol: Optional[float] = None) -> Tuple[int, int]:
    """(spent_lamports incl. priority + tip, synthetic tokens at DECIMALS=6)."""
    size = size_sol or P["size_sol"]
    spent = int(size * 1e9) + P["priority_lamports"] + P["tip_lamports"]
    tokens = int(size / px * 1e6)
    return spent, tokens


async def onchain_price(mint: str) -> Optional[Dict[str, Any]]:
    """One read-only on-chain price for mint: pump.fun bonding curve, else the canonical PumpSwap pool.
    Uses the scout source's background RPC bucket (yields to the live CA lane). Never raises."""
    try:
        from app.pump import rpc as prpc
        from app.pump.curve import (PUMP_PROGRAM_ID, curve_price_sol, decode_curve)
        from app.pump.solana_pda import bonding_curve_pda
        from app.scout.source import source
        res = await source.call("getAccountInfo", [bonding_curve_pda(mint), {"encoding": "base64",
                                                                              "commitment": "processed"}], bg=True)
        acc = prpc._decode_account((res or {}).get("value"))
        if not acc or acc.get("owner") != PUMP_PROGRAM_ID:
            return None
        c = decode_curve(acc["data"])
        now = int(time.time() * 1000)
        if not c.get("quote_is_sol", True):
            return None
        if not c.get("complete"):
            px = curve_price_sol(c)
            return {"px": px, "ms": now, "src": "curve", "curve": c} if px else None
        pool = await _read_pool(mint, c.get("quote_mint"), source)
        if pool and pool.get("price_sol"):
            return {"px": pool["price_sol"], "ms": int(time.time() * 1000), "src": "pumpswap", "pool": pool}
    except Exception as exc:  # noqa: BLE001
        logger.debug("paper realism onchain read %s: %s", mint[:6], type(exc).__name__)
    return None


async def _read_pool(mint: str, quote_mint: Optional[str], source: Any) -> Optional[Dict[str, Any]]:
    from app.pump import rpc as prpc
    from app.pump.curve import PUMP_AMM_PROGRAM_ID, canonical_pumpswap_pool, decode_pool, token_amount
    pool = canonical_pumpswap_pool(mint, quote_mint) if quote_mint else canonical_pumpswap_pool(mint)
    res = await source.call("getAccountInfo", [pool, {"encoding": "base64", "commitment": "processed"}], bg=True)
    acc = prpc._decode_account((res or {}).get("value"))
    if not acc or acc.get("owner") != PUMP_AMM_PROGRAM_ID:
        return None
    p = decode_pool(acc["data"])
    res = await source.call("getMultipleAccounts", [[p["base_vault"], p["quote_vault"]],
                                                     {"encoding": "base64", "commitment": "processed"}], bg=True)
    vals = [prpc._decode_account(v) for v in ((res or {}).get("value") or [])]
    if len(vals) != 2 or not all(vals):
        return None
    base, quote_raw = token_amount(vals[0]["data"]), token_amount(vals[1]["data"])
    eff = quote_raw + p["virtual_quote_reserves"]
    price = (eff / 1e9) / (base / 1e6) if base > 0 and eff > 0 else None
    return {"base_reserve": base, "effective_quote_reserves": eff, "price_sol": price}


# ---------------- exits ----------------
def exit_px(P: Dict[str, Any], reason: str, mark_px: float, entry_px: float, lc: Optional[Dict[str, Any]] = None) -> float:
    """Realistic sell price for a paper exit decided on mark_px (before per-side fee bps / lamports)."""
    lc = lc or P.get("live_cfg") or {}
    if reason == "hard_stop" and entry_px:
        # Live stop fills land near the line (p50 ×0.98, p10 ×0.82). Paper marks are lagging DexScreener prints
        # every 5 s, so a deeper "gap" below the live p10 is mostly mark lag, not a real fill — floor it there.
        line = entry_px * (1 - float(lc.get("hard_stop_pct", 0.35)))
        return min(line * P["hard_stop_ratio"], max(mark_px, line * P["hard_stop_ratio_p10"]))
    if reason.startswith("rug_flag") and entry_px:
        # drain sells: post-drain mark, but not below what live drain sells realise at p10
        return max(mark_px * P["generic_ratio"], entry_px * P["drain_mult_p10"])
    return mark_px * P["generic_ratio"]


def proceeds_lamports(P: Dict[str, Any], tokens: int, px: float) -> int:
    gross = tokens / 1e6 * px * 1e9
    return max(0, int(gross * (1 - P["fee_bps"] / 1e4)) - P["priority_lamports"] - P["tip_lamports"])


class DrainWatch:
    """Price proxy of the live liquidity-drain check: quote reserves −f ⇒ price ≈ ×(1−f)² on a constant-product pool."""

    def __init__(self) -> None:
        self.px: Dict[str, Deque[Tuple[int, float]]] = collections.defaultdict(lambda: collections.deque(maxlen=200))

    def check(self, key: str, px: float, now: int, P: Dict[str, Any]) -> Optional[str]:
        dq = self.px[key]
        dq.append((now, px))
        win = P["drain_window_s"] * 1000
        while dq and now - dq[0][0] > win:
            dq.popleft()
        peak = max(v for _, v in dq)
        thr = (1 - P["drain_frac"]) ** 2
        if peak > 0 and px <= peak * thr:
            dq.clear()
            return f"price −{(1 - px / peak) * 100:.0f}% within {P['drain_window_s']:g}s (live drain line ≈ −{(1 - thr) * 100:.0f}%)"
        return None

    def drop(self, key: str) -> None:
        self.px.pop(key, None)


# ---------------- gates (per-arm virtual live ledger) ----------------
class GateLedger:
    """Which live gates would have refused a realistic paper entry, judged on this arm's own virtual live book."""

    def __init__(self) -> None:
        self.trades: List[int] = []                  # entry ms of gated (would-have-traded) entries
        self.pnl: List[Tuple[int, float]] = []       # (close ms, realized SOL) of gated entries
        self.open: Dict[str, int] = {}               # pid -> entry ms (gated, still open)

    def to_dict(self) -> Dict[str, Any]:
        return {"trades": self.trades[-500:], "pnl": self.pnl[-1000:], "open": self.open}

    def load(self, d: Optional[Dict[str, Any]]) -> None:
        d = d or {}
        self.trades = [int(x) for x in d.get("trades") or []]
        self.pnl = [(int(a), float(b)) for a, b in d.get("pnl") or []]
        self.open = {k: int(v) for k, v in (d.get("open") or {}).items()}

    def refusals(self, arm: str, now_ms: int, *, would_live_buy: Optional[bool], trader_px: Optional[float],
                 entry_px: Optional[float], P: Dict[str, Any]) -> List[str]:
        lc = P.get("live_cfg") or {}
        live_arm = P["live_arm"]
        why: List[str] = []
        if would_live_buy is False:
            why.append("rugcheck/mesh")
        if (lc.get("chase_filter_enabled", True) and live_arm in (lc.get("chase_arms") or ["scout_solo", "scout_confirmed"])
                and trader_px and entry_px and (entry_px / trader_px - 1) * 100 > float(lc.get("max_chase_pct", 5))):
            why.append(f"chase>{float(lc.get('max_chase_pct', 5)):g}%")
        try:
            from app.live.engine import night_paused
            if night_paused(live_arm, lc, now_ms):
                why.append("night_pause")
        except Exception:  # noqa: BLE001
            pass
        day = now_ms - 86400_000
        n = sum(1 for t in self.trades if t >= day)
        pnl = sum(v for t, v in self.pnl if t >= day)
        cap = int(lc.get("max_trades_per_day", 10) or 10)
        ext = int(lc.get("max_trades_per_day_profit") or cap)
        eff = ext if (lc.get("profit_extend_enabled") and pnl >= 0) else cap
        if live_arm not in (lc.get("trade_cap_uncounted_arms") or []) and live_arm not in (lc.get("trade_cap_exempt_arms") or []) \
                and n >= eff:
            why.append(f"trades/day {n}/{eff}")
        if live_arm not in (lc.get("loss_limit_exempt_arms") or []) and pnl <= -float(lc.get("daily_loss_limit_sol", 0.6)):
            why.append(f"loss_limit {pnl:+.3f}")
        mx = (lc.get("own_pool_max_open") or {}).get(live_arm) or lc.get("max_open_positions", 3)
        if len(self.open) >= int(mx):
            why.append(f"max_open {len(self.open)}/{int(mx)}")
        return why

    def opened(self, pid: str, now_ms: int) -> None:
        self.trades.append(now_ms)
        self.open[pid] = now_ms

    def closed(self, pid: str, now_ms: int, pnl_sol: float) -> None:
        if pid in self.open:
            self.open.pop(pid, None)
            self.pnl.append((now_ms, float(pnl_sol)))
        cut = now_ms - 3 * 86400_000
        self.trades = [t for t in self.trades if t >= cut]
        self.pnl = [(t, v) for t, v in self.pnl if t >= cut]


def describe() -> Dict[str, Any]:
    c = calib()
    return {"enabled": enabled(), "calibration_file": c.get("file") or "built-in defaults (Oct 18 2026)",
            "calibration_generated_ms": c.get("generated_ms"), "window_days": c.get("window_days"),
            "entry": "on-chain curve/pool price at signal + live p50 latency, never below trader fill; else "
                     "max(trader × (1 + live p50 slippage), fresh non-lagging mark); else skipped",
            "exit": ("live rules; hard stop at line × live p50 ratio, gaps floored at live p10; drain at post-drain mark floored at live p10 drain multiple; others mark × live ratio; price-proxy drain"),
            "fees": "live fee_bps per side + live p50 priority lamports + sender tip per tx",
            "gates": "tagged per entry on a per-arm virtual live ledger; headline = realistic (all entries); "
                     "'gated' = only entries no live gate would have refused"}


# ---------------- v1.38 snapshot for the offline learner ----------------
# The daily learner runs with no network and no .env (systemd InaccessiblePaths), so it cannot rebuild the live
# config itself. The coach writes the non-secret parts it needs here: per-arm realism params + the live exit/gate
# numbers. URL / path / token keys are dropped.
_SNAP_DROP = ("_url", "_path", "_file", "token")
_snap_state: Dict[str, float] = {"t": 0.0}


def _safe_cfg(lc: Dict[str, Any]) -> Dict[str, Any]:
    out = {}
    for k, v in (lc or {}).items():
        if any(k.endswith(x) or x in k for x in _SNAP_DROP):
            continue
        if isinstance(v, str) and ("http" in v or "/" in v):
            continue
        out[k] = v
    return out


def snapshot_path() -> Path:
    return Path(os.getenv("PAPER_REALISM_SNAPSHOT_PATH", str(data_dir() / "paper_realism_snapshot.json")))


def write_snapshot(force: bool = False) -> bool:
    now = time.time()
    if not force and now - _snap_state["t"] < 600:
        return False
    _snap_state["t"] = now
    try:
        arms = {}
        lc: Dict[str, Any] = {}
        for a in ARM_MAP:
            P = _params(a)
            lc = P.get("live_cfg") or lc
            arms[a] = {k: v for k, v in P.items() if k != "live_cfg"}
        p = snapshot_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({"generated_ms": int(now * 1000), "version": "v1.38", "params": arms,
                                   "live_cfg": _safe_cfg(lc)}, default=str))
        os.replace(tmp, p)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("paper realism snapshot failed: %s", type(exc).__name__)
        return False
