#!/usr/bin/env python3
"""v2 tick fetcher for the Fomo self-learner (PAPER / OFFLINE data only). Added Oct 15, 2026.

Runs from fomo-learn-ticks.timer (~3:15 AM CT, before fomo-learn at 4:07) as user `fomo`, no credentials.
For every paper exit-variant signal and live buy from the last LOOKBACK days that has no complete tick file yet,
pulls the public pump.fun trade history (swap-api.pump.fun/v2/coins/<mint>/trades, newest first, cursor paged) and
stores trades from entry-2 min to entry+3 h in data/learn/ticks/<mint>.json as [t_s, price_sol, side, sol, wallet8].
Writes ONLY data/learn/ticks/ and a line in data/learn/learn.log. Rate-limited (public endpoint, ~1 req/1.5 s).
"""
import json, os, time, urllib.request
from datetime import datetime
from pathlib import Path

DATA = Path(os.getenv("FOMO_DATA_DIR", "/opt/fomo/coach/data"))
OUT = DATA / "learn" / "ticks"
LOOKBACK_D = float(os.getenv("LEARN_TICKS_LOOKBACK_D", "3"))
MAXP = int(os.getenv("LEARN_TICKS_MAX_PAGES", "80"))
SLP = float(os.getenv("LEARN_TICKS_SLEEP", "1.5"))
BUDGET_S = float(os.getenv("LEARN_TICKS_BUDGET_S", "2700"))
HORIZON = 3 * 3600


def jl(name, kinds):
    p = DATA / name
    if not p.exists():
        return
    with p.open(errors="replace") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("kind") in kinds:
                yield r


def get(u):
    for i in range(5):
        try:
            q = urllib.request.Request(u, headers={"user-agent": "Mozilla/5.0", "accept": "application/json"})
            return json.load(urllib.request.urlopen(q, timeout=20))
        except Exception:  # noqa: BLE001
            time.sleep(20 * (i + 1))
    return None


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def main():
    t0 = time.time()
    OUT.mkdir(parents=True, exist_ok=True)
    since = (t0 - LOOKBACK_D * 86400) * 1000
    want = {}
    for r in jl("exit_variants.jsonl", {"variant_closed"}):
        if r.get("variant") == "A" and r.get("entry_ms", 0) >= since:
            want[r["mint"]] = min(want.get(r["mint"], 1e18), r["entry_ms"])
    for r in jl("live_trades.jsonl", {"buy"}):
        e = r.get("entry_ms") or r["ts_ms"]
        if e >= since:
            want[r["mint"]] = min(want.get(r["mint"], 1e18), e)
    done = fetched = skipped = 0
    for m, e in sorted(want.items(), key=lambda kv: kv[1]):
        f = OUT / f"{m}.json"
        if f.exists():
            try:
                if json.loads(f.read_text()).get("complete"):
                    done += 1
                    continue
            except ValueError:
                pass
        if not m.endswith("pump") or time.time() - t0 > BUDGET_S or t0 < e / 1000 + HORIZON:
            skipped += 1
            continue
        lo, hi = e / 1000 - 120, e / 1000 + HORIZON
        cur, keep, pages, complete = "0", [], 0, False
        while pages < MAXP:
            d = get(f"https://swap-api.pump.fun/v2/coins/{m}/trades?limit=100&cursor={cur}")
            pages += 1
            time.sleep(SLP)
            if not d:
                break
            tr = d.get("trades") or []
            for t in tr:
                x = ts(t["timestamp"])
                if lo <= x <= hi:
                    try:
                        keep.append([x, float(t["priceSol"]), t["type"][0], float(t.get("amountSol") or 0),
                                     (t.get("userAddress") or "")[:8]])
                    except (TypeError, ValueError, KeyError):
                        pass
            if not tr or ts(tr[-1]["timestamp"]) < lo:
                complete = True
                break
            pg = d.get("pagination") or {}
            if not pg.get("hasMore"):
                complete = True
                break
            cur = pg["nextCursor"]
        keep.sort()
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps({"mint": m, "entry_ms": e, "complete": complete, "pages": pages, "ticks": keep}))
        os.replace(tmp, f)
        fetched += 1
    with (DATA / "learn" / "learn.log").open("a") as fh:
        fh.write(f"{datetime.now().strftime('%F %T')} ticks ok want={len(want)} have={done} fetched={fetched} "
                 f"skipped={skipped} {time.time() - t0:.0f}s\n")


if __name__ == "__main__":
    main()
