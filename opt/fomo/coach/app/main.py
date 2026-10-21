from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.config import settings
from app.exec_api import router as exec_router
from app.health import build_health
from app.mesh import mesh_health_summary
from app.ingest.buy import router as buy_router
from app.ingest.fomo_activity import router as fomo_activity_router
from app.scoring import (
    annotate_signals,
    attach_mesh,
    maybe_attach_gate,
    maybe_attach_plan,
    compute_signals,
    get_signal,
    maybe_attach_jev,
    maybe_attach_narrative,
    maybe_notify_telegram,
)
from app.demo_loop import start_demo_loop, stop_demo_loop
from app.state import state

logging.basicConfig(level=logging.INFO)
from app import logredact as _logredact  # noqa: E402  v1.17.1: keep ?api-key= out of logs
_logredact.install()
logger = logging.getLogger("coach")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Start demo sample loop when DEMO_MODE so /ui is not blank after restart."""
    start_demo_loop()
    from app.pump.paper import paper
    from app.pump.trade_stream import streams

    if paper.on_stream not in streams.listeners:
        streams.listeners.append(paper.on_stream)
    # v1.12 low-priority FOMO top-trader watch (own endpoints/queue; yields to the Fluffy CA lane)
    from app.fomo_onchain.keyless import keyless
    from app.fomo_onchain.source import manager as fomo_manager
    try:
        fomo_manager.start()
        keyless.start()
    except Exception as exc:  # noqa: BLE001 — never block startup on the optional source
        logger.warning("fomo-onchain start failed: %s", exc)
    # v1.24 VPS policy signer: verify wallet + dry-run sell probes (only with SIGNER_BACKEND=socket)
    try:
        from app.live import signer_client, signer_shadow
        if signer_client.enabled():
            import asyncio as _aio
            _aio.get_running_loop().create_task(signer_client.verify_wallet())
            if signer_shadow.enabled():
                _aio.get_running_loop().create_task(signer_shadow.sell_loop())
    except Exception as exc:  # noqa: BLE001
        logger.warning("signer shadow start failed: %s", type(exc).__name__)
    # v1.33 speed: keep the CA-lane RPC connection warm
    try:
        import asyncio as _aio2
        from app.pump.curve import warm_loop
        _aio2.get_running_loop().create_task(warm_loop())
    except Exception as exc:  # noqa: BLE001
        logger.warning("rpc warm start failed: %s", type(exc).__name__)
    # v1.13 scout-wallet paper test (own WS/RPC; paper only)
    from app.scout.engine import engine as scout_engine
    from app.scout.source import source as scout_source
    try:
        scout_engine.load()
        if scout_engine.on_trade not in scout_source.listeners:
            scout_source.listeners.append(scout_engine.on_trade)
        scout_source.start()
        scout_engine.ensure_mark_loop()
        scout_engine.start_price_loop()
    except Exception as exc:  # noqa: BLE001
        logger.warning("scout start failed: %s", exc)
    # v1.16: detection audit (daily + on demand), learning-log outcome tracker, persisted latency timelines
    from app.learning.outcomes import tracker as outcome_tracker
    try:
        from app.pump import fast_lane as _fl
        _fl.load_persisted()
        from app.pump.paper import paper as _paper
        _paper.mark_restart_orphans()
        from app.scout.audit import auditor
        auditor.load()
        if scout_source.started_ms:
            auditor.start_loop()
        outcome_tracker.resume()
    except Exception as exc:  # noqa: BLE001
        logger.warning("v1.16 start failed: %s", type(exc).__name__)
    # v1.17: daily wallet scores (advisory only) + local rugger-creator list (rug checks)
    from app.learning.wallet_scores import job as wallet_job
    try:
        wallet_job.start()
        from app.live.rugcheck import build_rugger_list
        r = build_rugger_list()
        logger.info("rugger list: %s creators (%s new)", r.get("creators"), r.get("new"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("v1.17 start failed: %s", type(exc).__name__)
    try:                                    # v1.17.4 recap wallet snapshots (read-only, public RPC, low priority)
        from app.recap_wallet import book as _wallet_book
        _wallet_book.start()
    except Exception as exc:  # noqa: BLE001
        logger.warning("recap wallet snapshots start failed: %s", type(exc).__name__)
    # v1.14 live auto-trade (buys need JUPITER_EXEC_ENABLED + LIVE_AUTOTRADE_ENABLED + LIVE_ARMS)
    from app.live.auth import ensure_token
    from app.live.engine import engine as live_engine
    try:
        ensure_token()                      # creates the 0600 local API token file if missing (never printed)
        live_engine.load()
        if live_engine.on_stream not in streams.listeners:
            streams.listeners.append(live_engine.on_stream)
        live_engine.ensure_loop()           # resume exits for persisted open live positions
        live_engine.start_reconcile()       # v1.15: pending buys / untracked bought mints vs on-chain balances
    except Exception as exc:  # noqa: BLE001
        logger.warning("live engine start failed: %s", type(exc).__name__)
    try:                                    # v1.19 fomo_top_paper (PAPER ONLY): window start + resume open paper marks
        from app.paper_fomo_top import cfg as _ftp_cfg, paper as _ftp
        if _ftp_cfg()["enabled"]:
            _ftp.load()
    except Exception as exc:  # noqa: BLE001
        logger.warning("fomo_top_paper start failed: %s", type(exc).__name__)
    try:                                    # v1.29 paper_v3_new (PAPER ONLY): window start + resume open paper marks
        from app.paper_v3 import cfg as _v3_cfg, paper as _v3
        if _v3_cfg()["enabled"]:
            _v3.load()
    except Exception as exc:  # noqa: BLE001
        logger.warning("paper_v3 start failed: %s", type(exc).__name__)
    try:                                    # v1.34 kol_confluence (PAPER ONLY)
        from app.paper_kol_confluence import cfg as _kc_cfg, paper as _kc
        if _kc_cfg()["enabled"]:
            _kc.load()
    except Exception as exc:  # noqa: BLE001
        logger.warning("kol_confluence start failed: %s", type(exc).__name__)
    try:                                    # v1.32 paper_top4 (PAPER ONLY)
        from app.paper_top4 import cfg as _t4_cfg, paper as _t4
        if _t4_cfg()["enabled"]:
            _t4.load()
    except Exception as exc:  # noqa: BLE001
        logger.warning("paper_top4 start failed: %s", type(exc).__name__)
    try:                                    # v1.22 momentum_paper (PAPER ONLY): window start + score/mark loops
        from app.paper_momentum import cfg as _mom_cfg, paper as _mom
        if _mom_cfg()["enabled"]:
            _mom.load()
            _mom.ensure_loop()
    except Exception as exc:  # noqa: BLE001
        logger.warning("momentum_paper start failed: %s", type(exc).__name__)
    try:
        yield
    finally:
        live_engine.save()
        await stop_demo_loop()
        await streams.stop_all()
        await keyless.stop()
        await fomo_manager.stop()
        await scout_source.stop()
        await outcome_tracker.stop()
        await wallet_job.stop()
        try:
            from app.recap_wallet import book as _wallet_book
            await _wallet_book.stop()
        except Exception:  # noqa: BLE001
            pass
        from app.live.watch import watch as _acct_watch
        await _acct_watch.stop()
        from app.scout.filters import filters as scout_filters
        scout_filters.save(force=True)


app = FastAPI(
    title="Fomo Signal Coach",
    version=__version__,
    description="Recommend-only Signal Coach — no auto-trade. Jupiter exec dry-run by default.",
    lifespan=lifespan,
)

app.include_router(buy_router)
app.include_router(fomo_activity_router)
app.include_router(exec_router)
from app.live.api import router as live_router  # noqa: E402
app.include_router(live_router)
from app.recap import router as recap_router  # noqa: E402  v1.17.3 read-only Day Recap (/recap, /api/recap)
app.include_router(recap_router)

_STATIC = Path(__file__).resolve().parents[1] / "static"


@app.get("/health")
async def health() -> Dict[str, Any]:
    h = await run_in_threadpool(build_health)
    try:
        h["pump_lane"] = _pump_status()
    except Exception as exc:  # noqa: BLE001
        h["pump_lane"] = {"status": "error", "error": str(exc)[:200]}
    try:
        h["fomo_onchain"] = _fomo_status()
    except Exception as exc:  # noqa: BLE001
        h["fomo_onchain"] = {"status": "error", "error": str(exc)[:200]}
    try:
        h["scout"] = _scout_status()
        from app.live.engine import engine as live_engine
        h["live"] = live_engine.summary()
    except Exception as exc:  # noqa: BLE001
        h["scout"] = {"status": "error", "error": str(exc)[:200]}
    # v1.16: top-level warnings (live engine + scout detection audit)
    warns = list(((h.get("live") or {}).get("warnings")) or [])
    try:
        from app.scout.audit import auditor
        det = auditor.health()
        if isinstance(h.get("scout"), dict):
            h["scout"]["detection"] = det
        warns += det["warnings"]
    except Exception as exc:  # noqa: BLE001
        warns.append(f"scout detection audit status error: {type(exc).__name__}")
    h["warnings"] = warns
    try:                                   # v1.19 fomo_top_paper status (paper only; never a warning)
        from app.paper_fomo_top import cfg as _ftp_cfg, paper as _ftp
        _c = _ftp_cfg()
        h["paper_fomo_top"] = {"enabled": _c["enabled"], "active": _ftp.active() if _c["enabled"] else False,
                               "start_ms": _ftp.start_ms, "end_ms": _ftp.end_ms, "open": len(_ftp.book),
                               "stats": dict(_ftp.stats), "endpoint": "/api/paper/fomo-top/summary"}
    except Exception as exc:  # noqa: BLE001
        h["paper_fomo_top"] = {"status": "error", "error": type(exc).__name__}
    try:                                   # v1.29 paper_v3_new status (paper only; never a warning)
        from app.paper_v3 import cfg as _v3_cfg, paper as _v3
        _c3 = _v3_cfg()
        h["paper_v3"] = {"enabled": _c3["enabled"], "active": _v3.active() if _c3["enabled"] else False,
                         "start_ms": _v3.start_ms, "end_ms": _v3.end_ms, "open": len(_v3.book),
                         "wallets": len(_c3["targets"]), "watch_only": len(_c3["watch"]),
                         "stats": dict(_v3.stats), "endpoint": "/api/paper/v3/summary"}
    except Exception as exc:  # noqa: BLE001
        h["paper_v3"] = {"status": "error", "error": type(exc).__name__}
    try:                                   # v1.34 kol_confluence status (paper only; never a warning)
        from app.paper_kol_confluence import cfg as _kc_cfg, paper as _kc, live_cfg as _kc_live
        from app.live.config import config as _lcfg
        _ck = _kc_cfg()

        def _kc_live_health():                 # v1.35 live lane status
            lc, L = _kc_live(), _lcfg()
            return {"enabled": lc["enabled"], "in_live_arms": "kol_confluence" in L["arms"],
                    "size_sol": min(L["sizes_sol"].get("kol_confluence", 0.0), L["max_sol_per_trade"]),
                    "rules": L["rules"].get("kol_confluence"), "trail_pct": L.get("kol_confluence_trail_pct"),
                    "cap_bypass": {"enabled": lc["bypass_enabled"], "min_wallets": lc["bypass_min"],
                                   "count": lc["bypass_count"], "requires_jev": lc["bypass_require_jev"]},
                    "trades_day_cap": "none (uncounted)" if "kol_confluence" in L.get("trade_cap_uncounted_arms", [])
                    else "shared", "max_open": (L.get("own_pool_max_open") or {}).get("kol_confluence", "shared"),
                    "open_now": _kc_open("kol_confluence"),
                    "loss_limit_exempt": "kol_confluence" in (L.get("loss_limit_exempt_arms") or []),
                    "night_pause_exempt": "kol_confluence" in (L.get("night_pause_exempt_arms") or []),
                    "routed": len(_kc.live_routed), "strong_tried": len(_kc.strong_tried)}

        def _kc_open(a):
            from app.live.engine import engine as _le
            return _le.open_count(arm=a)
        h["kol_confluence"] = {"enabled": _ck["enabled"], "active": _kc.active() if _ck["enabled"] else False,
                               "paper_only": not _kc_live()["enabled"], "live": _kc_live_health(),
                               "n": _ck["n"], "window_min": _ck["window_min"],
                               "open": len(_kc.book), "tracked_mints": len(_kc.buys), "stats": dict(_kc.stats),
                               "endpoint": "/api/paper/kol_confluence/summary"}
    except Exception as exc:  # noqa: BLE001
        h["kol_confluence"] = {"status": "error", "error": type(exc).__name__}
    try:                                   # v1.21 scout bench (top-level copy of live.scout_bench)
        from app.live.bench import bench as _bench
        h["scout_bench"] = _bench.summary(short=True)
    except Exception as exc:  # noqa: BLE001
        h["scout_bench"] = {"status": "error", "error": type(exc).__name__}
    try:                                   # v1.22 momentum_paper status (paper only; never a warning)
        from app.paper_momentum import paper as _mom
        h["paper_momentum"] = _mom.health()
    except Exception as exc:  # noqa: BLE001
        h["paper_momentum"] = {"status": "error", "error": type(exc).__name__}
    try:                                   # v1.20 direct pump.fun path (shadow/live/off)
        from app.live import pumpdirect as _pd
        _pc = _pd.cfg()
        h["direct_pump"] = {"enabled": _pc["enabled"], "mode": _pd.mode(),
                            "live_arms": _pc.get("live_arms"), "live_sides": _pc.get("live_sides"),  # v1.36 trial
                            "slippage_pct": round(_pc["slippage_pct"] * 100, 2), "stats": dict(_pd._stats),
                            "endpoint": "/api/direct-pump/summary"}
    except Exception as exc:  # noqa: BLE001
        h["direct_pump"] = {"status": "error", "error": type(exc).__name__}
    try:                                   # v1.17 advisory wallet scores (status only; never a warning)
        from app.learning.wallet_scores import job as _wjob
        h["wallet_scores"] = _wjob.status()
    except Exception as exc:  # noqa: BLE001
        h["wallet_scores"] = {"status": "error", "error": type(exc).__name__}
    try:
        h["mesh"] = await mesh_health_summary()
    except Exception as exc:  # noqa: BLE001
        h["mesh"] = {"enabled": settings.mesh_enabled, "status": "down", "error": str(exc)[:200],
                     "fail_safe": "escalate_human"}
    return h


def _should_notify(sig: Any) -> bool:
    gate = getattr(sig, "gate", None) or {}
    return sig.score >= settings.telegram_min_score and not gate.get("blocked")


@app.get("/")
def root() -> Dict[str, Any]:
    return {
        "service": "fomo-signal-coach",
        "version": __version__,
        "recommend_only": True,
        "auto_trade": False,
        "health": "/health",
        "ui": "/ui/",
        "ingest": ["/api/ingest/buy", "/api/ingest/fomo-activity"],
        "signals": ["/api/signals", "/api/signals/{token_address}"],
        "mesh": ["/api/mesh/enrich/{token_address}"],
        "exec": ["/api/exec/quote", "/api/exec/swap", "/api/exec/meta"],
        "pump": ["/api/pump/curve/{mint}", "/api/pump/streams", "/api/pump/stream/{mint}",
                 "/api/latency", "/api/latency/{mint}", "/api/paper"],
        "fomo_onchain": ["/api/fomo-onchain", "/api/fomo-onchain/wallets"],
        "scout": ["/api/scout", "/api/scout/signals", "/api/scout/alerts", "/api/scout/audit",
                  "/api/exit-variants/summary"],
        "learning": ["/api/learning/summary", "/api/learning/candidates"],
        "v1_17": ["/api/wallets/scores", "/api/live/rugcheck"],
    }


def _scout_status() -> Dict[str, Any]:
    from app.scout.engine import engine
    from app.scout.source import source
    st = source.status()
    rep = engine.report()
    return {"enabled": st["enabled"], "running": st["running"], "paper_only": True,
            "wallets": st["wallets"], "subscriptions": st["subscriptions"],
            "subscription_kinds": st.get("subscription_kinds"),
            "triggers": {k: st["stats"].get(k, 0) for k in ("via_pump_uva", "via_amm_uva", "via_lamports", "via_poll",
                                                             "tx_path", "tx_resolved", "tx_no_trade",
                                                             "lamport_triggers", "lamport_unresolved")},
            "ws_msg_rate_per_s_60s": st["ws_msg_rate_per_s_60s"], "seen": rep["seen"],
            "arms": {a: {k: v for k, v in d.items() if k in ("signals", "enter", "skip", "entries_mints")}
                     for a, d in rep["arms"].items()},
            "errors": {k: v for k, v in st["stats"].items() if k.endswith("_errors") or k == "rpc_429"}}


@app.get("/api/scout")
def scout_report() -> Dict[str, Any]:
    """Scout-wallet paper test: per arm × exit rule × fill latency — count, win rate, avg/median PnL after fees,
    edge gap. Plus source status and skip-filter state."""
    from app.scout.engine import engine
    from app.scout.filters import filters
    from app.scout.roster import roster
    from app.scout.source import source
    return {"ok": True, "recommend_only": True, "auto_trade": False, "paper_only": True,
            "report": engine.report(), "source": source.status(), "filters": filters.status(),
            "roster": [{"label": w.label, "role": w.role, "wallet": w.wallet, "buys": w.buys, "sells": w.sells,
                        "last_ms": w.last_ms} for w in roster.by_wallet.values() if w.role != "fomo"],
            "roster_skipped": roster.skipped}


@app.get("/api/scout/audit")
async def scout_audit(run: bool = Query(default=False, description="start a fresh read-only audit in the background"),
                window_h: float = Query(default=24.0, ge=1.0, le=48.0)) -> Dict[str, Any]:
    """v1.16 detection audit: scout wallets' on-chain buys (free RPC, read-only) vs what the scout lane detected.
    Returns the last result; run=true starts a new one in the background (at most one per 15 min)."""
    from app.scout.audit import auditor
    auditor.load()
    started = auditor.request(window_h=window_h) if run else None
    return {"ok": True, "read_only": True, "running": auditor.running, "run_request": started,
            "health": auditor.health(), "last": auditor.last}


@app.get("/api/learning/summary")
def learning_summary(days: float = Query(default=7.0, ge=0.5, le=60.0),
                     min_version: str = Query(default="1.16.0-demo")) -> Dict[str, Any]:
    """v1.16 learning log scorecard (descriptive only — nothing is auto-tuned)."""
    from app.learning.outcomes import tracker
    from app.learning.scorecard import summarize
    return {"ok": True, "read_only": True, "outcomes": tracker.status(),
            "scorecard": summarize(days=days, min_version=min_version)}


@app.get("/api/wallets/scores")
def wallet_scores(role: Optional[str] = Query(default=None), limit: int = Query(default=200, ge=1, le=500)) -> Dict[str, Any]:
    """v1.17 wallet scores — ADVISORY ONLY (nothing in the coach reads this to weight, add or drop wallets).
    Refreshed daily into data/wallet_scores.json by a background job (free public RPC + GeckoTerminal candles)."""
    from app.learning.wallet_scores import job, read
    cur = read() or {}
    ws = [w for w in cur.get("wallets") or [] if role is None or w.get("role") == role][:limit]
    return {"ok": True, "read_only": True, "advisory_only": True, "job": job.status(),
            "generated_ms": cur.get("generated_ms"), "window_days": cur.get("window_days"),
            "counts": cur.get("counts"), "recommendations": cur.get("recommendations"), "wallets": ws,
            "note": cur.get("note") or "no scores yet — the daily job writes data/wallet_scores.json"}


@app.get("/api/live/signer_shadow")
async def live_signer_shadow() -> Dict[str, Any]:
    """v1.24 policy-signer status + dry-run verdicts on real Jupiter orders (read-only; nothing signed or sent)."""
    from app.live import signer_client, signer_shadow
    return {"ok": True, "read_only": True, "signer": await signer_client.health(), **signer_shadow.summary()}


@app.get("/api/live/rugcheck")
def live_rugcheck() -> Dict[str, Any]:
    """v1.17 rug / bundle pre-buy checks: config, counters and the last decisions (read-only)."""
    from app.live.rugcheck import checker, cfg
    c = cfg()
    return {"ok": True, "read_only": True, **checker.summary(),
            "thresholds": {k: v for k, v in c.items() if k not in ("checks", "rpc_url")}}


@app.get("/api/learning/candidates")
def learning_candidates(limit: int = Query(default=50, ge=1, le=500),
                        arm: Optional[str] = Query(default=None)) -> Dict[str, Any]:
    from collections import deque
    from app.learning.candidates import rows
    import time as _t
    out = deque(maxlen=limit)
    for r in rows(int(_t.time() * 1000) - 7 * 86_400_000):
        if arm is None or r.get("arm") == arm:
            out.append(r)
    return {"ok": True, "read_only": True, "candidates": list(out)[::-1]}


@app.get("/api/scout/signals")
def scout_signals() -> Dict[str, Any]:
    from app.scout.engine import engine
    return {"ok": True, "paper_only": True, **engine.view()}


@app.get("/api/paper/fomo-top/summary")
def paper_fomo_top_summary() -> Dict[str, Any]:
    """v1.19 PAPER-ONLY copy test of FOMO wallet zakum (fomo_top_paper; NotARandomUser leg retired in v1.29)."""
    from app.paper_fomo_top import paper
    return paper.summary()


@app.get("/api/paper/v3/summary")
def paper_v3_summary() -> Dict[str, Any]:
    """v1.29 PAPER-ONLY 7-day copy test of the 10 vetted wallets from wallets_v3 (paper_v3_new)."""
    from app.paper_v3 import paper
    return paper.summary()


@app.get("/api/paper/kol_confluence/summary")
def paper_kol_confluence_summary() -> Dict[str, Any]:
    """v1.34 PAPER-ONLY multi-wallet confluence (≥N distinct watched KOL/cryptohub wallets on one mint in a window)."""
    from app.paper_kol_confluence import paper
    return paper.summary()


@app.get("/api/paper/top4/summary")
def paper_top4_summary() -> Dict[str, Any]:
    """v1.32 PAPER-ONLY copy of Yenni / Lynk / Inquixit / Zef, half at 2x (paper_top4)."""
    from app.paper_top4 import paper
    return paper.summary()


@app.get("/api/paper/momentum/summary")
def paper_momentum_summary() -> Dict[str, Any]:
    """v1.22 PAPER-ONLY momentum arm (cluster + chatter + flow + health score)."""
    from app.paper_momentum import paper
    return paper.summary()


@app.get("/api/paper/momentum/tg_search/next")
def paper_momentum_tg_next(request: Request, n: int = 1, searches_hour: Optional[int] = None,
                           searches_day: Optional[int] = None, max_hour: Optional[int] = None,
                           max_day: Optional[int] = None, min_interval_s: Optional[float] = None,
                           disabled_until: Optional[int] = None, next_ok_ms: Optional[int] = None,
                           posts_remaining: Optional[int] = None, posts_daily: Optional[int] = None,
                           posts_off_until: Optional[int] = None, floods: Optional[int] = None) -> Dict[str, Any]:
    """v1.23 (loopback only): the Telegram watcher pulls public-search requests (PAPER ONLY momentum arm)."""
    from app.live.auth import require_localhost
    require_localhost(request)
    from app.paper_momentum import paper
    st = {k: v for k, v in dict(searches_hour=searches_hour, searches_day=searches_day, max_hour=max_hour,
                                max_day=max_day, min_interval_s=min_interval_s, disabled_until=disabled_until,
                                next_ok_ms=next_ok_ms, posts_remaining=posts_remaining, posts_daily=posts_daily,
                                posts_off_until=posts_off_until, floods=floods).items()}
    return paper.tg_next(max(0, min(int(n), 3)), st)


@app.post("/api/paper/momentum/tg_search/result")
def paper_momentum_tg_result(body: Dict[str, Any], request: Request) -> Dict[str, Any]:
    """v1.23 (loopback only): counts + channel usernames from the watcher's public search. Never message text."""
    from app.live.auth import require_localhost
    require_localhost(request)
    from app.paper_momentum import paper
    return {"ok": True, **paper.tg_result(list(body.get("results") or []))}


@app.post("/api/paper/momentum/tg_search/request")
def paper_momentum_tg_request(body: Dict[str, Any], request: Request) -> Dict[str, Any]:
    """v1.23 (loopback only): manually queue one coin for a public search (verification)."""
    from app.live.auth import require_localhost
    require_localhost(request)
    from app.paper_momentum import paper
    return paper.queue_manual(str(body.get("mint") or ""), body.get("symbol"))


@app.post("/api/paper/momentum/mentions")
def paper_momentum_mentions(body: Dict[str, Any], request: Request) -> Dict[str, Any]:
    """v1.22 Telegram CA / $TICKER mention counts from the local fomo-telegram-pump watcher (loopback only).
    Only mint / ticker / chat title / timestamp are kept — never message text."""
    from app.live.auth import require_localhost
    require_localhost(request)
    from app.paper_momentum import paper
    items = body.get("mentions") if isinstance(body, dict) else None
    return {"ok": True, **paper.on_mentions(list(items or []))}


@app.get("/api/direct-pump/summary")
def direct_pump_summary(recent: int = Query(default=15, ge=0, le=200)) -> Dict[str, Any]:
    """v1.20 direct pump.fun buy/sell path: shadow simulations vs Jupiter fills + go-live checklist."""
    from app.live import pumpdirect
    return pumpdirect.summary(recent=recent)


@app.get("/api/exit-variants/summary")
def exit_variants_summary(include_restart_gap: bool = Query(default=False)) -> Dict[str, Any]:
    """v1.18 PAPER exit-variants test: per variant n / total / median / win rate / beats-A, all arms vs live-like."""
    from app.scout.exit_variants import variants
    return variants.summary(include_restart_gap=include_restart_gap)


@app.get("/api/scout/alerts")
def scout_alerts(since_ms: int = Query(default=0)) -> Dict[str, Any]:
    """Arm B entries for the watcher's first-alert push (watcher polls; Saved Messages + macOS only)."""
    from app.scout.engine import engine
    return {"ok": True, "alerts": [a for a in engine.alerts if a["ts_ms"] > since_ms][:20],
            "now_ms": int(__import__("time").time() * 1000)}


def _fomo_status() -> Dict[str, Any]:
    from app.fomo_onchain.keyless import keyless
    from app.fomo_onchain.source import manager
    from app.fomo_onchain.strategy import strategy
    from app.pump.paper import paper
    st = manager.status()
    return {"enabled": st["enabled"], "running": st["running"], "priority": "low (yields to Telegram/Fluffy CA lane)",
            "wallets": st["wallets"], "subscriptions": st["subscriptions"],
            "ws_msg_rate_per_s_60s": st["ws_msg_rate_per_s_60s"], "queue_depth": st["queue_depth"],
            "detected": {"buys": st["stats"].get("buys", 0), "sells": st["stats"].get("sells", 0)},
            "errors": {k: v for k, v in st["stats"].items() if k.endswith("_errors") or k in ("rpc_429", "queue_drops")},
            "paper_profile": paper.profile_summary("fomo_top_trader"), "strategy": dict(strategy.stats),
            "keyless": {"state": keyless.state, "alerts": keyless.stats.get("alerts", 0), "entry_trigger": False}}


@app.get("/api/fomo-onchain")
def fomo_onchain_view() -> Dict[str, Any]:
    """FOMO top-trader on-chain watch: source status, detected trades, strategy decisions, paper positions."""
    from app.fomo_onchain.keyless import keyless
    from app.fomo_onchain.source import manager
    from app.fomo_onchain.strategy import strategy
    return {"ok": True, "recommend_only": True, "auto_trade": False, "paper_only": True,
            "source": manager.status(), "trades": list(manager.trades)[:100], "strategy": strategy.view(),
            "keyless": {**keyless.status(), "alerts": list(keyless.alerts)[:30]}}


@app.get("/api/fomo-onchain/wallets")
def fomo_onchain_wallets(status: Optional[str] = Query(default=None)) -> Dict[str, Any]:
    """Per-wallet quality (0–100) from VERIFIED FOMO-co-signed activity; dropped wallets carry a reason."""
    from app.fomo_onchain.wallets import watchlist
    ws = sorted(watchlist.wallets.values(), key=lambda w: (-w.quality, w.rank))
    if status:
        ws = [w for w in ws if w.status == status]
    return {"ok": True, "counts": watchlist.counts(),
            "score_formula": "activity 45 (co-signed trades, caps at 6) + recency 30 + FOMO purity 15 + live 10",
            "wallets": [w.view() for w in ws]}


def _pump_status() -> Dict[str, Any]:
    from app.pump import rpc
    from app.pump.paper import paper
    from app.pump.trade_stream import streams

    st = streams.status()
    return {
        "curve_reader": "on-chain getAccountInfo (read-only)",
        "rpc": rpc.rpc_label(),
        "stream": {k: st[k] for k in ("enabled", "transport", "max_streams")} | {"active": len(st["active"])},
        "paper": paper.summary(),
        "jev_role_curve_lane": "advisory",
        "signing": False,
        "jupiter_exec_enabled": settings.jupiter_exec_enabled,
    }


@app.get("/api/pump/curve/{mint}")
async def pump_curve(mint: str) -> Dict[str, Any]:
    """Direct on-chain pump.fun bonding-curve read (no keys, no paid services)."""
    from app.pump.curve import read_curve
    from app.pump.fast_lane import fast_gate

    c = await read_curve(mint, use_cache=False)
    return {"ok": c.get("status") in ("ok", "not_pump_curve"), "read_only": True, "curve": c,
            "fast_gate": fast_gate(c)}


@app.get("/api/pump/streams")
def pump_streams() -> Dict[str, Any]:
    from app.pump.trade_stream import streams
    return {"ok": True, **streams.status()}


@app.post("/api/pump/stream/{mint}")
async def pump_stream_start(mint: str) -> Dict[str, Any]:
    """Start (or peek) the free logsSubscribe trade stream for one mint (read-only)."""
    from app.pump.curve import read_curve
    from app.pump.solana_pda import is_pubkey
    from app.pump.trade_stream import streams

    if not is_pubkey(mint):
        raise HTTPException(status_code=400, detail="invalid_mint")
    c = await read_curve(mint)
    s = streams.start(mint, curve=c)
    return {"ok": s is not None, "stream": s.summary() if s else None, "curve_status": c.get("status")}


@app.get("/api/pump/stream/{mint}")
def pump_stream_get(mint: str) -> Dict[str, Any]:
    from app.pump.fast_lane import card_extras
    ex = card_extras(mint)
    return {"ok": ex.get("stream") is not None, **ex}


@app.get("/api/latency")
def latency_recent(limit: int = Query(30, ge=1, le=200)) -> Dict[str, Any]:
    from app.pump.fast_lane import recent
    return {"ok": True, "timelines": recent(limit)}


@app.get("/api/latency/{mint}")
def latency_one(mint: str) -> Dict[str, Any]:
    from app.pump.fast_lane import timeline_for
    t = timeline_for(mint)
    if t is None:
        raise HTTPException(status_code=404, detail="no_timeline")
    return {"ok": True, "timeline": t}


@app.get("/api/pump/lane")
def pump_lane_view(limit: int = Query(12, ge=1, le=50)) -> Dict[str, Any]:
    """Recent CA-lane mints (any score) with curve, latency, stream, exit cards, paper position."""
    from app.pump.fast_lane import card_extras, recent
    from app.pump.paper import paper
    items = []
    for t in recent(limit):
        if t:
            items.append({"mint": t["mint"], "source": t["source"], **card_extras(t["mint"])})
    return {"ok": True, "recommend_only": True, "auto_trade": False, "paper_summary": paper.summary(),
            "items": items}


@app.get("/api/paper")
def paper_view() -> Dict[str, Any]:
    from app.pump.paper import paper
    return {"ok": True, "recommend_only": True, "auto_trade": False, **paper.view()}


@app.get("/api/clusters/{token_address}")
def cluster(token_address: str) -> Dict[str, Any]:
    return {"ok": True, "cluster": state.cluster_summary(token_address)}


@app.get("/api/signals")
async def list_signals(
    min_score: float = Query(50.0, ge=0.0, le=100.0, description="Minimum score threshold"),
) -> Dict[str, Any]:
    """List recent cluster signals above min_score (computed on read)."""
    signals = compute_signals(min_score=min_score)
    # QP Data Mesh enrichment first (throttled/cached) so Jev + rule gate see risk data
    await attach_mesh(signals)
    # Jev recommend-only gate + final mesh/Jev gate (fail closed / escalate_human when unsure)
    await run_in_threadpool(annotate_signals, signals)
    # Soft notify for high scores (log when telegram unset) — never for mesh-blocked tokens
    for sig in signals:
        if _should_notify(sig):
            await run_in_threadpool(maybe_notify_telegram, sig)
    return {
        "ok": True,
        "recommend_only": True,
        "auto_trade": False,
        "min_score": min_score,
        "count": len(signals),
        "signals": [s.to_dict() for s in signals],
    }


@app.get("/api/signals/{token_address}")
async def signal_detail(token_address: str) -> Dict[str, Any]:
    """Detail for one token — mesh enrichment + Jev + final gate + soft DeBot narrative."""
    sig = get_signal(token_address, min_score=0.0)
    if sig is None:
        raise HTTPException(status_code=404, detail="no_buys_for_token")
    await attach_mesh([sig], timeout=6.0)
    await run_in_threadpool(maybe_attach_jev, sig)
    maybe_attach_gate(sig)
    maybe_attach_plan(sig)
    await run_in_threadpool(maybe_attach_narrative, sig)
    if _should_notify(sig):
        await run_in_threadpool(maybe_notify_telegram, sig)
    return {
        "ok": True,
        "recommend_only": True,
        "auto_trade": False,
        "signal": sig.to_dict(),
    }


@app.get("/api/mesh/enrich/{token_address}")
async def mesh_enrich(token_address: str, chain: str = "solana", pool: str = "") -> Dict[str, Any]:
    """Ad-hoc QP Data Mesh enrichment + rule gate for any mint (CA announce preview; no buys needed)."""
    from app.mesh import enrich_token
    from app.risk_gate import combine, mesh_rule_decision

    mesh = await enrich_token(token_address, chain, pool=pool or None)
    rule = mesh_rule_decision(mesh)
    return {
        "ok": True,
        "recommend_only": True,
        "auto_trade": False,
        "mesh": mesh,
        "mesh_rule": rule,
        "gate_without_jev": combine({"jev_status": "unavailable"}, rule),
    }


@app.post("/api/ingest/helius")
async def helius_stub() -> JSONResponse:
    """Placeholder — register via scripts/create_helius_webhook.py once PUBLIC_BASE_URL is HTTPS."""
    return JSONResponse(
        {
            "ok": True,
            "soft_fail": True,
            "note": "Helius webhook receiver stub; wire SWAP → buy ingest after tunnel",
            "configured": settings.helius_configured,
            "webhook_ready": settings.helius_webhook_ready,
        }
    )


# Mount UI last so /health and /api/* win. Prefer /ui over /
if _STATIC.is_dir():
    app.mount("/ui", StaticFiles(directory=str(_STATIC), html=True), name="ui")
