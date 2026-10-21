#!/usr/bin/env python3
"""Weekly scorecard: candidates (entered + skipped) × shadow outcomes, split by arm and skip reason.

DESCRIPTIVE ONLY — it never changes thresholds, sizes, arms or settings (no auto-tuning, by design).
Usage:  .venv/bin/python scripts/scorecard.py [--days 7] [--min-version 1.16.0-demo] [--json] [--out FILE]
Reads data/candidates.jsonl and data/candidate_outcomes.jsonl (COACH_DATA_DIR overrides the data dir).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.learning.scorecard import summarize, to_markdown  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=float, default=7.0)
    ap.add_argument("--min-version", default="1.16.0-demo")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--out", default="")
    a = ap.parse_args(argv)
    s = summarize(days=a.days, min_version=a.min_version)
    text = json.dumps(s, indent=2) if a.json else to_markdown(s)
    if a.out:
        Path(a.out).write_text(text)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
