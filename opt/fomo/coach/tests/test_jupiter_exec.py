"""Jupiter Order & Execute — mocked HTTP; no real funds / no live API."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

from app.config import settings as real_settings
from app.jupiter_exec import (
    SOL_MINT,
    USDC_MINT,
    build_quote_response,
    fetch_order,
    jupiter_status_summary,
    resolve_mints,
    run_swap_flow,
)
from app.main import app


def _fake_order(*, with_tx: bool = True) -> dict:
    tx = (
        "AQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABAAED..."
        if with_tx
        else None
    )
    return {
        "transaction": tx,
        "requestId": "req-test-123",
        "inAmount": "10000000",
        "outAmount": "123456",
        "router": "metis",
        "mode": "ultra",
        "feeBps": 10,
        "feeMint": USDC_MINT,
        "lastValidBlockHeight": 99999999,
    }


class _FakeResp:
    def __init__(self, status: int, payload):
        self.status_code = status
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


def _settings_proxy(**overrides):
    """Proxy real settings with a few overrides (frozen dataclass safe)."""
    m = MagicMock(wraps=real_settings)
    m.jupiter_exec_enabled = overrides.get(
        "jupiter_exec_enabled", real_settings.jupiter_exec_enabled
    )
    m.jupiter_api_key = overrides.get("jupiter_api_key", real_settings.jupiter_api_key)
    m.jupiter_api_key_configured = bool(m.jupiter_api_key)
    m.solana_keypair_path = overrides.get(
        "solana_keypair_path", real_settings.solana_keypair_path
    )
    m.solana_keypair_configured = overrides.get(
        "solana_keypair_configured", real_settings.solana_keypair_configured
    )
    m.solana_rpc_url = real_settings.solana_rpc_url
    return m


def test_resolve_mints_buy_sell():
    mint = "DemoToken1111111111111111111111111111111"
    inp, out = resolve_mints(mint=mint, side="buy")
    assert inp == SOL_MINT and out == mint
    inp, out = resolve_mints(mint=mint, side="sell", quote_mint=USDC_MINT)
    assert inp == mint and out == USDC_MINT


def test_resolve_mints_explicit():
    a, b = resolve_mints(input_mint=SOL_MINT, output_mint=USDC_MINT)
    assert a == SOL_MINT and b == USDC_MINT


def test_status_summary_disabled_by_default():
    st = jupiter_status_summary()
    assert st["auto_trade"] is False
    assert st["recommend_only"] is True
    assert real_settings.jupiter_exec_enabled is False
    assert st["status"] == "disabled"
    assert st["mode"] == "recommend_only_dry_run"


def test_fetch_order_mocked():
    mock_client = MagicMock()
    mock_client.get.return_value = _FakeResp(200, _fake_order())
    wrap = fetch_order(
        input_mint=SOL_MINT,
        output_mint=USDC_MINT,
        amount="10000000",
        taker="FakeTaker1111111111111111111111111111111",
        client=mock_client,
    )
    assert wrap["ok"] is True
    assert wrap["order"]["requestId"] == "req-test-123"
    mock_client.get.assert_called_once()
    args, kwargs = mock_client.get.call_args
    assert args[0].endswith("/order")
    assert "params" in kwargs


def test_build_quote_dry():
    wrap = {"ok": True, "order": _fake_order()}
    q = build_quote_response(
        wrap,
        input_mint=SOL_MINT,
        output_mint=USDC_MINT,
        amount="10000000",
        taker="T",
    )
    assert q["dry_run"] is True
    assert q["auto_trade"] is False
    assert q["transaction_ready"] is True
    assert q["request_id"] == "req-test-123"


def test_run_swap_flow_dry_when_disabled():
    mock_client = MagicMock()
    mock_client.get.return_value = _FakeResp(200, _fake_order())
    with patch(
        "app.jupiter_exec.settings",
        _settings_proxy(jupiter_exec_enabled=False),
    ):
        out = run_swap_flow(
            input_mint=SOL_MINT,
            output_mint=USDC_MINT,
            amount="10000000",
            taker="FakeTaker1111111111111111111111111111111",
            confirm=True,
            client=mock_client,
        )
    assert out["dry_run"] is True
    assert out["executed"] is False
    assert out["swap_status"] == "dry_run_disabled"
    assert out["auto_trade"] is False
    mock_client.post.assert_not_called()


def test_run_swap_flow_awaiting_confirm_when_enabled():
    mock_client = MagicMock()
    mock_client.get.return_value = _FakeResp(200, _fake_order())
    with patch(
        "app.jupiter_exec.settings",
        _settings_proxy(jupiter_exec_enabled=True, solana_keypair_configured=False),
    ):
        out = run_swap_flow(
            input_mint=SOL_MINT,
            output_mint=USDC_MINT,
            amount="10000000",
            taker="FakeTaker1111111111111111111111111111111",
            confirm=False,
            client=mock_client,
        )
    assert out["dry_run"] is True
    assert out["swap_status"] == "awaiting_confirm"
    mock_client.post.assert_not_called()


def test_run_swap_flow_execute_with_client_signed():
    mock_client = MagicMock()
    mock_client.post.return_value = _FakeResp(
        200,
        {
            "status": "Success",
            "signature": "SigFake111",
            "code": 0,
            "totalInputAmount": "10000000",
            "totalOutputAmount": "120000",
        },
    )
    with patch(
        "app.jupiter_exec.settings",
        _settings_proxy(jupiter_exec_enabled=True, solana_keypair_configured=False),
    ):
        out = run_swap_flow(
            input_mint=SOL_MINT,
            output_mint=USDC_MINT,
            amount="10000000",
            taker="FakeTaker1111111111111111111111111111111",
            confirm=True,
            signed_transaction="BASE64SIGNEDFAKE",
            request_id="req-test-123",
            client=mock_client,
        )
    assert out["executed"] is True
    assert out["swap_status"] == "success"
    assert out["signature"] == "SigFake111"
    assert out["auto_trade"] is False
    mock_client.post.assert_called_once()
    mock_client.get.assert_not_called()


def test_api_quote_and_swap_dry():
    fake = _fake_order()

    def fake_get(url, params=None, headers=None):
        return _FakeResp(200, fake)

    instance = MagicMock()
    instance.get.side_effect = fake_get
    instance.post.side_effect = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("no execute")
    )
    instance.close = MagicMock()

    with patch("app.jupiter_exec.httpx.Client", return_value=instance):
        with patch(
            "app.jupiter_exec.settings",
            _settings_proxy(jupiter_exec_enabled=False),
        ):
            with patch(
                "app.exec_api.settings",
                _settings_proxy(jupiter_exec_enabled=False),
            ):
                client = TestClient(app)
                h = client.get("/health").json()
                assert "jupiter_exec" in h
                assert h["jupiter_exec"]["auto_trade"] is False
                assert h["auto_trade"] is False

                q = client.post(
                    "/api/exec/quote",
                    json={
                        "mint": "DemoToken1111111111111111111111111111111",
                        "side": "buy",
                        "taker": "FakeTaker1111111111111111111111111111111",
                    },
                )
                assert q.status_code == 200
                body = q.json()
                assert body["dry_run"] is True
                assert body["request_id"] == "req-test-123"

                from app.live import auth as live_auth
                app.dependency_overrides[live_auth.require_localhost] = lambda: None
                s = client.post(
                    "/api/exec/swap",
                    headers={"X-Live-Token": live_auth.ensure_token()},
                    json={
                        "mint": "DemoToken1111111111111111111111111111111",
                        "side": "buy",
                        "taker": "FakeTaker1111111111111111111111111111111",
                        "confirm": True,
                    },
                )
                assert s.status_code == 200
                sb = s.json()
                assert sb["dry_run"] is True
                assert sb["executed"] is False
                assert sb["swap_status"] == "dry_run_disabled"
                app.dependency_overrides.clear()


def test_api_sell_requires_amount():
    client = TestClient(app)
    r = client.post(
        "/api/exec/quote",
        json={
            "mint": "DemoToken1111111111111111111111111111111",
            "side": "sell",
        },
    )
    assert r.status_code == 400


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
