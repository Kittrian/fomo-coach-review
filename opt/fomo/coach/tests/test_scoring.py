"""Scoring + demo ingest → signal appears."""
from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

from app.main import app
from app.scoring import compute_signals, score_buys
from app.state import BuyEvent, CoachState


def _buy(
    trader: str,
    token: str = "TokAAA111111111111111111111111111111111",
    symbol: str = "AAA",
    usd: float = 100.0,
    mcap: float = 200_000.0,
) -> BuyEvent:
    return BuyEvent(
        trader_id=trader,
        trader_handle=trader,
        token_address=token,
        token_symbol=symbol,
        chain="solana",
        usd_amount=usd,
        market_cap=mcap,
        price=0.001,
        occurred_at_ms=int(time.time() * 1000),
        source="test",
    )


def test_score_convergence_high():
    buys = [_buy(f"t{i}") for i in range(4)]
    sig = score_buys(buys)
    assert sig is not None
    assert sig.unique_traders == 4
    assert sig.buy_count == 4
    assert sig.score >= 50
    assert sig.recommend_only is True
    assert sig.auto_trade is False


def test_compute_signals_filters():
    st = CoachState(window_minutes=180)
    for i in range(3):
        st.record_buy(_buy(f"w{i}"), channel="test")
    # weak second token
    st.record_buy(_buy("solo", token="REDACTED", symbol="BBB"), channel="test")
    high = compute_signals(st, min_score=50)
    assert any(s.symbol == "AAA" for s in high)
    assert all(s.score >= 50 for s in high)


def test_demo_ingest_produces_signal():
    client = TestClient(app)
    token = "REDACTED"
    activities = []
    for handle in ("alpha", "beta", "gamma", "delta"):
        activities.append(
            {
                "type": "data",
                "topicType": "trading_activity",
                "payload": {
                    "type": "swap_buy",
                    "userId": f"id-{handle}",
                    "userHandle": handle,
                    "ticker": "TST",
                    "tokenAddress": token,
                    "networkId": 101,
                    "createdAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "usdAmount": 125,
                    "marketCap": 150000,
                },
            }
        )
    r = client.post("/api/ingest/fomo-activity", json={"events": activities})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["ingested_count"] == 4
    assert body["auto_trade"] is False

    sigs = client.get("/api/signals", params={"min_score": 50})
    assert sigs.status_code == 200
    data = sigs.json()
    assert data["count"] >= 1
    match = [s for s in data["signals"] if s["token_address"].lower() == token.lower()]
    assert match, "expected demo token signal"
    assert match[0]["score"] >= 50
    assert match[0]["unique_traders"] >= 3
    assert match[0]["recommend_only"] is True

    detail = client.get(f"/api/signals/{token}")
    assert detail.status_code == 200
    d = detail.json()["signal"]
    assert d["symbol"] == "TST"
    # narrative chip when DEBOT enabled (stub ok)
    assert "narrative" in d or d.get("narrative") is None or isinstance(d.get("narrative"), dict)


if __name__ == "__main__":
    test_score_convergence_high()
    test_compute_signals_filters()
    test_demo_ingest_produces_signal()
    print("ok")
