"""v1.39: OKX router excluded, paper-only scout wallets, scout signer-budget reserve."""
from __future__ import annotations

import asyncio
import json
import time

import httpx

from tests.test_v119 import live_env, MINT  # noqa: F401
from app.live import engine as eng_mod, jupiter
from app.live.engine import LiveEngine


def test_jupiter_excludes_okx(monkeypatch):
    monkeypatch.setattr(jupiter, "pubkey", lambda: "Taker1111111111111111111111111111111111111")
    monkeypatch.delenv("LIVE_JUPITER_EXCLUDE_ROUTERS", raising=False)
    monkeypatch.setenv("LIVE_FEE_DYNAMIC", "false")
    seen = {}

    def h(req):
        seen.update(dict(req.url.params))
        return httpx.Response(200, json={"transaction": "AA==", "requestId": "r", "inAmount": "5", "outAmount": "5"})

    async def go(inp, out):
        async with httpx.AsyncClient(transport=httpx.MockTransport(h)) as cl:
            return await jupiter.order(input_mint=inp, output_mint=out, amount=5, slippage_bps=1500,
                                       max_priority_lamports=0, client=cl)
    asyncio.run(go("So11111111111111111111111111111111111111112", "M"))
    assert seen["excludeRouters"] == "okx"
    seen.clear(); asyncio.run(go("M", "So11111111111111111111111111111111111111112"))   # sells too
    assert seen["excludeRouters"] == "okx"
    monkeypatch.setenv("LIVE_JUPITER_EXCLUDE_ROUTERS", "")
    seen.clear(); asyncio.run(go("M", "So11111111111111111111111111111111111111112"))
    assert "excludeRouters" not in seen


def _env(live_env, monkeypatch):
    monkeypatch.setenv("LIVE_ARMS", "scout_solo,scout_confirmed,fluffy_pump_dump")
    monkeypatch.setenv("LIVE_TRADE_CAP_EXEMPT_ARMS", "fluffy_pump_dump")
    monkeypatch.setenv("LIVE_NIGHT_PAUSE_ENABLED", "false")
    monkeypatch.setenv("LIVE_LIMIT_WINDOW", "calendar")
    monkeypatch.setenv("LIVE_SIZE_SOL_FLUFFY_PUMP_DUMP", "0.20")
    led = live_env / "ledger_v139.jsonl"
    monkeypatch.setenv("LIVE_LEDGER_PATH", str(led))
    return led


def test_budget_reserve(live_env, monkeypatch):
    led = _env(live_env, monkeypatch)
    now = int(time.time() * 1000)
    rows = [{"ts_ms": now - 3600_000, "kind": "buy", "spent_lamports": 3_900_000_000},
            {"ts_ms": now - 25 * 3600_000, "kind": "buy", "spent_lamports": 5_000_000_000},   # aged out
            {"ts_ms": now - 60_000, "kind": "buy_failed", "size_sol": 0.1},
            {"ts_ms": now - 60_000, "kind": "buy_aborted", "size_sol": 9}]                   # never signed
    led.write_text("".join(json.dumps(r) + "\n" for r in rows))
    assert abs(eng_mod.buy_spend_24h(str(led)) - 4.0) < 1e-9
    monkeypatch.setenv("LIVE_SIGNER_BUDGET_SOL", "6")
    monkeypatch.setenv("LIVE_SCOUT_BUDGET_RESERVE_SOL", "2")
    e = LiveEngine()
    ok, why, _ = e.check_caps("fluffy_pump_dump", MINT)       # 4.0 + 0.2 > 6 - 2
    assert not ok and any("reserved for scouts" in w for w in why)
    ok, why, _ = e.check_caps("scout_solo", MINT)              # scouts may use the reserve
    assert not any("reserved for scouts" in w for w in why)
    monkeypatch.setenv("LIVE_SCOUT_BUDGET_RESERVE_SOL", "0")   # off
    ok, why, _ = e.check_caps("fluffy_pump_dump", MINT)
    assert not any("reserved for scouts" in w for w in why)


def test_paper_only_wallets(live_env, monkeypatch):
    _env(live_env, monkeypatch)
    R4 = "Cv5GgkpXvtXcsrMcKCJvx46BpMjiM61mpPU1j8RbbbVL"
    monkeypatch.setenv("LIVE_SCOUT_PAPER_ONLY_WALLETS", R4 + ",5B52w1ZW9tuwUduueP5J7HXz5AcGfruGoX6YoAudvyxG")
    assert R4 in eng_mod.scout_paper_only_wallets()
    e = LiveEngine()
    d = e.on_signal("scout_solo", MINT, detect_ms=int(time.time() * 1000), trader_wallets=[R4], ref_price_sol=1e-7)
    assert d["decision"] == "refused" and any("paper-only scout wallet" in w for w in d["reasons"])
    d = e.on_signal("scout_solo", MINT + "x", detect_ms=int(time.time() * 1000),
                    trader_wallets=["CkPFGv2Wv1vwdWjtXioEgb8jhZQfs3eVZez3QCetu7xD"], ref_price_sol=1e-7)
    assert not any("paper-only scout wallet" in w for w in d["reasons"])


def test_mesh_deferred_late_block(live_env, monkeypatch):
    _env(live_env, monkeypatch)
    monkeypatch.setenv("LIVE_DIRECT_MESH_MAX_WAIT_S", "0.25")
    assert eng_mod._direct_mesh_max_wait_s() == 0.25
    e = LiveEngine()
    calls = []
    monkeypatch.setattr(e, "force_exit", lambda mint, reason, **kw: calls.append((mint, reason, kw)))

    async def go(res):
        fut = asyncio.get_running_loop().create_future()
        e._mesh_deferred(MINT, fut)
        fut.set_result(res)
        await asyncio.sleep(0)
    asyncio.run(go((False, "mesh hard block: honeypot")))
    assert calls and calls[0][1] == "mesh_block_late" and calls[0][2]["urgent"]
    calls.clear()
    asyncio.run(go((False, "mesh down — no buy without the rug/honeypot check")))   # down late: log only
    assert not calls
    asyncio.run(go((True, "mesh ok")))
    assert not calls
