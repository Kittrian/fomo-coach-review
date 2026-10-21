"""Unit tests for TypeSafe Jev gate — mocks Workers AI; no live token required."""
from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.jev_gate import (
    DEFAULT_ACTION,
    FOMO_JEV_QUESTIONS,
    build_state,
    clear_jev_cache,
    evaluate_signal,
    parse_answers,
)
from app.scoring import annotate_signals, score_buys
from app.state import BuyEvent, CoachState


def _buy(trader: str, symbol: str = "DEMOA", token: str = "DemoToken1111111111111111111111111111111") -> BuyEvent:
    return BuyEvent(
        trader_id=trader,
        trader_handle=trader,
        token_address=token,
        token_symbol=symbol,
        chain="solana",
        usd_amount=150.0,
        market_cap=180_000.0,
        price=0.00012,
        occurred_at_ms=int(time.time() * 1000),
        source="test",
    )


def _mock_ok_payload(*, action: str = "show", noul: float = 0.9, score: float = 1.6) -> dict:
    return {
        "success": True,
        "result": {
            "model": "jev-1.13.0",
            "answers": {
                "action": {
                    "type": "choice",
                    "choice": action,
                    "confidence": 0.85,
                    "probabilities": {"skip": 0.05, "show": 0.8, "escalate_human": 0.15},
                },
                "signal_quality": {
                    "type": "score",
                    "score": score,
                    "confidence": 0.9,
                    "legend": {"0": "Low", "1": "Moderate", "2": "High"},
                    "probabilities": {"0": 0.0, "1": 0.4, "2": 0.6},
                },
                "safe_to_surface": {"type": "noul", "noul": noul},
            },
            "usage": {"input_tokens": 400, "output_tokens": 50},
        },
    }


def test_questions_shape():
    assert set(FOMO_JEV_QUESTIONS) == {"action", "signal_quality", "safe_to_surface"}
    assert FOMO_JEV_QUESTIONS["action"]["type"] == "choice"
    assert set(FOMO_JEV_QUESTIONS["action"]["criteria"]) == {
        "skip",
        "show",
        "escalate_human",
    }
    assert FOMO_JEV_QUESTIONS["signal_quality"]["type"] == "score"
    assert FOMO_JEV_QUESTIONS["safe_to_surface"]["type"] == "noul"


def test_parse_answers_ok():
    d = parse_answers(_mock_ok_payload())
    assert d.jev_status == "ok"
    assert d.action == "show"
    assert d.safe_to_surface is True
    assert d.signal_quality == 1.6
    assert d.recommend_only is True
    assert d.auto_trade is False


def test_parse_show_but_unsafe_escalates():
    d = parse_answers(_mock_ok_payload(action="show", noul=0.1))
    assert d.safe_to_surface is False
    assert d.action == DEFAULT_ACTION


def test_parse_missing_answers_fail_closed():
    d = parse_answers({"result": {"model": "jev-1.13.0", "answers": {}}})
    assert d.jev_status == "error"
    assert d.action == "escalate_human"
    assert d.safe_to_surface is False


def test_evaluate_without_creds_unavailable():
    clear_jev_cache()
    buys = [_buy(f"t{i}") for i in range(4)]
    sig = score_buys(buys)
    assert sig is not None
    # Force unavailable path regardless of env
    d = evaluate_signal(sig, force_unavailable=True, use_cache=False)
    assert d.jev_status == "unavailable"
    assert d.action == "escalate_human"
    assert d.safe_to_surface is False
    assert d.auto_trade is False
    assert "credentials" in (d.note or "").lower() or "CLOUDFLARE" in (d.note or "")


def test_evaluate_with_mock_http():
    clear_jev_cache()
    buys = [_buy(f"t{i}") for i in range(4)]
    sig = score_buys(buys)
    assert sig is not None

    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = _mock_ok_payload(action="show", noul=0.92, score=1.8)

    mock_client = MagicMock()
    mock_client.post.return_value = mock_resp

    with patch("app.jev_gate.credentials_ready", return_value=True), patch(
        "app.jev_gate.settings"
    ) as mock_settings:
        mock_settings.jev_gate_enabled = True
        mock_settings.cloudflare_account_id = "acct_test"
        mock_settings.cloudflare_api_token = "token_test"
        mock_settings.demo_mode = True
        d = evaluate_signal(sig, use_cache=False, client=mock_client)

    assert d.jev_status == "ok"
    assert d.action == "show"
    assert d.safe_to_surface is True
    assert mock_client.post.called
    call_kwargs = mock_client.post.call_args
    body = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
    assert body["model"] == "typesafe/jev"
    assert "questions" in body["input"]
    assert body["input"]["questions"]["action"]["type"] == "choice"


def test_build_state_recommend_only():
    buys = [_buy(f"t{i}") for i in range(3)]
    sig = score_buys(buys)
    st = build_state(sig, demo_mode=True)
    assert st["recommend_only"] is True
    assert st["auto_trade"] is False
    assert st["signal"]["ticker"] == "DEMOA"


def test_annotate_signals_demo_path():
    """Without Cloudflare creds, annotate still attaches fail-closed jev."""
    clear_jev_cache()
    st = CoachState(window_minutes=180)
    for i in range(4):
        st.record_buy(_buy(f"w{i}", symbol="DEMOA"), channel="test")
    for i in range(3):
        st.record_buy(
            _buy(
                f"x{i}",
                symbol="DEMOB",
                token="REDACTED",
            ),
            channel="test",
        )
    from app.scoring import compute_signals

    signals = compute_signals(st, min_score=50)
    assert len(signals) >= 1
    annotate_signals(signals)
    for sig in signals:
        assert sig.jev is not None
        assert sig.jev["action"] in {"skip", "show", "escalate_human"}
        assert sig.jev["safe_to_surface"] in (True, False)
        assert sig.jev["auto_trade"] is False
        assert sig.jev["recommend_only"] is True
        # Default box has no CF token → unavailable or ok if somehow configured
        assert sig.jev["jev_status"] in {"ok", "unavailable", "error", "disabled"}


def test_demoa_demob_sample_gate_output(capsys=None):
    """Document sample gate output shapes for DEMOA/DEMOB (mocked live + unavailable)."""
    clear_jev_cache()
    samples = {}
    for symbol, token, n in (
        ("DEMOA", "DemoToken1111111111111111111111111111111", 4),
        ("DEMOB", "DemoToken2222222222222222222222222222222", 3),
    ):
        buys = [_buy(f"{symbol.lower()}-{i}", symbol=symbol, token=token) for i in range(n)]
        sig = score_buys(buys)
        unavailable = evaluate_signal(sig, force_unavailable=True, use_cache=False).to_dict()
        mocked = parse_answers(
            _mock_ok_payload(
                action="show" if symbol == "DEMOA" else "escalate_human",
                noul=0.88 if symbol == "DEMOA" else 0.35,
                score=1.7 if symbol == "DEMOA" else 1.1,
            )
        ).to_dict()
        samples[symbol] = {
            "coach_score": sig.score if sig else None,
            "unavailable_path": {
                "jev_status": unavailable["jev_status"],
                "action": unavailable["action"],
                "safe_to_surface": unavailable["safe_to_surface"],
            },
            "mocked_live_path": {
                "jev_status": mocked["jev_status"],
                "action": mocked["action"],
                "signal_quality": mocked["signal_quality"],
                "safe_to_surface": mocked["safe_to_surface"],
            },
        }

    assert samples["DEMOA"]["unavailable_path"]["action"] == "escalate_human"
    assert samples["DEMOB"]["unavailable_path"]["safe_to_surface"] is False
    assert samples["DEMOA"]["mocked_live_path"]["action"] == "show"
    assert samples["DEMOB"]["mocked_live_path"]["action"] == "escalate_human"
    # Print for report capture when run as script
    import json

    print(json.dumps(samples, indent=2))


if __name__ == "__main__":
    test_questions_shape()
    test_parse_answers_ok()
    test_parse_show_but_unsafe_escalates()
    test_parse_missing_answers_fail_closed()
    test_evaluate_without_creds_unavailable()
    test_evaluate_with_mock_http()
    test_build_state_recommend_only()
    test_annotate_signals_demo_path()
    test_demoa_demob_sample_gate_output()
    print("ok")
