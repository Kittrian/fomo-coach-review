"""v1.15 blocking pre-buy gate for Telegram-sourced live arms (fluffy_pump_dump, zyrox).

A Telegram call may only become a real buy when ALL of these hold (otherwise the buy is refused, with the reason
logged + written to the live ledger as `tg_buy_refused`):
  * bonding-curve lane, curve not complete / not migrated (re-read fresh right before the buy);
  * token younger than LIVE_TG_MAX_AGE_S (default 600 s; unknown age → refuse);
  * no scheduled launch: no meta.pump_at_iso and not in scout_filters.scheduled_mints (see LIVE_TG_SCHEDULED_STRICT);
  * the deployer has no observed rugs;
  * paper had not already opened this mint before this call;
  * the watcher says this is the first sighting (meta.first_seen is not False, repeat_count <= 1);
  * mesh rug/honeypot answer is ok, not blocked, not stale (checked by the caller and again inside the engine).
Read-only: never buys, sells or calls an exec endpoint.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from app.live.config import config


def _future_launch(pump_at_iso: Optional[str], slack_s: Optional[float] = None) -> bool:
    """True only when the parsed launch time is more than LIVE_TG_SCHEDULED_SLACK_S (default 300 s) in the future.
    A launch time that is now/past (the CA post at launch) does not refuse. Unparseable → treated as future (refuse)."""
    if not pump_at_iso:
        return False
    import os
    from datetime import datetime, timezone
    slack = float(os.getenv("LIVE_TG_SCHEDULED_SLACK_S", "300")) if slack_s is None else slack_s
    try:
        t = datetime.fromisoformat(str(pump_at_iso).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return True
    return t.timestamp() - time.time() > slack


def static_reasons(mint: str, curve: Optional[Dict[str, Any]], lane: str, meta: Dict[str, Any], *,
                   paper_seen_before: bool, ingest_ms: Optional[int] = None) -> List[str]:
    from app.scout.filters import filters as scout_filters

    cfg = config()
    meta = meta or {}
    why: List[str] = []
    c = curve or {}
    if lane != "curve":
        why.append(f"lane {lane} — Telegram live buys only on the pre-migration bonding curve")
    if c.get("complete") or c.get("phase") == "migrated":
        why.append("curve complete / migrated")
    if _future_launch(meta.get("pump_at_iso")):
        why.append(f"scheduled launch (pump_at_iso={meta.get('pump_at_iso')})")
    sched = scout_filters.scheduled_mints.get(mint)
    if sched:
        announced_earlier = bool(ingest_ms) and int(sched.get("ts_ms") or 0) < int(ingest_ms) - 2000
        if cfg["tg_scheduled_strict"]:
            why.append(f"mint in scheduled_mints ({sched.get('source')})")
        elif _future_launch(sched.get("pump_at_iso")) or announced_earlier:
            why.append(f"scheduled/pre-announced mint ({sched.get('source')})")
    why += scout_filters.deployer_flags(c.get("creator"))
    if paper_seen_before:
        why.append("paper already opened this mint before this call")
    if meta.get("first_seen") is False:
        why.append("watcher: not the first sighting of this CA")
    try:
        if int(meta.get("repeat_count") or 1) > 1:
            why.append(f"watcher: repeat #{int(meta.get('repeat_count'))} of this CA")
    except (TypeError, ValueError):
        pass
    return why


async def token_age_s(mint: str) -> Optional[float]:
    """Seconds since the bonding curve was created, or None when unknown (caller refuses on None)."""
    try:
        from app.scout.source import source
        r = await source.token_age(mint)
    except Exception:  # noqa: BLE001
        return None
    if not r.get("ok"):
        return None
    if r.get("created_s"):
        return max(0.0, time.time() - float(r["created_s"]))
    if r.get("older_than_s"):
        return float(r["older_than_s"])
    lower = time.time() - float(r["oldest_seen_s"]) if r.get("oldest_seen_s") else None
    if lower is not None and lower > config()["tg_max_age_s"]:
        return lower              # history deeper than we paged, and already older than the limit
    return None


def mesh_reasons(mesh: Optional[Dict[str, Any]], rule: Dict[str, Any]) -> List[str]:
    why: List[str] = []
    if rule.get("blocked"):
        why.append("mesh hard block: " + ", ".join(map(str, (rule.get("reasons") or [])[:3])))
    elif rule.get("mesh_status") != "ok":
        why.append(f"mesh {rule.get('mesh_status')} — no rug/honeypot answer")
    if mesh_stale(mesh):
        why.append("mesh data stale (timed out / stale risk source)")
    return why


def mesh_stale(mesh: Optional[Dict[str, Any]]) -> bool:
    """Stale for buying = an old answer served on timeout, or the token_risk source itself was stale.
    (Stale price/metadata sources alone don't block — the rug/honeypot answer is what matters.)"""
    m = mesh or {}
    return bool(m.get("timed_out") or ((m.get("sources") or {}).get("token_risk") or {}).get("stale"))
