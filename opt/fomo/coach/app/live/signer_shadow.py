"""v1.24 signer shadow (VPS, before cutover). SIGNER_SHADOW_ENABLED=true + SIGNER_BACKEND=socket: when the live engine
refuses a live-arm buy (the backend runs with the kill switch on), build the REAL Jupiter /order for our wallet at that
arm's size and ask fomo-signer for a DRY-RUN verdict (static policy + ALT resolve + simulation balance diff). Every few
minutes do the same for a sell of a token the wallet already holds. Nothing is signed (dry_run, and the signer itself
runs SIGNER_MODE=shadow), nothing is sent. → data/signer_shadow.jsonl and GET /api/live/signer_shadow."""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import statistics
import time
from typing import Any, Dict, List, Optional

from app.paths import data_dir

logger = logging.getLogger("coach.live.signer_shadow")
WSOL = "So11111111111111111111111111111111111111112"
_state: Dict[str, Any] = {"last_buy": 0.0, "last_sell": 0.0, "busy": False}
_stats: Dict[str, int] = collections.defaultdict(int)


def enabled() -> bool:
    from app.live import signer_client
    return signer_client.enabled() and os.getenv("SIGNER_SHADOW_ENABLED", "false").strip().lower() in ("1", "true", "yes")


def _path():
    return data_dir() / "signer_shadow.jsonl"


def log(rec: Dict[str, Any]) -> None:
    try:
        with open(_path(), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, separators=(",", ":"), default=str) + "\n")
    except OSError:
        pass


async def probe_direct(side: str, mint: str, amount: int, *, arm: str = "", source: str = "") -> Optional[Dict[str, Any]]:
    """Same dry-run for the pump.fun direct path (bonding-curve coins only; ineligible → None, nothing logged)."""
    import httpx
    from solders.message import to_bytes_versioned  # type: ignore
    from app.live import pumpdirect, signer_client
    c = pumpdirect.cfg()
    async with httpx.AsyncClient(timeout=15.0) as cl:
        p = await pumpdirect.prepare(side, mint, amount, cl, slippage_pct=c["slippage_pct"])
    if not p.get("ok"):
        return None
    tx = pumpdirect.compile_tx(p["ixs"], p["user"], p["state"]["blockhash"], sign=False)
    rec: Dict[str, Any] = {"ts_ms": int(time.time() * 1000), "side": side, "mint": mint, "arm": arm,
                           "source": source, "amount": int(amount), "router": "pump_direct"}
    t1 = time.perf_counter()
    try:
        v = await signer_client.request(to_bytes_versioned(tx.message), pumpdirect.direct_intent(p, side, mint, amount),
                                        dry_run=True)
        rec.update(stage="signer", ok=bool(v.get("ok")), signed=bool(v.get("signed")),
                   reasons=(v.get("reasons") or [])[:4], programs=v.get("programs"), effects=v.get("effects"),
                   signer_ms=v.get("ms"), rtt_ms=round((time.perf_counter() - t1) * 1000, 1))
    except Exception as exc:  # noqa: BLE001
        rec.update(stage="signer", ok=None, error=f"signer {type(exc).__name__}")
    _stats[f"direct_{side}_{'pass' if rec.get('ok') else 'refuse' if rec.get('ok') is False else 'error'}"] += 1
    log(rec)
    return rec


async def probe(side: str, mint: str, amount: int, *, arm: str = "", source: str = "") -> Dict[str, Any]:
    from solders.message import to_bytes_versioned  # type: ignore
    from solders.transaction import VersionedTransaction  # type: ignore
    import base64
    from app.live import jupiter, signer_client
    from app.live.config import config
    c = config()
    prio = int(c.get("max_priority_lamports") or 2_000_000)
    slip = int(os.getenv("LIVE_BUY_SLIPPAGE_BPS" if side == "buy" else "LIVE_SELL_SLIPPAGE_BPS", "1500"))
    in_m, out_m = (WSOL, mint) if side == "buy" else (mint, WSOL)
    rec: Dict[str, Any] = {"ts_ms": int(time.time() * 1000), "side": side, "mint": mint, "arm": arm,
                           "source": source, "amount": int(amount)}
    t0 = time.perf_counter()
    o = await jupiter.order(input_mint=in_m, output_mint=out_m, amount=amount, slippage_bps=slip,
                            max_priority_lamports=prio)
    rec.update(order_ms=round((time.perf_counter() - t0) * 1000, 1), router=o.get("router"))
    if not o.get("ok"):
        rec.update(stage="order", error=str(o.get("error"))[:120])
    else:
        msg = VersionedTransaction.from_bytes(base64.b64decode(o["transaction"])).message
        t1 = time.perf_counter()
        try:
            v = await signer_client.request(to_bytes_versioned(msg), jupiter.swap_intent(
                in_m, out_m, amount, prio, o.get("router"), f"shadow:{source}"), dry_run=True)
            rec.update(stage="signer", ok=bool(v.get("ok")), signed=bool(v.get("signed")),
                       reasons=(v.get("reasons") or [])[:4], programs=v.get("programs"),
                       external_fee_payer=v.get("external_fee_payer"), effects=v.get("effects"),
                       signer_ms=v.get("ms"), rtt_ms=round((time.perf_counter() - t1) * 1000, 1))
        except Exception as exc:  # noqa: BLE001
            rec.update(stage="signer", ok=None, error=f"signer {type(exc).__name__}")
    _stats[f"{side}_{'pass' if rec.get('ok') else 'refuse' if rec.get('ok') is False else 'error'}"] += 1
    log(rec)
    return rec


def on_refused(arm: str, mint: str) -> None:
    """Engine hook (fire-and-forget, throttled). Never raises into the engine."""
    if not enabled() or _state["busy"]:
        return
    gap = float(os.getenv("SIGNER_SHADOW_MIN_INTERVAL_S", "60"))
    if time.time() - _state["last_buy"] < gap:
        return
    from app.live.config import arm_size_sol, config
    _state["last_buy"] = time.time()           # any refused arm is a real coin → coverage; size floor 0.20 SOL

    async def go() -> None:
        _state["busy"] = True
        try:
            lam = int(max(arm_size_sol(arm), 0.20) * 1e9)
            await probe("buy", mint, lam, arm=arm, source="refused_signal")
            await probe_direct("buy", mint, lam, arm=arm, source="refused_signal")
        except Exception as exc:  # noqa: BLE001
            logger.info("signer shadow probe failed: %s", type(exc).__name__)
        finally:
            _state["busy"] = False
    try:
        asyncio.get_running_loop().create_task(go())
    except RuntimeError:
        pass


async def held_tokens() -> List[Dict[str, Any]]:
    from app.live import jupiter
    from app.pump import rpc as _rpc
    w = jupiter.pubkey()
    out: List[Dict[str, Any]] = []
    for prog in ("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"):
        res = await _rpc.call("getTokenAccountsByOwner", [w, {"programId": prog}, {"encoding": "jsonParsed"}])
        for a in (res or {}).get("value") or []:
            info = (((a.get("account") or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
            amt = int((info.get("tokenAmount") or {}).get("amount") or 0)
            if amt > 0 and info.get("mint") != WSOL:
                out.append({"mint": info.get("mint"), "amount": amt})
    return out


async def sell_loop() -> None:
    """Every SIGNER_SHADOW_SELL_EVERY_S, dry-run a sell of a token the wallet holds (rotating)."""
    every = float(os.getenv("SIGNER_SHADOW_SELL_EVERY_S", "600"))
    i = 0
    await asyncio.sleep(30)
    while True:
        if enabled():
            try:
                toks = await held_tokens()
                if toks:
                    t = toks[i % len(toks)]
                    i += 1
                    await probe("sell", t["mint"], t["amount"], source="held_token")
            except Exception as exc:  # noqa: BLE001
                logger.info("signer shadow sell probe failed: %s", type(exc).__name__)
        await asyncio.sleep(every)


def summary(recent: int = 10) -> Dict[str, Any]:
    rows: List[Dict[str, Any]] = []
    try:
        with open(_path(), encoding="utf-8") as fh:
            for ln in fh:
                try:
                    rows.append(json.loads(ln))
                except ValueError:
                    pass
    except OSError:
        pass
    sg = [r for r in rows if r.get("stage") == "signer"]
    reasons = collections.Counter(x.split(":")[0][:70] for r in sg if r.get("ok") is False for x in r.get("reasons") or [])
    progs = collections.Counter(p for r in sg for p in r.get("programs") or [])
    ms = [r["signer_ms"] for r in sg if isinstance(r.get("signer_ms"), (int, float))]
    return {"enabled": enabled(), "probes": len(rows), "signer_verdicts": len(sg),
            "pass": sum(1 for r in sg if r.get("ok")), "refused": sum(1 for r in sg if r.get("ok") is False),
            "errors": sum(1 for r in sg if r.get("ok") is None), "signed_any": any(r.get("signed") for r in sg),
            "by_side": {s: {"pass": sum(1 for r in sg if r["side"] == s and r.get("ok")),
                            "refused": sum(1 for r in sg if r["side"] == s and r.get("ok") is False)} for s in ("buy", "sell")},
            "by_router": dict(collections.Counter(f"{r.get('router')}:{'pass' if r.get('ok') else 'refuse'}" for r in sg)),
            "refusal_reasons": dict(reasons.most_common(8)), "programs_seen": dict(progs.most_common(15)),
            "signer_ms_median": round(statistics.median(ms), 1) if ms else None,
            "signer_ms_p90": round(sorted(ms)[int(len(ms) * 0.9)], 1) if ms else None,
            "recent": [{k: r.get(k) for k in ("ts_ms", "side", "mint", "router", "ok", "reasons", "signer_ms", "error")}
                       for r in rows[-recent:]], "session_stats": dict(_stats)}
