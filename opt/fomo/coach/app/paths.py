"""Where runtime data lives. COACH_DATA_DIR overrides it (the test suite points it at a temp dir)."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REAL_DATA_DIR = ROOT / "data"


def data_dir() -> Path:
    d = os.getenv("COACH_DATA_DIR", "").strip()
    return Path(d) if d else REAL_DATA_DIR


def data_path(env: str, name: str) -> Path:
    """Path from env var `env`, else data_dir()/name."""
    v = os.getenv(env, "").strip()
    return Path(v) if v else data_dir() / name
