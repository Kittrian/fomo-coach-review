"""TypeSafe Jev gate via Cloudflare Workers AI — recommend-only, fail-closed.

Model: typesafe/jev
REST: POST .../accounts/{id}/ai/run  body { model, input: { state, questions } }

Never executes trades. On missing credentials or API failure:
  action=escalate_human, safe_to_surface=False, jev_status=unavailable.
"""
from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Protocol

import httpx

from app.config import settings

logger = logging.getLogger("coach.jev_gate")

JEV_MODEL = "typesafe/jev"
WORKERS_AI_RUN_URL = (
    "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run"
)

# Fail-closed defaults when Jev is unavailable
DEFAULT_ACTION = "escalate_human"
DEFAULT_SAFE = False
NOUL_SURFACE_THRESHOLD = 0.5  # noul >= threshold → safe_to_surface true


class SignalLike(Protocol):
    token_address: str
    symbol: str
    chain: str
    score: float
    unique_traders: int
    buy_count: int
    trader_handles: List[str]
    reasons: List[str]
    recommend_only: bool
    auto_trade: bool
    total_usd: Optional[float]
    avg_market_cap: Optional[float]
    meta: Dict[str, Any]


FOMO_JEV_QUESTIONS: Dict[str, Any] = {
    "action": {
        "type": "choice",
        "instructions": (
            "Recommend-only Signal Coach gate: should this meme-token cluster "
            "signal be hidden, shown as a recommendation card, or escalated to a "
            "human? Never choose anything that implies auto-trade or auto-buy."
        ),
        "criteria": {
            "skip": (
                "Weak, noisy, or unsafe to surface — do not show as a recommendation"
            ),
            "show": (
                "Credible enough cluster to surface as a recommend-only card "
                "(human still decides any wallet buy)"
            ),
            "escalate_human": (
                "Ambiguous, high-risk, or needs human review before surfacing"
            ),
        },
    },
    "signal_quality": {
        "type": "score",
        "instructions": (
            "How strong is this cluster signal for a recommend-only coach "
            "(convergence, size, early mcap, coherence of reasons)?"
        ),
        "criteria": [
            "Low quality: weak convergence or thin evidence",
            "Moderate quality: usable but incomplete cluster",
            "High quality: strong multi-trader cluster with coherent evidence",
        ],
    },
    "safe_to_surface": {
        "type": "noul",
        "instructions": (
            "Is it safe to surface this as a recommend-only card to the user? "
            "False when credentials/context are insufficient, demo-only noise, "
            "or risk of implying auto-trade. Prefer false when unsure."
        ),
        "criteria": {
            "true": "Safe to show as a recommend-only hint; human still decides",
            "false": "Not safe to surface without human escalation",
        },
    },
}


@dataclass
class JevDecision:
    """Structured gate output attached to each signal/card."""

    jev_status: str  # ok | unavailable | error | disabled
    action: str  # skip | show | escalate_human
    signal_quality: Optional[float] = None
    signal_quality_confidence: Optional[float] = None
    safe_to_surface: bool = False
    safe_to_surface_noul: Optional[float] = None
    action_confidence: Optional[float] = None
    action_probabilities: Optional[Dict[str, float]] = None
    model: Optional[str] = None
    recommend_only: bool = True
    auto_trade: bool = False
    note: Optional[str] = None
    raw_answers: Optional[Dict[str, Any]] = None
    usage: Optional[Dict[str, Any]] = None
    evaluated_at_ms: int = field(default_factory=lambda: int(time.time() * 1000))

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _unavailable(
    *,
    status: str = "unavailable",
    note: str,
) -> JevDecision:
    return JevDecision(
        jev_status=status,
        action=DEFAULT_ACTION,
        safe_to_surface=DEFAULT_SAFE,
        note=note,
        recommend_only=True,
        auto_trade=False,
    )


def build_state(sig: SignalLike, *, demo_mode: Optional[bool] = None) -> Dict[str, Any]:
    """Compact state object for typesafe/jev (object form allowed by Workers AI)."""
    demo = settings.demo_mode if demo_mode is None else demo_mode
    return {
        "product": "fomo-signal-coach",
        "recommend_only": True,
        "auto_trade": False,
        "demo_mode": demo,
        "constraint": "Never auto-trade or auto-buy; recommend-only handoff to wallet",
        "signal": {
            "ticker": getattr(sig, "symbol", None) or "?",
            "token_address": getattr(sig, "token_address", None),
            "chain": getattr(sig, "chain", None),
            "source": (getattr(sig, "meta", None) or {}).get("source")
            or "cluster_scoring",
            "coach_score": getattr(sig, "score", None),
            "unique_traders": getattr(sig, "unique_traders", None),
            "buy_count": getattr(sig, "buy_count", None),
            "trader_handles": list(getattr(sig, "trader_handles", None) or [])[:12],
            "reasons": list(getattr(sig, "reasons", None) or [])[:8],
            "total_usd": getattr(sig, "total_usd", None),
            "avg_market_cap": getattr(sig, "avg_market_cap", None),
        },
        "mesh": _mesh_state(getattr(sig, "mesh", None)),
    }


def _mesh_state(mesh: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Compact QP Data Mesh facts for Jev (price/liquidity/mcap/risk flags/curve)."""
    if not isinstance(mesh, dict):
        return {"status": "missing"}
    if mesh.get("status") != "ok":
        return {"status": mesh.get("status") or "unknown"}
    risk = mesh.get("risk") or {}
    curve = ((mesh.get("pump") or {}).get("curve") or {})
    return {
        "status": "ok",
        "price_usd": mesh.get("price_usd"),
        "liquidity_usd": mesh.get("liquidity_usd"),
        "market_cap_usd": mesh.get("market_cap_usd"),
        "change_24h_pct": mesh.get("change_24h_pct"),
        "risk_verdict": risk.get("verdict"),
        "risk_flags": [f.get("flag") for f in (risk.get("flags") or [])][:8],
        "mint_authority_live": risk.get("mint_authority_live"),
        "freeze_authority_live": risk.get("freeze_authority_live"),
        "honeypot": risk.get("honeypot"),
        "top10_holder_pct": risk.get("top10_holder_pct"),
        "pump_curve": {k: curve.get(k) for k in ("status", "graduation_progress_pct", "migrated")} if curve else None,
        "stale": mesh.get("stale"),
    }


def parse_answers(payload: Dict[str, Any]) -> JevDecision:
    """Map Workers AI / Jev response → fail-closed structured decision."""
    # Cloudflare may wrap as {result: {...}} or {result: {state, result: {answers}}}
    body = payload.get("result") if isinstance(payload.get("result"), dict) else payload
    if isinstance(body, dict) and "answers" not in body and isinstance(body.get("result"), dict):
        body = body["result"]
    answers = body.get("answers") or {}
    if not isinstance(answers, dict) or not answers:
        return _unavailable(
            status="error",
            note="Jev response missing answers — fail closed to escalate_human",
        )

    action_ans = answers.get("action") or {}
    quality_ans = answers.get("signal_quality") or {}
    safe_ans = answers.get("safe_to_surface") or {}

    raw_choice = action_ans.get("choice")
    action = raw_choice if raw_choice in {"skip", "show", "escalate_human"} else DEFAULT_ACTION

    noul_val = safe_ans.get("noul")
    safe_noul: Optional[float] = None
    if isinstance(noul_val, (int, float)):
        safe_noul = float(noul_val)
    # Fail closed: only surface when noul clearly true
    safe = bool(safe_noul is not None and safe_noul >= NOUL_SURFACE_THRESHOLD)

    # If model says show but not safe, escalate
    if action == "show" and not safe:
        action = DEFAULT_ACTION

    quality: Optional[float] = None
    q_conf: Optional[float] = None
    if isinstance(quality_ans.get("score"), (int, float)):
        quality = float(quality_ans["score"])
    if isinstance(quality_ans.get("confidence"), (int, float)):
        q_conf = float(quality_ans["confidence"])

    a_conf = action_ans.get("confidence")
    a_probs = action_ans.get("probabilities")
    if not isinstance(a_probs, dict):
        a_probs = None

    return JevDecision(
        jev_status="ok",
        action=action,
        signal_quality=quality,
        signal_quality_confidence=q_conf,
        safe_to_surface=safe,
        safe_to_surface_noul=safe_noul,
        action_confidence=float(a_conf) if isinstance(a_conf, (int, float)) else None,
        action_probabilities=a_probs,
        model=body.get("model") or JEV_MODEL,
        recommend_only=True,
        auto_trade=False,
        note=None,
        raw_answers=answers,
        usage=body.get("usage") if isinstance(body.get("usage"), dict) else None,
    )


def credentials_ready() -> bool:
    return bool(settings.cloudflare_account_id and settings.cloudflare_api_token)


def jev_status_summary() -> Dict[str, Any]:
    enabled = settings.jev_gate_enabled
    ready = credentials_ready()
    if not enabled:
        status = "disabled"
    elif not ready:
        status = "unset"
    else:
        status = "configured"
    if not enabled:
        note = "JEV_GATE_ENABLED=false — escalate_human pass-through"
    elif not ready:
        note = "Workers AI typesafe/jev gate; missing creds → escalate_human / unavailable"
    else:
        note = "Workers AI typesafe/jev gate configured; fail-closed on API errors"
    return {
        "enabled": enabled,
        "configured": ready,
        "status": status,
        "model": JEV_MODEL,
        "fail_closed": True,
        "recommend_only": True,
        "auto_trade": False,
        "note": note,
    }


# Short TTL cache: avoid re-hitting Workers AI on every UI poll
_cache: Dict[str, tuple[float, JevDecision]] = {}
_CACHE_TTL_SEC = 60.0
# Negative cache: don't re-hit Workers AI on every UI poll while it errors (e.g. 402 no credits)
_ERR_CACHE_TTL_SEC = 120.0


def _cache_key(sig: SignalLike) -> str:
    return (
        f"{(getattr(sig, 'token_address', '') or '').lower()}:"
        f"{getattr(sig, 'score', 0)}:"
        f"{getattr(sig, 'unique_traders', 0)}:"
        f"{getattr(sig, 'buy_count', 0)}"
    )


def call_workers_ai(
    state: Any,
    questions: Optional[Dict[str, Any]] = None,
    *,
    timeout: float = 30.0,
    client: Optional[httpx.Client] = None,
) -> Dict[str, Any]:
    """POST to Cloudflare Workers AI run endpoint for typesafe/jev."""
    if not credentials_ready():
        raise RuntimeError("CLOUDFLARE_ACCOUNT_ID / CLOUDFLARE_API_TOKEN unset")

    url = WORKERS_AI_RUN_URL.format(account_id=settings.cloudflare_account_id)
    headers = {
        "Authorization": f"Bearer {settings.cloudflare_api_token}",
        "Content-Type": "application/json",
    }
    body = {
        "model": JEV_MODEL,
        "input": {
            "state": state,
            "questions": questions or FOMO_JEV_QUESTIONS,
        },
    }

    owns_client = client is None
    http = client or httpx.Client(timeout=timeout)
    try:
        resp = http.post(url, headers=headers, json=body)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and data.get("success") is False:
            errs = data.get("errors") or data.get("messages") or "Workers AI error"
            raise RuntimeError(f"Workers AI unsuccessful: {errs}")
        return data if isinstance(data, dict) else {"raw": data}
    finally:
        if owns_client:
            http.close()


def evaluate_signal(
    sig: SignalLike,
    *,
    use_cache: bool = True,
    client: Optional[httpx.Client] = None,
    force_unavailable: bool = False,
) -> JevDecision:
    """Run Jev gate on one signal. Fail closed; never invent trades."""
    if not settings.jev_gate_enabled:
        return _unavailable(
            status="disabled",
            note="JEV_GATE_ENABLED=false — pass-through escalate_human",
        )

    if force_unavailable or not credentials_ready():
        return _unavailable(
            status="unavailable",
            note=(
                "Cloudflare Workers AI credentials missing "
                "(CLOUDFLARE_ACCOUNT_ID / CLOUDFLARE_API_TOKEN) — "
                "demo pass-through; fail closed to escalate_human"
            ),
        )

    key = _cache_key(sig)
    if use_cache:
        hit = _cache.get(key)
        if hit:
            ttl = _CACHE_TTL_SEC if hit[1].jev_status == "ok" else _ERR_CACHE_TTL_SEC
            if (time.time() - hit[0]) < ttl:
                return hit[1]

    try:
        state = build_state(sig)
        payload = call_workers_ai(state, client=client)
        decision = parse_answers(payload)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Jev gate soft-fail (fail closed): %s", exc)
        decision = _unavailable(
            status="error",
            note=f"Jev API failure — fail closed: {str(exc)[:240]}",
        )

    if use_cache and decision.jev_status in {"ok", "error"}:
        _cache[key] = (time.time(), decision)
    return decision


def attach_jev(sig: Any, *, use_cache: bool = True) -> Any:
    """Annotate Signal with .jev decision dict. Soft — never raises to callers."""
    try:
        decision = evaluate_signal(sig, use_cache=use_cache)
        sig.jev = decision.to_dict()
        # Keep meta mirror for adapters that only read meta
        if getattr(sig, "meta", None) is None:
            sig.meta = {}
        if isinstance(sig.meta, dict):
            sig.meta["jev"] = {
                "status": decision.jev_status,
                "action": decision.action,
                "safe_to_surface": decision.safe_to_surface,
            }
    except Exception as exc:  # noqa: BLE001
        logger.warning("attach_jev unexpected soft-fail: %s", exc)
        sig.jev = _unavailable(
            status="error",
            note=f"attach_jev soft-fail: {str(exc)[:200]}",
        ).to_dict()
    return sig


def clear_jev_cache() -> None:
    _cache.clear()
