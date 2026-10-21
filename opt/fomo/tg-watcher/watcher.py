#!/usr/bin/env python3
"""
Telethon watcher for PUMP signals in configured Telegram groups.

Writes the latest parsed event to:
  ~/Library/Application Support/BTCP-Drafts/pump-from-telegram.json

Does not log in interactively — run login_once.py first to create the session.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.tl.custom.dialog import Dialog
from telethon.tl.types import Channel, Chat

try:
    from coach_ingest import post_ca_announce
except ImportError:  # pragma: no cover
    post_ca_announce = None  # type: ignore

try:
    import alerts
except ImportError:  # pragma: no cover
    alerts = None  # type: ignore

import builtins

_builtin_print = builtins.print


def print(*args: Any, **kwargs: Any) -> None:  # noqa: A001 — v1.12.1: every log line gets a local timestamp
    ts = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + datetime.now().astimezone().strftime(" %Z")
    kwargs.setdefault("flush", True)
    _builtin_print(f"[{ts}]", *args, **kwargs)


DEFAULT_HOOK = Path.home() / "Library/Application Support/BTCP-Drafts/pump-from-telegram.json"
DEFAULT_GROUPS = ["Fluffy Alpha", "Fluffyalpha", "Zyrox Insider Calls", "SmugCalls"]
# v1.13: CA posts from these sources still go to the coach but do NOT fire the first-CA push alert
# (SmugCalls posts ~95 calls/day — arm C paper control only).
DEFAULT_NO_ALERT = "smugcalls"
# v1.13: the ONLY public channels the watcher may auto-join (Andrew approved SmugCalls on 2026-10-07).
# Hard-coded on purpose: env can narrow this list but never extend it.
AUTO_JOIN_ALLOWLIST = frozenset({"smugcalls"})

# Abbreviation → IANA
TZ_ALIASES: dict[str, str] = {
    "EST": "America/New_York",
    "EDT": "America/New_York",
    "ET": "America/New_York",
    "CST": "America/Chicago",
    "CDT": "America/Chicago",
    "CT": "America/Chicago",
    "PST": "America/Los_Angeles",
    "PDT": "America/Los_Angeles",
    "PT": "America/Los_Angeles",
    "UTC": "UTC",
    "GMT": "UTC",
}

TZ_TOKEN_RE = re.compile(
    r"\b(?:EST|EDT|CST|CDT|PST|PDT|UTC|GMT|ET|CT|PT|"
    r"America/[A-Za-z_]+|Europe/[A-Za-z_]+|UTC)\b",
    re.IGNORECASE,
)

# Times like: 3:30pm, 15:30, 3:30 PM, 3pm
TIME_RE = re.compile(
    r"\b(?P<h>1[0-2]|0?[1-9]|2[0-3]|0?\d)"
    r"(?::(?P<m>[0-5]\d))?"
    r"\s*(?P<ampm>[AaPp][Mm])?\b"
)

# Dates like: Oct 1, October 1st, 2026-10-01, 10/1, 10/1/26
DATE_RE = re.compile(
    r"(?:"
    r"(?P<mon_name>Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    r"\s+(?P<day1>\d{1,2})(?:st|nd|rd|th)?"
    r"(?:,?\s*(?P<year1>\d{4}))?"
    r"|"
    r"(?P<iso>\d{4}-\d{2}-\d{2})"
    r"|"
    r"(?P<md>\d{1,2}/\d{1,2}(?:/\d{2,4})?)"
    r")",
    re.IGNORECASE,
)

# Solana mint: base58 alphabet, typically 32–44 chars (often 43–44)
MINT_RE = re.compile(r"\b([1-9A-HJ-NP-Za-km-z]{32,44})\b")

# Avoid treating common non-mint base58-ish tokens as CAs when possible
MINT_DENY_PREFIXES = ("http", "t.me", "telegram")

DOLLAR_TICKER_RE = re.compile(r"(?<![A-Za-z0-9])\$([A-Za-z][A-Za-z0-9]{1,14})\b")
CALLING_TICKER_RE = re.compile(
    r"\b(?:now\s+)?(?:calling|ape|aping|buy(?:ing)?|launching|launched|introducing|presenting)\s+\$([A-Za-z][A-Za-z0-9]{1,14})\b",
    re.IGNORECASE,
)
LABEL_TICKER_RE = re.compile(r"\b(?:ticker|symbol)\s*[:=]\s*\$?([A-Za-z][A-Za-z0-9]{1,14})\b", re.IGNORECASE)
TICKER_DENY = {"CA", "TO", "IS", "IT", "THE", "AND", "SOL", "WSOL", "USD", "USDC", "USDT", "ETH", "BTC",
               "MC", "MCAP", "ATH", "PUMP", "DEX", "LP", "KOL", "KOLS", "NFA", "DYOR"}

# Mint-bearing links: pump.fun/coin/<m>, pump.fun/<m>, solscan.io/token/<m>, birdeye.so/token/<m>,
# gmgn.ai/sol/token/<m>, jup.ag/swap/SOL-<m>, photon/bullx/axiom style ?address=<m>; dexscreener (may be a pair)
_B58 = r"([1-9A-HJ-NP-Za-km-z]{32,44})"
LINK_MINT_RES = [
    (re.compile(r"pump\.fun/(?:coin/|advanced/coin/)?" + _B58, re.I), 4),
    (re.compile(r"(?:solscan\.io|birdeye\.so|solana\.fm)/(?:token|address)/" + _B58, re.I), 3),
    (re.compile(r"gmgn\.ai/sol/token/(?:[A-Za-z0-9]+_)?" + _B58, re.I), 3),
    (re.compile(r"jup\.ag/(?:swap/)?[A-Za-z0-9]+-" + _B58, re.I), 3),
    (re.compile(r"[?&](?:address|token|mint|ca)=" + _B58, re.I), 3),
    (re.compile(r"dexscreener\.com/solana/" + _B58, re.I), 2),
]

SELL_NOW_RE = re.compile(
    r"\b(?:"
    r"sell\s*now|dump\s*now|exit\s*now|take\s*profit\s*now|"
    r"gtfo|get\s*out|panic\s*sell|close\s*(?:the\s*)?position|"
    r"sell\s*(?:asap|immediately)|jeet\s*now"
    r")\b",
    re.IGNORECASE,
)

URL_RE = re.compile(r"https?://[^\s<>\]\)]+", re.IGNORECASE)

MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}


def expand_path(p: str) -> Path:
    return Path(os.path.expanduser(p)).resolve()


def resolve_tz_name(token: Optional[str]) -> Optional[str]:
    if not token:
        return None
    t = token.strip()
    upper = t.upper()
    if upper in TZ_ALIASES:
        return TZ_ALIASES[upper]
    # IANA already
    try:
        ZoneInfo(t)
        return t
    except ZoneInfoNotFoundError:
        # try case-normalized common form
        try:
            # America/Chicago style — preserve slash casing
            ZoneInfo(t)
            return t
        except Exception:
            return None


def parse_date_parts(text: str, now: datetime) -> tuple[int, int, int]:
    """Return (year, month, day); default to today (in caller tz) if no date."""
    m = DATE_RE.search(text)
    if not m:
        return now.year, now.month, now.day

    if m.group("iso"):
        y, mo, d = m.group("iso").split("-")
        return int(y), int(mo), int(d)

    if m.group("mon_name"):
        mo = MONTHS[m.group("mon_name").lower()]
        d = int(m.group("day1"))
        y = int(m.group("year1")) if m.group("year1") else now.year
        return y, mo, d

    if m.group("md"):
        parts = m.group("md").split("/")
        mo, d = int(parts[0]), int(parts[1])
        if len(parts) == 3:
            y = int(parts[2])
            if y < 100:
                y += 2000
        else:
            y = now.year
        return y, mo, d

    return now.year, now.month, now.day


def parse_pump_datetime(text: str) -> tuple[Optional[str], Optional[str]]:
    """
    Heuristic: find timezone + a clock time (+ optional date).
    Returns (pump_at_iso, time_zone_iana) or (None, tz) / (None, None).
    """
    tz_m = TZ_TOKEN_RE.search(text)
    tz_name = resolve_tz_name(tz_m.group(0) if tz_m else None)

    # Prefer America/Chicago if no tz mentioned (BTCP Drafts default)
    effective_tz = tz_name or "America/Chicago"
    try:
        zi = ZoneInfo(effective_tz)
    except ZoneInfoNotFoundError:
        zi = ZoneInfo("America/Chicago")
        effective_tz = "America/Chicago"

    now_local = datetime.now(zi)
    year, month, day = parse_date_parts(text, now_local)

    # Find a plausible time near pump keywords if possible
    candidates = list(TIME_RE.finditer(text))
    if not candidates:
        return None, tz_name

    # Prefer times near "pump" / "at" / "launch"
    chosen = candidates[0]
    lower = text.lower()
    for m in candidates:
        window = lower[max(0, m.start() - 24) : m.end() + 24]
        if any(k in window for k in ("pump", "launch", "call", " at ", "entry")):
            chosen = m
            break

    h = int(chosen.group("h"))
    minute = int(chosen.group("m") or 0)
    ampm = chosen.group("ampm")
    if ampm:
        ampm = ampm.upper()
        if ampm == "PM" and h < 12:
            h += 12
        elif ampm == "AM" and h == 12:
            h = 0
    elif h <= 12 and not chosen.group("m"):
        # bare "3" / "3pm" already handled; bare hour without ampm is ambiguous — keep as-is
        pass

    try:
        local_dt = datetime(year, month, day, h, minute, 0, tzinfo=zi)
    except ValueError:
        return None, tz_name

    # If time already passed today and no explicit date, roll to next day
    if not DATE_RE.search(text) and local_dt < now_local:
        from datetime import timedelta
        local_dt = local_dt + timedelta(days=1)

    return local_dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"), effective_tz


def looks_like_mint(token: str, text: str) -> bool:
    low = token.lower()
    if any(low.startswith(p) for p in MINT_DENY_PREFIXES):
        return False
    # Solana pubkeys are base58; reject if mostly digits only and short-ish noise
    if token.isdigit():
        return False
    # Prefer tokens near "ca", "mint", "contract", or solscan/dexscreener urls
    idx = text.find(token)
    window = text[max(0, idx - 40) : idx + len(token) + 40].lower()
    if any(k in window for k in ("ca", "mint", "contract", "solscan", "dexscreener", "birdeye", "pump.fun")):
        return True
    # Otherwise accept longest base58 blob (common CA paste)
    return len(token) >= 40


def mint_candidates(text: str, urls: Optional[list[str]] = None) -> list[tuple[int, int, str]]:
    """(score, len, mint) candidates from text + link URLs (entities/buttons/previews)."""
    blob = text + ("\n" + "\n".join(urls) if urls else "")
    out: dict[str, int] = {}
    for rx, score in LINK_MINT_RES:
        for m in rx.finditer(blob):
            out[m.group(1)] = max(out.get(m.group(1), 0), score)
    for h in MINT_RE.findall(text):
        if looks_like_mint(h, text):
            out[h] = max(out.get(h, 0), 3)
    res = []
    for mint, score in out.items():
        if mint.isdigit():
            continue
        if mint.endswith("pump"):
            score += 1
        res.append((score, len(mint), mint))
    res.sort(reverse=True)
    return res


def parse_mint(text: str, urls: Optional[list[str]] = None) -> Optional[str]:
    c = mint_candidates(text, urls)
    return c[0][2] if c else None


def _ok_ticker(sym: Optional[str], mint: Optional[str]) -> bool:
    if not sym or len(sym) < 2:
        return False
    if sym.upper() in TICKER_DENY:
        return False
    if mint and sym == mint[: len(sym)]:
        return False
    return True


def parse_symbol(text: str, mint: Optional[str]) -> Optional[str]:
    """Prefer: 'calling $X' → $X nearest the CA → 'ticker: X' → first plausible $X. Never word fragments."""
    for m in CALLING_TICKER_RE.finditer(text):
        if _ok_ticker(m.group(1), mint):
            return m.group(1).upper()
    if mint and mint in text:
        idx = text.find(mint)
        best = None
        for m in DOLLAR_TICKER_RE.finditer(text):
            if not _ok_ticker(m.group(1), mint):
                continue
            dist = idx - m.end() if m.end() <= idx else m.start() - (idx + len(mint))
            if dist <= 160 and (best is None or dist < best[0]):
                best = (dist, m.group(1))
        if best:
            return best[1].upper()
    m = LABEL_TICKER_RE.search(text)
    if m and _ok_ticker(m.group(1), mint):
        return m.group(1).upper()
    for m in DOLLAR_TICKER_RE.finditer(text):
        if _ok_ticker(m.group(1), mint):
            return m.group(1).upper()
    return None


def parse_message(text: str, urls: Optional[list[str]] = None) -> dict[str, Any]:
    pump_at_iso, time_zone = parse_pump_datetime(text)
    mint = parse_mint(text, urls)
    symbol = parse_symbol(text, mint)
    sell_now = bool(SELL_NOW_RE.search(text))
    raw_urls = list(dict.fromkeys(URL_RE.findall(text) + list(urls or [])))
    return {
        "pump_at_iso": pump_at_iso,
        "time_zone": time_zone,
        "mint": mint,
        "symbol": symbol,
        "sell_now": sell_now,
        "raw_urls": raw_urls,
    }


def message_links(message: Any) -> list[str]:
    """Hidden URLs Telethon doesn't put in .message: text-link entities, URL buttons, web-page previews."""
    urls: list[str] = []
    for ent in (getattr(message, "entities", None) or []):
        u = getattr(ent, "url", None)
        if isinstance(u, str) and u:
            urls.append(u)
    markup = getattr(message, "reply_markup", None)
    for row in (getattr(markup, "rows", None) or []):
        for btn in (getattr(row, "buttons", None) or []):
            u = getattr(btn, "url", None)
            if isinstance(u, str) and u:
                urls.append(u)
    wp = getattr(getattr(message, "media", None), "webpage", None)
    for attr in ("url", "display_url"):
        u = getattr(wp, attr, None)
        if isinstance(u, str) and u:
            urls.append(u)
    return list(dict.fromkeys(urls))


def _msg_date_ms(message: Any) -> Optional[int]:
    """Telegram server message.date → ms epoch (None if unavailable)."""
    try:
        d = getattr(message, "date", None)
        if d is None:
            return None
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return int(d.timestamp() * 1000)
    except Exception:  # noqa: BLE001
        return None


def write_event(hook_path: Path, event: dict[str, Any]) -> None:
    hook_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = hook_path.with_suffix(hook_path.suffix + ".tmp")
    tmp.write_text(json.dumps(event, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(hook_path)


def title_matches(dialog_title: str, needle: str) -> bool:
    return needle.casefold() in (dialog_title or "").casefold()


async def resolve_target_entities(
    client: TelegramClient, needles: list[str]
) -> tuple[list[Any], list[str]]:
    """
    Return (matched_entities, missing_needles).
    Soft-fails: lists similar titles when a needle is missing.
    """
    dialogs: list[Dialog] = []
    async for d in client.iter_dialogs():
        dialogs.append(d)

    matched: list[Any] = []
    matched_titles: list[str] = []
    missing: list[str] = []

    for needle in needles:
        needle = needle.strip()
        if not needle:
            continue
        found = None
        # 1) dialog title substring match
        for d in dialogs:
            title = d.name or ""
            uname = (getattr(d.entity, "username", None) or "")
            if title_matches(title, needle) or (uname and needle.casefold().lstrip("@") == uname.casefold()):
                found = d.entity
                matched.append(found)
                matched_titles.append(title or uname)
                print(f"  ✓ matched '{needle}' → '{title or uname}' (id={getattr(found, 'id', '?')} username={uname or '-'})")
                break
        # 2) direct entity resolve (@username / invite-less public channel)
        if found is None:
            for cand in (needle, needle.lstrip("@"), f"@{needle.lstrip('@')}"):
                try:
                    ent = await client.get_entity(cand)
                    found = ent
                    title = getattr(ent, "title", None) or getattr(ent, "username", None) or str(getattr(ent, "id", "?"))
                    matched.append(found)
                    matched_titles.append(str(title))
                    print(f"  ✓ resolved '{needle}' via get_entity('{cand}') → '{title}' (id={getattr(ent, 'id', '?')})")
                    break
                except Exception:
                    continue
        if found is None:
            missing.append(needle)
            print(f"  ✗ group not found for '{needle}'")
            similar = [
                (d.name or "")
                for d in dialogs
                if isinstance(d.entity, (Channel, Chat))
                and any(tok and tok.casefold() in (d.name or "").casefold() for tok in needle.split())
            ][:12]
            if not similar:
                # fallback: show a few group titles containing any word
                similar = [
                    (d.name or "")
                    for d in dialogs
                    if d.is_group or d.is_channel
                ][:15]
            if similar:
                print("    similar titles:")
                for s in similar:
                    print(f"      - {s}")

    return matched, missing


def load_config() -> dict[str, Any]:
    load_dotenv()
    api_id = os.getenv("TELEGRAM_API_ID", "").strip()
    api_hash = os.getenv("TELEGRAM_API_HASH", "").strip()
    session = os.getenv("SESSION_NAME", "fomo_pump").strip() or "fomo_pump"
    groups_raw = os.getenv("TARGET_GROUPS", ",".join(DEFAULT_GROUPS))
    groups = [g.strip() for g in groups_raw.split(",") if g.strip()]
    hook = expand_path(os.getenv("BTCP_PUMP_HOOK", str(DEFAULT_HOOK)))

    if not api_id or not api_hash:
        print(
            "Missing TELEGRAM_API_ID / TELEGRAM_API_HASH in .env.\n"
            "Copy .env.example → .env and fill values from https://my.telegram.org",
            file=sys.stderr,
        )
        sys.exit(1)
    try:
        api_id_int = int(api_id)
    except ValueError:
        print("TELEGRAM_API_ID must be an integer.", file=sys.stderr)
        sys.exit(1)

    no_alert = [g.strip().casefold() for g in os.getenv("NO_ALERT_GROUPS", DEFAULT_NO_ALERT).split(",") if g.strip()]
    auto_join = [g.strip().lstrip("@") for g in os.getenv("AUTO_JOIN_PUBLIC", "SmugCalls").split(",")
                 if g.strip() and g.strip().lstrip("@").casefold() in AUTO_JOIN_ALLOWLIST]
    scout_alerts = os.getenv("SCOUT_ALERTS_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
    coach_url = os.getenv("COACH_INGEST_URL", "http://127.0.0.1:8790").rstrip("/")
    coach_enabled = os.getenv("COACH_INGEST_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
    return {
        "api_id": api_id_int,
        "api_hash": api_hash,
        "session": session,
        "groups": groups,
        "hook": hook,
        "coach_url": coach_url,
        "coach_enabled": coach_enabled,
        "no_alert": no_alert,
        "auto_join": auto_join,
        "scout_alerts": scout_alerts,
        "momentum_mentions": os.getenv("MOMENTUM_MENTIONS_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"},
        "momentum_tg_global": os.getenv("MOMENTUM_TG_GLOBAL_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"},
    }


# ---------------- v1.22 momentum mention counter (coach momentum_paper; PAPER ONLY) ----------------
MOM_TICKER_RE = re.compile(r"(?<![A-Za-z0-9])\$([A-Za-z][A-Za-z0-9]{1,15})(?![A-Za-z0-9])")
MOM_TICKER_DENY = {"SOL", "USD", "USDC", "USDT", "BTC", "ETH", "USD1", "K", "M", "B"}


def mention_items(text: str, urls: Optional[list[str]], chat: str, ts_ms: int) -> list[dict[str, Any]]:
    """CA / $TICKER mentions in one message → [{mint?, symbol?, chat, ts_ms}]. Never includes the message text."""
    items: list[dict[str, Any]] = []
    mints = [m for sc, _, m in mint_candidates(text or "", urls) if sc >= 3][:3]
    for m in mints:
        items.append({"mint": m, "symbol": parse_symbol(text or "", m), "chat": chat, "ts_ms": ts_ms})
    if not mints:
        seen: set[str] = set()
        for t in MOM_TICKER_RE.findall(text or ""):
            u = t.upper()
            if u in MOM_TICKER_DENY or u in TICKER_DENY or u in seen:
                continue
            seen.add(u)
            items.append({"symbol": u, "chat": chat, "ts_ms": ts_ms})
            if len(seen) >= 3:
                break
    return items


def post_mentions(base_url: str, items: list[dict[str, Any]], timeout: float = 4.0) -> dict[str, Any]:
    import urllib.request
    req = urllib.request.Request(base_url.rstrip("/") + "/api/paper/momentum/mentions",
                                 data=json.dumps({"mentions": items}).encode(), method="POST",
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 — loopback coach only
        return json.loads(r.read().decode() or "{}")


def alerts_muted(source_title: str, username: Optional[str], no_alert: list[str]) -> bool:
    keys = [(source_title or "").casefold(), (username or "").casefold()]
    return any(n and any(n in k for k in keys if k) for n in no_alert)


async def ensure_joined(client: TelegramClient, names: list[str]) -> None:
    """Join allow-listed PUBLIC channels so Telethon receives live updates. Never posts anything."""
    from telethon.errors import UserNotParticipantError
    from telethon.tl.functions.channels import GetParticipantRequest, JoinChannelRequest
    for name in names:
        if name.casefold() not in AUTO_JOIN_ALLOWLIST:
            continue
        try:
            ent = await client.get_entity(name)
        except Exception as exc:  # noqa: BLE001
            print(f"  ✗ auto-join: cannot resolve @{name}: {type(exc).__name__}")
            continue
        try:
            await client(GetParticipantRequest(ent, "me"))
            print(f"  ✓ already a member of @{name}")
            continue
        except UserNotParticipantError:
            pass
        except Exception as exc:  # noqa: BLE001
            print(f"  ? membership check @{name}: {type(exc).__name__} — trying join")
        try:
            await client(JoinChannelRequest(ent))
            print(f"  ✓ joined public channel @{name} (read-only; nothing posted)")
        except Exception as exc:  # noqa: BLE001
            print(f"  ✗ auto-join @{name} failed: {type(exc).__name__}: {str(exc)[:120]}")


async def amain() -> None:
    cfg = load_config()
    session_path = Path(cfg["session"])
    if not session_path.with_suffix(".session").exists() and not Path(f"{cfg['session']}.session").exists():
        # Telethon stores as SESSION_NAME.session in cwd
        print(
            f"No session file '{cfg['session']}.session' found.\n"
            "Run `python login_once.py` once (interactive) before starting the watcher.",
            file=sys.stderr,
        )
        # Still continue — Telethon may error clearly; we avoid interactive start here.

    print("=== FOMO Telegram PUMP watcher ===")
    print(f"Session: {cfg['session']}")
    print(f"Targets: {cfg['groups']}")
    print(f"Hook JSON: {cfg['hook']}")
    print(f"Coach ingest: {'ON → '+cfg['coach_url'] if cfg.get('coach_enabled') else 'OFF'} (CA announce only; sell/dip skipped)")

    client = TelegramClient(cfg["session"], cfg["api_id"], cfg["api_hash"])
    await client.connect()
    if not await client.is_user_authorized():
        print(
            "Session not authorized. Run login_once.py first (do not use interactive start here).",
            file=sys.stderr,
        )
        await client.disconnect()
        sys.exit(2)

    me = await client.get_me()
    print(f"Connected as {getattr(me, 'username', None) or me.id}")

    if cfg.get("auto_join"):
        print(f"Auto-join (allow-listed public channels): {cfg['auto_join']}")
        await ensure_joined(client, cfg["auto_join"])

    print("Resolving dialogs…")
    entities, missing = await resolve_target_entities(client, cfg["groups"])
    if not entities:
        print(
            "No target groups resolved — watching nothing. "
            "Fix TARGET_GROUPS or join the chats, then restart.",
            file=sys.stderr,
        )
        await client.disconnect()
        sys.exit(3)
    if missing:
        print(f"Warning: continuing without unmatched groups: {missing}")

    hook: Path = cfg["hook"]

    seen = alerts.SeenStore() if alerts else None
    processed: set[tuple[Any, Any, str]] = set()  # (chat_id, msg_id, mint) — edits of the same CA msg are no-ops

    async def handle(event: Any, edited: bool) -> None:
        tg_recv_ms = int(time.time() * 1000)  # local receive time — keep as first line (coach latency)
        msg = event.message
        text = msg.message or ""  # includes media captions
        urls = message_links(msg)
        if not text.strip() and not urls:
            return
        chat = await event.get_chat()
        source = getattr(chat, "title", None) or getattr(chat, "username", None) or str(getattr(chat, "id", "unknown"))
        mid = getattr(msg, "id", None)
        kind = "EDIT" if edited else "NEW"
        parsed = parse_message(text, urls)
        posted_ms = _msg_date_ms(msg)
        payload = {
            "source_group": source,
            "text": text,
            **parsed,
            "received_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "telegram_message_id": mid,
            "telegram_chat_id": getattr(chat, "id", None),
            "edited": edited,
            "link_urls": urls,
        }
        posted = (datetime.fromtimestamp(posted_ms / 1000).astimezone().strftime("%H:%M:%S")
                  if posted_ms else "?")
        muted = alerts_muted(source, getattr(chat, "username", None), cfg.get("no_alert") or [])
        try:
            if not muted:  # v1.13: control feeds (SmugCalls) never overwrite the BTCP/Fluffy hook JSON
                write_event(hook, payload)
            flags = []
            if payload.get("pump_at_iso"):
                flags.append(f"pump_at={payload['pump_at_iso']} ({payload.get('time_zone')})")
            if payload.get("mint"):
                flags.append(f"mint={payload['mint'][:8]}…")
            if payload.get("symbol"):
                flags.append(f"${payload['symbol']}")
            if payload.get("sell_now"):
                flags.append("SELL_NOW")
            if urls:
                flags.append(f"links={len(urls)}")
            print(f"[{source}] msg={mid} {kind} posted={posted} "
                  + ("(control feed: hook not written) " if muted else f"wrote event → {hook.name}  ")
                  + (" | ".join(flags) if flags else "(no fields matched)"))
        except OSError as exc:
            print(f"[{source}] msg={mid} Failed to write hook JSON: {exc}", file=sys.stderr)

        mint = payload.get("mint")
        if not mint or payload.get("sell_now"):
            if payload.get("sell_now") and mint:
                print(f"  → msg={mid} skipped coach ingest (sell/dip — entry rule is CA announce only)")
            return
        chat_id = payload.get("telegram_chat_id")
        pkey = (chat_id, mid, mint)
        if pkey in processed:
            print(f"  → msg={mid} {kind} same CA already handled for this message — no-op")
            return
        processed.add(pkey)
        first, count, _ = seen.note(chat_id, mint, mid, source_name=source) if seen else (True, 1, False)
        # first sighting per (source, mint) → push alert right away (does not wait for the coach)
        if first and alerts and muted:
            print(f"  → msg={mid} first CA from {source} — alert muted (NO_ALERT_GROUPS); coach ingest only")
        elif first and alerts:
            asyncio.get_running_loop().create_task(alerts.first_ca_alert(
                client, mint=mint, symbol=payload.get("symbol"), source=source, message_id=mid, edited=edited,
                log=print))
        elif not first:
            print(f"  → msg={mid} repeat #{count} of {mint[:8]}… from {source} (dedupe: no re-alert; coach counts only)")

        if cfg.get("coach_enabled") and post_ca_announce:
            result = await asyncio.to_thread(
                post_ca_announce,
                mint=mint,
                symbol=payload.get("symbol"),
                source_group=source if "smug" not in (getattr(chat, "username", "") or "").casefold() else "SmugCalls",
                text=text,
                message_id=mid,
                chat_id=chat_id,
                sell_now=False,
                pump_at_iso=payload.get("pump_at_iso"),
                tg_recv_ms=tg_recv_ms,
                tg_message_ms=posted_ms,
                first_seen=bool(first),
                repeat_count=int(count or 1),
            )
            if result.get("ok"):
                dup = " DUPLICATE(no pipeline)" if result.get("duplicate") else ""
                print(f"  → msg={mid} coach ingest ok{dup} cluster={result.get('cluster')} "
                      f"mesh_sched={result.get('mesh_enrichment_scheduled')}")
            elif result.get("skipped"):
                print(f"  → msg={mid} coach skip: {result.get('reason')}")
            else:
                print(f"  → msg={mid} coach ingest soft-fail: {result.get('error') or result}", file=sys.stderr)

    @client.on(events.NewMessage(chats=entities))
    async def on_new_message(event: events.NewMessage.Event) -> None:
        await handle(event, edited=False)

    @client.on(events.MessageEdited(chats=entities))
    async def on_edited_message(event: events.MessageEdited.Event) -> None:
        await handle(event, edited=True)

    # v1.22 momentum mention counter: every GROUP / CHANNEL the account is in (never private chats / Saved Messages).
    # Read-only; only mint / ticker / chat title / timestamp go to the coach (loopback), batched every 5 s.
    if cfg.get("momentum_mentions"):
        mbuf: list[dict[str, Any]] = []
        mstats: dict[str, Any] = {"msgs": 0, "items": 0, "posts": 0, "errors": 0, "chats": set()}

        @client.on(events.NewMessage())
        async def on_any_message(event: events.NewMessage.Event) -> None:
            try:
                if event.is_private:
                    return
                msg = event.message
                text = msg.message or ""
                urls = message_links(msg)
                if not text and not urls:
                    return
                chat = await event.get_chat()
                title = getattr(chat, "title", None) or getattr(chat, "username", None) or str(getattr(chat, "id", "?"))
                items = mention_items(text, urls, title, _msg_date_ms(msg) or int(time.time() * 1000))
                mstats["msgs"] += 1
                if items:
                    mstats["chats"].add(title)
                    mbuf.extend(items)
                    del mbuf[:-2000]
            except Exception:  # noqa: BLE001 — never disturb the main handlers
                mstats["errors"] += 1

        async def mention_flush_loop() -> None:
            last_report = time.time()
            while True:
                await asyncio.sleep(5)
                if mbuf:
                    batch = mbuf[:500]
                    del mbuf[:500]
                    try:
                        await asyncio.to_thread(post_mentions, cfg["coach_url"], batch)
                        mstats["posts"] += 1
                        mstats["items"] += len(batch)
                    except Exception as exc:  # noqa: BLE001
                        mstats["errors"] += 1
                        print(f"[momentum] mention post failed: {type(exc).__name__}")
                if time.time() - last_report > 900:
                    last_report = time.time()
                    print(f"[momentum] last 15 min: {mstats['msgs']} group/channel msgs, {mstats['items']} mentions "
                          f"posted, {len(mstats['chats'])} chats with mentions, errors {mstats['errors']}")
                    mstats.update(msgs=0, items=0, errors=0, chats=set())

        asyncio.get_running_loop().create_task(mention_flush_loop())
        try:
            n_groups = sum(1 for d in await asyncio.wait_for(client.get_dialogs(), 20) if d.is_group or d.is_channel)
        except Exception:  # noqa: BLE001
            n_groups = -1
        print(f"Momentum mention counter: ON (all {n_groups} groups/channels; CA + $TICKER counts → coach every 5 s)")

    # v1.23 Telegram PUBLIC search for coach-requested coins (read-only; strict rate limits; counts only)
    if cfg.get("momentum_tg_global"):
        try:
            import tg_public_search as tgps
            tcfg = tgps.config()
            asyncio.get_running_loop().create_task(tgps.search_loop(client, cfg["coach_url"], log=print, cfg=tcfg))
            print(f"Telegram public search: ON (≥{tcfg['min_interval_s']:.0f}s apart, ≤{tcfg['max_per_hour']}/h, "
                  f"≤{tcfg['max_per_day']}/day; FloodWait > {tcfg['disable_flood_s']:.0f}s → off until midnight; "
                  f"never joins / sends / marks read)")
        except Exception as exc:  # noqa: BLE001
            print(f"Telegram public search: failed to start ({type(exc).__name__}) — mention counter unaffected")

    # one-shot TEST alert (Saved Messages + macOS only; never touches the coach ingest / live lane)
    flag = Path(__file__).with_name("send_test_alert.flag")
    if alerts and flag.exists():
        test_mint = (flag.read_text().strip() or "DwcXyhEcSvzWgakDKpetZLFvbAHutpGpzhbU4iempump").split()[0]
        flag.unlink(missing_ok=True)
        print(f"TEST alert requested via {flag.name} → Saved Messages + macOS (no ingest) mint={test_mint[:8]}…")
        asyncio.get_running_loop().create_task(alerts.first_ca_alert(
            client, mint=test_mint, symbol="TEST", source="TEST (watcher self-check)", message_id="test",
            test=True, log=print))

    # v1.13: scout arm B paper entries → push alert (Saved Messages + macOS only; polled from the coach, read-only)
    async def scout_alert_loop() -> None:
        since = int(time.time() * 1000)
        while True:
            await asyncio.sleep(3)
            try:
                new, since = await asyncio.to_thread(alerts.poll_scout_alerts, since)
                for a in new:
                    print(f"[scout B] {a.get('scout')} + {a.get('confirmer')} mint={str(a.get('mint'))[:8]}… → alert")
                    await alerts.scout_alert(client, a, log=print)
            except Exception as exc:  # noqa: BLE001
                print(f"[scout B] poll error: {type(exc).__name__}: {str(exc)[:120]}")

    if alerts and cfg.get("scout_alerts") and hasattr(alerts, "poll_scout_alerts"):
        asyncio.get_running_loop().create_task(scout_alert_loop())
        print("Scout arm-B alert poller: ON (coach /api/scout/alerts every 3 s)")

    # v1.14: LIVE buy/sell alerts (Saved Messages + macOS; token read from the coach's 0600 file, never printed)
    async def live_alert_loop() -> None:
        since = int(time.time() * 1000) - 60_000      # catch alerts raised just before a watcher restart
        while True:
            await asyncio.sleep(2)
            try:
                new, since = await asyncio.to_thread(alerts.poll_live_alerts, since)
                for a in new:
                    print(f"[live] {a.get('kind')} {a.get('arm') or ''} mint={str(a.get('mint') or '')[:8]}… → alert")
                    await alerts.live_alert(client, a, log=print)
            except Exception as exc:  # noqa: BLE001
                print(f"[live] poll error: {type(exc).__name__}: {str(exc)[:120]}")

    if alerts and os.getenv("LIVE_ALERTS_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"} \
            and hasattr(alerts, "poll_live_alerts"):
        asyncio.get_running_loop().create_task(live_alert_loop())
        print("Live trade alert poller: ON (coach /api/live/alerts every 2 s)")

    print("Listening for NewMessage + MessageEdited… (Ctrl+C to stop)")
    await client.run_until_disconnected()


def main() -> None:
    try:
        import asyncio
        asyncio.run(amain())
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
