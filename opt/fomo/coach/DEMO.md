# Demo loop — verified 2026-10-02 (CT)

Coach version **1.9.0-demo** on `http://127.0.0.1:8790` (FOMOAPI unset, `DEMO_MODE=true`, recommend-only). Jev gate enabled; Cloudflare creds optional (degraded `unavailable` path).

## Start

```bash
cd /workspace/fomo-signal-coach
./run.sh
```

## Health

```bash
curl -s http://127.0.0.1:8790/health | python3 -m json.tool
```

Expect: `"version": "1.9.0-demo"`, `"auto_trade": false`, `"recommend_only": true`, `"jev_gate": { "status": "unset" | "configured", ... }`, `"jupiter_exec": { "status": "disabled", ... }`.

## Auto demo loop (1.9.1+)

When `DEMO_MODE=true`, the coach **auto-seeds** DEMOA/DEMOB recommend-only buys on startup and every ~90s (`app/demo_loop.py`). Check `/health` → `demo_loop.running`. Manual script below is optional.

## Synthetic Live Feed buys

```bash
cd /workspace/fomo-signal-coach
.venv/bin/python scripts/demo_feed.py
```

Posts 7 `swap_buy` activities (DEMOA ×4 traders, DEMOB ×3) to `POST /api/ingest/fomo-activity`.

## Signals (+ Jev annotation)

```bash
curl -s 'http://127.0.0.1:8790/api/signals?min_score=50' | python3 -m json.tool
```

Verified sample (scores may vary slightly with window/timing):

- `$DEMOA` coach score ~**93.0**, unique_traders **4**, recommend_only **true**
- `$DEMOB` coach score ~**84.0**, unique_traders **3**, recommend_only **true**

Without Cloudflare creds, each card includes fail-closed Jev:

```json
"jev": {
  "jev_status": "unavailable",
  "action": "escalate_human",
  "safe_to_surface": false,
  "recommend_only": true,
  "auto_trade": false,
  "note": "Cloudflare Workers AI credentials missing ..."
}
```

With creds set, live `action` / `signal_quality` / `safe_to_surface` come from `typesafe/jev`.

Detail:

```bash
curl -s 'http://127.0.0.1:8790/api/signals/DemoToken1111111111111111111111111111111' | python3 -m json.tool
```


## Jupiter quote (dry-run)

```bash
curl -s http://127.0.0.1:8790/health | python3 -c "import sys,json; print(json.load(sys.stdin)['jupiter_exec'])"
curl -s -X POST http://127.0.0.1:8790/api/exec/quote \
  -H 'Content-Type: application/json' \
  -d '{"mint":"DemoToken1111111111111111111111111111111","side":"buy"}' | python3 -m json.tool
```

Keep `JUPITER_EXEC_ENABLED=false`. Do not enable on main funds. UI chips call the same quote endpoint.

## Enable live Jev

```bash
# .env — placeholders only in git; fill locally
JEV_GATE_ENABLED=true
CLOUDFLARE_ACCOUNT_ID=...
CLOUDFLARE_API_TOKEN=...   # Workers AI permission

.venv/bin/python scripts/jev_live_check.py   # skips if unset
```

## UI

Open [http://127.0.0.1:8790/ui/](http://127.0.0.1:8790/ui/) — dark recommend cards polling `/api/signals` with a Jev chip. JSON root stays at `/`.

## Tests

```bash
.venv/bin/python -m pytest tests/ -q
```
