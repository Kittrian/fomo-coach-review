"""POST /api/exec/quote and /api/exec/swap — Jupiter confirm-to-execute (dry by default)."""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.config import settings
from app.live.auth import require_live_token, require_localhost
from app.jupiter_exec import (
    DEFAULT_BUY_AMOUNT_LAMPORTS,
    SOL_MINT,
    USDC_MINT,
    build_quote_response,
    fetch_order,
    resolve_mints,
    resolve_taker,
    run_swap_flow,
)

router = APIRouter(prefix="/api/exec", tags=["jupiter-exec"])


class ExecQuoteBody(BaseModel):
    """Quote a Jupiter swap. Prefer mint+side from the coach UI, or explicit mints."""

    mint: Optional[str] = Field(None, description="Token mint (with side=buy|sell)")
    side: Optional[str] = Field(None, description="buy|sell relative to mint")
    input_mint: Optional[str] = None
    output_mint: Optional[str] = None
    amount: Optional[str] = Field(
        None,
        description="Atomic units of input mint (default 0.01 SOL lamports for buys)",
    )
    taker: Optional[str] = Field(
        None, description="Wallet pubkey — required for assembled unsigned tx"
    )
    quote_mint: str = Field(
        SOL_MINT, description="Counter asset for mint+side (SOL or USDC mint)"
    )


class ExecSwapBody(ExecQuoteBody):
    """Swap path: dry-run unless JUPITER_EXEC_ENABLED + confirm + signed."""

    confirm: bool = Field(
        False,
        description="Must be true to call Jupiter /execute when ENABLE is on",
    )
    signed_transaction: Optional[str] = Field(
        None, description="Base64 signed versioned tx (Phantom / local)"
    )
    request_id: Optional[str] = Field(
        None, description="From prior /order quote when posting a client-signed tx"
    )


def _resolve_or_400(body: ExecQuoteBody) -> tuple[str, str, str]:
    try:
        inp, out = resolve_mints(
            mint=body.mint,
            side=body.side,
            input_mint=body.input_mint,
            output_mint=body.output_mint,
            quote_mint=body.quote_mint or SOL_MINT,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    amount = (body.amount or "").strip()
    if not amount:
        # Default only for SOL→token buys; sells need an explicit token amount
        if body.side and body.side.lower() == "buy":
            amount = DEFAULT_BUY_AMOUNT_LAMPORTS
        elif body.input_mint == SOL_MINT or inp == SOL_MINT:
            amount = DEFAULT_BUY_AMOUNT_LAMPORTS
        else:
            raise HTTPException(
                status_code=400,
                detail="amount required (atomic units of input mint) for sells / non-SOL input",
            )
    return inp, out, amount


@router.post("/quote")
def exec_quote(body: ExecQuoteBody) -> Dict[str, Any]:
    """Always dry — Jupiter /order quote (+ unsigned tx if taker set)."""
    inp, out, amount = _resolve_or_400(body)
    taker = resolve_taker(body.taker)
    order_wrap = fetch_order(
        input_mint=inp,
        output_mint=out,
        amount=amount,
        taker=taker,
    )
    return build_quote_response(
        order_wrap,
        input_mint=inp,
        output_mint=out,
        amount=amount,
        taker=taker,
        side=body.side,
        mint=body.mint,
    )


@router.post("/swap", dependencies=[Depends(require_localhost), Depends(require_live_token)])
def exec_swap(body: ExecSwapBody) -> Dict[str, Any]:
    """Dry-run unless JUPITER_EXEC_ENABLED=true AND confirm=true AND signed. v1.14: loopback + X-Live-Token,
    SOL-input amount capped by LIVE_MAX_SOL_PER_TRADE."""
    inp, out, amount = _resolve_or_400(body)
    from app.live.config import config as live_config
    cap = int(live_config()["max_sol_per_trade"] * 1e9)
    if inp == SOL_MINT and int(amount) > cap:
        raise HTTPException(status_code=400, detail=f"amount exceeds LIVE_MAX_SOL_PER_TRADE ({cap} lamports)")
    result = run_swap_flow(
        input_mint=inp,
        output_mint=out,
        amount=amount,
        taker=body.taker,
        confirm=bool(body.confirm),
        signed_transaction=body.signed_transaction,
        request_id=body.request_id,
    )
    # Product lock reminder on every response
    result["recommend_only"] = True
    result["auto_trade"] = False
    result["jupiter_exec_enabled"] = settings.jupiter_exec_enabled
    return result


@router.get("/meta")
def exec_meta() -> Dict[str, Any]:
    """UI helper — known quote mints + safety flags (no secrets)."""
    from app.jupiter_exec import jupiter_status_summary

    from app.jupiter_exec import watch_wallet_pubkey

    w = watch_wallet_pubkey()
    return {
        "ok": True,
        "sol_mint": SOL_MINT,
        "usdc_mint": USDC_MINT,
        "default_buy_amount_lamports": DEFAULT_BUY_AMOUNT_LAMPORTS,
        "watch_wallet": (w[:4] + "…" + w[-4:]) if w else None,
        "watch_wallet_full": w,  # public address — safe to show
        "jupiter_exec": jupiter_status_summary(),
    }
