"""v1.41 late-entry filter helpers."""
from app.live import engine


def test_age_cap_default_off(monkeypatch):
    monkeypatch.delenv("LIVE_MAX_SIGNAL_AGE_MS", raising=False)
    assert engine._max_signal_age_ms() == 0


def test_age_cap_parse(monkeypatch):
    monkeypatch.setenv("LIVE_MAX_SIGNAL_AGE_MS", "2200")
    assert engine._max_signal_age_ms() == 2200
    monkeypatch.setenv("LIVE_MAX_SIGNAL_AGE_MS", "junk")
    assert engine._max_signal_age_ms() == 0
    monkeypatch.setenv("LIVE_MAX_SIGNAL_AGE_MS", "-5")
    assert engine._max_signal_age_ms() == 0


def test_age_arms(monkeypatch):
    monkeypatch.delenv("LIVE_SIGNAL_AGE_ARMS", raising=False)
    assert engine._signal_age_arms() == {"scout_solo", "scout_confirmed"}
    monkeypatch.setenv("LIVE_SIGNAL_AGE_ARMS", "scout_solo")
    assert engine._signal_age_arms() == {"scout_solo"}


def test_closure_has_age_gate():
    import inspect
    src = inspect.getsource(engine.LiveEngine._buy) if hasattr(engine, "LiveEngine") else open(engine.__file__).read()
    assert "late_entry_refused_pre_sign" in src and "_max_signal_age_ms()" in src
