"""v1.16 append-only log of every scout/confirmer trade the scout lane detected (data/scout_detections.jsonl).

Rows: {"ts_ms","kind":"trade","wallet","label","role","sig","mint","is_buy","sol","path","decode_path","venue",
       "quote_mint","quote_is_sol","ix_name","event_version"}
plus {"ts_ms","kind":"heartbeat"} every SCOUT_HEARTBEAT_S (default 300 s) while the lane runs. The daily audit
(app/scout/audit.py) only counts on-chain buys that happened while heartbeats show the lane was up.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from app.paths import data_path

logger = logging.getLogger("coach.scout.detections")


def path() -> Path:
    return data_path("SCOUT_DETECTIONS_PATH", "scout_detections.jsonl")


def heartbeat_s() -> float:
    try:
        return float(os.getenv("SCOUT_HEARTBEAT_S", "300"))
    except ValueError:
        return 300.0


def append(row: Dict[str, Any]) -> None:
    try:
        p = path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as fh:
            fh.write(json.dumps({"ts_ms": int(time.time() * 1000), **row}, default=str) + "\n")
    except Exception as exc:  # noqa: BLE001
        logger.warning("detections log failed: %s", type(exc).__name__)


def rows(since_ms: int = 0) -> Iterator[Dict[str, Any]]:
    p = path()
    if not p.exists():
        return
    with p.open() as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if int(r.get("ts_ms") or 0) >= since_ms:
                yield r


def coverage(since_ms: int, now_ms: Optional[int] = None, *, live_since_ms: Optional[int] = None) -> List[Tuple[int, int]]:
    """Intervals [start, end] (ms) when the lane was up, from heartbeats/trade rows ≤ 1.4 × heartbeat apart.
    `live_since_ms` (this process's start) extends coverage to now."""
    now_ms = now_ms or int(time.time() * 1000)
    gap = int(heartbeat_s() * 1000 * 1.4)
    ts = sorted(int(r["ts_ms"]) for r in rows(since_ms - gap))
    out: List[Tuple[int, int]] = []
    for t in ts:
        if out and t - out[-1][1] <= gap:
            out[-1] = (out[-1][0], t)
        else:
            out.append((t, t))
    if live_since_ms:
        out.append((max(live_since_ms, since_ms), now_ms))
    out.sort()
    merged: List[Tuple[int, int]] = []
    for a, b in out:
        if merged and a <= merged[-1][1] + gap:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return [(max(a, since_ms), b) for a, b in merged if b >= since_ms]


def covered(t_ms: int, cov: List[Tuple[int, int]], *, slack_ms: int = 0) -> bool:
    return any(a - slack_ms <= t_ms <= b + slack_ms for a, b in cov)
