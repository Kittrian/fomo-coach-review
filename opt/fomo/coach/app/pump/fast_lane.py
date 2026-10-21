"""CA fast lane: per-stage latency timeline + curve-first gate, with mesh/JEV/quote running after.

Stages (ms, wall clock): tg_message → tg_recv → ingest → curve_read → [paper_entry] → mesh → jev → quote.
`tg_message_ms` / `tg_recv_ms` are taken from buy meta when the Telegram watcher provides them
(meta keys: tg_message_ms|telegram_message_ms, tg_recv_ms|telegram_recv_ms); otherwise omitted.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional

from app.paths import data_path
from app.pump.curve import curve_summary, read_curve
from app.pump.solana_pda import WSOL_MINT, is_pubkey

logger = logging.getLogger("coach.pump.fast_lane")

STAGES = ("tg_message", "tg_recv", "ingest", "curve_read", "paper_entry", "mesh", "live", "jev", "quote")
_MAX = 200
_timelines: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()  # by mint (latest event)
_curves: Dict[str, Dict[str, Any]] = {}
_tasks: set = set()


def _now() -> int:
    return int(time.time() * 1000)


# ---------------- v1.16: /api/latency survives restarts ----------------
_persist_loaded = False


def latency_log_path():
    return data_path("LATENCY_LOG_PATH", "latency_timelines.jsonl")


def _persist(t: Dict[str, Any]) -> None:
    try:
        p = latency_log_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as fh:
            fh.write(json.dumps({k: v for k, v in t.items() if k != "restored"}, default=str) + "\n")
        if p.stat().st_size > int(os.getenv("LATENCY_LOG_MAX_BYTES", str(4 * 2**20))):
            lines = p.read_text().splitlines()[-1000:]
            tmp = p.with_name(p.name + ".tmp")
            tmp.write_text("\n".join(lines) + "\n")
            tmp.replace(p)
    except Exception as exc:  # noqa: BLE001
        logger.warning("latency persist failed: %s", type(exc).__name__)


def load_persisted() -> int:
    """Seed the in-memory timelines from the log (oldest first, newest wins). Idempotent."""
    global _persist_loaded
    if _persist_loaded:
        return 0
    _persist_loaded = True
    n = 0
    try:
        p = latency_log_path()
        if not p.exists():
            return 0
        for line in p.read_text().splitlines()[-_MAX * 3:]:
            try:
                t = json.loads(line)
            except ValueError:
                continue
            if not isinstance(t, dict) or not t.get("mint") or t["mint"] in _timelines:
                continue
            t["restored"] = True
            _timelines[t["mint"]] = t
            n += 1
        while len(_timelines) > _MAX:
            _timelines.popitem(last=False)
    except Exception as exc:  # noqa: BLE001
        logger.warning("latency load failed: %s", type(exc).__name__)
    return n


def _meta_ms(meta: Dict[str, Any], *keys: str) -> Optional[int]:
    for k in keys:
        v = meta.get(k)
        if isinstance(v, (int, float)) and v > 0:
            return int(v if v > 1e12 else v * 1000)
    return None


def new_timeline(mint: str, *, source: str, event_id: Optional[str], meta: Dict[str, Any],
                 ingest_ms: Optional[int] = None) -> Dict[str, Any]:
    t = {"mint": mint, "source": source, "event_id": event_id, "stages": {}, "durations_ms": {},
         "status": "running", "started_ms": ingest_ms or _now()}
    st = t["stages"]
    m = _meta_ms(meta, "tg_message_ms", "telegram_message_ms")
    r = _meta_ms(meta, "tg_recv_ms", "telegram_recv_ms")
    if m:
        st["tg_message"] = m
    if r:
        st["tg_recv"] = r
    st["ingest"] = t["started_ms"]
    _timelines[mint] = t
    _timelines.move_to_end(mint)
    while len(_timelines) > _MAX:
        _timelines.popitem(last=False)
    return t


def mark(t: Dict[str, Any], stage: str, *, took_ms: Optional[float] = None, **info: Any) -> None:
    t["stages"][stage] = _now()
    if took_ms is not None:
        t["durations_ms"][stage] = round(took_ms, 1)
    if info:
        t.setdefault("info", {})[stage] = info


def view(t: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not t:
        return None
    st = t["stages"]
    order = [s for s in STAGES if s in st]
    base = st.get("tg_message") or st.get("tg_recv") or st.get("ingest")
    rows = []
    prev = None
    for s in order:
        rows.append({"stage": s, "at_ms": st[s], "since_start_ms": st[s] - base if base else None,
                     "delta_ms": (st[s] - st[prev]) if prev else 0, "took_ms": t["durations_ms"].get(s)})
        prev = s
    return {"mint": t["mint"], "source": t["source"], "event_id": t.get("event_id"), "status": t["status"],
            "restored": bool(t.get("restored")),
            "stages": rows, "total_ms": (st[order[-1]] - base) if base and order else None,
            "ingest_to_curve_ms": (st["curve_read"] - st["ingest"]) if "curve_read" in st else None,
            "ingest_to_paper_entry_ms": (st["paper_entry"] - st["ingest"]) if "paper_entry" in st else None,
            "info": t.get("info", {})}


def timeline_for(mint: str) -> Optional[Dict[str, Any]]:
    load_persisted()
    return view(_timelines.get(mint))


def recent(limit: int = 30) -> List[Dict[str, Any]]:
    load_persisted()
    return [view(t) for t in list(_timelines.values())[-limit:]][::-1]


def curve_for(mint: str) -> Optional[Dict[str, Any]]:
    return _curves.get(mint)


def fast_gate(curve: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Curve-first decision for pre-migration pump.fun coins (no mesh/JEV wait)."""
    if not curve or curve.get("status") != "ok":
        return {"lane": "standard", "fast_ok": False,
                "reason": f"no curve ({(curve or {}).get('status', 'missing')}) → standard mesh/JEV gate"}
    if curve.get("complete"):
        return {"lane": "migrated", "fast_ok": False, "reason": "curve complete → migrated: hard-block risk gate applies"}
    warns = []
    prog = curve.get("progress_pct")
    if isinstance(prog, (int, float)) and prog >= 90:
        warns.append(f"curve {prog:.0f}% — migration imminent")
    if curve.get("is_mayhem_mode"):
        warns.append("mayhem-mode curve (non-standard virtual reserves)")
    if not curve.get("quote_is_sol", True):
        warns.append("non-SOL quote mint — no SOL curve price; escalate (no paper sim)")
    ok = bool(curve.get("price_sol"))
    return {"lane": "curve", "fast_ok": ok, "warnings": warns,
            "reason": "pre-migration bonding curve: mint/freeze/honeypot are program-controlled; "
                      "JEV advisory, mesh hard-block still wins"}


def _is_fluffy(source: str, meta: Dict[str, Any]) -> bool:
    s = (source or "").lower()
    g = str(meta.get("source_group") or "").lower()
    return "fluff" in s or "fluff" in g or s in {"fluffy/telegram", "telegram"}


async def _live_decide(t: Dict[str, Any], mint: str, live_arm: str, *, lane: str, curve: Dict[str, Any],
                       meta: Dict[str, Any], ingest_ms: int, paper_seen_before: bool, mesh: Optional[Dict[str, Any]],
                       rule: Dict[str, Any], age_task: Optional["asyncio.Task"] = None,
                       extra: Optional[List[str]] = None) -> Dict[str, Any]:
    """v1.15 blocking pre-buy gate → real buy only when every check passes (see app/live/tg_gate.py)."""
    from app.live import tg_gate
    from app.live.config import config as live_config
    from app.live.engine import engine as live_engine

    detect = meta.get("tg_recv_ms") or ingest_ms
    lcfg = live_config()
    if live_arm not in lcfg["arms"] or not lcfg["autotrade_enabled"]:
        if age_task:
            age_task.cancel()
        d = live_engine.on_signal(live_arm, mint, detect_ms=detect, ref_price_sol=curve.get("price_sol"), meta=meta)
        mark(t, "live", decision=d["decision"], reasons=d["reasons"][:3])
        return d
    why = tg_gate.static_reasons(mint, curve, lane, meta, paper_seen_before=paper_seen_before, ingest_ms=ingest_ms)
    why += tg_gate.mesh_reasons(mesh, rule)
    why += list(extra or [])
    from app.fomo_onchain import market
    if market.sol_usd() is None:
        why.append("no SOL/USD price yet")              # v1.16: no arm enters before a SOL/USD price exists
    fresh: Dict[str, Any] = {}
    fresh_task = asyncio.ensure_future(read_curve(mint, use_cache=False)) if not why else None   # v1.33 parallel
    if not why:
        age = await age_task if age_task else await tg_gate.token_age_s(mint)
        if age is None:
            why.append("token age unknown — refuse")
        elif age > lcfg["tg_max_age_s"]:
            why.append(f"token age {age:.0f}s > {lcfg['tg_max_age_s']:.0f}s")
    elif age_task:
        age_task.cancel()
    if not why:
        fresh = await fresh_task
    elif fresh_task is not None:
        fresh_task.cancel()
    if not why:
        if fresh.get("status") != "ok":
            why.append(f"fresh curve read failed ({fresh.get('status')})")
        elif fresh.get("complete"):
            why.append("curve completed before the buy")
    if why:
        d = live_engine.refuse(live_arm, mint, why, detect_ms=detect)
    else:
        d = live_engine.on_signal(live_arm, mint, detect_ms=detect,
                                  ref_price_sol=fresh.get("price_sol") or curve.get("price_sol"),
                                  meta={**meta, "tg_gate_passed": True, "lane": lane})
    mark(t, "live", decision=d["decision"], reasons=d["reasons"][:3])
    return d


async def run_pipeline(mint: str, *, source: str, meta: Dict[str, Any], event_id: Optional[str],
                       ingest_ms: int, chain: str = "solana") -> Dict[str, Any]:
    from app.live.engine import arm_for_telegram as live_arm_for_telegram, engine as live_engine
    from app.pump.paper import paper
    from app.pump.trade_stream import streams

    t = new_timeline(mint, source=source, event_id=event_id, meta=meta, ingest_ms=ingest_ms)
    paper_seen_before = paper.already_opened(mint)          # v1.15: before this call's own paper entry
    age_task = None
    mesh_fut = None
    try:
        # v1.33 speed: start the mesh rug/honeypot check NOW, in parallel with the curve read
        # (live budget for Telegram arms); still awaited with a hard timeout before any buy.
        from app.mesh import enrich_with_timeout
        _fl = _is_fluffy(source, meta)
        _arm0 = live_arm_for_telegram(source, meta) if _fl else None
        mesh_fut = asyncio.ensure_future(enrich_with_timeout(
            mint, chain, timeout=float(os.getenv("FAST_LANE_MESH_TIMEOUT_S", "8")), live=bool(_arm0)))
        t_mesh0 = time.perf_counter()
        c = await read_curve(mint)
        _curves[mint] = c
        g = fast_gate(c)
        mark(t, "curve_read", took_ms=c.get("elapsed_ms"), status=c.get("status"), phase=c.get("phase"),
             lane=g["lane"])
        fluffy = _is_fluffy(source, meta)
        if c.get("status") == "ok":
            streams.start(mint, curve=c)
        live_arm = live_arm_for_telegram(source, meta) if fluffy else None
        if live_arm and g["lane"] == "curve" and g["fast_ok"]:
            from app.live.config import config as live_config
            if live_arm in live_config()["arms"]:
                from app.live import tg_gate
                age_task = asyncio.get_running_loop().create_task(tg_gate.token_age_s(mint))   # overlaps the mesh wait
        from app.fomo_onchain import market as _market
        if fluffy and g["lane"] == "curve" and g["fast_ok"] and _market.sol_usd() is None:
            mark(t, "paper_entry", skipped="no SOL/USD price yet")
        elif fluffy and g["lane"] == "curve" and g["fast_ok"]:
            # v1.15: NO live buy here any more — the real buy waits for the mesh + Telegram pre-buy gate below
            pos = await paper.open_from_curve(mint, c, source=source,
                                              signal_latency_ms=_now() - t["stages"]["ingest"])
            if pos:
                mark(t, "paper_entry", venue=pos.entry_venue, entry_price_sol=pos.entry_price_sol)
        # slow gate (does NOT block the curve lane)
        mesh = await mesh_fut          # hard timeout inside enrich_with_timeout; timeout → pending/stale → refuse
        mark(t, "mesh", took_ms=(time.perf_counter() - t_mesh0) * 1000, status=mesh.get("status"), parallel=True)
        from app.risk_gate import mesh_rule_decision
        rule = mesh_rule_decision(mesh)
        if rule.get("blocked") and paper.open_for(mint):
            # post-entry hard block → simulated emergency exit
            pos = paper.open_for(mint)
            await paper._sell(pos, pos.tokens_left, "mesh_hard_block_after_entry", streams.get(mint))
        if rule.get("blocked"):
            live_engine.force_exit(mint, "mesh_hard_block_after_entry", signals=rule.get("reasons") or [])
        # v1.15: curve-lane live buy only now — after the mesh answer and the blocking Telegram pre-buy gate
        if live_arm and g["lane"] == "curve" and g["fast_ok"]:
            await _live_decide(t, mint, live_arm, lane="curve", curve=c, meta=meta, ingest_ms=ingest_ms,
                               paper_seen_before=paper_seen_before, mesh=mesh, rule=rule, age_task=age_task)
            age_task = None
        # migrated / non-curve fluffy tokens: paper entry only after the hard-block gate passes
        if fluffy and g["lane"] != "curve" and c.get("status") == "ok" and not rule.get("blocked") \
                and rule.get("mesh_status") == "ok":
            pos = await paper.open_from_curve(mint, c, source=source,
                                              signal_latency_ms=_now() - t["stages"]["ingest"])
            if pos:
                mark(t, "paper_entry", venue=pos.entry_venue, entry_price_sol=pos.entry_price_sol)
        # JEV (advisory on curve lane) — sync client, keep it off the event loop
        t0 = time.perf_counter()
        jev_action = None
        jev_status = None
        try:
            from app.scoring import get_signal, maybe_attach_jev
            sig = get_signal(mint, min_score=0.0)
            if sig is not None:
                sig.mesh = mesh
                await asyncio.wait_for(asyncio.to_thread(maybe_attach_jev, sig),
                                       timeout=float(os.getenv("FAST_LANE_JEV_TIMEOUT_S", "20")))
                jev_action = (sig.jev or {}).get("action")
                jev_status = (sig.jev or {}).get("jev_status")
            else:
                jev_status = "no_signal"
        except asyncio.TimeoutError:
            jev_status = "timeout"
        mark(t, "jev", took_ms=(time.perf_counter() - t0) * 1000, status=jev_status, action=jev_action,
             advisory=g["lane"] == "curve")
        # v1.15: migrated / non-curve lane — live needs risk_gate.combine == "show" AND the Telegram gate
        # (which refuses non-curve lanes, so Telegram arms never live-buy migrated coins)
        if live_arm and g["lane"] != "curve" and c.get("status") == "ok":
            from app.risk_gate import combine
            dec = combine({"jev_status": jev_status, "action": jev_action}, rule, fast_gate=g)
            extra = [] if dec.get("action") == "show" else [f"risk gate {dec.get('action')} ({dec.get('decided_by')})"]
            await _live_decide(t, mint, live_arm, lane=g["lane"], curve=c, meta=meta, ingest_ms=ingest_ms,
                               paper_seen_before=paper_seen_before, mesh=mesh, rule=rule, extra=extra)
        # Jupiter quote (dry, no taker → no tx) for the plan clip, for comparison/latency
        t0 = time.perf_counter()
        from app.pump.paper import _jupiter_quote, _plan
        q = await asyncio.to_thread(_jupiter_quote, WSOL_MINT, mint, int(_plan()["entry_sol"] * 1e9))
        mark(t, "quote", took_ms=(time.perf_counter() - t0) * 1000, ok=q["ok"], router=q.get("router"),
             out_amount=q.get("out_amount"), error=(q.get("error") or None))
        await paper.attach_jupiter_entry_quote(mint, q)
        t["status"] = "done"
    except Exception as exc:  # noqa: BLE001
        logger.warning("fast lane error for %s: %s", mint, exc)
        t["status"] = "error"
        t["error"] = str(exc)[:200]
    finally:
        if age_task is not None and not age_task.done():
            age_task.cancel()
        if mesh_fut is not None and not mesh_fut.done():
            mesh_fut.cancel()
        _persist(t)
        _log_tg_candidate(t, mint, source=source, meta=meta, live_arm=locals().get("live_arm"),
                          curve=locals().get("c"), gate=locals().get("g"), mesh=locals().get("mesh"),
                          rule=locals().get("rule"), jev=(locals().get("jev_status"), locals().get("jev_action")))
    return view(t) or {}


def _log_tg_candidate(t: Dict[str, Any], mint: str, *, source: str, meta: Dict[str, Any], live_arm: Optional[str],
                      curve: Optional[Dict[str, Any]], gate: Optional[Dict[str, Any]], mesh: Optional[Dict[str, Any]],
                      rule: Optional[Dict[str, Any]], jev: Any) -> None:
    """v1.16 learning log: one candidate row per Telegram CA pipeline run (entered or not), never raises."""
    try:
        from app.learning import candidates as learn
        info = t.get("info") or {}
        live = info.get("live") or {}
        pe = info.get("paper_entry") or {}
        if live:
            decision = "buy" if live.get("decision") == "buy" else "skip"
            reasons = list(live.get("reasons") or []) or [str(live.get("decision"))]
        elif (gate or {}).get("lane") != "curve" or not (gate or {}).get("fast_ok"):
            decision, reasons = "skip", [str((gate or {}).get("reason") or "no curve")]
        else:
            decision, reasons = "skip", ["no live arm for this source"]
        st = t.get("stages") or {}
        m = mesh or {}
        feats = {
            "lane": (gate or {}).get("lane"), "fast_ok": (gate or {}).get("fast_ok"),
            "gate_warnings": (gate or {}).get("warnings"),
            "paper_entered": "entry_price_sol" in pe, "paper_skip": pe.get("skipped"),
            "mesh_status": m.get("status"), "mesh_timed_out": m.get("timed_out"),
            "mesh_blocked": (rule or {}).get("blocked"), "mesh_reasons": ((rule or {}).get("reasons") or [])[:3],
            "mesh_warnings": ((rule or {}).get("warnings") or [])[:3],
            "jev_status": (jev or (None, None))[0], "jev_action": (jev or (None, None))[1],
            "first_seen": meta.get("first_seen"), "repeat_count": meta.get("repeat_count"),
            "pump_at_iso": meta.get("pump_at_iso"), "source_group": meta.get("source_group"),
            "symbol": meta.get("symbol") or meta.get("token_symbol"),
            "ingest_to_curve_ms": (st["curve_read"] - st["ingest"]) if "curve_read" in st and "ingest" in st else None,
            "tg_to_ingest_ms": (st["ingest"] - st["tg_message"]) if "tg_message" in st and "ingest" in st else None,
            "ingest_to_live_ms": (st["live"] - st["ingest"]) if "live" in st and "ingest" in st else None,
            **learn.curve_features(curve if (curve or {}).get("status") == "ok" else None),
            **learn.flow_features(mint), **learn.sol_usd_features(),
        }
        learn.log_candidate(arm=live_arm or ("fluffy_pump_dump" if _is_fluffy(source, meta) else f"tg:{source}"),
                            source=source, mint=mint, decision=decision, reasons=reasons, features=feats,
                            ref_price_sol=(curve or {}).get("price_sol"),
                            detect_ms=st.get("tg_recv") or st.get("ingest"), decision_ms=st.get("live"),
                            entered=decision == "buy")
    except Exception as exc:  # noqa: BLE001
        logger.warning("tg candidate log failed: %s", type(exc).__name__)


def schedule(mint: str, *, source: str, meta: Dict[str, Any], event_id: Optional[str],
             ingest_ms: int, chain: str = "solana") -> bool:
    if os.getenv("FAST_LANE_ENABLED", "true").strip().lower() not in {"1", "true", "yes", "on"}:
        return False
    if (chain or "").lower() != "solana" or not is_pubkey(mint):
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    task = loop.create_task(run_pipeline(mint, source=source, meta=meta, event_id=event_id,
                                         ingest_ms=ingest_ms, chain=chain))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return True


def card_extras(mint: str) -> Dict[str, Any]:
    """Curve, latency, exit cards, paper — attached to signal cards."""
    from app.pump import exit_signals
    from app.pump.paper import paper
    from app.pump.trade_stream import streams

    c = _curves.get(mint)
    s = streams.get(mint)
    pos = paper.positions.get(mint)
    out: Dict[str, Any] = {"curve": curve_summary(c) if c else None, "fast_gate": fast_gate(c) if c else None,
                           "latency": timeline_for(mint), "stream": s.summary() if s else None,
                           "paper": pos.view() if pos else None}
    if pos and pos.last_cards:
        out["exit_signals"] = pos.last_cards
    elif s:
        entry = (c or {}).get("price_sol")
        tl = _timelines.get(mint) or {}
        out["exit_signals"] = exit_signals.evaluate(
            trades=list(s.trades), creator=s.creator, price=s.last_price_sol, high=s.high_price_sol,
            entry_price=entry, entry_ms=tl.get("started_ms"),
            real_sol=(s.real_sol / 1e9) if s.real_sol is not None else None,
            peak_real_sol=(s.peak_real_sol / 1e9) if s.peak_real_sol is not None else None,
            migrated=s.migrated)
    else:
        out["exit_signals"] = None
    return out
