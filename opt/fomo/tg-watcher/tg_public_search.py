"""v1.23 Telegram PUBLIC search for the coach's momentum_paper arm (PAPER ONLY — feeds a score, never a trade).

Uses the watcher's existing user session, read-only:
  * channels.checkSearchPostsFlood + channels.searchPosts (public channels incl. ones we never joined) — FREE daily
    slots only; allow_paid_stars is never set, so this can never spend Stars.
  * messages.searchGlobal fallback (chats the account is already in), private user chats excluded.
Never joins, never sends, never marks anything read. Message text is matched in memory and dropped; only counts and
public channel usernames leave this module. Account safety: ≥ MIN_INTERVAL between searches, hourly + daily caps,
FloodWait honoured in full plus exponential backoff, any FloodWait > DISABLE_FLOOD_S switches searching off until
local midnight (persisted across restarts). A failed search is reported as unknown, never as zero.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

CA_OK = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
TICK_OK = re.compile(r"^\$[A-Za-z0-9]{2,12}$")


def _on(name: str, d: str = "false") -> bool:
    return os.getenv(name, d).strip().lower() in {"1", "true", "yes", "on"}


def _num(name: str, d: float) -> float:
    try:
        return float(os.getenv(name, "").strip() or d)
    except ValueError:
        return d


def config() -> dict[str, Any]:
    return {
        "enabled": _on("MOMENTUM_TG_GLOBAL_ENABLED"),
        "min_interval_s": max(5.0, _num("MOMENTUM_TG_GLOBAL_MIN_INTERVAL_S", 20)),
        "max_per_hour": int(_num("MOMENTUM_TG_GLOBAL_MAX_PER_HOUR", 120)),
        "max_per_day": int(_num("MOMENTUM_TG_GLOBAL_MAX_PER_DAY", 1000)),
        "disable_flood_s": _num("MOMENTUM_TG_GLOBAL_DISABLE_FLOOD_S", 300),
        "backoff_base_s": _num("MOMENTUM_TG_GLOBAL_BACKOFF_S", 60),
        "limit": int(min(100, _num("MOMENTUM_TG_GLOBAL_LIMIT", 50))),
        "use_posts": _on("MOMENTUM_TG_GLOBAL_USE_POSTS", "true"),
        "posts_for_ticker": _on("MOMENTUM_TG_GLOBAL_POSTS_FOR_TICKER", "false"),
        "poll_s": max(2.0, _num("MOMENTUM_TG_GLOBAL_POLL_S", 5)),
        "state_path": Path(os.getenv("MOMENTUM_TG_GLOBAL_STATE",
                                     str(Path(__file__).with_name("tg_public_search_state.json")))),
    }


def next_local_midnight(now: float) -> float:
    d = datetime.fromtimestamp(now).astimezone()
    return (d + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def _fmt(ts: float) -> str:
    return datetime.fromtimestamp(ts).astimezone().strftime("%a %-I:%M:%S %p %Z")


class Limiter:
    """Search spacing + caps + FloodWait handling. State persists so a restart never resets a flood backoff."""

    def __init__(self, cfg: dict[str, Any], now_fn: Callable[[], float] = time.time) -> None:
        self.c, self.now = cfg, now_fn
        self.calls: list[float] = []
        self.next_ok = 0.0
        self.disabled_until = 0.0
        self.floods = 0
        self.consec_floods = 0
        self.posts_off_until = 0.0
        self.posts_remaining: Optional[int] = None
        self.posts_daily: Optional[int] = None
        self.load()

    def load(self) -> None:
        try:
            st = json.loads(self.c["state_path"].read_text())
        except Exception:  # noqa: BLE001
            return
        now = self.now()
        self.calls = [float(t) for t in st.get("calls") or [] if now - float(t) < 86400]
        self.next_ok = float(st.get("next_ok") or 0)
        self.disabled_until = float(st.get("disabled_until") or 0)
        self.floods = int(st.get("floods") or 0)
        self.consec_floods = int(st.get("consec_floods") or 0)
        self.posts_off_until = float(st.get("posts_off_until") or 0)

    def save(self) -> None:
        try:
            p = self.c["state_path"]
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps({"calls": self.calls[-3000:], "next_ok": self.next_ok,
                                       "disabled_until": self.disabled_until, "floods": self.floods,
                                       "consec_floods": self.consec_floods, "posts_off_until": self.posts_off_until}))
            tmp.replace(p)
        except Exception:  # noqa: BLE001
            pass

    def _count(self, secs: float) -> int:
        now = self.now()
        self.calls = [t for t in self.calls if now - t < 86400]
        return sum(1 for t in self.calls if now - t < secs)

    def can_search(self) -> tuple[bool, str]:
        now = self.now()
        if now < self.disabled_until:
            return False, f"disabled after a long FloodWait until {_fmt(self.disabled_until)}"
        if self.next_ok - now > self.c["min_interval_s"] + 1:
            return False, f"FloodWait backoff until {_fmt(self.next_ok)}"
        if self._count(3600) >= self.c["max_per_hour"]:
            return False, "hourly cap"
        if self._count(86400) >= self.c["max_per_day"]:
            return False, "daily cap"
        return True, "ok"

    def wait_s(self) -> float:
        now = self.now()
        last = self.calls[-1] if self.calls else 0.0
        return max(0.0, self.next_ok - now, last + self.c["min_interval_s"] - now)

    def take(self) -> None:
        now = self.now()
        self.calls.append(now)
        self.next_ok = max(self.next_ok, now + self.c["min_interval_s"])
        self.save()

    def success(self) -> None:
        self.consec_floods = 0

    def flood(self, seconds: float) -> dict[str, Any]:
        now = self.now()
        self.floods += 1
        self.consec_floods += 1
        backoff = min(1800.0, self.c["backoff_base_s"] * (2 ** (self.consec_floods - 1)))
        self.next_ok = now + float(seconds) + backoff                      # honour in full, plus backoff
        disabled = float(seconds) > self.c["disable_flood_s"]
        if disabled:
            self.disabled_until = max(next_local_midnight(now), self.next_ok)
        self.save()
        return {"seconds": seconds, "backoff_s": backoff, "next_ok": self.next_ok, "disabled": disabled,
                "disabled_until": self.disabled_until if disabled else None}

    def status(self) -> dict[str, Any]:
        return {"searches_hour": self._count(3600), "searches_day": self._count(86400),
                "max_hour": self.c["max_per_hour"], "max_day": self.c["max_per_day"],
                "min_interval_s": self.c["min_interval_s"],
                "disabled_until": int(self.disabled_until * 1000) if self.disabled_until > self.now() else None,
                "next_ok_ms": int(self.next_ok * 1000) if self.next_ok > self.now() else None,
                "posts_remaining": self.posts_remaining, "posts_daily": self.posts_daily,
                "posts_off_until": int(self.posts_off_until * 1000) if self.posts_off_until > self.now() else None,
                "floods": self.floods}


def _matcher(q: str) -> Callable[[str], bool]:
    if q.startswith("$"):
        rx = re.compile(r"(?<![A-Za-z0-9])\$" + re.escape(q[1:]) + r"(?![A-Za-z0-9])", re.IGNORECASE)
        return lambda t: bool(rx.search(t))
    return lambda t: q in t


def _ts(m: Any) -> Optional[float]:
    d = getattr(m, "date", None)
    if d is None:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.timestamp()


def count_messages(messages: list[Any], chats: list[Any], query: str, since_ts: float) -> dict[str, Any]:
    """Telethon search results → counts. Private user chats skipped; text only matched, never kept."""
    match = _matcher(query)
    unames = {getattr(ch, "id", None): getattr(ch, "username", None) for ch in chats or []}
    keys: set = set()
    peers: set = set()
    users: set = set()
    n_in_window = 0
    for m in messages or []:
        ts = _ts(m)
        if ts is None or ts < since_ts:
            continue
        n_in_window += 1
        peer = getattr(m, "peer_id", None)
        pid = getattr(peer, "channel_id", None) or getattr(peer, "chat_id", None)
        if pid is None:
            continue
        text = getattr(m, "message", None) or ""
        urls = " ".join(str(getattr(e, "url", "") or "") for e in (getattr(m, "entities", None) or []))
        if not (match(text) or match(urls)):
            continue
        keys.add((pid, getattr(m, "id", None)))
        peers.add(pid)
        if unames.get(pid):
            users.add(unames[pid])
    return {"keys": keys, "peers": peers, "usernames": users, "in_window": n_in_window}


async def search_one(client: Any, q: str, since_ts: float, lim: Limiter, cfg: dict[str, Any], allow_posts: bool,
                     log: Callable[[str], None] = print) -> tuple[str, Any]:
    from telethon.errors import FloodError, RPCError
    from telethon.tl.functions.channels import CheckSearchPostsFloodRequest, SearchPostsRequest
    from telethon.tl.functions.messages import SearchGlobalRequest
    from telethon.tl.types import InputMessagesFilterEmpty, InputPeerEmpty
    if allow_posts and cfg["use_posts"] and lim.now() >= lim.posts_off_until:
        try:
            fl = await client(CheckSearchPostsFloodRequest(query=q), flood_sleep_threshold=0)
            lim.posts_daily, lim.posts_remaining = getattr(fl, "total_daily", None), getattr(fl, "remains", None)
            if getattr(fl, "query_is_free", False) or (fl.remains or 0) > 0:
                r = await client(SearchPostsRequest(offset_rate=0, offset_peer=InputPeerEmpty(), offset_id=0,
                                                    limit=cfg["limit"], query=q), flood_sleep_threshold=0)
                sf = getattr(r, "search_flood", None)
                if sf is not None:
                    lim.posts_daily, lim.posts_remaining = sf.total_daily, sf.remains
                return "posts", r
            lim.posts_off_until = float(getattr(fl, "wait_till", None) or next_local_midnight(lim.now()))
            lim.save()
            log(f"[tg-public] free public-post searches used up ({fl.total_daily}/day) — never paying Stars; "
                f"joined-chat search only until {_fmt(lim.posts_off_until)}")
        except FloodError:
            raise
        except RPCError as exc:
            lim.posts_off_until = next_local_midnight(lim.now())
            lim.save()
            log(f"[tg-public] public-post search unavailable ({type(exc).__name__}) — joined-chat search only today")
    r = await client(SearchGlobalRequest(q=q, filter=InputMessagesFilterEmpty(),
                                         min_date=datetime.fromtimestamp(since_ts, timezone.utc), max_date=None,
                                         offset_rate=0, offset_peer=InputPeerEmpty(), offset_id=0,
                                         limit=cfg["limit"]), flood_sleep_threshold=0)
    return "global", r


async def run_request(client: Any, req: dict[str, Any], lim: Limiter, cfg: dict[str, Any],
                      log: Callable[[str], None] = print, sleep: Callable[[float], Any] = asyncio.sleep) -> dict[str, Any]:
    from telethon.errors import FloodError
    mint = str(req.get("mint") or "")
    since = lim.now() - float(req.get("window_min") or 30) * 60
    keys: set = set()
    peers: set = set()
    users: set = set()
    methods: list[str] = []
    errors: list[str] = []
    capped = False
    for i, q in enumerate(list(req.get("queries") or [])[:2]):
        if not (CA_OK.match(q) or TICK_OK.match(q)):
            errors.append("bad query")
            continue
        ok, why = lim.can_search()
        if not ok:
            errors.append(why)
            break
        w = lim.wait_s()
        if w > 0:
            await sleep(w)
        lim.take()
        try:
            method, r = await search_one(client, q, since, lim, cfg, allow_posts=(i == 0 or cfg["posts_for_ticker"]),
                                         log=log)
            lim.success()
        except FloodError as exc:
            secs = float(getattr(exc, "seconds", None) or 60)
            info = lim.flood(secs)
            msg = (f"[tg-public] FloodWait {secs:.0f}s on search → paused until {_fmt(info['next_ok'])} "
                   f"(backoff {info['backoff_s']:.0f}s)")
            if info["disabled"]:
                msg += f"; > {cfg['disable_flood_s']:.0f}s so searching is OFF until {_fmt(info['disabled_until'])}"
            log(msg)
            errors.append(f"FloodWait {secs:.0f}s")
            break
        except Exception as exc:  # noqa: BLE001
            errors.append(type(exc).__name__)
            continue
        cnt = count_messages(getattr(r, "messages", None) or [], getattr(r, "chats", None) or [], q, since)
        methods.append(method)
        keys |= cnt["keys"]
        peers |= cnt["peers"]
        users |= cnt["usernames"]
        capped = capped or cnt["in_window"] >= cfg["limit"]
    if not methods:
        return {"mint": mint, "ok": False, "error": ("; ".join(errors) or "no search")[:80]}
    return {"mint": mint, "ok": True, "channels": len(peers), "messages": len(keys), "usernames": sorted(users)[:20],
            "method": "+".join(methods), "capped": capped, "partial": bool(errors)}


def coach_next(base_url: str, n: int, status: dict[str, Any], timeout: float = 4.0) -> dict[str, Any]:
    qs = urllib.parse.urlencode({"n": n, **{k: v for k, v in status.items() if v is not None}})
    with urllib.request.urlopen(base_url.rstrip("/") + "/api/paper/momentum/tg_search/next?" + qs,  # noqa: S310
                                timeout=timeout) as r:
        return json.loads(r.read().decode() or "{}")


def coach_result(base_url: str, results: list[dict[str, Any]], timeout: float = 4.0) -> dict[str, Any]:
    req = urllib.request.Request(base_url.rstrip("/") + "/api/paper/momentum/tg_search/result",
                                 data=json.dumps({"results": results}).encode(), method="POST",
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 — loopback coach only
        return json.loads(r.read().decode() or "{}")


async def search_loop(client: Any, coach_url: str, log: Callable[[str], None] = print,
                      cfg: Optional[dict[str, Any]] = None) -> None:
    from telethon.errors import FloodError
    from telethon.tl.functions.channels import CheckSearchPostsFloodRequest
    cfg = cfg or config()
    lim = Limiter(cfg)
    ok, why = lim.can_search()
    if ok and cfg["use_posts"]:
        try:                                      # one read-only quota check so the log shows the free daily slots
            lim.take()
            fl = await client(CheckSearchPostsFloodRequest(), flood_sleep_threshold=0)
            lim.posts_daily, lim.posts_remaining = fl.total_daily, fl.remains
            log(f"[tg-public] public-post search: {fl.remains}/{fl.total_daily} free searches left today "
                f"(paid searches never used)")
        except FloodError as exc:
            info = lim.flood(float(getattr(exc, "seconds", None) or 60))
            log(f"[tg-public] FloodWait on quota check → paused until {_fmt(info['next_ok'])}")
        except Exception as exc:  # noqa: BLE001
            log(f"[tg-public] quota check failed: {type(exc).__name__} (joined-chat search still available)")
    elif not ok:
        log(f"[tg-public] limiter: {why}")
    last_report = time.time()
    done = {"ok": 0, "unknown": 0}
    while True:
        await asyncio.sleep(cfg["poll_s"])
        try:
            ok, why = lim.can_search()
            resp = await asyncio.to_thread(coach_next, coach_url, 1 if ok else 0, lim.status())
        except Exception:  # noqa: BLE001
            continue
        for req in resp.get("requests") or []:
            try:
                res = await run_request(client, req, lim, cfg, log=log)
            except Exception as exc:  # noqa: BLE001
                res = {"mint": req.get("mint"), "ok": False, "error": type(exc).__name__}
            done["ok" if res.get("ok") else "unknown"] += 1
            try:
                await asyncio.to_thread(coach_result, coach_url, [res])
            except Exception as exc:  # noqa: BLE001
                log(f"[tg-public] result post failed: {type(exc).__name__}")
            st = lim.status()
            if res.get("ok"):
                u = ", ".join("@" + x for x in res["usernames"][:6])
                log(f"[tg-public] {str(req.get('mint'))[:8]}… {'+'.join('CA' if CA_OK.match(q) else q for q in req.get('queries') or [])}"
                    f" → {res['channels']} channels / {res['messages']} msgs in {req.get('window_min')} min "
                    f"via {res['method']}{' (page full)' if res['capped'] else ''}{' [' + u + ']' if u else ''} | "
                    f"hour {st['searches_hour']}/{st['max_hour']}, free post searches left {st['posts_remaining']}")
            else:
                log(f"[tg-public] {str(req.get('mint'))[:8]}… → unknown ({res.get('error')}) | hour "
                    f"{st['searches_hour']}/{st['max_hour']}")
        if time.time() - last_report > 3600:
            last_report = time.time()
            st = lim.status()
            log(f"[tg-public] last hour: {done['ok']} searched, {done['unknown']} unknown | searches/h "
                f"{st['searches_hour']}/{st['max_hour']}, day {st['searches_day']}/{st['max_day']}, floods {st['floods']}")
            done = {"ok": 0, "unknown": 0}
