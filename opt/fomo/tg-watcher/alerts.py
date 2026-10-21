"""First-CA push alerts (v1.12.1): macOS notification + a note to the account's OWN Saved Messages.

Safety: the Telegram destination is hard-coded to "me" (Saved Messages). There is deliberately no
parameter to choose another peer. Read-only coach calls only (GET curve / latency / signals) — this
module never ingests, never trades.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any, Optional

SAVED_MESSAGES = "me"  # never change: alerts go to Andrew's own Saved Messages only
SEEN_PATH = Path(os.path.expanduser(os.getenv(
    "WATCHER_SEEN_PATH", "~/Library/Application Support/FOMO-Signal-Coach/watcher-seen-mints.json")))


def _coach() -> str:
    return os.getenv("COACH_INGEST_URL", "http://127.0.0.1:8790").rstrip("/")


# ---------------- dedupe store (per source chat + mint), persisted across restarts ----------------
class SeenStore:
    def __init__(self, path: Path = SEEN_PATH) -> None:
        self.path = path
        self.data: dict[str, Any] = {}
        try:
            if path.exists():
                self.data = json.loads(path.read_text() or "{}")
        except Exception:  # noqa: BLE001
            self.data = {}

    @staticmethod
    def key(source_key: Any, mint: str) -> str:
        return f"{source_key}|{mint}"

    def note(self, source_key: Any, mint: str, message_id: Any, *, source_name: str = "") -> tuple[bool, int, bool]:
        """Record a sighting. Returns (first_seen, count, msg_already_seen_for_this_mint)."""
        k = self.key(source_key, mint)
        rec = self.data.get(k)
        first = rec is None
        if first:
            rec = {"source": source_name, "first_ms": int(time.time() * 1000), "count": 0, "message_ids": []}
        msg_seen = message_id in rec["message_ids"]
        rec["count"] += 1
        if not msg_seen and message_id is not None:
            rec["message_ids"] = (rec["message_ids"] + [message_id])[-50:]
        rec["last_ms"] = int(time.time() * 1000)
        self.data[k] = rec
        self._save()
        return first, rec["count"], msg_seen

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=1))
            tmp.replace(self.path)
        except Exception:  # noqa: BLE001
            pass


# ---------------- macOS notification ----------------
def mac_notify(title: str, subtitle: str, message: str, sound: str = "Glass") -> bool:
    """osascript display notification; strings passed as argv (no AppleScript string interpolation)."""
    script = ['-e', 'on run argv',
              '-e', f'display notification (item 3 of argv) with title (item 1 of argv) '
                    f'subtitle (item 2 of argv) sound name "{sound}"',
              '-e', 'end run']
    try:
        r = subprocess.run(["/usr/bin/osascript", *script, title[:120], subtitle[:120], message[:240]],
                           capture_output=True, timeout=6)
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False


# ---------------- read-only coach / public snapshots ----------------
def _get_json(url: str, timeout: float) -> Optional[dict]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "fomo-telegram-pump/alerts"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return None


def curve_snapshot(mint: str, timeout: float = 4.0) -> dict:
    d = _get_json(f"{_coach()}/api/pump/curve/{mint}", timeout) or {}
    c = d.get("curve") or {}
    g = d.get("fast_gate") or {}
    return {"status": c.get("status"), "phase": c.get("phase"), "progress_pct": c.get("progress_pct"),
            "mcap_sol": c.get("mcap_sol"), "price_sol": c.get("price_sol"), "migrated": bool(c.get("complete")),
            "pool": (c.get("pumpswap") or {}).get("pool"), "creator": c.get("creator"), "lane": g.get("lane")}


def dex_snapshot(mint: str, timeout: float = 4.0) -> dict:
    d = _get_json(f"https://api.dexscreener.com/latest/dex/tokens/{mint}", timeout) or {}
    pairs = d.get("pairs") or []
    if not pairs:
        return {}
    p = max(pairs, key=lambda x: (x.get("liquidity") or {}).get("usd") or 0)
    return {"dex": p.get("dexId"), "mcap_usd": p.get("marketCap"), "price_usd": p.get("priceUsd"),
            "chg": p.get("priceChange") or {}, "liq_usd": (p.get("liquidity") or {}).get("usd"),
            "symbol": (p.get("baseToken") or {}).get("symbol")}


def coach_verdict(mint: str, wait_s: float = 25.0) -> dict:
    """Wait (bounded) for the coach fast lane to finish, then read the gate. Read-only."""
    t0 = time.time()
    lat = None
    while time.time() - t0 < wait_s:
        lat = (_get_json(f"{_coach()}/api/latency/{mint}", 3.0) or {}).get("timeline")
        if lat and lat.get("status") in ("done", "error"):
            break
        time.sleep(1.5)
    sig = (_get_json(f"{_coach()}/api/signals/{mint}", 5.0) or {}).get("signal") or {}
    gate = sig.get("gate") or {}
    paper = sig.get("paper") or {}
    return {"pipeline": (lat or {}).get("status", "pending"), "action": gate.get("action"),
            "decided_by": gate.get("decided_by"), "lane": gate.get("lane"), "jev": gate.get("jev_action"),
            "mesh": gate.get("mesh_status"), "blocked": gate.get("blocked"),
            "paper": (f"{paper.get('status')} @ {paper.get('entry_price_sol'):.3e} SOL"
                      if paper.get("entry_price_sol") else "none")}


def _fmt_num(x: Any, nd: int = 0) -> str:
    try:
        return f"{float(x):,.{nd}f}"
    except (TypeError, ValueError):
        return "?"


def format_alert(*, mint: str, symbol: Optional[str], source: str, message_id: Any, curve: dict,
                 verdict: Optional[dict] = None, dex: Optional[dict] = None, test: bool = False,
                 edited: bool = False) -> str:
    head = "🧪 TEST — not a real signal (alert pipeline check)\n" if test else ""
    lines = [f"{head}🚨 FIRST CA — {source}{' (edited msg)' if edited else ''}",
             f"${symbol or '?'}  msg #{message_id}",
             f"CA: {mint}", ""]
    if curve.get("status"):
        mig = "MIGRATED (PumpSwap)" if curve.get("migrated") else f"on curve {_fmt_num(curve.get('progress_pct'), 1)}%"
        lines.append(f"Curve: {curve.get('status')} · {mig} · mcap {_fmt_num(curve.get('mcap_sol'))} SOL")
    else:
        lines.append("Curve: unavailable")
    if dex:
        ch = dex.get("chg") or {}
        lines.append(f"Dex: {dex.get('dex')} mcap ${_fmt_num(dex.get('mcap_usd'))} · liq ${_fmt_num(dex.get('liq_usd'))}"
                     f" · m5 {ch.get('m5', '?')}% h1 {ch.get('h1', '?')}%")
    if verdict is None:
        lines.append("Coach: running…")
    else:
        lines.append(f"Coach: {verdict.get('action') or '?'} (by {verdict.get('decided_by') or '?'}, lane "
                     f"{verdict.get('lane') or curve.get('lane') or '?'}, jev {verdict.get('jev') or '?'}, mesh "
                     f"{verdict.get('mesh') or '?'}) · paper {verdict.get('paper')} · pipeline {verdict.get('pipeline')}")
    lines += ["Recommend-only · JUPITER_EXEC off", "",
              f"https://pump.fun/coin/{mint}", f"https://dexscreener.com/solana/{mint}"]
    return "\n".join(lines)


async def first_ca_alert(client: Any, *, mint: str, symbol: Optional[str], source: str, message_id: Any,
                         test: bool = False, edited: bool = False, log=print) -> None:
    """Fire immediately (mac + Saved Messages), then edit the Saved Message with coach verdict + Dex."""
    label = "TEST " if test else ""
    ok = await asyncio.to_thread(mac_notify, f"{label}FIRST CA — {source}"[:120], f"${symbol or '?'}  {mint[:6]}…{mint[-4:]}",
                                 ("TEST alert — ignore. " if test else "") + f"CA {mint}")
    log(f"  → alert mac_notify ok={ok} msg={message_id}")
    curve = await asyncio.to_thread(curve_snapshot, mint)
    msg = None
    try:
        msg = await client.send_message(SAVED_MESSAGES, format_alert(
            mint=mint, symbol=symbol, source=source, message_id=message_id, curve=curve, test=test, edited=edited),
            link_preview=False)
        log(f"  → alert Saved Messages sent id={getattr(msg, 'id', '?')} msg={message_id}")
    except Exception as exc:  # noqa: BLE001
        log(f"  → alert Saved Messages FAILED: {type(exc).__name__}: {str(exc)[:160]} msg={message_id}")
    verdict = await asyncio.to_thread(coach_verdict, mint, 8.0 if test else 25.0)
    dex = await asyncio.to_thread(dex_snapshot, mint)
    if msg is not None:
        try:
            await client.edit_message(SAVED_MESSAGES, msg, format_alert(
                mint=mint, symbol=symbol, source=source, message_id=message_id, curve=curve,
                verdict=verdict, dex=dex, test=test, edited=edited), link_preview=False)
            log(f"  → alert updated with verdict action={verdict.get('action')} msg={message_id}")
        except Exception as exc:  # noqa: BLE001
            log(f"  → alert edit failed: {type(exc).__name__}: {str(exc)[:120]}")


# ---------------- v1.13 scout arm B entry alert (paper signal; Saved Messages + macOS only) ----------------
def format_scout_alert(a: dict) -> str:
    mint = a.get("mint") or "?"
    gap = a.get("edge_gap_pct")
    mcap = a.get("mcap_usd")
    return "\n".join([
        "🛰 SCOUT B (paper) — scout + confirmer bought on the curve",
        f"{a.get('scout') or '?'} + {a.get('confirmer') or '?'}",
        f"CA: {mint}",
        f"Scout mcap ${_fmt_num(mcap)} · our +2s fill vs scout {gap:+.1f}%" if gap is not None
        else f"Scout mcap ${_fmt_num(mcap)}",
        "Paper test only — recommend-only · JUPITER_EXEC off",
        "", f"https://pump.fun/coin/{mint}", f"https://dexscreener.com/solana/{mint}"])


async def scout_alert(client: Any, a: dict, log=print) -> None:
    mint = a.get("mint") or "?"
    ok = await asyncio.to_thread(mac_notify, "SCOUT B (paper)", f"{a.get('scout')} + {a.get('confirmer')}",
                                 f"CA {mint}")
    log(f"  → scout alert mac_notify ok={ok} id={a.get('id')}")
    try:
        msg = await client.send_message(SAVED_MESSAGES, format_scout_alert(a), link_preview=False)
        log(f"  → scout alert Saved Messages sent id={getattr(msg, 'id', '?')}")
    except Exception as exc:  # noqa: BLE001
        log(f"  → scout alert Saved Messages FAILED: {type(exc).__name__}: {str(exc)[:160]}")


def poll_scout_alerts(since_ms: int, timeout: float = 4.0) -> tuple[list, int]:
    d = _get_json(f"{_coach()}/api/scout/alerts?since_ms={int(since_ms)}", timeout) or {}
    alerts_ = sorted(d.get("alerts") or [], key=lambda x: x.get("ts_ms", 0))
    new_since = max([since_ms] + [x.get("ts_ms", 0) for x in alerts_])
    return alerts_, new_since


# ---------------- v1.14 LIVE trade alerts (coach /api/live/alerts, token-protected) ----------------
LIVE_TOKEN_PATH = os.getenv("LIVE_API_TOKEN_PATH", "~/.config/fomo-coach/live-api-token")


def _live_token() -> Optional[str]:
    """Read the coach's local API token from its 0600 file. Never logged or printed."""
    try:
        return Path(os.path.expanduser(LIVE_TOKEN_PATH)).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def poll_live_alerts(since_ms: int, timeout: float = 4.0) -> tuple[list, int]:
    tok = _live_token()
    if not tok:
        return [], since_ms
    try:
        req = urllib.request.Request(f"{_coach()}/api/live/alerts?since_ms={int(since_ms)}",
                                     headers={"User-Agent": "fomo-telegram-pump/alerts", "X-Live-Token": tok})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8", "replace")) or {}
    except Exception:  # noqa: BLE001
        return [], since_ms
    alerts_ = sorted(d.get("alerts") or [], key=lambda x: x.get("ts_ms", 0))
    return alerts_, max([since_ms] + [x.get("ts_ms", 0) for x in alerts_])


_LIVE_TITLES = {"live_buy": "🟢 LIVE BUY", "live_sell": "🔴 LIVE SELL", "live_buy_failed": "⚠️ LIVE BUY FAILED",
                "live_sell_failed": "⚠️ LIVE SELL FAILED (retrying)", "live_kill": "🛑 LIVE KILL SWITCH"}


def format_live_alert(a: dict) -> str:
    kind, mint = a.get("kind") or "live", a.get("mint") or ""
    lines = [f"{_LIVE_TITLES.get(kind, kind)} — {a.get('arm') or ''}".rstrip(" —")]
    if mint:
        lines.append(f"CA: {mint}")
    bits = []
    if a.get("sol") is not None:
        bits.append(f"{a['sol']} SOL")
    if a.get("pnl_sol") is not None:
        bits.append(f"PnL {a['pnl_sol']:+.4f} SOL")
    if a.get("reason"):
        bits.append(f"reason {a['reason']}")
    if a.get("edge_gap_pct") is not None:
        bits.append(f"edge gap {a['edge_gap_pct']:+.1f}%")
    if a.get("error"):
        bits.append(f"error {str(a['error'])[:120]}")
    if kind == "live_kill":
        bits.append("sell all" if a.get("sell_all") else "no new buys")
    if bits:
        lines.append(" · ".join(bits))
    if a.get("signature"):
        lines.append(f"tx {a['signature']}")
    if a.get("solscan"):
        lines.append(a["solscan"])
    if mint:
        lines.append(f"https://dexscreener.com/solana/{mint}")
    return "\n".join(lines)


async def live_alert(client: Any, a: dict, log=print) -> None:
    title = _LIVE_TITLES.get(a.get("kind"), "LIVE")
    sub = f"{a.get('arm') or ''} {a.get('sol') if a.get('sol') is not None else ''} SOL".strip()
    ok = await asyncio.to_thread(mac_notify, title, sub, f"CA {a.get('mint') or '-'} tx {(a.get('signature') or '-')[:16]}…",
                                 "Hero" if a.get("kind") == "live_buy" else "Glass")
    log(f"  → live alert mac_notify ok={ok} id={a.get('id')}")
    try:
        msg = await client.send_message(SAVED_MESSAGES, format_live_alert(a), link_preview=False)
        log(f"  → live alert Saved Messages sent id={getattr(msg, 'id', '?')}")
    except Exception as exc:  # noqa: BLE001
        log(f"  → live alert Saved Messages FAILED: {type(exc).__name__}: {str(exc)[:160]}")
