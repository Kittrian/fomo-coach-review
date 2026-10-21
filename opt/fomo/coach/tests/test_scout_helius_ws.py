"""v1.17.2 item 2: scout websocket on Helius (primary) with automatic fallback to the current endpoint.
The key must never appear in status output or error text. No network: websockets.connect is faked."""
from __future__ import annotations

import asyncio
import json

import pytest

from app.scout import source as src

FAKE_KEY = "00000000-1111-2222-3333-444444444444"


@pytest.fixture
def helius_env(monkeypatch):
    monkeypatch.setenv("HELIUS_API_KEY", FAKE_KEY)
    monkeypatch.setenv("SCOUT_WS_HELIUS", "true")
    monkeypatch.setenv("SCOUT_WS_URL", "wss://fallback.example")
    monkeypatch.setenv("SCOUT_WS_BACKOFF_S", "0.01")
    monkeypatch.setenv("SCOUT_WS_HELIUS_COOLDOWN_S", "600")


def test_helius_url_needs_flag_and_key(monkeypatch):
    monkeypatch.delenv("SCOUT_WS_HELIUS", raising=False)
    monkeypatch.setenv("HELIUS_API_KEY", FAKE_KEY)
    assert src._helius_ws_url() == ""
    monkeypatch.setenv("SCOUT_WS_HELIUS", "true")
    monkeypatch.setenv("HELIUS_API_KEY", "")
    assert src._helius_ws_url() == ""
    monkeypatch.setenv("HELIUS_API_KEY", FAKE_KEY)
    assert src._helius_ws_url().startswith("wss://mainnet.helius-rpc.com/?api-key=")


def test_default_unchanged_without_flag(monkeypatch):
    monkeypatch.delenv("SCOUT_WS_HELIUS", raising=False)
    s = src.ScoutSource()
    url, prov = s._pick_ws()
    assert url == s.ws_url and prov == "default"


def test_pick_cooldown_and_daily_cap(helius_env, monkeypatch):
    s = src.ScoutSource()
    assert s._pick_ws()[1] == "helius"
    s._helius_failed("x")
    assert s._pick_ws()[1] == "helius"                     # one failure is not enough
    s._helius_failed("x")
    url, prov = s._pick_ws()
    assert prov == "fallback" and url == "wss://fallback.example" and s.stats["helius_ws_fallbacks"] == 1
    s.helius_cooldown_until = 0
    assert s._pick_ws()[1] == "helius"
    s._helius_today()["bytes"] = int(s.helius_max_mb_day * 1e6) + 1
    assert s._pick_ws()[1] == "fallback"                   # credit guard


def test_status_and_errors_never_show_key(helius_env):
    s = src.ScoutSource()
    s._err("ws", RuntimeError(f"connect failed wss://mainnet.helius-rpc.com/?api-key={FAKE_KEY} 401"))
    blob = json.dumps(s.status(), default=str)
    assert FAKE_KEY not in blob and "api-key=<redacted>" in blob
    assert s.status()["ws_helius"]["configured"] is True and s.status()["ws_host"] == "fallback.example"


class _FakeWS:
    def __init__(self, url, log):
        self.url, self.log = url, log

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def send(self, msg):
        m = json.loads(msg)
        self.log.append(("send", self.url, m["method"]))
        self._last = m["id"]

    async def recv(self):
        if getattr(self, "_last", None) is not None:
            rid, self._last = self._last, None
            return json.dumps({"jsonrpc": "2.0", "id": rid, "result": 77})
        await asyncio.sleep(10)


def test_shard_falls_back_after_helius_connect_failures(helius_env, monkeypatch):
    import websockets
    log = []

    def connect(url, **kw):
        log.append(("connect", url))
        if "helius" in url:
            raise OSError("refused")
        return _FakeWS(url, log)
    monkeypatch.setattr(websockets, "connect", connect)
    s = src.ScoutSource()
    s.sub_method["KEY1"], s.sub_kind["KEY1"] = "logs", "pump_uva"
    sh = {"idx": 0, "want": ["KEY1"], "subs": {}, "connected": False, "reconnects": 0}

    async def go():
        t = asyncio.ensure_future(s._ws_shard(sh))
        for _ in range(200):
            await asyncio.sleep(0.01)
            if sh["subs"]:
                break
        t.cancel()
        await asyncio.gather(t, return_exceptions=True)
    asyncio.run(go())
    urls = [u for k, u, *_ in [(x[0], x[1]) for x in log] if k == "connect"]
    assert sum("helius" in u for u in urls) == 2 and urls[-1] == "wss://fallback.example"
    assert sh["provider"] == "fallback" and sh["subs"] == {77: "KEY1"} and s.stats["helius_ws_fallbacks"] == 1
    assert FAKE_KEY not in json.dumps(list(s.last_errors))


def test_shard_uses_helius_and_counts_bytes(helius_env, monkeypatch):
    import websockets
    log = []
    monkeypatch.setattr(websockets, "connect", lambda url, **kw: _FakeWS(url, log))
    s = src.ScoutSource()
    s.sub_method["KEY1"], s.sub_kind["KEY1"] = "account", "wallet"
    sh = {"idx": 0, "want": ["KEY1"], "subs": {}, "connected": False, "reconnects": 0}

    async def go():
        t = asyncio.ensure_future(s._ws_shard(sh))
        for _ in range(200):
            await asyncio.sleep(0.01)
            if sh["subs"]:
                break
        t.cancel()
        await asyncio.gather(t, return_exceptions=True)
    asyncio.run(go())
    assert sh["provider"] == "helius" and s.stats["ws_connects_helius"] == 1
    assert s.stats["ws_bytes_helius"] > 0 and s._helius_today()["bytes"] == s.stats["ws_bytes_helius"]
    assert ("send", s.helius_ws_url, "accountSubscribe") in log


def test_websockets_logger_records_are_redacted(caplog):
    import logging
    from app import logredact
    logredact.install()
    with caplog.at_level(logging.DEBUG, logger="websockets.client"):
        logging.getLogger("websockets.client").debug("connecting to wss://mainnet.helius-rpc.com/?api-key=%s", FAKE_KEY)
    assert FAKE_KEY not in caplog.text
