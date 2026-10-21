"""v1.23 momentum_paper: Telegram PUBLIC search queue (searches run in the watcher), chatter evidence sum with
unknown ≠ zero, twitterapi.io X source (off until key), endpoints loopback-only. PAPER ONLY. No network."""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from app import paper_momentum as pm

MINT = "MintMOM11111111111111111111111111111111pump"
W = ["WalA1111111111111111111111111111111111111111", "WalB1111111111111111111111111111111111111111",
     "WalC1111111111111111111111111111111111111111"]


@pytest.fixture
def mom(monkeypatch, tmp_path):
    for k, v in {"PAPER_MOMENTUM_ENABLED": "true", "PAPER_MOMENTUM_LOG_PATH": str(tmp_path / "pm.jsonl"),
                 "MOMENTUM_SCORE_LOG_PATH": str(tmp_path / "scores.jsonl"),
                 "PAPER_MOMENTUM_STATE_PATH": str(tmp_path / "pm_state.json"),
                 "WALLET_SCORES_PATH": str(tmp_path / "ws.json"), "MOMENTUM_TG_GLOBAL_ENABLED": "true"}.items():
        monkeypatch.setenv(k, v)
    for k in ("X_BEARER_TOKEN", "MOMENTUM_X_ENABLED", "TWITTERAPI_IO_KEY"):
        monkeypatch.delenv(k, raising=False)
    (tmp_path / "ws.json").write_text(json.dumps({"wallets": [{"wallet": w, "label": w[:4], "score": 0.2} for w in W]}))
    P = pm.PaperMomentum()
    monkeypatch.setattr(pm, "paper", P)
    P.load()
    return P


def _dx(buys5=40, buys60=120, v5=6000, v60=20000, liq=20000, h1=30.0, sym="ZORBLAX"):
    return {"baseToken": {"address": MINT, "symbol": sym}, "quoteToken": {"symbol": "SOL"}, "dexId": "pumpswap",
            "pairAddress": "Pool1", "priceUsd": "0.001", "liquidity": {"usd": liq}, "marketCap": 900000,
            "txns": {"m5": {"buys": buys5, "sells": 10}, "h1": {"buys": buys60, "sells": 60}},
            "volume": {"m5": v5, "h1": v60}, "priceChange": {"m5": 5.0, "h1": h1},
            "pairCreatedAt": int(time.time() * 1000) - 40 * 60_000}


def _wire(P, monkeypatch, dx=None):
    async def fdx(mints):
        return {MINT: dx or _dx()}

    async def fgt(pool, now):
        return {"ub_5m": 25, "ub_prev15": 15}

    async def frug(mint):
        return {"ok": True, "refuse": [], "unknown": [], "checks": {}}
    monkeypatch.setattr(P, "fetch_dex", fdx)
    monkeypatch.setattr(P, "fetch_gt", fgt)
    monkeypatch.setattr(P, "rug", frug)


def test_distinctive_ticker():
    assert pm.distinctive_ticker("$zorblax") == "ZORBLAX"
    for bad in ("PEPE", "CAT", "AI", "SOL", "1234", "AB", "X-Y", None, "", "VERYLONGTICKERNAME"):
        assert pm.distinctive_ticker(bad) is None


def test_public_signal_and_unknown_is_not_zero():
    c = pm.cfg()
    now = 10_000_000
    full = pm.public_signal({"ts": now, "channels": 5, "messages": 15}, now, c)
    assert full["s"] == pytest.approx(1.0)
    assert pm.public_signal({"ts": now, "unknown": True, "error": "FloodWait"}, now, c) is None
    assert pm.public_signal({"ts": now - 25 * 60_000, "channels": 5, "messages": 9}, now, c) is None    # stale
    zero = pm.chatter_signal([], None, now, c, {"ts": now, "channels": 0, "messages": 0})
    unk = pm.chatter_signal([], None, now, c, {"ts": now, "unknown": True, "error": "FloodWait"})
    assert zero["tg_public"]["channels"] == 0 and zero["tg_public"]["s"] == 0.0
    assert unk["tg_public"] == {"unknown": True, "error": "FloodWait"}                 # logged as unknown, not 0


def test_chatter_is_capped_weighted_evidence_sum():
    c = pm.cfg()
    now = 10_000_000
    tg = [{"ts": now - 60_000, "chat": "a"}, {"ts": now - 30_000, "chat": "b"}]       # 2 chats, spike → 0.5
    only_priv = pm.chatter_signal(tg, None, now, c)
    assert only_priv["s"] == pytest.approx(0.5)
    both = pm.chatter_signal(tg, None, now, c, {"ts": now, "channels": 2, "messages": 6})
    pub_s = 0.7 * 2 / 5 + 0.3 * 6 / 15
    assert both["s"] == pytest.approx(0.5 + 0.8 * pub_s, abs=1e-3)
    big = pm.chatter_signal(tg * 3, None, now, c, {"ts": now, "channels": 9, "messages": 99})
    assert big["s"] == 1.0                                                               # capped


def test_scorer_queues_only_hopeful_and_rechecks(mom, monkeypatch):
    P = mom
    _wire(P, monkeypatch)
    now = int(time.time() * 1000)
    for w in W:
        P.on_wallet_buy(w, MINT, source="fomo", ts_ms=now - 30_000)
    asyncio.run(P.tick(now))
    assert MINT in P.tgq and P.tgq[MINT]["queries"] == [MINT, "$ZORBLAX"]
    nx = P.tg_next(3, {"searches_hour": 1, "max_hour": 120})
    assert nx["requests"][0]["queries"] == [MINT, "$ZORBLAX"] and nx["requests"][0]["window_min"] == 30
    assert P.tg_next(3)["requests"] == []                                               # in flight, not resent
    # a hopeless coin is never queued
    P2 = pm.PaperMomentum()
    P2.load()
    _wire(P2, monkeypatch, _dx(buys5=1, buys60=200, v5=10, v60=50000))
    P2._cand(MINT, now, "telegram")
    asyncio.run(P2.tick(now))
    assert P2.tgq == {}


def test_generic_ticker_searches_ca_only_and_vetoed_coins_skip(mom, monkeypatch):
    P = mom
    _wire(P, monkeypatch, _dx(sym="PEPE"))
    now = int(time.time() * 1000)
    for w in W:
        P.on_wallet_buy(w, MINT, source="fomo", ts_ms=now - 30_000)
    asyncio.run(P.tick(now))
    assert P.tgq[MINT]["queries"] == [MINT]
    P2 = pm.PaperMomentum()
    P2.load()
    _wire(P2, monkeypatch, _dx(h1=400.0))
    for w in W:
        P2.on_wallet_buy(w, MINT, source="fomo", ts_ms=now - 30_000)
    asyncio.run(P2.tick(now))
    assert P2.tgq == {}                                                                 # already ran: no search


def test_results_feed_score_and_log_counts_only(mom, monkeypatch, tmp_path):
    P = mom
    _wire(P, monkeypatch)
    now = int(time.time() * 1000)
    for w in W:
        P.on_wallet_buy(w, MINT, source="fomo", ts_ms=now - 30_000)
    asyncio.run(P.tick(now))
    P.tg_next(1)
    r = P.tg_result([{"mint": MINT, "ok": True, "channels": 3, "messages": 7, "usernames": ["chanA", "chanB"],
                      "method": "posts", "capped": False, "text": "SHOULD NOT BE STORED"}])
    assert r == {"accepted": 1} and MINT not in P.tgq
    res = P.score(P.cands[MINT], int(time.time() * 1000))
    assert res["signals"]["chatter"]["tg_public"]["channels"] == 3
    P._log_score(P.cands[MINT], res, int(time.time() * 1000), force=True)
    raw = (tmp_path / "scores.jsonl").read_text()
    assert "SHOULD NOT BE STORED" not in raw
    last = json.loads(raw.strip().splitlines()[-1])
    assert last["chatter_counts"]["tg_public"]["channels"] == 3 and last["chatter_counts"]["tg_public_usernames"] == ["chanA", "chanB"]
    assert any('"kind": "tg_public_search"' in ln for ln in raw.splitlines())
    s = P.summary()
    assert s["sources"]["telegram_public"]["results_ok"] == 1


def test_failed_and_timed_out_searches_are_unknown(mom, monkeypatch):
    P = mom
    P._cand(MINT, int(time.time() * 1000), "t")
    P.queue_manual(MINT, "ZORBLAX")
    P.tg_next(1)
    P.tg_result([{"mint": MINT, "ok": False, "error": "FloodWaitError"}])
    assert P.cands[MINT]["tg_pub"]["unknown"] is True
    res = P.score(P.cands[MINT], int(time.time() * 1000))
    assert res["signals"]["chatter"]["tg_public"]["unknown"] is True
    P.queue_manual(MINT)
    P.tg_next(1)
    P.tgq[MINT]["sent_ms"] -= 6 * 60_000
    P.tg_next(1)
    assert P.cands[MINT]["tg_pub"] == {**P.cands[MINT]["tg_pub"], "unknown": True, "error": "timeout"}
    assert P.stats["tg_global_unknown"] == 2


def test_off_switch_no_queue(mom, monkeypatch):
    P = mom
    monkeypatch.setenv("MOMENTUM_TG_GLOBAL_ENABLED", "false")
    _wire(P, monkeypatch)
    now = int(time.time() * 1000)
    for w in W:
        P.on_wallet_buy(w, MINT, source="fomo", ts_ms=now - 30_000)
    asyncio.run(P.tick(now))
    assert P.tgq == {} and P.tg_next(1) == {"ok": True, "enabled": False, "requests": []}
    assert P.queue_manual(MINT)["ok"] is False
    assert "off" in P.sources()["telegram_public"]["status"]
    # public results are ignored by the scorer while off
    P.cands[MINT]["tg_pub"] = {"ts": now, "channels": 5, "messages": 15}
    assert P.score(P.cands[MINT], now)["signals"]["chatter"]["tg_public"] is None


def test_queue_bounded(mom, monkeypatch):
    P = mom
    monkeypatch.setenv("MOMENTUM_TG_GLOBAL_QUEUE_MAX", "2")
    pre = {"score": 0.5, "signals": {"flow": {"veto": None}, "health": {"veto": None}}}
    now = int(time.time() * 1000)
    for i in range(4):
        cd = P._cand(f"M{i}", now, "t")
        P.maybe_queue_tg(cd, {**pre, "score": 0.5 + i * 0.01}, now)
    assert len(P.tgq) == 2 and set(P.tgq) == {"M2", "M3"}


def test_twitterapi_io_off_until_key_then_one_page_and_unknown_on_fail(mom, monkeypatch):
    P = mom
    assert pm.cfg()["x_enabled"] is False and pm.cfg()["x_provider"] is None
    monkeypatch.setenv("TWITTERAPI_IO_KEY", "k-test")
    assert pm.cfg()["x_enabled"] is False                                             # still needs the flag
    assert "key present" in P.sources()["x"]["status"]
    monkeypatch.setenv("MOMENTUM_X_ENABLED", "true")
    c = pm.cfg()
    assert c["x_enabled"] and c["x_provider"] == "twitterapi_io"
    seen = []
    from datetime import datetime, timezone

    def ts(ago_min):
        return datetime.fromtimestamp(time.time() - ago_min * 60, timezone.utc).strftime("%a %b %d %H:%M:%S %z %Y")

    class R:
        def __init__(self, code, body):
            self.status_code, self._b = code, body

        def json(self):
            return self._b

    bodies = [R(200, {"tweets": [{"createdAt": ts(2), "author": {"userName": "a"}, "text": "x"},
                                 {"createdAt": ts(4), "author": {"userName": "b"}},
                                 {"createdAt": ts(20), "author": {"userName": "a"}}],
                      "has_next_page": False, "next_cursor": ""}),
              R(429, {})]

    class C:
        is_closed = False

        async def get(self, url, params=None, headers=None):
            seen.append({"url": url, "params": params, "key": headers.get("X-API-Key")})
            return bodies.pop(0)
    P._http = C()
    cd = P._cand(MINT, int(time.time() * 1000), "t")
    cd["symbol"] = "ZORBLAX"
    x = asyncio.run(P.fetch_x(cd, int(time.time() * 1000)))
    assert x["window"] == 2 and x["baseline"] == 1 and x["authors"] == 2 and x["provider"] == "twitterapi_io"
    q = seen[0]["params"]["query"]
    assert f'"{MINT}"' in q and '"$ZORBLAX"' in q and "since_time:" in q and seen[0]["params"]["cursor"] == ""
    assert "advanced_search" in seen[0]["url"] and seen[0]["key"] == "k-test" and len(seen) == 1   # one page
    assert "text" not in json.dumps(x)
    cd2 = P._cand("Other1111", int(time.time() * 1000), "t")
    x2 = asyncio.run(P.fetch_x(cd2, int(time.time() * 1000)))
    assert x2["unknown"] is True                                                       # failed = unknown
    ch = pm.chatter_signal([], x2, int(time.time() * 1000), pm.cfg())
    assert ch["x"]["unknown"] is True and ch["s"] == 0.0


def test_tg_search_endpoints_loopback_only(mom, monkeypatch):
    from fastapi.testclient import TestClient
    from app.live import auth
    from app.main import app
    c = TestClient(app)
    assert c.get("/api/paper/momentum/tg_search/next").status_code == 403
    assert c.post("/api/paper/momentum/tg_search/result", json={"results": []}).status_code == 403
    assert c.post("/api/paper/momentum/tg_search/request", json={"mint": MINT}).status_code == 403
    monkeypatch.setattr(auth, "LOOPBACK", set(auth.LOOPBACK) | {"testclient"})
    assert c.post("/api/paper/momentum/tg_search/request", json={"mint": MINT, "symbol": "ZORBLAX"}).json()["ok"]
    nx = c.get("/api/paper/momentum/tg_search/next", params={"n": 1, "searches_hour": 3, "max_hour": 120}).json()
    assert nx["requests"][0]["queries"] == [MINT, "$ZORBLAX"]
    r = c.post("/api/paper/momentum/tg_search/result",
               json={"results": [{"mint": MINT, "ok": True, "channels": 1, "messages": 2}]}).json()
    assert r == {"ok": True, "accepted": 1}
    s = c.get("/api/paper/momentum/summary").json()
    tp = s["sources"]["telegram_public"]
    assert tp["results_ok"] == 1 and tp["watcher_limiter"]["searches_hour"] == 3
    assert c.get("/health").json()["paper_momentum"]["telegram_public"] == "watcher polling"


def test_stale_queued_requests_become_unknown(mom):
    P = mom
    P._cand(MINT, int(time.time() * 1000), "t")
    P.queue_manual(MINT)
    P.tgq[MINT]["req_ms"] -= 16 * 60_000
    assert P.tg_next(0)["requests"] == []
    assert MINT not in P.tgq and P.cands[MINT]["tg_pub"]["unknown"] is True
