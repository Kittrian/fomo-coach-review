"""v1.38: dynamic priority floor + capped Sender tip."""
import asyncio
import time

import httpx

from app.live import fees, jupiter


def _feed(p75):
    fees._feed.update(p75=p75, ok_t=time.time(), t=time.time(), err=None)


def test_floor_without_feed(monkeypatch):
    monkeypatch.setenv("LIVE_FEE_DYNAMIC", "true")
    fees._feed.update(p75=None, ok_t=0, t=time.time())
    p = fees.plan(100_000_000, want_tip=True, signer_tip_cap=250_000)
    assert p["priority_lamports"] == 50_000 and p["tip_lamports"] == 5_000


def test_budget_half_percent(monkeypatch):
    for size in (100_000_000, 200_000_000):
        for p75 in (1_000, 10_000, 400_000, 5_000_000):
            _feed(p75)
            for tip in (True, False):
                p = fees.plan(size, want_tip=tip, signer_tip_cap=250_000)
                assert p["total_lamports"] <= size * 0.005 + 1
                assert p["priority_lamports"] >= 50_000
                assert p["tip_lamports"] <= 250_000


def test_signer_cap_respected():
    _feed(400_000)
    p = fees.plan(200_000_000, want_tip=True, signer_tip_cap=5_000)
    assert p["tip_lamports"] == 5_000 and not p["dual_route"]


def test_dual_url():
    assert fees.dual_url("http://ewr-sender.helius-rpc.com/fast?swqos_only=true") == "http://ewr-sender.helius-rpc.com/fast"
    assert "swqos_only" not in fees.dual_url("http://x/fast?swqos_only=true&api-key=K")


def test_jupiter_buy_exact_priority(monkeypatch):
    monkeypatch.setenv("LIVE_FEE_DYNAMIC", "true")
    monkeypatch.setattr(jupiter, "pubkey", lambda: "Taker1111111111111111111111111111111111111")
    _feed(10_000)
    seen = {}

    def h(req):
        seen.update(dict(req.url.params))
        return httpx.Response(200, json={"transaction": "AA==", "requestId": "r", "inAmount": "100000000",
                                         "outAmount": "5", "prioritizationFeeLamports": 50_000,
                                         "inputMint": "So11111111111111111111111111111111111111112", "outputMint": "M"})

    async def go(inp, out):
        async with httpx.AsyncClient(transport=httpx.MockTransport(h)) as cl:
            return await jupiter.order(input_mint=inp, output_mint=out, amount=100_000_000, slippage_bps=1500,
                                       max_priority_lamports=2_000_000, client=cl)
    o = asyncio.run(go("So11111111111111111111111111111111111111112", "M"))
    assert seen["broadcastFeeType"] == "exactFee" and int(seen["priorityFeeLamports"]) == 50_000
    assert o["fee_plan"]["priority_lamports"] == 50_000
    seen.clear()
    asyncio.run(go("M", "So11111111111111111111111111111111111111112"))       # sells unchanged
    assert seen["broadcastFeeType"] == "maxCap" and seen["priorityFeeLamports"] == "2000000"
    monkeypatch.setenv("LIVE_FEE_DYNAMIC", "false")
    seen.clear()
    asyncio.run(go("So11111111111111111111111111111111111111112", "M"))
    assert seen["broadcastFeeType"] == "maxCap"
