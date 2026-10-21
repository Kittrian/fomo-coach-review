"""v1.23 watcher public search: rate limits, FloodWait handling, never pays / joins / sends / marks read, counts only."""
import asyncio
import inspect
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tg_public_search as tgps  # noqa: E402
from telethon.errors import FloodWaitError  # noqa: E402

MINT = "DwcXyhEcSvzWgakDKpetZLFvbAHutpGpzhbU4iempump"


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def cfg(tmp, **kw):
    c = tgps.config()
    c.update(state_path=Path(tmp) / "st.json", min_interval_s=20, max_per_hour=120, max_per_day=1000,
             disable_flood_s=300, backoff_base_s=60, limit=50, use_posts=True, posts_for_ticker=False)
    c.update(kw)
    return c


def msg(pid, mid, text, ago_min, channel=True, now=None, url=None):
    now = now or datetime.now(timezone.utc)
    peer = NS(channel_id=pid) if channel else NS(user_id=pid)
    ents = [NS(url=url)] if url else None
    return NS(id=mid, peer_id=peer, message=text, date=now - timedelta(minutes=ago_min), entities=ents)


def flood(seconds):
    e = FloodWaitError(request=None, capture=seconds)
    return e


class FakeClient:
    def __init__(self, posts=None, glob=None, flood_info=None, raise_on=None):
        self.calls = []
        self.posts, self.glob = posts, glob
        self.flood_info = flood_info or NS(query_is_free=False, total_daily=10, remains=5, wait_till=None)
        self.raise_on = raise_on or {}

    async def __call__(self, req, ordered=False, flood_sleep_threshold=None):
        name = type(req).__name__
        self.calls.append((name, req, flood_sleep_threshold))
        if name in self.raise_on:
            raise self.raise_on[name]
        if name == "CheckSearchPostsFloodRequest":
            return self.flood_info
        if name == "SearchPostsRequest":
            return self.posts
        if name == "SearchGlobalRequest":
            return self.glob
        raise AssertionError(f"unexpected Telegram call {name}")


class TestLimiter(unittest.TestCase):
    def test_spacing_hour_cap_and_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            clk = Clock()
            L = tgps.Limiter(cfg(tmp, max_per_hour=3), clk)
            self.assertEqual(L.can_search(), (True, "ok"))
            L.take()
            self.assertAlmostEqual(L.wait_s(), 20)
            clk.t += 20
            L.take(); clk.t += 20; L.take(); clk.t += 20
            self.assertEqual(L.can_search(), (False, "hourly cap"))
            clk.t += 3600
            self.assertTrue(L.can_search()[0])
            L2 = tgps.Limiter(cfg(tmp, max_per_hour=3), clk)
            self.assertEqual(len(L2.calls), 3)

    def test_floodwait_honoured_plus_backoff_and_long_flood_disables_until_midnight(self):
        with tempfile.TemporaryDirectory() as tmp:
            clk = Clock()
            L = tgps.Limiter(cfg(tmp), clk)
            info = L.flood(30)
            self.assertEqual(info["next_ok"], clk.t + 30 + 60)
            self.assertFalse(L.can_search()[0])
            info2 = L.flood(30)                                 # consecutive → doubled backoff
            self.assertEqual(info2["backoff_s"], 120)
            clk.t += 200
            self.assertTrue(L.can_search()[0])
            big = L.flood(301)
            self.assertTrue(big["disabled"])
            self.assertGreaterEqual(L.disabled_until, tgps.next_local_midnight(clk.t))
            clk.t += 3600
            ok, why = L.can_search()
            self.assertFalse(ok)
            self.assertIn("disabled", why)
            # survives a watcher restart
            self.assertFalse(tgps.Limiter(cfg(tmp), clk).can_search()[0])
            clk.t = L.disabled_until + 1
            self.assertTrue(tgps.Limiter(cfg(tmp), clk).can_search()[0])


class TestCounting(unittest.TestCase):
    def test_counts_window_peers_and_drops_text(self):
        now = datetime.now(timezone.utc)
        ms = [msg(1, 1, f"buy {MINT} now", 5, now=now), msg(1, 2, f"{MINT}", 10, now=now),
              msg(2, 3, "chart", 3, now=now, url=f"https://pump.fun/coin/{MINT}"),
              msg(3, 4, f"{MINT}", 45, now=now),                      # outside 30 min
              msg(4, 5, f"{MINT}", 2, channel=False, now=now),        # private user chat
              msg(5, 6, "unrelated", 1, now=now)]
        chats = [NS(id=1, username="chanA"), NS(id=2, username=None), NS(id=5, username="x")]
        c = tgps.count_messages(ms, chats, MINT, (now - timedelta(minutes=30)).timestamp())
        self.assertEqual((len(c["peers"]), len(c["keys"]), c["usernames"]), (2, 3, {"chanA"}))
        t = tgps.count_messages([msg(1, 1, "$ZORB to the moon", 1, now=now), msg(2, 2, "$ZORBLAX", 1, now=now)],
                                chats, "$ZORB", (now - timedelta(minutes=30)).timestamp())
        self.assertEqual(len(t["keys"]), 1)                         # $ZORB ≠ $ZORBLAX


class TestRunRequest(unittest.TestCase):
    def _run(self, client, req, L, c):
        async def nosleep(s):
            L.now.t += s
        return asyncio.run(tgps.run_request(client, req, L, c, log=lambda *_: None, sleep=nosleep))

    def test_posts_for_ca_global_for_ticker_never_paid(self):
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            clk = Clock(now.timestamp())
            c = cfg(tmp)
            L = tgps.Limiter(c, clk)
            posts = NS(messages=[msg(1, 1, MINT, 2, now=now), msg(2, 2, MINT, 4, now=now)],
                       chats=[NS(id=1, username="pubA"), NS(id=2, username="pubB")],
                       search_flood=NS(total_daily=10, remains=4))
            glob = NS(messages=[msg(3, 9, "$ZORBLAX pumping", 1, now=now)], chats=[NS(id=3, username="grpC")])
            fc = FakeClient(posts=posts, glob=glob)
            res = self._run(fc, {"mint": MINT, "queries": [MINT, "$ZORBLAX"], "window_min": 30}, L, c)
            self.assertEqual(res, {"mint": MINT, "ok": True, "channels": 3, "messages": 3,
                                   "usernames": ["grpC", "pubA", "pubB"], "method": "posts+global",
                                   "capped": False, "partial": False})
            names = [n for n, _, _ in fc.calls]
            self.assertEqual(names, ["CheckSearchPostsFloodRequest", "SearchPostsRequest", "SearchGlobalRequest"])
            for n, req, fst in fc.calls:
                self.assertEqual(fst, 0)                            # FloodWait always surfaces to us
                if n == "SearchPostsRequest":
                    self.assertIsNone(req.allow_paid_stars)         # never pays Stars
            self.assertEqual(L.posts_remaining, 4)
            self.assertEqual(len(L.calls), 2)
            self.assertGreaterEqual(L.calls[1] - L.calls[0], 20)

    def test_no_free_slots_falls_back_to_joined_chats_only(self):
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            clk = Clock(now.timestamp())
            c = cfg(tmp)
            L = tgps.Limiter(c, clk)
            fc = FakeClient(glob=NS(messages=[], chats=[]),
                            flood_info=NS(query_is_free=False, total_daily=10, remains=0, wait_till=None))
            res = self._run(fc, {"mint": MINT, "queries": [MINT]}, L, c)
            self.assertTrue(res["ok"])
            self.assertEqual((res["channels"], res["method"]), (0, "global"))
            self.assertNotIn("SearchPostsRequest", [n for n, _, _ in fc.calls])
            self.assertGreater(L.posts_off_until, clk.t)

    def test_floodwait_is_unknown_not_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            clk = Clock()
            c = cfg(tmp, use_posts=False)
            L = tgps.Limiter(c, clk)
            fc = FakeClient(raise_on={"SearchGlobalRequest": flood(900)})
            res = self._run(fc, {"mint": MINT, "queries": [MINT, "$ZORBLAX"]}, L, c)
            self.assertEqual(res["ok"], False)
            self.assertIn("FloodWait 900s", res["error"])
            self.assertEqual(len(fc.calls), 1)                      # stopped after the flood
            self.assertGreater(L.disabled_until, clk.t)
            res2 = self._run(FakeClient(glob=NS(messages=[], chats=[])), {"mint": MINT, "queries": [MINT]}, L, c)
            self.assertFalse(res2["ok"])
            self.assertIn("disabled", res2["error"])

    def test_bad_queries_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = cfg(tmp)
            L = tgps.Limiter(c, Clock())
            fc = FakeClient()
            res = self._run(fc, {"mint": "x", "queries": ["hello world", "$a b"]}, L, c)
            self.assertFalse(res["ok"])
            self.assertEqual(fc.calls, [])


class TestReadOnly(unittest.TestCase):
    def test_module_never_joins_sends_or_marks_read(self):
        src = inspect.getsource(tgps)
        for bad in ("JoinChannel", "ImportChatInvite", "send_message", "SendMessage", "ReadHistory",
                    "send_read_acknowledge", "ReadMentions", "allow_paid_stars=", "Forward", "mark_read"):
            self.assertNotIn(bad, src)

    def test_off_by_default(self):
        os.environ.pop("MOMENTUM_TG_GLOBAL_ENABLED", None)
        self.assertFalse(tgps.config()["enabled"])


if __name__ == "__main__":
    unittest.main()
