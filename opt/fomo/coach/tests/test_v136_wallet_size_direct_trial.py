"""v1.36: per-wallet scout size, per-arm direct-buy trial, kol back under the loss limit."""
import importlib


def test_wallet_sizes_parse(monkeypatch):
    from app.live import config as lc
    monkeypatch.setenv("LIVE_SCOUT_WALLET_SIZE_SOL", "AAA:0.10, BBB:0.1,bad,CCC:x,DDD:0")
    c = lc.config()
    assert c["scout_wallet_sizes"] == {"AAA": 0.10, "BBB": 0.1}
    monkeypatch.setenv("LIVE_SCOUT_WALLET_SIZE_SOL", "")
    assert lc.config()["scout_wallet_sizes"] == {}


def test_mode_for(monkeypatch):
    from app.live import pumpdirect as pd
    monkeypatch.setenv("LIVE_DIRECT_PUMP_ENABLED", "true")
    monkeypatch.setenv("LIVE_DIRECT_PUMP_MODE", "shadow")
    monkeypatch.setenv("LIVE_DIRECT_PUMP_LIVE_ARMS", "kol_confluence")
    monkeypatch.delenv("LIVE_DIRECT_PUMP_LIVE_SIDES", raising=False)
    assert pd.mode() == "shadow"
    assert pd.mode_for("buy", "kol_confluence") == "live"
    assert pd.mode_for("sell", "kol_confluence") == "shadow"
    assert pd.mode_for("buy", "scout_solo") == "shadow"
    assert pd.mode_for("buy", None) == "shadow"
    monkeypatch.setenv("LIVE_DIRECT_PUMP_LIVE_ARMS", "")
    assert pd.mode_for("buy", "kol_confluence") == "shadow"
    monkeypatch.setenv("LIVE_DIRECT_PUMP_ENABLED", "false")
    assert pd.mode_for("buy", "kol_confluence") == "off"
    monkeypatch.setenv("LIVE_DIRECT_PUMP_ENABLED", "true")
    monkeypatch.setenv("LIVE_DIRECT_PUMP_MODE", "live")
    assert pd.mode_for("sell", "scout_solo") == "live"


def test_loss_limit_default_counts_kol(monkeypatch):
    from app.live import config as lc
    monkeypatch.setenv("LIVE_LOSS_LIMIT_EXEMPT_ARMS", "")
    assert lc.config()["loss_limit_exempt_arms"] == []
