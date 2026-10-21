"""v1.22 momentum_paper: pure signal maths, trigger flow, exits (live + T30), Telegram mentions, X off without a token,
cost caps — and PAPER ONLY: never signs / sends / touches the live engine. No network."""
from __future__ import annotations

import asyncio
import inspect
import json
import time

import pytest

from app import paper_momentum as pm
from app.live import jupiter, pumpdirect
from app.live.engine import engine as live_engine

MINT = "MintMOM11111111111111111111111111111111pump"
W = ["WalA1111111111111111111111111111111111111111", "WalB1111111111111111111111111111111111111111",
     "WalC1111111111111111111111111111111111111111", "WalD1111111111111111111111111111111111111111"]


@pytest.fixture
def mom(monkeypatch, tmp_path):
    for k, v in {"PAPER_MOMENTUM_ENABLED": "true", "PAPER_MOMENTUM_LOG_PATH": str(tmp_path / "pm.jsonl"),
                 "MOMENTUM_SCORE_LOG_PATH": str(tmp_path / "scores.jsonl"),
                 "PAPER_MOMENTUM_STATE_PATH": str(tmp_path / "pm_state.json"),
                 "WALLET_SCORES_PATH": str(tmp_path / "ws.json")}.items():
        monkeypatch.setenv(k, v)
    for k in ("X_BEARER_TOKEN", "MOMENTUM_X_ENABLED", "TWITTERAPI_IO_KEY", "MOMENTUM_TG_GLOBAL_ENABLED"):
        monkeypatch.delenv(k, raising=False)
    (tmp_path / "ws.json").write_text(json.dumps({"wallets": [
        {"wallet": W[0], "label": "a", "score": 0.25}, {"wallet": W[1], "label": "b", "score": 0.1},
        {"wallet": W[2], "label": "c", "score": 0.0}, {"wallet": W[3], "label": "d", "score": -0.2}]}))
    P = pm.PaperMomentum()
    monkeypatch.setattr(pm, "paper", P)

    # hard proof: any signing / sending path raises
    def boom(*a, **k):
        raise AssertionError("paper momentum must never sign or send")

    async def aboom(*a, **k):
        raise AssertionError("paper momentum must never sign or send")
    for name in ("swap", "order", "execute"):
        monkeypatch.setattr(jupiter, name, aboom)
    for name in ("sign_partial", "keypair"):
        monkeypatch.setattr(jupiter, name, boom)
    monkeypatch.setattr(pumpdirect, "swap", aboom)
    from app.fomo_onchain import market
    monkeypatch.setattr(market, "sol_usd", lambda: 200.0)
    return P


def _dx(buys5=40, buys60=120, v5=6000, v60=20000, liq=20000, px="0.001", h1=30.0, age_min=40):
    return {"baseToken": {"address": MINT, "symbol": "MOM"}, "quoteToken": {"symbol": "SOL"}, "dexId": "pumpswap",
            "pairAddress": "Pool1", "priceUsd": px, "liquidity": {"usd": liq}, "marketCap": 900000,
            "txns": {"m5": {"buys": buys5, "sells": 10}, "h1": {"buys": buys60, "sells": 60}},
            "volume": {"m5": v5, "h1": v60}, "priceChange": {"m5": 5.0, "h1": h1},
            "pairCreatedAt": int(time.time() * 1000) - age_min * 60_000}


# ---------------- pure signals ----------------
def test_cluster_signal_counts_window_and_weights():
    c = pm.cfg()
    now = 10_000_000
    buys = [{"wallet": W[0], "ts": now - 60_000, "score": 0.25}, {"wallet": W[1], "ts": now - 120_000, "score": 0.1},
            {"wallet": W[2], "ts": now - 30 * 60_000, "score": 0.0}]
    s = pm.cluster_signal(buys, now, c)
    assert s["wallets"] == 2 and not s["met"]
    buys.append({"wallet": W[2], "ts": now - 5_000, "score": 0.0})
    s = pm.cluster_signal(buys, now, c)
    assert s["met"] and s["wallets"] == 3 and s["weighted"] == pytest.approx(2 + 1.4 + 1) and s["s"] == pytest.approx(0.9778, abs=1e-3)
    zero = pm.cluster_signal([{"wallet": x, "ts": now, "score": 0} for x in W[:3]], now, c)
    assert zero["s"] == pytest.approx(0.6667, abs=1e-3)       # score weighting matters


def test_chatter_signal_spike_vs_baseline():
    c = pm.cfg()
    now = 10_000_000
    tg = [{"ts": now - i * 60_000, "chat": f"ch{i}"} for i in range(4)]
    s = pm.chatter_signal(tg, None, now, c)
    assert s["tg_spike"] and s["tg_chats"] == 4 and s["s"] == 1.0
    noisy = tg + [{"ts": now - 20 * 60_000 - i * 60_000, "chat": "old"} for i in range(30)]
    s2 = pm.chatter_signal(noisy, None, now, c)
    assert not s2["tg_spike"] and s2["s"] == pytest.approx(0.4)            # always-mentioned coin: no spike
    sx = pm.chatter_signal([], {"window": 20, "baseline": 6}, now, c)
    assert sx["x"]["spike"] and sx["s"] == 1.0


def test_flow_signal_acceleration_and_runup_veto():
    c = pm.cfg()
    f = pm.flow_signal(_dx(), {"ub_5m": 30, "ub_prev15": 30}, c)
    assert f["acc_buys"] == pytest.approx(4.0) and f["s"] > 0.8 and f["veto"] is None
    flat = pm.flow_signal(_dx(buys5=10, buys60=120, v5=1700, v60=20000), None, c)
    assert flat["s"] < 0.05
    ran = pm.flow_signal(_dx(h1=400.0), None, c)
    assert ran["veto"] and "already ran" in ran["veto"]
    thin = pm.flow_signal(_dx(buys5=5, buys60=10), None, c)
    assert thin["s"] <= 0.5                                                 # too few buys → damped


def test_health_signal_rug_and_liquidity():
    c = pm.cfg()
    now = 10_000_000
    ok = pm.health_signal({"ok": True, "refuse": [], "unknown": []}, [(now - 600_000, 10000), (now, 12000)], now, c)
    assert ok["s"] == 1.0 and ok["liq_trend"] == "up" and ok["veto"] is None
    pulled = pm.health_signal({"ok": True, "refuse": [], "unknown": []}, [(now - 300_000, 20000), (now, 9000)], now, c)
    assert pulled["veto"] and "liquidity pulled" in pulled["veto"]
    rug = pm.health_signal({"ok": False, "refuse": ["rugcheck curve_floor: 1.5 SOL"], "unknown": []}, [(now, 9000)], now, c)
    assert rug["veto"].startswith("rug:")


def test_combine_threshold_core_and_weights(monkeypatch):
    c = pm.cfg()
    sig = {"cluster": {"s": 1.0}, "chatter": {"s": 0.0}, "flow": {"s": 0.8}, "health": {"s": 1.0, "veto": None}}
    sig["flow"]["veto"] = None
    r = pm.combine(sig, c)
    assert r["trigger"] and r["score"] == pytest.approx((0.35 + 0.24 + 0.2) / 1.0)
    weak = pm.combine({"cluster": {"s": 0.2}, "chatter": {"s": 1.0}, "flow": {"s": 0.3, "veto": None},
                       "health": {"s": 1.0, "veto": None}}, c)
    assert not weak["trigger"] and any("no core signal" in w for w in weak["why"])
    monkeypatch.setenv("MOMENTUM_W_CHATTER", "3")
    monkeypatch.setenv("MOMENTUM_REQUIRE_CLUSTER_OR_FLOW", "false")
    monkeypatch.setenv("MOMENTUM_THRESHOLD", "0.7")
    assert pm.combine({"cluster": {"s": 0.2}, "chatter": {"s": 1.0}, "flow": {"s": 0.3, "veto": None},
                       "health": {"s": 1.0, "veto": None}}, pm.cfg())["trigger"]


def test_unique_buyers_from_gt_rows():
    from datetime import datetime, timezone
    now = int(time.time() * 1000)

    def row(kind, who, ago_s):
        ts = datetime.fromtimestamp((now - ago_s * 1000) / 1000, timezone.utc).isoformat().replace("+00:00", "Z")
        return {"attributes": {"kind": kind, "tx_from_address": who, "block_timestamp": ts}}
    rows = [row("buy", "a", 10), row("buy", "a", 20), row("buy", "b", 100), row("sell", "c", 30), row("buy", "d", 600)]
    assert pm.unique_buyers(rows, now) == {"ub_5m": 2, "ub_prev15": 1}


# ---------------- flow end-to-end (mocked network) ----------------
def _wire(P, monkeypatch, dx=None, rug=None):
    calls = {"dex": 0, "gt": 0, "rug": 0}

    async def fdx(mints):
        calls["dex"] += 1
        return {MINT: dx or _dx()}

    async def fgt(pool, now):
        calls["gt"] += 1
        return {"ub_5m": 25, "ub_prev15": 15}

    async def frug(mint):
        calls["rug"] += 1
        return rug or {"ok": True, "refuse": [], "unknown": [], "checks": {}}
    monkeypatch.setattr(P, "fetch_dex", fdx)
    monkeypatch.setattr(P, "fetch_gt", fgt)
    monkeypatch.setattr(P, "rug", frug)
    return calls


def test_cluster_plus_flow_triggers_paper_entry_and_logs(mom, monkeypatch, tmp_path):
    P = mom
    P.load()
    calls = _wire(P, monkeypatch)
    now = int(time.time() * 1000)
    for i, w in enumerate(W[:3]):
        assert P.on_wallet_buy(w, MINT, source="fomo", ts_ms=now - i * 1000) == "noted"
    assert P.on_wallet_buy("Unranked111", MINT, source="fomo") == "unranked"
    opened = asyncio.run(P.tick(now))
    assert len(opened) == 1 and calls["rug"] == 1 and calls["gt"] == 1
    b = P.book[opened[0]]
    assert set(b["tracks"]) == {"live_exit", "t30"}
    p = b["tracks"]["live_exit"]
    assert p.entry_price_sol == pytest.approx(0.001 / 200 * 1.015) and "paper" in p.flags and p.size_sol == 0.20
    rows = [json.loads(x) for x in (tmp_path / "scores.jsonl").read_text().splitlines()]
    assert rows[-1]["trigger"] and set(rows[-1]["s"]) == {"cluster", "chatter", "flow", "health"}
    # one entry per coin
    assert asyncio.run(P.tick(now + 30_000)) == []


def test_non_triggering_coins_are_logged(mom, monkeypatch, tmp_path):
    P = mom
    P.load()
    _wire(P, monkeypatch, dx=_dx(buys5=5, buys60=200, v5=100, v60=30000))
    P.on_wallet_buy(W[0], MINT, source="scout")
    assert asyncio.run(P.tick()) == []
    rows = [json.loads(x) for x in (tmp_path / "scores.jsonl").read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["trigger"] is False and rows[0]["why"]


def test_vetoes_block_entry(mom, monkeypatch):
    P = mom
    P.load()
    _wire(P, monkeypatch, rug={"ok": False, "refuse": ["rugcheck bundle: 9 wallets"], "unknown": []})
    for w in W[:3]:
        P.on_wallet_buy(w, MINT, source="fomo")
    assert asyncio.run(P.tick()) == []
    P2 = pm.PaperMomentum()
    P2.load()
    _wire(P2, monkeypatch, dx=_dx(h1=500.0))
    for w in W[:3]:
        P2.on_wallet_buy(w, MINT, source="fomo")
    assert asyncio.run(P2.tick()) == []


def test_cost_guard_no_rug_or_gt_for_hopeless_coins(mom, monkeypatch):
    P = mom
    P.load()
    calls = _wire(P, monkeypatch, dx=_dx(buys5=2, buys60=100, v5=10, v60=10000))
    P.on_mentions([{"mint": MINT, "chat": "x", "ts_ms": int(time.time() * 1000)}])
    asyncio.run(P.tick())
    assert calls["rug"] == 0 and calls["gt"] == 0


def test_gt_and_rug_rate_caps(mom, monkeypatch):
    monkeypatch.setenv("MOMENTUM_GT_MAX_PER_MIN", "2")
    P = mom
    assert P._gt_budget() and P._gt_budget() and not P._gt_budget()
    monkeypatch.setenv("MOMENTUM_RUG_MAX_PER_HOUR", "0")
    assert asyncio.run(P.rug(MINT)) is None and P.stats["rug_capped"] == 1


def test_rug_uses_public_rpc_never_helius(mom, monkeypatch):
    monkeypatch.setenv("SOLANA_RPC_URL", "https://mainnet.helius-rpc.com/?api-key=SECRET")
    seen = []
    from app.pump import rpc as _rpc

    async def call(method, params, client=None, url=None, timeout=4.0):
        seen.append(url)
        return None
    monkeypatch.setattr(_rpc, "call", call)
    P = mom
    asyncio.run(P.rug("DwcXyhEcSvzWgakDKpetZLFvbAHutpGpzhbU4iempump"))       # a real base58 mint (PDA derivable)
    assert seen and all(u == pm.PUBLIC_RPC for u in seen)


# ---------------- exits ----------------
def _opened(P, monkeypatch):
    P.load()
    _wire(P, monkeypatch)
    for w in W[:3]:
        P.on_wallet_buy(w, MINT, source="fomo")
    pid = asyncio.run(P.tick())[0]
    return pid, P.book[pid]


def test_exits_live_half_at_2x_and_t30_pre2x_trail(mom, monkeypatch, tmp_path):
    P = mom
    pid, b = _opened(P, monkeypatch)
    e = b["tracks"]["live_exit"].entry_price_sol
    t0 = b["tracks"]["live_exit"].entry_ms
    P.mark(pid, e * 1.6, t0 + 20_000)
    P.mark(pid, e * 1.05, t0 + 40_000)                        # 1.05 < 1.6 × 0.70 → T30 trail; live keeps holding
    assert b["tracks"]["t30"].status == "closed" and b["tracks"]["t30"].exit_reason == "pre2x_trail_30"
    assert b["tracks"]["live_exit"].status == "open"
    P.mark(pid, e * 2.1, t0 + 60_000)
    lt = b["tracks"]["live_exit"]
    assert lt.tp_done and lt.tokens_left == pytest.approx(lt.tokens_initial / 2, rel=0.01)
    P.mark(pid, e * 1.3, t0 + 80_000)                         # 2.1 × 0.70 = 1.47 → trail
    assert pid not in P.book
    closes = [json.loads(x) for x in (tmp_path / "pm.jsonl").read_text().splitlines() if '"close"' in x]
    assert {c["track"] for c in closes} == {"live_exit", "t30"}


def test_hard_stop_both_tracks(mom, monkeypatch):
    P = mom
    pid, b = _opened(P, monkeypatch)
    e = b["tracks"]["live_exit"].entry_price_sol
    P.mark(pid, e * 0.6, b["tracks"]["live_exit"].entry_ms + 10_000)
    assert pid not in P.book


# ---------------- paper only / not counted ----------------
def test_never_touches_live_engine_or_caps(mom, monkeypatch):
    before = (dict(live_engine.day()), list(live_engine.window_events), dict(live_engine.positions),
              dict(live_engine.bought_mints))
    P = mom
    pid, b = _opened(P, monkeypatch)
    P.mark(pid, b["tracks"]["live_exit"].entry_price_sol * 0.5, int(time.time() * 1000) + 5_000)
    after = (dict(live_engine.day()), list(live_engine.window_events), dict(live_engine.positions),
             dict(live_engine.bought_mints))
    assert before == after


def test_module_has_no_signing_imports():
    src = inspect.getsource(pm)
    for bad in ("import jupiter", "from app.live import jupiter", "pumpdirect", "keypair", "sign_partial",
                "sendTransaction", "/execute"):
        assert bad not in src


# ---------------- telegram / x / endpoints / off switch ----------------
def test_mentions_ingest_and_ticker_mapping(mom, monkeypatch):
    P = mom
    P.load()
    now = int(time.time() * 1000)
    r = P.on_mentions([{"mint": MINT, "symbol": "$mom", "chat": "A", "ts_ms": now},
                       {"symbol": "MOM", "chat": "B", "ts_ms": now}, {"chat": "C"}])
    assert r == {"accepted": 2, "ignored": 1} and MINT in P.cands
    assert len(P._tg_for(P.cands[MINT])) == 2
    assert P.sources()["telegram"]["status"] == "receiving"


def test_x_off_without_token_and_counts_with_token(mom, monkeypatch):
    c = pm.cfg()
    assert c["x_enabled"] is False and "no TWITTERAPI_IO_KEY / X_BEARER_TOKEN" in pm.PaperMomentum().sources()["x"]["status"]
    monkeypatch.setenv("MOMENTUM_X_ENABLED", "true")
    monkeypatch.setenv("X_BEARER_TOKEN", "tok123")
    monkeypatch.setenv("MOMENTUM_X_MAX_CALLS_DAY", "1")
    P = mom
    seen = {}

    class R:
        status_code = 200

        def json(self):
            from datetime import datetime, timezone
            n = datetime.now(timezone.utc)
            return {"data": [{"start": n.strftime("%Y-%m-%dT%H:%M:00Z"), "tweet_count": 7},
                             {"start": "2020-01-01T00:00:00Z", "tweet_count": 3}]}

    class C:
        is_closed = False

        async def get(self, url, params=None, headers=None):
            seen.update(url=url, params=params, auth=headers.get("Authorization"))
            return R()
    P._http = C()
    cd = P._cand(MINT, int(time.time() * 1000), "t")
    cd["symbol"] = "MOM"
    x = asyncio.run(P.fetch_x(cd, int(time.time() * 1000)))
    assert x == {"window": 7, "baseline": 3, "provider": "x_counts"} and "counts/recent" in seen["url"] and seen["auth"] == "Bearer tok123"
    assert MINT in seen["params"]["query"] and "$MOM" in seen["params"]["query"]
    cd2 = P._cand("Other1111", int(time.time() * 1000), "t")
    asyncio.run(P.fetch_x(cd2, int(time.time() * 1000)))
    assert P.stats["x_calls"] == 1 and P.stats["x_capped"] == 1          # daily cap holds


def test_off_switch(monkeypatch, tmp_path):
    monkeypatch.setenv("PAPER_MOMENTUM_ENABLED", "false")
    monkeypatch.setenv("PAPER_MOMENTUM_STATE_PATH", str(tmp_path / "s.json"))
    P = pm.PaperMomentum()
    assert not P.active() and P.on_wallet_buy(W[0], MINT, source="fomo") is None
    assert P.on_mentions([{"mint": MINT, "chat": "a"}]) == {"accepted": 0, "ignored": 1}
    assert not (tmp_path / "s.json").exists()


def test_seven_day_window(mom):
    P = mom
    P.load()
    assert P.end_ms - P.start_ms == 7 * 86400 * 1000
    assert not P.active(P.end_ms + 1)


def test_endpoints(mom, monkeypatch):
    from fastapi.testclient import TestClient
    from app.live import auth
    from app.main import app
    c = TestClient(app)
    s = c.get("/api/paper/momentum/summary").json()
    assert s["paper_only"] and s["enabled"] and set(s["tracks"]) == {"live_exit", "t30"}
    assert c.post("/api/paper/momentum/mentions", json={"mentions": []}).status_code == 403   # loopback only
    app.dependency_overrides[auth.require_localhost] = lambda: None
    monkeypatch.setattr(auth, "LOOPBACK", set(auth.LOOPBACK) | {"testclient"})
    try:
        r = c.post("/api/paper/momentum/mentions", json={"mentions": [{"mint": MINT, "chat": "A"}]}).json()
        assert r["accepted"] == 1
    finally:
        app.dependency_overrides.pop(auth.require_localhost, None)
    h = c.get("/health").json()
    assert h["paper_momentum"]["enabled"] is True and h["paper_momentum"]["endpoint"] == "/api/paper/momentum/summary"
