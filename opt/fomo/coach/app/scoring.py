"""Cluster scoring — recommend-only signals from CoachState buys."""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from app.config import settings
from app.state import BuyEvent, CoachState, state

logger = logging.getLogger("coach.scoring")


@dataclass
class Signal:
    token_address: str
    symbol: str
    chain: str
    score: float
    unique_traders: int
    buy_count: int
    trader_handles: List[str]
    reasons: List[str]
    recommend_only: bool = True
    auto_trade: bool = False
    window_minutes: int = 180
    occurred_from_ms: Optional[int] = None
    occurred_to_ms: Optional[int] = None
    total_usd: Optional[float] = None
    avg_market_cap: Optional[float] = None
    narrative: Optional[Dict[str, Any]] = None
    jev: Optional[Dict[str, Any]] = None
    mesh: Optional[Dict[str, Any]] = None  # QP Data Mesh enrichment (price/liquidity/mcap/risk/curve)
    gate: Optional[Dict[str, Any]] = None  # final recommend-only gate (mesh rules + Jev)
    plan: Optional[Dict[str, Any]] = None  # recommend-only entry/exit ladder (Fluffy pump→dump)
    source: Optional[str] = None  # e.g. fluffy/telegram
    curve: Optional[Dict[str, Any]] = None  # direct on-chain pump.fun bonding curve (read-only)
    fast_gate: Optional[Dict[str, Any]] = None  # curve-first lane decision
    latency: Optional[Dict[str, Any]] = None  # per-stage timeline (tg → ingest → curve → mesh → jev → quote)
    stream: Optional[Dict[str, Any]] = None  # free logsSubscribe trade stream summary
    exit_signals: Optional[List[Dict[str, Any]]] = None  # early-dump exit cards (recommend-only)
    paper: Optional[Dict[str, Any]] = None  # paper-trading position (simulated, never signed)
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        # drop empty narrative for list views (detail may attach it)
        return d


def _clamp(n: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, n))


def score_buys(buys: List[BuyEvent], *, min_convergence: Optional[int] = None) -> Optional[Signal]:
    """Score a token's buys in the cluster window. Returns None if no buys."""
    if not buys:
        return None

    min_conv = min_convergence if min_convergence is not None else settings.min_wallet_convergence
    traders = {b.trader_id for b in buys}
    handles = sorted({b.trader_handle for b in buys if b.trader_handle})
    unique = len(traders)
    buy_count = len(buys)

    usd_vals = [b.usd_amount for b in buys if b.usd_amount is not None and b.usd_amount > 0]
    mcap_vals = [b.market_cap for b in buys if b.market_cap is not None and b.market_cap > 0]
    total_usd = sum(usd_vals) if usd_vals else None
    avg_mcap = (sum(mcap_vals) / len(mcap_vals)) if mcap_vals else None

    reasons: List[str] = []
    score = 0.0

    # Unique trader convergence (primary)
    if unique >= min_conv:
        trader_pts = min(unique * 18.0, 55.0)
        score += trader_pts
        reasons.append(f"{unique} distinct traders in cluster window (≥{min_conv} convergence)")
    elif unique >= 2:
        score += unique * 10.0
        reasons.append(f"{unique} traders (below min convergence {min_conv})")
    else:
        score += 5.0
        reasons.append("single trader — weak cluster")

    # Buy count density
    buy_pts = min(buy_count * 4.0, 20.0)
    score += buy_pts
    reasons.append(f"{buy_count} buys in window")

    # USD size hint
    if total_usd is not None:
        if total_usd >= 500:
            score += 12.0
            reasons.append(f"strong USD flow (~${total_usd:,.0f})")
        elif total_usd >= 100:
            score += 8.0
            reasons.append(f"moderate USD flow (~${total_usd:,.0f})")
        else:
            score += 4.0
            reasons.append(f"light USD flow (~${total_usd:,.0f})")

    # Market-cap hint (early / meme-sized preferred)
    if avg_mcap is not None:
        if avg_mcap < 500_000:
            score += 10.0
            reasons.append(f"early mcap hint (~${avg_mcap:,.0f})")
        elif avg_mcap < 5_000_000:
            score += 6.0
            reasons.append(f"mid mcap hint (~${avg_mcap:,.0f})")
        else:
            score += 2.0
            reasons.append(f"higher mcap (~${avg_mcap:,.0f})")

    score = round(_clamp(score), 1)

    # Prefer latest symbol/chain from buys
    latest = max(buys, key=lambda b: b.occurred_at_ms)
    occurred_from = min(b.occurred_at_ms for b in buys)
    occurred_to = max(b.occurred_at_ms for b in buys)

    # Prefer Fluffy/telegram source if any buy carries it
    sources = [b.source for b in buys if b.source]
    source = None
    for pref in ("fluffy/telegram", "telegram"):
        if any((s or "").lower() == pref for s in sources):
            source = pref
            break
    if source is None and sources:
        source = sources[-1]
    meta: Dict[str, Any] = {}
    # carry telegram group from latest fluffy-ish buy
    for b in reversed(buys):
        m = b.meta or {}
        if m.get("source_group") or "fluff" in (b.source or "").lower():
            meta = {
                "source_group": m.get("source_group"),
                "telegram_message_id": m.get("telegram_message_id"),
                "entry_rule": m.get("entry_rule") or "ca_announce_not_dip",
                "sources": sorted({s for s in sources if s}),
            }
            break

    return Signal(
        token_address=latest.token_address,
        symbol=latest.token_symbol or "?",
        chain=latest.chain or "unknown",
        score=score,
        unique_traders=unique,
        buy_count=buy_count,
        trader_handles=handles[:20],
        reasons=reasons,
        recommend_only=True,
        auto_trade=False,
        window_minutes=settings.cluster_window_minutes,
        occurred_from_ms=occurred_from,
        occurred_to_ms=occurred_to,
        total_usd=round(total_usd, 2) if total_usd is not None else None,
        avg_market_cap=round(avg_mcap, 2) if avg_mcap is not None else None,
        source=source,
        meta=meta,
    )


def _low_priority_in_signals() -> bool:
    import os
    return os.getenv("FOMO_ONCHAIN_IN_SIGNALS", "false").strip().lower() in {"1", "true", "yes", "on"}


def compute_signals(
    coach: Optional[CoachState] = None,
    *,
    min_score: float = 50.0,
    token_address: Optional[str] = None,
) -> List[Signal]:
    """Compute signals on read from buffered buys within the cluster window."""
    st = coach or state
    buys = st.recent_buys(token_address)
    if not _low_priority_in_signals():
        # v1.12: fomo-onchain / fomoapi-keyless buys feed their own paper profile, not the
        # Telegram/JEV/mesh signal path (keeps the Fluffy CA lane's budgets untouched)
        from app.fomo_onchain import LOW_PRIORITY_SOURCES
        buys = [b for b in buys if b.source not in LOW_PRIORITY_SOURCES]
    by_token: Dict[str, List[BuyEvent]] = defaultdict(list)
    for b in buys:
        by_token[b.token_address.lower()].append(b)

    signals: List[Signal] = []
    for _key, group in by_token.items():
        sig = score_buys(group)
        if sig is None:
            continue
        if sig.score >= min_score:
            signals.append(sig)

    signals.sort(key=lambda s: (-s.score, -(s.occurred_to_ms or 0)))
    return signals


def get_signal(token_address: str, *, min_score: float = 0.0) -> Optional[Signal]:
    """Detail for one token (any score ≥ min_score, default 0 for detail view)."""
    signals = compute_signals(min_score=min_score, token_address=token_address)
    if not signals:
        # still return scored object even if below threshold when buys exist
        buys = state.recent_buys(token_address)
        return score_buys(buys) if buys else None
    return signals[0]


def maybe_notify_telegram(sig: Signal) -> None:
    """Soft Telegram notify — log 'would notify' when unset; never crash."""
    try:
        from app.feeds.telegram_notify import notify_signal

        notify_signal(sig)
    except Exception as exc:  # noqa: BLE001
        logger.warning("telegram notify soft-fail: %s", exc)


def maybe_attach_narrative(sig: Signal) -> Signal:
    """Soft DeBot narrative chip — never block scoring."""
    try:
        from app.feeds.debot import enrich_narrative

        chip = enrich_narrative(sig.token_address, sig.symbol)
        if chip:
            sig.narrative = chip
    except Exception as exc:  # noqa: BLE001
        logger.warning("debot narrative soft-fail: %s", exc)
        if settings.debot_narrative_enabled:
            sig.narrative = {
                "status": "soft_fail",
                "source": "debot",
                "text": None,
                "note": str(exc)[:200],
            }
    return sig


def maybe_attach_jev(sig: Signal) -> Signal:
    """TypeSafe Jev recommend-only gate — fail closed; never blocks scoring hard."""
    try:
        from app.jev_gate import attach_jev

        attach_jev(sig)
    except Exception as exc:  # noqa: BLE001
        logger.warning("jev gate soft-fail: %s", exc)
        sig.jev = {
            "jev_status": "error",
            "action": "escalate_human",
            "safe_to_surface": False,
            "recommend_only": True,
            "auto_trade": False,
            "note": str(exc)[:200],
        }
    return sig


POOL_META_KEYS = ("pool", "pool_address", "pair_address", "pair", "amm", "lp")


def pool_hint(token_address: str) -> Optional[str]:
    """Pool/pair address carried by any recent buy's meta (enables mesh pool + verify_onchain)."""
    for b in reversed(state.recent_buys(token_address)):
        m = b.meta or {}
        for k in POOL_META_KEYS:
            v = m.get(k)
            if isinstance(v, str) and 32 <= len(v) <= 44:
                return v
    return None


async def attach_mesh(signals: List[Signal], *, timeout: float = 3.0) -> List[Signal]:
    """QP Data Mesh enrichment for each signal (throttled + cached; pending if slow)."""
    import asyncio

    from app.mesh import enrich_with_timeout

    async def one(sig: Signal) -> None:
        try:
            sig.mesh = await enrich_with_timeout(
                sig.token_address, sig.chain or "solana", pool=pool_hint(sig.token_address), timeout=timeout
            )
            if sig.mesh.get("status") == "ok" and (sig.symbol in ("?", "", None)) and sig.mesh.get("symbol"):
                sig.symbol = sig.mesh["symbol"]
        except Exception as exc:  # noqa: BLE001
            logger.warning("mesh attach soft-fail: %s", exc)
            sig.mesh = {"status": "error", "note": str(exc)[:200]}

    await asyncio.gather(*(one(s) for s in signals))
    return signals


def attach_pump(sig: Signal) -> Signal:
    """Attach curve / fast-gate / latency / stream / exit cards / paper from the pump lane registries."""
    try:
        from app.pump.fast_lane import card_extras

        ex = card_extras(sig.token_address)
        for k in ("curve", "fast_gate", "latency", "stream", "exit_signals", "paper"):
            setattr(sig, k, ex.get(k))
    except Exception as exc:  # noqa: BLE001
        logger.warning("pump lane attach soft-fail: %s", exc)
    return sig


def maybe_attach_gate(sig: Signal) -> Signal:
    """Final gate: mesh hard block always wins. Pre-migration curve coins: curve fast gate decides and
    Jev is advisory. Migrated / non-pump tokens: unchanged (Jev ok overrides; mesh rules otherwise)."""
    try:
        from app.risk_gate import combine, mesh_rule_decision

        attach_pump(sig)
        sig.gate = combine(sig.jev, mesh_rule_decision(sig.mesh), fast_gate=sig.fast_gate)
    except Exception as exc:  # noqa: BLE001
        logger.warning("gate soft-fail: %s", exc)
        sig.gate = {
            "action": "escalate_human",
            "decided_by": "fail_safe_error",
            "blocked": False,
            "safe_to_surface": False,
            "recommend_only": True,
            "auto_trade": False,
            "dry_run": True,
            "note": str(exc)[:200],
        }
    return sig


def maybe_attach_plan(sig: Signal) -> Signal:
    """Recommend-only Fluffy pump→dump ladder — never executes."""
    try:
        from app.trade_plan import attach_trade_plan

        attach_trade_plan(sig)
    except Exception as exc:  # noqa: BLE001
        logger.warning("trade plan soft-fail: %s", exc)
        sig.plan = {
            "recommend_only": True,
            "auto_trade": False,
            "live_exec": False,
            "note": str(exc)[:160],
        }
    return sig


def annotate_signals(signals: List[Signal]) -> List[Signal]:
    """Run Jev gate + final mesh/Jev gate + trade plan on each signal before UI/API surfaces.

    Call attach_mesh() first (async) so Jev state + rule gate see mesh data.
    """
    for sig in signals:
        maybe_attach_jev(sig)
        maybe_attach_gate(sig)
        maybe_attach_plan(sig)
    return signals
