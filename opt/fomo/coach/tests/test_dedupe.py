"""v1.12.1: one Fluffy paper entry per mint (no duplicate open logs) + ingest pipeline dedupe per (source, mint).
Offline: Jupiter quote / fast lane / mesh are monkeypatched."""
import asyncio
import json

from fastapi.testclient import TestClient

from app.pump import paper as P

MINT = "DwcXyhEcSvzWgakDKpetZLFvbAHutpGpzhbU4iempump"
MIGRATED = {"status": "ok", "complete": True, "quote_is_sol": True, "price_sol": 8e-7}


def _fake_quote(calls):
    def q(inp, out, amount):
        calls.append(amount)
        import time
        time.sleep(0.05)  # widen the old race window
        return {"ok": True, "out_amount": 470_000_000_000, "router": "metis", "elapsed_ms": 50.0, "error": None}
    return q


def test_concurrent_open_logs_once_and_never_reopens(tmp_path, monkeypatch):
    log = tmp_path / "paper.jsonl"
    monkeypatch.setattr(P, "LOG_PATH", log)
    monkeypatch.setattr(P.PaperEngine, "_ensure_ticker", lambda self: None)
    calls = []
    monkeypatch.setattr(P, "_jupiter_quote", _fake_quote(calls))
    eng = P.PaperEngine()

    async def go():
        return await asyncio.gather(*[eng.open_from_curve(MINT, MIGRATED, source="fluffy/telegram") for _ in range(3)])

    res = asyncio.run(go())
    assert sum(1 for r in res if r) == 1
    assert log.read_text().count('"kind": "open"') == 1
    pos = eng.positions[MINT]
    eng._close(pos, "max_hold")
    # a later repeat of the same CA must not re-open the closed slot
    assert asyncio.run(eng.open_from_curve(MINT, MIGRATED, source="fluffy/telegram")) is None
    assert log.read_text().count('"kind": "open"') == 1


def test_opened_mints_seeded_from_log(tmp_path, monkeypatch):
    log = tmp_path / "paper.jsonl"
    log.write_text(json.dumps({"kind": "open", "mint": MINT, "profile": "fluffy_pump_dump"}) + "\n"
                   + json.dumps({"kind": "open", "mint": "Other111", "profile": "fomo_top_trader"}) + "\n")
    monkeypatch.setattr(P, "LOG_PATH", log)
    eng = P.PaperEngine()
    assert eng.already_opened(MINT) and not eng.already_opened("Other111")


def test_ingest_pipeline_runs_once_per_source_mint(monkeypatch):
    from app.ingest import buy as B
    from app.main import app
    sched, mesh = [], []
    monkeypatch.setattr(B.fast_lane, "schedule", lambda mint, **k: sched.append((mint, k.get("source"))) or True)
    monkeypatch.setattr(B, "schedule_enrichment", lambda *a, **k: mesh.append(a) or True)
    B._seen_pipeline.clear()
    c = TestClient(app)
    body = {"trader_id": "tg:fluffyalpha", "token_address": MINT, "source": "fluffy/telegram"}
    r1 = c.post("/api/ingest/buy", json=body).json()
    r2 = c.post("/api/ingest/buy", json=body).json()
    r3 = c.post("/api/ingest/buy", json={**body, "source": "telegram", "trader_id": "tg:zyrox"}).json()
    assert r1["first_seen"] is True and r1["fast_lane_scheduled"] is True
    assert r2["duplicate"] is True and r2["repeat_count"] == 2 and r2["fast_lane_scheduled"] is False
    assert r3["first_seen"] is True
    assert sched == [(MINT, "fluffy/telegram"), (MINT, "telegram")] and len(mesh) == 2
    B._seen_pipeline.clear()
