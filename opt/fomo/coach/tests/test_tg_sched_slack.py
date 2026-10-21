import time
from datetime import datetime, timezone, timedelta
from app.live import tg_gate
from app.scout.filters import filters as F

M = "DrySched111111111111111111111111111111111pump"
CURVE = {"complete": False, "phase": "curve"}

def iso(dt): return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _run(monkeypatch, strict, pa):
    monkeypatch.setenv("LIVE_TG_SCHEDULED_STRICT", strict)
    F.scheduled_mints.pop(M, None)
    meta = {"pump_at_iso": pa}
    F.note_telegram("fluffy/telegram", M, meta)
    try:
        return tg_gate.static_reasons(M, CURVE, "curve", meta, paper_seen_before=False, ingest_ms=int(time.time() * 1000))
    finally:
        F.scheduled_mints.pop(M, None)

def test_bare_ca_allowed(monkeypatch):
    assert _run(monkeypatch, "false", None) == []

def test_now_and_past_allowed(monkeypatch):
    now = datetime.now(timezone.utc)
    assert _run(monkeypatch, "false", iso(now)) == []
    assert _run(monkeypatch, "false", iso(now - timedelta(hours=3))) == []
    assert _run(monkeypatch, "false", iso(now + timedelta(minutes=3))) == []

def test_future_refused(monkeypatch):
    r = _run(monkeypatch, "false", iso(datetime.now(timezone.utc) + timedelta(hours=2)))
    assert any("scheduled launch" in w for w in r)

def test_strict_still_refuses(monkeypatch):
    assert any("scheduled_mints" in w for w in _run(monkeypatch, "true", None))

def test_other_gates_kept(monkeypatch):
    monkeypatch.setenv("LIVE_TG_SCHEDULED_STRICT", "false")
    r = tg_gate.static_reasons(M, {"complete": True}, "amm", {"first_seen": False, "repeat_count": 3}, paper_seen_before=True)
    assert len(r) >= 4
