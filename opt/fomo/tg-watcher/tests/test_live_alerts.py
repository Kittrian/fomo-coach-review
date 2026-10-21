import alerts


def test_format_live_buy_has_solscan():
    t = alerts.format_live_alert({"kind": "live_buy", "arm": "zyrox", "mint": "MintX", "sol": 0.4,
                                  "signature": "SIGX", "solscan": "https://solscan.io/tx/SIGX", "edge_gap_pct": 3.2})
    assert "LIVE BUY" in t and "https://solscan.io/tx/SIGX" in t and "0.4 SOL" in t and "MintX" in t


def test_format_live_sell_pnl():
    t = alerts.format_live_alert({"kind": "live_sell", "arm": "scout_solo", "mint": "M", "sol": 0.05, "pnl_sol": -0.05,
                                  "reason": "hard_stop", "signature": "S", "solscan": "https://solscan.io/tx/S"})
    assert "PnL -0.0500 SOL" in t and "hard_stop" in t


def test_poll_without_token_is_noop(monkeypatch, tmp_path):
    monkeypatch.setattr(alerts, "LIVE_TOKEN_PATH", str(tmp_path / "missing"))
    assert alerts.poll_live_alerts(123) == ([], 123)


def test_saved_messages_only():
    assert alerts.SAVED_MESSAGES == "me"
