#!/usr/bin/env python3
"""v1.16 one-off data hygiene (non-destructive): mark bad rows instead of deleting them.

  data/scout_trades.jsonl  — "excluded": "test_fixture" (MintAAAA… rows written by old tests),
                             "recap_post" (SmugCalls recap/brag posts + their paper positions),
                             "restart_gap" (paper positions whose marks were interrupted by a restart)
  data/paper_trades.jsonl  — append-only {"kind":"excluded","excluded":"restart_gap"} markers for opens with no close

Every file is copied to data/archive/20261007-fomoant/ before it is changed. Rows are never removed or reordered;
marked rows only gain an "excluded" key. Dry run by default; --apply writes. live_trades.jsonl / live_state.json are
never opened for writing.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ingest.recap import recap_reason  # noqa: E402

NEVER = {"live_trades.jsonl", "live_state.json"}


def archive(data: Path, name: str, tag: str) -> Path:
    if name in NEVER:
        raise SystemExit(f"refusing to touch {name}")
    dst_dir = data / "archive" / tag
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / name
    if not dst.exists():                      # keep the FIRST (pre-change) copy
        shutil.copy2(data / name, dst)
    return dst


def mark_scout(lines):
    rows = []
    for l in lines:
        try:
            rows.append(json.loads(l))
        except ValueError:
            rows.append(None)
    recap_sig, gap_ids = set(), set()
    for r in rows:
        if not r:
            continue
        if r.get("kind") == "signal" and r.get("arm") == "smugcalls_control" and recap_reason(r.get("snippet")):
            recap_sig.add(r.get("id"))
        if r.get("kind") == "pos" and "restart_gap" in ((r.get("pos") or {}).get("flags") or []):
            gap_ids.add((r.get("pos") or {}).get("id"))
    counts = {"test_fixture": 0, "recap_post": 0, "restart_gap": 0}
    out = []
    for l, r in zip(lines, rows):
        if not r or r.get("excluded"):
            out.append(l)
            continue
        mint = str(r.get("mint") or (r.get("pos") or {}).get("mint") or "")
        ex = None
        if mint.startswith("MintAAAA"):
            ex = "test_fixture"
        elif (r.get("kind") == "signal" and r.get("id") in recap_sig) or \
                (r.get("kind") == "pos" and (r.get("pos") or {}).get("signal_id") in recap_sig):
            ex = "recap_post"
        elif r.get("kind") == "pos" and (r.get("pos") or {}).get("id") in gap_ids:
            ex = "restart_gap"
        if ex:
            counts[ex] += 1
            out.append(json.dumps({**r, "excluded": ex}) + "\n")
        else:
            out.append(l)
    return out, counts, {"recap_signals": len(recap_sig), "restart_gap_positions": len(gap_ids)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=str(Path(__file__).resolve().parents[1] / "data"))
    ap.add_argument("--tag", default="20261007-fomoant")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    data = Path(a.data_dir)
    report = {"apply": a.apply}
    sp = data / "scout_trades.jsonl"
    if sp.exists():
        for attempt in range(5):
            st = sp.stat()
            lines = sp.read_text().splitlines(keepends=True)
            out, counts, info = mark_scout(lines)
            report["scout_trades"] = {"rows": len(lines), "marked": counts, **info}
            if not a.apply or not any(counts.values()):
                break
            report["scout_trades"]["archive"] = str(archive(data, sp.name, a.tag))
            tmp = sp.with_name(sp.name + ".v116tmp")
            tmp.write_text("".join(out))
            now = sp.stat()
            if (now.st_size, now.st_mtime_ns) != (st.st_size, st.st_mtime_ns):   # the coach appended meanwhile: redo
                tmp.unlink()
                time.sleep(0.2)
                continue
            os.replace(tmp, sp)
            break
    pp = data / "paper_trades.jsonl"
    if pp.exists():
        if a.apply:
            report["paper_trades_archive"] = str(archive(data, pp.name, a.tag))
            from app.pump import paper as paper_mod
            old = paper_mod.LOG_PATH
            paper_mod.LOG_PATH = pp
            try:
                report["paper_restart_gap_markers_appended"] = paper_mod.PaperEngine.mark_restart_orphans()
            finally:
                paper_mod.LOG_PATH = old
        else:
            opened, closed = set(), set()
            for l in pp.read_text().splitlines():
                r = json.loads(l)
                (opened if r.get("kind") == "open" else closed if r.get("kind") == "close" else set()).add(r.get("id"))
            report["paper_open_without_close"] = len(opened - closed)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
