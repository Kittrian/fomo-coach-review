"""v1.39c: post-buy checks — late rugcheck fail auto-sells; arms env."""
from __future__ import annotations

import asyncio

from tests.test_v119 import live_env, MINT  # noqa: F401
from app.live import engine as eng_mod
from app.live.engine import LiveEngine


def test_arms_env(monkeypatch):
    monkeypatch.setenv("LIVE_POST_BUY_CHECK_ARMS", "scout_solo,scout_confirmed")
    assert eng_mod._post_buy_check_arms() == {"scout_solo", "scout_confirmed"}
    monkeypatch.setenv("LIVE_POST_BUY_CHECK_ARMS", "")
    assert eng_mod._post_buy_check_arms() == set()


def test_rug_deferred(live_env, monkeypatch):
    e = LiveEngine()
    exits, logged = [], []
    monkeypatch.setattr(e, "force_exit", lambda mint, reason, **kw: exits.append((reason, kw)))
    monkeypatch.setattr(e, "_ledger", lambda kind, **kw: logged.append(kind))

    async def go(res):
        fut = asyncio.get_running_loop().create_future()
        meta = {}
        e._rug_deferred(MINT, "scout_solo", fut, meta)
        fut.set_result(res)
        await asyncio.sleep(0)
        return meta
    m = asyncio.run(go({"ok": False, "refuse": ["dev holds 40%"]}))
    assert exits and exits[0][0] == "rugcheck_post_buy" and exits[0][1]["urgent"] and "post_buy_check_fail" in logged
    assert m["_rug"]["ok"] is False
    exits.clear()
    asyncio.run(go({"ok": True, "refuse": []}))
    assert not exits
    assert e.stats["post_buy_check_fail"] == 1
