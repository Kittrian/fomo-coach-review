"""Syntax/unit checks for Live Feed → buy ingest adapter."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.adapters.live_feed import adapt_batch, normalize_one


def test_raw_swap_buy():
    raw = {
        "type": "swap_buy",
        "userId": "trader-1",
        "userHandle": "alpha",
        "ticker": "PEPE",
        "tokenAddress": "REDACTED",
        "networkId": 101,
        "createdAt": "2026-09-30T18:00:00Z",
        "usdAmount": 120.5,
    }
    ev, disp, _ = normalize_one(raw)
    assert disp == "ingested"
    assert ev is not None
    assert ev.chain == "solana"
    assert ev.token_symbol == "PEPE"


def test_envelope_and_sell_ignored():
    env = {
        "type": "data",
        "topicType": "trading_activity",
        "payload": {
            "type": "swap_sell",
            "userId": "t2",
            "userHandle": "beta",
            "ticker": "X",
            "tokenAddress": "0xabc",
            "networkId": 8453,
            "createdAt": "2026-09-30T18:00:00Z",
        },
    }
    ev, disp, detail = normalize_one(env)
    assert ev is None and disp == "ignored"


def test_trade_event_v1():
    tev = {
        "schemaVersion": 1,
        "id": "evt1",
        "source": "fomo",
        "traderId": "trader-9",
        "traderHandle": "gamma",
        "chain": "solana",
        "networkId": 101,
        "tokenAddress": "REDACTED",
        "tokenSymbol": "FOMO",
        "action": "buy",
        "usdAmount": 50,
        "occurredAt": 1727712000000,
        "receivedAt": 1727712001000,
    }
    batch = adapt_batch({"events": [tev]})
    assert batch["received"] == 1
    assert len(batch["ingested"]) == 1


if __name__ == "__main__":
    test_raw_swap_buy()
    test_envelope_and_sell_ignored()
    test_trade_event_v1()
    print("ok")
