"""v1.32 `paper_top4` — PAPER-ONLY copy of the four best paper_v3 wallets (Andrew approved Oct 14, 8:06 PM CT).

Wallets: Yenni, Lynk, Inquixit, Zef (list comes from app.learn_overrides, default these four; the daily learner may
swap handles on paper only, always a subset of the paper_v3 roster). Exit of record: half2x_c30 (sell half at 2x, 30 s
candle stop on the rest); live_exit / follow_wallet tracks are kept for comparison. Same machinery and guarantees as
paper_v3 (subclass): never imports the signer, never calls Jupiter, never touches live positions / caps / kill.
Fill gate for promotion: fill_vs_trader ≤ PAPER_TOP4_MAX_FILL_GAP_PCT (5%) — recorded per entry, not enforced.
Feed: the paper_v3 scout-websocket subscriptions (app.scout.engine routes role paper_v3 events here too), so this arm
needs PAPER_V3_ENABLED=true. Own 30-day window, log data/paper_top4.jsonl, state data/paper_top4_state.json.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

from app.paper_fomo_top import _f
from app.paper_v3 import PaperV3, cfg as v3cfg
from app.paths import data_dir

ARM = "paper_top4"


def cfg() -> Dict[str, Any]:
    base = v3cfg()
    try:
        from app import learn_overrides
        handles = set(learn_overrides.get()["top4_handles"])
    except Exception:  # noqa: BLE001
        handles = {"Yenni", "Lynk", "Inquixit", "Zef"}
    on = os.getenv("PAPER_TOP4_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
    return {**base,
            "enabled": bool(on and base["enabled"]),
            "targets": {a: h for a, h in base["targets"].items() if h in handles},
            "whales": {}, "watch": {},
            "days": _f("PAPER_TOP4_DAYS", 30),
            "max_fill_gap_pct": _f("PAPER_TOP4_MAX_FILL_GAP_PCT", 5),
            "log_path": Path(os.getenv("PAPER_TOP4_LOG_PATH", str(data_dir() / "paper_top4.jsonl"))),
            "state_path": Path(os.getenv("PAPER_TOP4_STATE_PATH", str(data_dir() / "paper_top4_state.json")))}


class PaperTop4(PaperV3):
    arm = ARM

    def cfg(self) -> Dict[str, Any]:
        return cfg()

    def summary(self, recent: int = 15) -> Dict[str, Any]:  # type: ignore[override]
        s = super().summary(recent)
        c = self.cfg()
        s.update(exit_of_record="half2x_c30", handles=sorted(c["targets"].values()),
                 promotion_rule=(f"≥50 closes per wallet, PF > 1.3 with the top 3 trades removed, median fill-vs-trader "
                                 f"≤ {c['max_fill_gap_pct']:g}% — then a LIVE suggestion for Andrew (never automatic)"))
        return s


paper = PaperTop4()
