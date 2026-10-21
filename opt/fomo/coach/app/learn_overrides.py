"""v1.32 PAPER-ONLY overrides written by the daily self-learning job (scripts/learn_daily.py).

Only paper arms read this module (exit-variants CHASE5 gate, paper_top4 wallet list). The live engine never
imports it (enforced by tests/test_v132_learn.py). Every value is clamped to hard bounds here, so a bad or
hand-edited file can never push a paper rule outside the ranges Andrew approved:
  scout_chase_cap_pct  3 … 15  (default 5)
  top4_handles         subset of the paper_v3 targets (default Yenni, Lynk, Inquixit, Zef)
File: data/learn/paper_overrides.json  ({"values": {...}, "applied": [...]}). Missing/invalid → defaults.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List

from app.paths import data_dir

BOUNDS = {"scout_chase_cap_pct": (3.0, 15.0)}
DEFAULTS: Dict[str, Any] = {"scout_chase_cap_pct": 5.0, "top4_handles": ["Yenni", "Lynk", "Inquixit", "Zef"]}
ALLOWED_HANDLES = {"Numer0", "Inquixit", "Lynk", "peacefuldestroy", "Tom", "brunowsky", "Zef", "Veloce", "Gucci",
                   "Yenni", "Mitch"}
_cache: Dict[str, Any] = {"t": 0.0, "v": None}


def path():
    return data_dir() / "learn" / "paper_overrides.json"


def clamp(values: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(DEFAULTS)
    try:
        c = float(values.get("scout_chase_cap_pct", out["scout_chase_cap_pct"]))
        lo, hi = BOUNDS["scout_chase_cap_pct"]
        out["scout_chase_cap_pct"] = max(lo, min(hi, c))
    except (TypeError, ValueError):
        pass
    hs = values.get("top4_handles")
    if isinstance(hs, list):
        hs2: List[str] = [h for h in hs if isinstance(h, str) and h in ALLOWED_HANDLES][:6]
        if hs2:
            out["top4_handles"] = hs2
    return out


def get() -> Dict[str, Any]:
    now = time.time()
    if _cache["v"] is not None and now - _cache["t"] < 60:
        return _cache["v"]
    v = dict(DEFAULTS)
    try:
        p = path()
        if p.exists():
            v = clamp((json.loads(p.read_text()) or {}).get("values") or {})
    except Exception:  # noqa: BLE001 — paper add-on: fall back to defaults
        v = dict(DEFAULTS)
    if os.getenv("PAPER_LEARN_OVERRIDES", "true").strip().lower() in {"0", "false", "no", "off"}:
        v = dict(DEFAULTS)
    _cache.update(t=now, v=v)
    return v


def reset_cache() -> None:
    _cache.update(t=0.0, v=None)
