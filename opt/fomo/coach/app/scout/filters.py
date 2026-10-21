"""Skip filters for the scout arms — free data only.

* migrated          — real-time: TradeEvent/curve shows the curve complete (or no pump curve) → skip.
* scheduled launch  — real-time: the mint was announced by a scheduled-launch/deployer channel (Fluffy, or any
                      Telegram post carrying a parsed "pump at" time) → skip.
* deployer rugs     — real-time, LEARNED from our own observations (no paid API): a creator is flagged after a
                      token of theirs that we tracked dumped ≥80% from its peak within 30 min while on the curve.
                      Starts empty and grows; persisted to data/scout_filters.json.
* dump wallets      — PARTIAL / TODO: the first 20 buyers of every entered mint are scanned in the background
                      (free RPC, low priority). Wallets that show up among the first 20 buyers of ≥2 observed
                      dumps get labelled. At entry time the scan usually isn't finished (fills are at +2 s/+5 s),
                      so the k≥3 check is recorded post-hoc on the trade and reported as a split, not used as a
                      real-time skip. TODO: make it real-time once the labelled set is large enough to pre-filter
                      by watching those wallets directly.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from app.scout import SCHEDULED_SOURCES

from app.paths import data_dir  # noqa: E402
DATA_DIR = data_dir()
PATH = Path(os.getenv("SCOUT_FILTERS_PATH", str(DATA_DIR / "scout_filters.json")))
RUG_DROP = float(os.getenv("SCOUT_RUG_DROP_PCT", "0.80"))
RUG_WINDOW_S = float(os.getenv("SCOUT_RUG_WINDOW_S", "1800"))
DUMP_LABEL_MIN = int(os.getenv("SCOUT_DUMP_LABEL_MIN", "2"))
DUMP_K = int(os.getenv("SCOUT_DUMP_K", "3"))


class Filters:
    def __init__(self) -> None:
        self.scheduled_mints: Dict[str, Dict[str, Any]] = {}
        self.deployers: Dict[str, Dict[str, Any]] = {}       # creator -> {tokens: [...], rugs: [...]}
        self.first_buyers: Dict[str, List[str]] = {}         # mint -> first 20 buyers
        self.dump_counts: Dict[str, int] = {}                # wallet -> # observed dumps it was early in
        self.dumped_mints: Set[str] = set()
        self._dirty = False
        self.load()

    # ---------------- persistence ----------------
    def load(self) -> None:
        try:
            d = json.loads(PATH.read_text())
        except Exception:
            return
        self.deployers = d.get("deployers", {})
        self.first_buyers = d.get("first_buyers", {})
        self.dump_counts = d.get("dump_counts", {})
        self.dumped_mints = set(d.get("dumped_mints", []))
        self.scheduled_mints = d.get("scheduled_mints", {})

    def save(self, force: bool = False) -> None:
        if not (self._dirty or force):
            return
        try:
            PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = PATH.with_suffix(".tmp")
            fb = dict(list(self.first_buyers.items())[-3000:])
            tmp.write_text(json.dumps({"saved_ms": int(time.time() * 1000), "deployers": self.deployers,
                                       "first_buyers": fb, "dump_counts": self.dump_counts,
                                       "dumped_mints": sorted(self.dumped_mints)[-5000:],
                                       "scheduled_mints": dict(list(self.scheduled_mints.items())[-2000:])}))
            tmp.replace(PATH)
            self._dirty = False
        except Exception:
            pass

    # ---------------- scheduled-launch channels ----------------
    def note_telegram(self, source: str, mint: str, meta: Dict[str, Any]) -> None:
        if source in SCHEDULED_SOURCES or (meta or {}).get("pump_at_iso"):
            self.scheduled_mints[mint] = {"source": source, "ts_ms": int(time.time() * 1000),
                                          "pump_at_iso": (meta or {}).get("pump_at_iso")}
            self._dirty = True

    # ---------------- deployers ----------------
    def deployer_flags(self, creator: Optional[str]) -> List[str]:
        if not creator:
            return []
        d = self.deployers.get(creator) or {}
        return [f"deployer had {len(d['rugs'])} observed rug(s)"] if d.get("rugs") else []

    def note_token(self, creator: Optional[str], mint: str) -> None:
        if not creator:
            return
        d = self.deployers.setdefault(creator, {"tokens": [], "rugs": []})
        if mint not in d["tokens"]:
            d["tokens"] = (d["tokens"] + [mint])[-50:]
            self._dirty = True

    def note_outcome(self, *, creator: Optional[str], mint: str, peak_price: float, price: float,
                     peak_ms: int, created_ms: Optional[int], now_ms: int) -> Optional[str]:
        """Called by the engine's mark loop. Labels rugs (deployer) and dumps (early buyers)."""
        if mint in self.dumped_mints or not peak_price or price is None:
            return None
        drop = 1 - price / peak_price
        base = created_ms or peak_ms
        if drop >= RUG_DROP and now_ms - base <= RUG_WINDOW_S * 1000:
            self.dumped_mints.add(mint)
            if creator:
                d = self.deployers.setdefault(creator, {"tokens": [], "rugs": []})
                if mint not in d["rugs"]:
                    d["rugs"].append(mint)
            if created_ms and peak_ms - created_ms <= 15 * 60_000:      # MadeOnSol-style: peak <15 min
                for w in self.first_buyers.get(mint, []):
                    self.dump_counts[w] = self.dump_counts.get(w, 0) + 1
            self._dirty = True
            return "dump"
        return None

    # ---------------- dump wallets ----------------
    def dump_wallets_k(self, mint: str) -> Optional[int]:
        fb = self.first_buyers.get(mint)
        if fb is None:
            return None
        return sum(1 for w in fb if self.dump_counts.get(w, 0) >= DUMP_LABEL_MIN)

    def labelled_dump_wallets(self) -> int:
        return sum(1 for v in self.dump_counts.values() if v >= DUMP_LABEL_MIN)

    def status(self) -> Dict[str, Any]:
        return {"scheduled_mints": len(self.scheduled_mints), "deployers_tracked": len(self.deployers),
                "deployers_with_rugs": sum(1 for d in self.deployers.values() if d.get("rugs")),
                "first20_scanned": len(self.first_buyers), "dumped_mints_observed": len(self.dumped_mints),
                "labelled_dump_wallets": self.labelled_dump_wallets(), "dump_k": DUMP_K,
                "dump_filter_mode": "post-hoc annotation (TODO real-time)"}


filters = Filters()
