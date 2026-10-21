"""v1.20 direct pump.fun bonding-curve trades (no Jupiter) — `buy_exact_quote_in_v3` / `sell_v3`.

Sky, 2026-10-08 5:58 PM CT: "buy directly from pump.fun". For coins still on the bonding curve we build the official
pump program instruction ourselves (pump-public-docs TRADE_V3.md + idl/pump.json, 17 accounts, verified against real
mainnet txs in tests/fixtures/pump_v3_mainnet.json), sign locally with the existing keypair loader, send through our
own RPC (Helius) and confirm. Anything else → the existing Jupiter path, unchanged.

Modes (LIVE_DIRECT_PUMP_ENABLED=false → OFF, nothing here runs):
  shadow (default)  live trades still go through Jupiter. For each live buy/sell we fetch the curve, build the direct tx
                    UNSIGNED (default signature) and simulateTransaction it (sigVerify off, replaceRecentBlockhash) →
                    what it would have filled + how long it took. Nothing signed ever leaves the box; nothing is sent.
  live              bonding-curve coins use the direct path; anything not eligible, or any failure BEFORE sending, falls
                    back to Jupiter. After a send the outcome is never retried through Jupiter (no double-buy): an
                    unknown outcome returns `ambiguous` and the engine's existing on-chain reconcile takes over.

Why buy_exact_quote_in_v3: it spends exactly the configured SOL (0.20) and enforces `min_tokens_out`, the token-side
equivalent of max_sol_cost; sell_v3 enforces `min_sol_output`. v3 keeps the protocol/creator fee on the curve (no
fee-recipient / creator-vault accounts → 17 accounts, fewer things to get wrong). Cashback coins don't support v3
(error 6094) → ineligible → Jupiter.
Key material: only `jupiter.keypair()` (in memory); never logged. RPC URLs may embed API keys: never logged.
"""
from __future__ import annotations

import asyncio
import base64
import collections
import json
import logging
import os
import statistics
import struct
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

from app.paths import data_dir
from app.live import fees as _fees   # v1.38 dynamic priority floor + capped tip

logger = logging.getLogger("coach.live.pumpdirect")

PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMP_FEES_PROGRAM = "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ"
PUMP_GLOBAL = "4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf"
FEE_CONFIG = "8Wf5TiAheLUqBrKXeYg2JtAFFMWtKdG2BSFgqUcPVwTt"          # ["fee_config", pump] under pump_fees
EVENT_AUTHORITY = "Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1"     # ["__event_authority"] under pump
WSOL = "So11111111111111111111111111111111111111112"
TOKEN_PROGRAM = "REDACTED"
TOKEN_2022_PROGRAM = "REDACTED"
ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
SYSTEM_PROGRAM = "11111111111111111111111111111111"
# pump-public-docs FEE_RECIPIENTS.md "Buyback fee recipients" (also Global.buyback_fee_recipients, offset 741)
BUYBACK_FEE_RECIPIENTS = (
    "5YxQFdt3Tr9zJLvkFccqXVUwhdTWJQc1fFg2YPbxvxeD", "9M4giFFMxmFGXtc3feFzRai56WbBqehoSeRE5GK7gf7",
    "GXPFM2caqTtQYC2cJ5yJRi9VDkpsYZXzYdwYpGnLmtDL", "3BpXnfJaUTiwXnJNe7Ej1rcbzqTTQUvLShZaWazebsVR",
    "5cjcW9wExnJJiqgLjq7DEG75Pm6JBgE1hNv4B2vHXUW6", "EHAAiTxcdDwQ3U4bU6YcMsQGaekdzLS3B5SmYo46kJtL",
    "5eHhjP8JaYkz83CWwvGU2uMUXefd3AazWGx4gpcuEEYD", "A7hAgCzFw14fejgCp387JUJRMNyz4j89JKnhtKU8piqW")
DISC = {"buy_exact_quote_in_v3": bytes([225, 247, 80, 30, 213, 179, 132, 136]),
        "sell_v3": bytes([28, 146, 222, 119, 38, 196, 105, 213])}
TOKEN_ACCOUNT_AMOUNT_OFFSET = 64
DEFAULT_PROTOCOL_BPS, DEFAULT_CREATOR_BPS = 95, 30                  # fallback only (fee_config read normally)


# ---------------- config ----------------
def _f(name: str, d: float) -> float:
    try:
        return float(os.getenv(name, str(d)).strip())
    except ValueError:
        return d


def cfg() -> Dict[str, Any]:
    mode = os.getenv("LIVE_DIRECT_PUMP_MODE", "shadow").strip().lower()
    return {
        "enabled": os.getenv("LIVE_DIRECT_PUMP_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"},
        "mode": mode if mode in ("shadow", "live") else "shadow",
        # v1.36 (Sky approved Oct 17, 2026 9:39 PM CT): live trial on these arms / sides only while the global mode is
        # shadow. Every other arm and side stays on Jupiter (shadow = build + simulate only).
        "live_arms": [a.strip() for a in os.getenv("LIVE_DIRECT_PUMP_LIVE_ARMS", "").split(",") if a.strip()],
        "live_sides": [s.strip().lower() for s in os.getenv("LIVE_DIRECT_PUMP_LIVE_SIDES", "buy").split(",")
                       if s.strip().lower() in ("buy", "sell")],
        # 15%: same as the live Jupiter buy cap (LIVE_BUY_SLIPPAGE_BPS=1500) so the direct path never accepts a worse
        # fill than today; the curve quote comes from reserves read ~0.1 s before signing, so most of the band is
        # protection against other buyers landing first in the same/next slot on a fast pump.
        "slippage_pct": max(0.0, min(0.5, _f("LIVE_DIRECT_SLIPPAGE_PCT", 15) / 100)),
        "sell_slippage_max_pct": max(0.0, min(0.9, _f("LIVE_SELL_SLIPPAGE_MAX_BPS", 5000) / 10_000)),
        "cu_limit": int(_f("LIVE_DIRECT_CU_LIMIT", 120_000)),     # real buy sim incl. ATA create: 75.6k CU
        "priority_lamports": int(_f("LIVE_DIRECT_PRIORITY_LAMPORTS", 50_000)),
        "max_priority_lamports": int(_f("LIVE_MAX_PRIORITY_LAMPORTS", 2_000_000)),
        "simulate": os.getenv("LIVE_DIRECT_SIMULATE", "true").strip().lower() in {"1", "true", "yes", "on"},
        "confirm_timeout_s": _f("LIVE_DIRECT_CONFIRM_TIMEOUT_S", 30),
        "sell_confirm_timeout_s": _f("LIVE_DIRECT_SELL_CONFIRM_TIMEOUT_S", 30),   # v1.40e: then Jupiter fallback
        "rebroadcast_s": _f("LIVE_DIRECT_REBROADCAST_S", 2.0),
        "confirm_poll_s": max(0.1, _f("LIVE_DIRECT_CONFIRM_POLL_S", 0.4)),      # v1.25
        # v1.26 Helius Sender for direct buys AND sells (needs SIGNER_BACKEND=socket: the tip accounts + tip cap come
        # from fomo-signer, which is the only place they are pinned). Off → unchanged plain RPC send.
        "sender_enabled": os.getenv("LIVE_SENDER_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"},
        "sender_tip_lamports": int(_f("LIVE_SENDER_TIP_LAMPORTS", 5_000)),
        "sender_url": os.getenv("LIVE_SENDER_URL", "").strip() or "http://ewr-sender.helius-rpc.com/fast?swqos_only=true",
        "sender_use_rpc_key": os.getenv("LIVE_SENDER_USE_RPC_KEY", "true").strip().lower() in {"1", "true", "yes", "on"},
        "sender_also_rpc": os.getenv("LIVE_SENDER_ALSO_RPC", "true").strip().lower() in {"1", "true", "yes", "on"},
        "sender_timeout_s": _f("LIVE_SENDER_TIMEOUT_S", 2.0),
        "rpc_url": os.getenv("LIVE_DIRECT_RPC_URL", "").strip(),     # empty → SOLANA_RPC_URL (Helius)
        "log_path": Path(os.getenv("LIVE_DIRECT_LOG_PATH", str(data_dir() / "direct_pump.jsonl"))),
        # go-live evidence thresholds (shadow summary)
        "golive_min_buys": int(_f("LIVE_DIRECT_GOLIVE_MIN_BUYS", 20)),
        "golive_min_sells": int(_f("LIVE_DIRECT_GOLIVE_MIN_SELLS", 10)),
        "golive_min_sim_ok": _f("LIVE_DIRECT_GOLIVE_MIN_SIM_OK", 0.95),
        # v1.45 better fills. price_bound: min_tokens_out also >= SOL in / (whale fill x (1 + chase% + slack%)) so a
        # late buy FAILS on-chain instead of filling high. whale_cu: buy CU price >= the whale's (capped).
        "price_bound": os.getenv("LIVE_DIRECT_PRICE_BOUND", "false").strip().lower() in {"1", "true", "yes", "on"},
        "price_bound_chase_pct": _f("LIVE_MAX_CHASE_PCT", 15.0),
        "price_bound_slack_pct": _f("LIVE_DIRECT_PRICE_BOUND_SLACK_PCT", 0.5),
        "whale_cu": os.getenv("LIVE_MATCH_WHALE_CU", "false").strip().lower() in {"1", "true", "yes", "on"},
        "whale_cu_max_micro": int(_f("LIVE_WHALE_CU_MAX_MICROLAMPORTS", 2_000_000)),
        "whale_cu_max_age_s": _f("LIVE_WHALE_CU_MAX_AGE_S", 30.0),
    }


def mode() -> str:
    c = cfg()
    return c["mode"] if c["enabled"] else "off"


def mode_for(side: str, arm: Optional[str]) -> str:
    """v1.36: global live → live everywhere (old behaviour). Global shadow + arm in LIVE_DIRECT_PUMP_LIVE_ARMS + side
    in LIVE_DIRECT_PUMP_LIVE_SIDES → live for that trade only; everything else shadow (Jupiter)."""
    c = cfg()
    if not c["enabled"]:
        return "off"
    if c["mode"] == "live":
        return "live"
    if arm and arm in c["live_arms"] and side in c["live_sides"]:
        return "live"
    return "shadow"


def _rpc_url() -> str:
    from app.pump import rpc as _rpc_mod
    return cfg()["rpc_url"] or _rpc_mod.rpc_url()


# ---------------- PDAs / accounts ----------------
def _pk(s: str):
    from solders.pubkey import Pubkey  # type: ignore
    return Pubkey.from_string(s)


def pda(seeds: List[bytes], program: str) -> str:
    from solders.pubkey import Pubkey  # type: ignore
    return str(Pubkey.find_program_address(seeds, _pk(program))[0])


def ata(owner: str, mint: str, token_program: str) -> str:
    return pda([bytes(_pk(owner)), bytes(_pk(token_program)), bytes(_pk(mint))], ATA_PROGRAM)


def bonding_curve(mint: str) -> str:
    return pda([b"bonding-curve", bytes(_pk(mint))], PUMP_PROGRAM)


def user_volume_accumulator(user: str) -> str:
    return pda([b"user_volume_accumulator", bytes(_pk(user))], PUMP_PROGRAM)


def v3_accounts(mint: str, user: str, base_token_program: str, buyback: str) -> List[Tuple[str, bool, bool]]:
    """[(pubkey, is_signer, is_writable)] in TRADE_V3.md order (same for buy_v3 / buy_exact_quote_in_v3 / sell_v3)."""
    bc = bonding_curve(mint)
    return [
        (PUMP_GLOBAL, False, False),
        (mint, False, False),
        (WSOL, False, False),                                          # quote_mint (SOL-paired → wrapped SOL)
        (base_token_program, False, False),
        (TOKEN_PROGRAM, False, False),                                 # quote_token_program (WSOL → SPL Token)
        (bc, False, True),
        (ata(bc, mint, base_token_program), False, True),
        (ata(bc, WSOL, TOKEN_PROGRAM), False, True),                   # unused for SOL pairs, derived address
        (user, True, True),
        (ata(user, mint, base_token_program), False, True),
        (ata(user, WSOL, TOKEN_PROGRAM), False, True),                 # unused for SOL pairs, derived address
        (user_volume_accumulator(user), False, True),
        (FEE_CONFIG, False, False),
        (buyback, False, True),
        (SYSTEM_PROGRAM, False, False),
        (EVENT_AUTHORITY, False, False),
        (PUMP_PROGRAM, False, False),
    ]


def buy_data(spendable_lamports: int, min_tokens_out: int) -> bytes:
    return DISC["buy_exact_quote_in_v3"] + struct.pack("<QQ", int(spendable_lamports), int(min_tokens_out)) + b"\x00"


def sell_data(amount: int, min_sol_output: int) -> bytes:
    return DISC["sell_v3"] + struct.pack("<QQ", int(amount), int(min_sol_output))


def _ix(program: str, metas: List[Tuple[str, bool, bool]], data: bytes):
    from solders.instruction import AccountMeta, Instruction  # type: ignore
    return Instruction(_pk(program), data, [AccountMeta(_pk(k), s, w) for k, s, w in metas])


def ata_create_idempotent_ix(payer: str, owner: str, mint: str, token_program: str):
    return _ix(ATA_PROGRAM, [(payer, True, True), (ata(owner, mint, token_program), False, True), (owner, False, False),
                             (mint, False, False), (SYSTEM_PROGRAM, False, False), (token_program, False, False)], b"\x01")


def compute_ixs(cu_limit: int, priority_lamports: int) -> list:
    from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price  # type: ignore
    micro = int(priority_lamports) * 1_000_000 // max(1, int(cu_limit))
    return [set_compute_unit_limit(int(cu_limit)), set_compute_unit_price(int(micro))]


def pick_buyback(mint: str) -> str:
    return BUYBACK_FEE_RECIPIENTS[bytes(_pk(mint))[0] % len(BUYBACK_FEE_RECIPIENTS)]  # spread, deterministic


# ---------------- fees / quotes ----------------
def decode_fee_config(data: bytes) -> Dict[str, Any]:
    """FeeConfig: disc8, bump u8, admin 32, flat_fees(3×u64), fee_tiers vec<(u128, 3×u64)>, …"""
    o = 8 + 1 + 32
    flat = struct.unpack_from("<QQQ", data, o)
    o += 24
    n = struct.unpack_from("<I", data, o)[0]
    o += 4
    tiers = []
    for _ in range(min(n, 64)):
        thr = int.from_bytes(data[o:o + 16], "little")
        lp, proto, creator = struct.unpack_from("<QQQ", data, o + 16)
        tiers.append({"threshold": thr, "lp": lp, "protocol": proto, "creator": creator})
        o += 40
    return {"flat": {"lp": flat[0], "protocol": flat[1], "creator": flat[2]}, "tiers": tiers}


def market_cap_lamports(curve: Dict[str, Any]) -> int:
    vtr = int(curve.get("virtual_token_reserves") or 0)
    return int(curve.get("virtual_quote_reserves") or 0) * int(curve.get("token_total_supply") or 0) // vtr if vtr else 0


def fee_bps(curve: Dict[str, Any], fee_cfg: Optional[Dict[str, Any]]) -> Dict[str, int]:
    """pump FEE_PROGRAM_README computeFeesBps/calculateFeeTier: tier by market cap; first tier below threshold."""
    if fee_cfg and fee_cfg.get("tiers"):
        tiers = fee_cfg["tiers"]
        mc = market_cap_lamports(curve)
        chosen = tiers[0]
        if mc >= tiers[0]["threshold"]:
            for t in reversed(tiers):
                if mc >= t["threshold"]:
                    chosen = t
                    break
        proto, creator = chosen["protocol"], chosen["creator"]
    else:
        proto, creator = DEFAULT_PROTOCOL_BPS, DEFAULT_CREATOR_BPS
    if int(curve.get("creator_fee_bps") or 0) > 0:                     # custom-pair coins only (0 for SOL pairs)
        creator = int(curve["creator_fee_bps"])
    return {"protocol": int(proto), "creator": int(creator), "total": int(proto) + int(creator)}


def quote_buy(curve: Dict[str, Any], sol_in: int, total_fee_bps: int) -> int:
    """Tokens out for an exact SOL-in buy (fee taken from the input), capped at the curve's real tokens."""
    vtr, vqr = int(curve["virtual_token_reserves"]), int(curve["virtual_quote_reserves"])
    net = int(sol_in) * 10_000 // (10_000 + int(total_fee_bps))
    if net <= 0 or vtr <= 0:
        return 0
    out = net * vtr // (vqr + net)
    return max(0, min(out, int(curve.get("real_token_reserves") or out)))


def quote_sell(curve: Dict[str, Any], tokens: int, total_fee_bps: int) -> int:
    """Lamports out after fees for selling `tokens` into the curve."""
    vtr, vqr = int(curve["virtual_token_reserves"]), int(curve["virtual_quote_reserves"])
    if tokens <= 0 or vtr <= 0:
        return 0
    gross = int(tokens) * vqr // (vtr + int(tokens))
    gross = min(gross, int(curve.get("real_quote_reserves") or gross))
    fee = -(-gross * int(total_fee_bps) // 10_000)                     # ceil
    return max(0, gross - fee)


def min_out(expected: int, slippage_pct: float) -> int:
    return max(0, int(int(expected) * (1.0 - float(slippage_pct))))


# ---------------- RPC ----------------
class RpcRefused(Exception):
    """The RPC answered with an HTTP 4xx / JSON-RPC error (nothing accepted)."""


class SenderError(Exception):
    """Sender call failed. definite=True → Sender answered with an explicit refusal (nothing accepted)."""

    def __init__(self, msg: str, definite: bool):
        super().__init__(msg)
        self.definite = definite


_tips_cache: Dict[str, Any] = {"t": 0.0, "v": None}


async def sender_tip_info() -> Optional[Dict[str, Any]]:
    """Tip accounts + cap from fomo-signer (op sender_tips), cached 10 min. None → Sender not usable (plain RPC)."""
    from app.live import signer_client
    if not signer_client.enabled():
        return None
    if _tips_cache["v"] and time.time() - _tips_cache["t"] < 600:
        return _tips_cache["v"]
    try:
        r = await signer_client.call({"op": "sender_tips"}, timeout=3.0)
    except Exception:  # noqa: BLE001
        return None
    if not (r.get("ok") and r.get("accounts") and int(r.get("max_tip_lamports") or 0) > 0):
        return None
    _tips_cache.update(t=time.time(), v={"accounts": list(r["accounts"]), "max": int(r["max_tip_lamports"])})
    return _tips_cache["v"]


def _sender_url(c: Dict[str, Any]) -> str:
    url = c["sender_url"]
    if c["sender_use_rpc_key"] and "api-key=" not in url:
        from urllib.parse import parse_qs, urlparse
        key = (parse_qs(urlparse(_rpc_url()).query).get("api-key") or [""])[0]
        if key:
            url += ("&" if "?" in url else "?") + "api-key=" + key
    return url


# v1.43: blockhash keep-warm. LIVE_BH_CACHE=false → old per-trade getLatestBlockhash. A cached hash older than
# LIVE_BH_CACHE_MAX_S (default 8 s; a blockhash is valid ~60 s) is never used → fetch fallback.
_BH: Dict[str, Any] = {"v": None, "t": 0.0, "task": None, "hits": 0, "miss": 0}


def _bh_on() -> bool:
    return os.getenv("LIVE_BH_CACHE", "true").strip().lower() in {"1", "true", "yes", "on"}


def _bh_store(v: Optional[str]) -> None:
    if v:
        _BH["v"], _BH["t"] = v, time.time()


def _bh_cached() -> Optional[str]:
    if not _bh_on():
        return None
    _bh_ensure()
    if _BH["v"] and time.time() - _BH["t"] < _f("LIVE_BH_CACHE_MAX_S", 8.0):
        _BH["hits"] += 1
        return _BH["v"]
    _BH["miss"] += 1
    return None


async def _bh_loop() -> None:
    async with httpx.AsyncClient(timeout=5.0) as cl:
        while _bh_on():
            try:
                r = await _rpc("getLatestBlockhash", [{"commitment": "confirmed"}], cl, timeout=3.0)
                _bh_store(((r or {}).get("value") or {}).get("blockhash"))
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(_f("LIVE_BH_REFRESH_S", 1.0))
    _BH["task"] = None


def _bh_ensure() -> None:
    t = _BH.get("task")
    if t is None or t.done():
        try:
            _BH["task"] = asyncio.get_running_loop().create_task(_bh_loop())
        except RuntimeError:
            pass


def bh_status() -> Dict[str, Any]:
    return {"on": _bh_on(), "age_s": round(time.time() - _BH["t"], 2) if _BH["v"] else None,
            "hits": _BH["hits"], "miss": _BH["miss"]}


# v1.43: signatureSubscribe confirm wake-up (LIVE_WS_CONFIRM=false → polling only). The websocket only WAKES the
# poll loop early; getSignatureStatuses stays the single source of truth for landed / err.
def _ws_url() -> str:
    key = os.getenv("HELIUS_API_KEY", "").strip()
    if not key or os.getenv("LIVE_WS_CONFIRM", "true").strip().lower() not in {"1", "true", "yes", "on"}:
        return ""
    return os.getenv("LIVE_WS_CONFIRM_BASE", "wss://mainnet.helius-rpc.com/").strip() + "?api-key=" + key


async def _ws_watch(sig: str, ev: "asyncio.Event", tm: Dict[str, Any]) -> None:
    url = _ws_url()
    if not url:
        return
    t0 = time.perf_counter()
    try:
        import websockets
        async with websockets.connect(url, open_timeout=2.0, ping_interval=None, max_size=2 ** 16) as ws:
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "signatureSubscribe",
                                      "params": [sig, {"commitment": "confirmed"}]}))
            tm["ws_sub_ms"] = _ms(t0)
            while True:
                m = json.loads(await ws.recv())
                if m.get("method") == "signatureNotification":
                    tm["ws_notify_ms"] = _ms(t0)
                    ev.set()
                    return
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001   never log the url (key)
        tm["ws_error"] = type(exc).__name__


async def _sender_send(signed_b64: str, cl: httpx.AsyncClient, c: Dict[str, Any]) -> None:
    body = {"jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
            "params": [signed_b64, {"encoding": "base64", "skipPreflight": True, "maxRetries": 0}]}
    try:
        r = await cl.post(_sender_url(c), json=body, timeout=c["sender_timeout_s"])
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        raise SenderError(f"sender connect {type(exc).__name__}", definite=True) from None   # request never left
    except Exception as exc:  # noqa: BLE001   read timeout etc.: may have been accepted
        raise SenderError(f"sender {type(exc).__name__}", definite=False) from None
    if r.status_code != 200:
        raise SenderError(f"sender http {r.status_code}", definite=400 <= r.status_code < 500)
    try:
        j = r.json()
    except Exception:  # noqa: BLE001
        raise SenderError("sender bad json", definite=False) from None
    if "error" in j:
        raise SenderError(f"sender error {str(j['error'])[:120]}", definite=True)


async def _rpc(method: str, params: List[Any], client: httpx.AsyncClient, timeout: float = 6.0) -> Any:
    from app.pump import rpc as _rm
    _u = _rm.gk_url(_rpc_url())
    _fb = _rm.gk_fallback(_u)
    _b = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    try:
        r = await client.post(_u, json=_b, timeout=timeout)
        if _fb and (r.status_code >= 500 or r.status_code in (403, 404, 429)):
            raise httpx.HTTPError(f"gatekeeper http {r.status_code}")
        if _fb:
            _rm.gk_ok()
    except httpx.HTTPError:
        if not _fb:
            raise
        _rm.gk_failed()
        r = await client.post(_fb, json=_b, timeout=timeout)
    if 400 <= r.status_code < 500:
        raise RpcRefused(f"{method} http {r.status_code}")
    if r.status_code != 200:
        raise RuntimeError(f"{method} http {r.status_code}")
    j = r.json()
    if "error" in j:
        raise RpcRefused(f"{method} error {str(j['error'])[:140]}")
    return j.get("result")


_fee_cache: Dict[str, Any] = {"t": 0.0, "v": None}


async def _fee_config(client: httpx.AsyncClient) -> Optional[Dict[str, Any]]:
    if _fee_cache["v"] is not None and time.time() - _fee_cache["t"] < 300:
        return _fee_cache["v"]
    try:
        res = await _rpc("getAccountInfo", [FEE_CONFIG, {"encoding": "base64", "commitment": "confirmed"}], client)
        raw = base64.b64decode(((res or {}).get("value") or {}).get("data", [""])[0])
        _fee_cache.update(t=time.time(), v=decode_fee_config(raw))
    except Exception as exc:  # noqa: BLE001
        logger.info("pumpdirect fee_config read failed (%s) — default fees", type(exc).__name__)
    return _fee_cache["v"]


async def fetch_state(mint: str, user: str, client: httpx.AsyncClient, *, blockhash: bool = True) -> Dict[str, Any]:
    """One getMultipleAccounts [curve, mint, user ATA (both token programs), user] + blockhash + fee config."""
    from app.pump.curve import decode_curve
    bc = bonding_curve(mint)
    keys = [bc, mint, ata(user, mint, TOKEN_PROGRAM), ata(user, mint, TOKEN_2022_PROGRAM), user]
    acc_t = _rpc("getMultipleAccounts", [keys, {"encoding": "base64", "commitment": "processed"}], client)
    cached_bh = _bh_cached() if blockhash else None                 # v1.43: warm blockhash, no fetch on the hot path
    bh_t = (asyncio.sleep(0, {"value": {"blockhash": cached_bh}}) if cached_bh else
            _rpc("getLatestBlockhash", [{"commitment": "confirmed"}], client) if blockhash else asyncio.sleep(0, None))
    res, bh, fees = await asyncio.gather(acc_t, bh_t, _fee_config(client))
    if blockhash and not cached_bh:
        _bh_store(((bh or {}).get("value") or {}).get("blockhash"))
    vals = (res or {}).get("value") or [None] * len(keys)

    def dec(v):
        if not v:
            return None
        d = v.get("data")
        return {"owner": v.get("owner"), "lamports": v.get("lamports"),
                "data": base64.b64decode(d[0]) if isinstance(d, list) and d else b""}
    cur_a, mint_a, ata_a, ata22_a, user_a = (dec(v) for v in vals)
    out: Dict[str, Any] = {"bonding_curve": bc, "fee_config": fees, "slot": ((res or {}).get("context") or {}).get("slot"),
                           "blockhash": ((bh or {}).get("value") or {}).get("blockhash") if blockhash else None,
                           "user_lamports": (user_a or {}).get("lamports")}
    out["curve_owner"] = (cur_a or {}).get("owner")
    try:
        out["curve"] = decode_curve(cur_a["data"]) if cur_a and cur_a.get("owner") == PUMP_PROGRAM else None
    except Exception:  # noqa: BLE001
        out["curve"] = None
    out["token_program"] = (mint_a or {}).get("owner")
    ta = ata22_a if out["token_program"] == TOKEN_2022_PROGRAM else ata_a
    out["user_ata_exists"] = ta is not None
    out["user_tokens"] = struct.unpack_from("<Q", ta["data"], TOKEN_ACCOUNT_AMOUNT_OFFSET)[0] \
        if ta and len(ta.get("data") or b"") >= 72 else 0
    return out


def eligibility(st: Dict[str, Any], side: str, amount: int) -> Tuple[bool, str]:
    c = st.get("curve")
    if not c:
        return False, "no pump bonding curve (PumpSwap / other venue)"
    if c.get("complete"):
        return False, "curve complete (graduated / migrating)"
    if not c.get("quote_is_sol", True):
        return False, "non-SOL quote coin"
    if c.get("is_cashback_coin"):
        return False, "cashback coin (v3 unsupported)"
    if st.get("token_program") not in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
        return False, f"unknown token program {str(st.get('token_program'))[:8]}"
    if side == "sell" and int(st.get("user_tokens") or 0) <= 0:
        return False, "no token balance to sell"
    return True, "ok"


# ---------------- build ----------------
def price_bound_min_tokens(sol_in: int, ref_price_sol: Optional[float], chase_pct: float, slack_pct: float,
                           decimals: int = 6) -> int:
    """v1.45: fewest tokens we accept for `sol_in` lamports so our gross price <= whale fill x (1+chase+slack)."""
    if not ref_price_sol or ref_price_sol <= 0 or sol_in <= 0 or chase_pct <= 0:
        return 0
    max_px = float(ref_price_sol) * (1.0 + (float(chase_pct) + max(0.0, float(slack_pct))) / 100.0)
    return max(0, int((int(sol_in) / 1e9) / max_px * (10 ** int(decimals))))


def build(side: str, mint: str, user: str, amount: int, st: Dict[str, Any], *, slippage_pct: float,
          cu_limit: int, priority_lamports: int, price_floor: int = 0) -> Dict[str, Any]:
    """Pure: instructions + quote. side='buy' amount=lamports in; side='sell' amount=tokens."""
    curve, token_program = st["curve"], st["token_program"]
    fees = fee_bps(curve, st.get("fee_config"))
    buyback = pick_buyback(mint)
    metas = v3_accounts(mint, user, token_program, buyback)
    ixs = compute_ixs(cu_limit, priority_lamports)
    if side == "buy":
        expected = quote_buy(curve, amount, fees["total"])
        floor = min_out(expected, slippage_pct)
        if floor <= 0:
            raise ValueError("buy quote is 0")
        if price_floor and price_floor > floor:           # v1.45: whale-price bound is tighter than the slippage band
            floor = int(price_floor)
        ixs.append(ata_create_idempotent_ix(user, user, mint, token_program))
        ixs.append(_ix(PUMP_PROGRAM, metas, buy_data(amount, floor)))
    else:
        expected = quote_sell(curve, amount, fees["total"])
        floor = min_out(expected, slippage_pct)
        ixs.append(_ix(PUMP_PROGRAM, metas, sell_data(amount, floor)))
    return {"ixs": ixs, "expected_out": expected, "min_out": floor, "fees_bps": fees, "buyback": buyback,
            "market_cap_lamports": market_cap_lamports(curve)}


def compile_tx(ixs: list, user: str, blockhash: Optional[str], *, sign: bool):
    """→ VersionedTransaction (v0). sign=False → default (all-zero) signature: cannot execute anywhere."""
    from solders.hash import Hash  # type: ignore
    from solders.message import MessageV0  # type: ignore
    from solders.signature import Signature  # type: ignore
    from solders.transaction import VersionedTransaction  # type: ignore
    bh = Hash.from_string(blockhash) if blockhash else Hash.default()
    msg = MessageV0.try_compile(_pk(user), ixs, [], bh)
    if sign:
        from app.live.jupiter import keypair
        return VersionedTransaction(msg, [keypair()])
    return VersionedTransaction.populate(msg, [Signature.default()])


async def sign_compiled(p: Dict[str, Any], side: str, mint: str, amount: int):
    """v1.24: local sign (default) or fomo-signer with the stated intent (SIGNER_BACKEND=socket)."""
    from app.live import signer_client
    if not signer_client.enabled():
        return compile_tx(p["ixs"], p["user"], p["state"]["blockhash"], sign=True)
    from solders.transaction import VersionedTransaction  # type: ignore
    tx = compile_tx(p["ixs"], p["user"], p["state"]["blockhash"], sign=False)
    signed = await signer_client.sign_tx_b64(_b64(tx), direct_intent(p, side, mint, amount))
    return VersionedTransaction.from_bytes(base64.b64decode(signed))


def direct_intent(p: Dict[str, Any], side: str, mint: str, amount: int) -> Dict[str, Any]:
    WSOL = "So11111111111111111111111111111111111111112"
    return {"kind": side, "input_mint": WSOL if side == "buy" else mint, "output_mint": mint if side == "buy" else WSOL,
            "amount": int(amount if side == "buy" else p["amount"]), "max_priority_lamports": int(p["priority_lamports"]),
            "router": "pump_direct", "tag": "pump_direct", "tip_lamports": int(p.get("tip_lamports") or 0)}


def _b64(tx) -> str:
    return base64.b64encode(bytes(tx)).decode("ascii")


async def simulate(tx_b64: str, client: httpx.AsyncClient, *, signed: bool, watch: List[str],
                   keep_blockhash: bool = False) -> Dict[str, Any]:
    params = [tx_b64, {"encoding": "base64", "sigVerify": bool(signed),
                       "replaceRecentBlockhash": not signed and not keep_blockhash,
                       "commitment": "processed", "accounts": {"encoding": "base64", "addresses": watch}}]
    try:
        res = await _rpc("simulateTransaction", params, client, timeout=8.0)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "transport_error": True, "err": f"simulate {type(exc).__name__}"}
    v = (res or {}).get("value") or {}
    if not v:
        return {"ok": False, "transport_error": True, "err": "simulate: empty result"}
    accs = []
    for a in v.get("accounts") or []:
        if not a:
            accs.append(None)
            continue
        d = a.get("data")
        raw = base64.b64decode(d[0]) if isinstance(d, list) and d else b""
        accs.append({"lamports": a.get("lamports"), "data": raw})
    logs = v.get("logs") or []
    return {"ok": v.get("err") is None, "transport_error": False, "err": v.get("err"), "units": v.get("unitsConsumed"),
            "accounts": accs, "logs_tail": [str(x)[:160] for x in logs[-3:]]}


def _sim_fill(side: str, sim: Dict[str, Any], st: Dict[str, Any], priority_lamports: int, tip: int = 0) -> Optional[int]:
    """Simulated tokens out (buy) / lamports out (sell) from the post-sim account states [user ATA, user]."""
    accs = sim.get("accounts") or []
    if side == "buy":
        a = accs[0] if accs else None
        if not a or len(a.get("data") or b"") < 72:
            return None
        return struct.unpack_from("<Q", a["data"], TOKEN_ACCOUNT_AMOUNT_OFFSET)[0] - int(st.get("user_tokens") or 0)
    u = accs[1] if len(accs) > 1 else None
    if not u or st.get("user_lamports") is None:
        return None
    return int(u["lamports"]) - int(st["user_lamports"]) + 5000 + int(priority_lamports) + int(tip)  # add back fees


# v1.43: buy-state prefetch. The engine starts fetch_state when the buy begins, so it runs DURING the safety /
# chase / age gates instead of after them. prepare() uses it only if it finished OK and is at most
# LIVE_PREFETCH_MAX_MS old (default 400); anything else → fresh fetch exactly as before. LIVE_PREFETCH=false → off.
_PREFETCH: Dict[str, Any] = {}
_PF_STATS: Dict[str, int] = {"started": 0, "used": 0, "stale": 0, "failed": 0}


def prefetch_on() -> bool:
    return os.getenv("LIVE_PREFETCH", "true").strip().lower() in {"1", "true", "yes", "on"}


def prefetch(mint: str, client: httpx.AsyncClient) -> None:
    if not prefetch_on():
        return
    from app.live.jupiter import pubkey
    user = pubkey()
    if not user:
        return
    async def _go():
        st = await fetch_state(mint, user, client, blockhash=True)
        return time.time(), user, st
    f = asyncio.ensure_future(_go())
    f.add_done_callback(lambda x: x.cancelled() or x.exception())
    _PREFETCH[mint] = f
    _PF_STATS["started"] += 1
    for k in [k for k, v in _PREFETCH.items() if v.done() and k != mint][:-20]:
        _PREFETCH.pop(k, None)


async def _take_prefetch(mint: str, user: str) -> Optional[Dict[str, Any]]:
    f = _PREFETCH.pop(mint, None)
    if f is None or not prefetch_on():
        return None
    try:
        t_done, u, st = await asyncio.wait_for(asyncio.shield(f), timeout=1.0)
    except Exception:  # noqa: BLE001
        _PF_STATS["failed"] += 1
        return None
    if u != user or not st.get("blockhash") or (time.time() - t_done) * 1000 > _f("LIVE_PREFETCH_MAX_MS", 400):
        _PF_STATS["stale"] += 1
        return None
    _PF_STATS["used"] += 1
    return st


def prefetch_status() -> Dict[str, Any]:
    return {"on": prefetch_on(), **_PF_STATS}


async def prepare(side: str, mint: str, amount: int, client: httpx.AsyncClient, *, slippage_pct: float,
                  blockhash: bool = True, ref_price: Optional[float] = None) -> Dict[str, Any]:
    from app.live.jupiter import pubkey
    c = cfg()
    user = pubkey()
    if not user:
        return {"ok": False, "why": "keypair not loadable"}
    t0 = time.perf_counter()
    try:
        st = await _take_prefetch(mint, user) if side == "buy" and blockhash else None
        pref = st is not None
        if st is None:
            st = await fetch_state(mint, user, client, blockhash=blockhash)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "why": f"state fetch failed ({type(exc).__name__})", "fetch_ms": _ms(t0)}
    fetch_ms = _ms(t0)
    if pref:
        st = {**st, "_prefetched": True}
    ok, why = eligibility(st, side, amount)
    if not ok:
        return {"ok": False, "why": why, "fetch_ms": fetch_ms, "state": _st_view(st)}
    if side == "sell":
        amount = min(int(amount), int(st["user_tokens"]))
    fee_plan = None                      # v1.38: buys get a live-feed priority (floored) + p75 tip, <= budget
    sell_fp = side == "sell" and os.getenv("LIVE_FEE_SELLS", "false").strip().lower() in {"1", "true", "yes", "on"}
    if (side == "buy" or sell_fp) and _fees.enabled():   # v1.40e: sells too, sized on the expected SOL out
        ti0 = await sender_tip_info() if (c["sender_enabled"] and c["sender_tip_lamports"] > 0) else None
        fp_size = int(amount) if side == "buy" else int(quote_sell(st["curve"], int(amount),
                                                                   fee_bps(st["curve"], st.get("fee_config"))["total"]))
        fee_plan = _fees.plan(fp_size, side=side, route="pump_direct",
                              signer_tip_cap=(ti0 or {}).get("max"), want_tip=bool(ti0))
        prio = max(0, min(int(fee_plan["priority_lamports"]), c["max_priority_lamports"]))
    else:
        prio = max(0, min(c["priority_lamports"], c["max_priority_lamports"]))
    fills: Dict[str, Any] = {}
    if side == "buy" and c["whale_cu"]:                  # v1.45: match the whale's CU price (capped, >= our plan)
        from app.live import whalefee
        wcu = whalefee.get(mint, c["whale_cu_max_age_s"])
        fills["whale_cu_micro"] = wcu
        if wcu:
            wl = min(int(wcu), c["whale_cu_max_micro"]) * int(c["cu_limit"]) // 1_000_000
            if wl > prio:
                fills["prio_before"] = prio
                prio = min(wl, c["max_priority_lamports"])
    pfloor = 0
    if side == "buy" and c["price_bound"] and ref_price:
        pfloor = price_bound_min_tokens(int(amount), ref_price, c["price_bound_chase_pct"], c["price_bound_slack_pct"])
        fills.update(price_bound_min_tokens=pfloor, ref_price_sol=ref_price, chase_pct=c["price_bound_chase_pct"])
    t1 = time.perf_counter()
    try:
        b = build(side, mint, user, amount, st, slippage_pct=slippage_pct, cu_limit=c["cu_limit"], priority_lamports=prio,
                  price_floor=pfloor)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "why": f"build failed ({type(exc).__name__}: {str(exc)[:60]})", "fetch_ms": fetch_ms}
    tip = 0
    tip_acct = None
    if c["sender_enabled"] and c["sender_tip_lamports"] > 0:
        ti = await sender_tip_info()
        if ti:
            import random
            from solders.system_program import TransferParams, transfer  # type: ignore
            tip = (min(int(fee_plan["tip_lamports"]), ti["max"]) if fee_plan and fee_plan.get("tip_lamports")
                   else min(c["sender_tip_lamports"], ti["max"]))
            tip_acct = random.choice(ti["accounts"])
            b["ixs"].append(transfer(TransferParams(from_pubkey=_pk(user), to_pubkey=_pk(tip_acct), lamports=tip)))
    return {"ok": True, "user": user, "state": st, "amount": amount, "priority_lamports": prio, **b,
            "tip_lamports": tip, "tip_account": tip_acct, "fee_plan": fee_plan, "fills": fills,
            "fetch_ms": fetch_ms, "build_ms": _ms(t1),
            "watch": [ata(user, mint, st["token_program"]), user]}


def _ms(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000, 1)


def _st_view(st: Dict[str, Any]) -> Dict[str, Any]:
    c = st.get("curve") or {}
    return {"curve": bool(c), "complete": c.get("complete"), "quote_is_sol": c.get("quote_is_sol"),
            "cashback": c.get("is_cashback_coin"), "mayhem": c.get("is_mayhem_mode"),
            "token_program": "2022" if st.get("token_program") == TOKEN_2022_PROGRAM else
            "spl" if st.get("token_program") == TOKEN_PROGRAM else st.get("token_program"),
            "slot": st.get("slot")}


# ---------------- live swap (same contract as jupiter.swap) ----------------
async def swap(*, side: str, mint: str, amount: int, slippage_bps: int = 0, client: Optional[httpx.AsyncClient] = None,
               on_signed: Optional[Any] = None, pre_execute: Optional[Any] = None,
               allow_sim_transport_error: bool = False, pre_sign_quote: Optional[Any] = None,
               ref_price: Optional[float] = None) -> Dict[str, Any]:
    """Direct pump trade. `fallback: True` ⇒ NOTHING was sent: the caller may use Jupiter. Otherwise the result is
    execute()-shaped (ok / ambiguous / signature / total_in / total_out) and must NOT be retried elsewhere."""
    c = cfg()
    own = client is None
    cl = client or httpx.AsyncClient(timeout=15.0)
    tm: Dict[str, Any] = {"path": "pump_direct"}
    t_all = time.perf_counter()
    try:
        slip = c["slippage_pct"] if side == "buy" else min(c["sell_slippage_max_pct"],
                                                           max(c["slippage_pct"], slippage_bps / 10_000))
        p = await prepare(side, mint, amount, cl, slippage_pct=slip,
                          **({"ref_price": ref_price} if side == "buy" and ref_price else {}))
        tm.update(fetch_ms=p.get("fetch_ms"), build_ms=p.get("build_ms"))
        if not p["ok"]:
            return {"ok": False, "fallback": True, "stage": "ineligible", "error": p["why"], "timing": tm}
        if pre_sign_quote is not None and side == "buy":    # v1.40: chase filter on the local curve quote, pre-signer
            try:
                q_ok, q_why = pre_sign_quote({"in_amount": amount, "out_amount": p["expected_out"], "path": "pump_direct"})
            except Exception as exc:  # noqa: BLE001   a broken gate never signs
                q_ok, q_why = False, f"pre-sign quote gate error ({type(exc).__name__})"
            if not q_ok:
                return {"ok": False, "stage": "gate", "error": q_why, "timing": tm, "pre_sign": True}
        from app.live.jupiter import parallel_sim_enabled
        par = bool(c["simulate"]) and parallel_sim_enabled()      # v1.25: coach sim overlaps the signer request
        sim_t = None
        if par:
            utx = compile_tx(p["ixs"], p["user"], p["state"]["blockhash"], sign=False)
            sim_t = asyncio.ensure_future(simulate(_b64(utx), cl, signed=False, watch=p["watch"], keep_blockhash=True))
        t_s = time.perf_counter()
        try:
            tx = await sign_compiled(p, side, mint, amount)
        except Exception as exc:  # noqa: BLE001
            if sim_t is not None:
                sim_t.cancel()
            return {"ok": False, "fallback": True, "stage": "sign", "error": f"sign {type(exc).__name__}", "timing": tm}
        tm["sign_ms"] = _ms(t_s)
        signed = _b64(tx)
        sig = str(tx.signatures[0])
        info = {"router": "pump_direct", "in_amount": p["amount"] if side == "sell" else amount,
                "out_amount": p["expected_out"], "min_out": p["min_out"], "fees_bps": p["fees_bps"],
                "slippage_pct": round(slip * 100, 2), "priority_lamports": p["priority_lamports"]}
        sim: Dict[str, Any] = {}
        if c["simulate"]:
            t_sim = time.perf_counter()
            sim = (await sim_t) if sim_t is not None else await simulate(signed, cl, signed=True, watch=p["watch"])
            tm["sim_ms"] = _ms(t_sim)       # parallel: only the extra wait after the signer answered
            tm["sim_parallel"] = par
            if not sim.get("ok") and not (sim.get("transport_error") and allow_sim_transport_error):
                return {"ok": False, "fallback": True, "stage": "simulate", "order": info,
                        "error": f"direct simulation failed: {str(sim.get('err'))[:120]}", "timing": tm,
                        "simulation": {k: v for k, v in sim.items() if k != "accounts"}}
        if pre_execute is not None:
            try:
                if getattr(pre_execute, "wants_quote", False):      # v1.21 chase filter sees the curve quote
                    q_in = amount if side == "buy" else p["amount"]
                    g_ok, g_why, g_requote = await pre_execute(
                        time.perf_counter() - t_all, quote={"in_amount": q_in, "out_amount": p["expected_out"],
                                                            "path": "pump_direct"})
                else:
                    g_ok, g_why, g_requote = await pre_execute(time.perf_counter() - t_all)
            except Exception as exc:  # noqa: BLE001
                g_ok, g_why, g_requote = False, f"pre-execute gate error ({type(exc).__name__})", False
            if not g_ok:
                return {"ok": False, "stage": "gate", "order": info, "error": g_why, "timing": tm}   # no fallback
            if g_requote:
                return {"ok": False, "stage": "requote", "order": info, "error": "curve quote aged at the gate",
                        "timing": tm}
        if on_signed is not None:
            try:
                on_signed(sig)
            except Exception:  # noqa: BLE001
                pass
        t_send = time.perf_counter()
        info["tip_lamports"] = int(p.get("tip_lamports") or 0)
        if p.get("fills"):
            info["fills_v145"] = {**p["fills"], "min_out": p.get("min_out"), "expected_out": p.get("expected_out"),
                                  "priority_lamports": p.get("priority_lamports")}
        fp = p.get("fee_plan")
        if fp:                            # v1.38: what this trade paid (and why) goes into the ledger order info
            info["fee_plan"] = {k: fp.get(k) for k in ("budget_lamports", "priority_lamports", "tip_lamports",
                                                     "total_lamports", "pct_of_size", "p75_tip_feed", "capped",
                                                     "dual_route")}
            if fp.get("dual_route") and info["tip_lamports"] >= _fees.cfg()["dual_min"]:
                c = {**c, "sender_url": _fees.dual_url(c["sender_url"])}
        use_sender = bool(p.get("tip_lamports"))
        maybe_accepted = False          # v1.26: Sender may have taken it → never allow the Jupiter fallback
        tm["sender"] = "off"
        par_send = use_sender and c["sender_also_rpc"] and \
            os.getenv("LIVE_PARALLEL_SEND", "true").strip().lower() in {"1", "true", "yes", "on"}
        if par_send:     # v1.43: RPC copy of the SAME signed tx goes out with Sender, not after it (lands once)
            maybe_accepted = True       # the RPC copy may be accepted → never allow the Jupiter fallback
            tm["parallel_send"] = True
            _rpc_copy = asyncio.ensure_future(_rpc("sendTransaction", [signed, {"encoding": "base64",
                                              "skipPreflight": True, "maxRetries": 0}], cl, timeout=2.0))
            _rpc_copy.add_done_callback(lambda f: f.cancelled() or f.exception())
        if use_sender:
            try:
                await _sender_send(signed, cl, c)
                tm["sender"] = "sender"
            except SenderError as exc:
                tm["sender"] = "rpc_fallback"
                tm["sender_error"] = str(exc)[:80]
                # A parallel RPC copy may already be in flight. A definite Sender refusal says nothing about
                # that independent copy, so never clear its "may have landed" guard (or try a Jupiter fallback).
                maybe_accepted = maybe_accepted or not exc.definite
            tm["sender_ms"] = _ms(t_send)
        if tm["sender"] == "sender":
            if c["sender_also_rpc"] and not par_send:      # same signed tx (same signature) → can only land once
                try:
                    await _rpc("sendTransaction", [signed, {"encoding": "base64", "skipPreflight": True, "maxRetries": 0}],
                               cl, timeout=2.0)
                except Exception:  # noqa: BLE001
                    pass
            tm["send_ms"] = _ms(t_send)
            return {**(await _confirm_or_ambiguous(sig, signed, cl, c, tm, side, mint, p, sender=True)), "order": info,
                    "simulation": {k: v for k, v in sim.items() if k != "accounts"}}
        try:
            await _rpc("sendTransaction", [signed, {"encoding": "base64", "skipPreflight": True, "maxRetries": 0}], cl)
        except RpcRefused as exc:
            # explicit refusal (nothing accepted). Double-check the signature is unknown before allowing a fallback.
            seen = await _status(sig, cl)
            if seen is None and not maybe_accepted:
                return {"ok": False, "fallback": True, "stage": "send_refused", "error": str(exc)[:120],
                        "order": info, "timing": tm, "presigned_signature": sig}
            tm["send_ms"] = _ms(t_send)
        except Exception as exc:  # noqa: BLE001   timeout / 5xx: may have been accepted → never fall back
            tm["send_ms"] = _ms(t_send)
            return {**(await _confirm_or_ambiguous(sig, signed, cl, c, tm, side, mint, p)),
                    "order": info, "send_error": type(exc).__name__}
        tm["send_ms"] = _ms(t_send)
        return {**(await _confirm_or_ambiguous(sig, signed, cl, c, tm, side, mint, p)), "order": info,
                "simulation": {k: v for k, v in sim.items() if k != "accounts"}}
    finally:
        if own:
            await cl.aclose()


async def _status(sig: str, cl: httpx.AsyncClient) -> Optional[Dict[str, Any]]:
    try:
        st = await _rpc("getSignatureStatuses", [[sig], {"searchTransactionHistory": False}], cl)
        return ((st or {}).get("value") or [None])[0]
    except Exception:  # noqa: BLE001
        return None


async def _confirm_or_ambiguous(sig: str, signed: str, cl: httpx.AsyncClient, c: Dict[str, Any], tm: Dict[str, Any],
                                side: str, mint: str, p: Dict[str, Any], sender: bool = False) -> Dict[str, Any]:
    t_c = time.perf_counter()
    t_end = time.time() + (c["sell_confirm_timeout_s"] if side == "sell" else c["confirm_timeout_s"])
    last_send = time.time()
    ev = asyncio.Event()
    ws_t = asyncio.ensure_future(_ws_watch(sig, ev, tm)) if _ws_url() else None
    try:
        return await _confirm_loop(sig, signed, cl, c, tm, side, mint, p, sender, t_c, t_end, last_send, ev)
    finally:
        if ws_t is not None:
            ws_t.cancel()


async def _confirm_loop(sig, signed, cl, c, tm, side, mint, p, sender, t_c, t_end, last_send, ev):
    while time.time() < t_end:
        v = await _status(sig, cl)
        if v and v.get("confirmationStatus") in ("confirmed", "finalized"):
            tm["confirm_ms"] = _ms(t_c)
            if v.get("err"):
                return {"ok": False, "ambiguous": False, "stage": "execute", "signature": sig, "timing": tm,
                        "error": f"pump direct tx failed {str(v['err'])[:100]}"}
            fill = await _fill(sig, cl, p["user"], side, mint, p)
            return {"ok": True, "status": "Success", "stage": "execute", "ambiguous": False, "signature": sig,
                    "sender": "pump_direct", "timing": tm, **fill}
        if time.time() - last_send >= c["rebroadcast_s"]:
            last_send = time.time()
            if sender:
                try:
                    await _sender_send(signed, cl, c)
                except Exception:  # noqa: BLE001
                    pass
            try:
                await _rpc("sendTransaction", [signed, {"encoding": "base64", "skipPreflight": True, "maxRetries": 0}], cl)
            except Exception:  # noqa: BLE001
                pass
        try:
            await asyncio.wait_for(ev.wait(), timeout=c["confirm_poll_s"])
            if ev.is_set():
                if not tm.get("ws_woke"):
                    tm["ws_woke"] = True
                # signatureSubscribe sends a one-shot notification at the requested commitment. Clear its wake-up
                # event after observing it; otherwise a lagging status RPC leaves ev set and this loop busy-polls.
                ev.clear()
        except asyncio.TimeoutError:
            pass
    tm["confirm_ms"] = _ms(t_c)
    return {"ok": False, "ambiguous": True, "stage": "execute", "signature": sig, "timing": tm,
            "error": "pump direct: not confirmed in time"}


async def _fill(sig: str, cl: httpx.AsyncClient, user: str, side: str, mint: str, p: Dict[str, Any]) -> Dict[str, int]:
    """total_in / total_out from the landed tx (same parser fastfill uses); quote fallback if unreadable."""
    from app.scout.txparse import parse_wallet_trades
    tx = None
    for _ in range(3):
        try:
            tx = await _rpc("getTransaction", [sig, {"encoding": "json", "commitment": "confirmed",
                                                     "maxSupportedTransactionVersion": 1}], cl)
        except Exception:  # noqa: BLE001
            tx = None
        if tx:
            break
        await asyncio.sleep(0.5)
    for ev in parse_wallet_trades(tx, user) if tx else []:
        if ev["mint"] != mint:
            continue
        if side == "buy" and ev["is_buy"]:
            return {"total_in": abs(int(ev["sol_amount"])), "total_out": int(ev["token_amount"])}
        if side == "sell" and not ev["is_buy"]:
            return {"total_in": int(ev["token_amount"]), "total_out": abs(int(ev["sol_amount"]))}
    # unreadable: buy spent exactly `amount` (exact-quote-in); tokens / SOL unknown → 0 (engine reads the chain)
    if side == "buy":
        return {"total_in": int(p["amount"]), "total_out": 0, "fill_unparsed": True}   # engine reads the chain
    return {"total_in": int(p["amount"]), "total_out": int(p["expected_out"]), "fill_unparsed": True}


# ---------------- shadow ----------------
_stats: Dict[str, int] = collections.defaultdict(int)


async def shadow(side: str, mint: str, amount: int, *, slippage_bps: int = 0,
                 client: Optional[httpx.AsyncClient] = None) -> Dict[str, Any]:
    """Build the direct tx exactly as live would (incl. blockhash) but UNSIGNED; simulate it. Never sends."""
    c = cfg()
    own = client is None
    cl = client or httpx.AsyncClient(timeout=15.0)
    rec: Dict[str, Any] = {"side": side, "mint": mint, "amount": int(amount), "t_start_ms": int(time.time() * 1000)}
    t_all = time.perf_counter()
    try:
        slip = c["slippage_pct"] if side == "buy" else min(c["sell_slippage_max_pct"],
                                                           max(c["slippage_pct"], slippage_bps / 10_000))
        p = await prepare(side, mint, amount, cl, slippage_pct=slip)
        rec.update(fetch_ms=p.get("fetch_ms"), build_ms=p.get("build_ms"), eligible=bool(p["ok"]))
        if c.get("sender_enabled"):                                  # v1.28: what Sender would have done (never sent)
            from urllib.parse import urlsplit
            rec["sender"] = {"would_use": bool(p.get("tip_lamports")), "tip_lamports": int(p.get("tip_lamports") or 0),
                             "tip_account": (p.get("tip_account") or "")[:6] or None,
                             "host": urlsplit(c.get("sender_url") or "").hostname, "fallback": "rpc"}
        if not p["ok"]:
            rec["why"] = p["why"]
            rec["state"] = p.get("state")
            return rec
        t_c = time.perf_counter()
        tx = compile_tx(p["ixs"], p["user"], p["state"]["blockhash"], sign=False)
        rec["compile_ms"] = _ms(t_c)
        from app.live import signer_client
        if signer_client.enabled():                                  # v1.24: policy verdict instead (dry run)
            try:
                t_s = time.perf_counter()
                from solders.message import to_bytes_versioned  # type: ignore
                v = await signer_client.request(to_bytes_versioned(tx.message), direct_intent(p, side, mint, amount),
                                                dry_run=True)
                rec["signer"] = {"ok": v.get("ok"), "reasons": (v.get("reasons") or [])[:3], "ms": v.get("ms")}
                rec["sign_ms"] = _ms(t_s)
            except Exception as exc:  # noqa: BLE001
                rec["signer"] = {"ok": None, "error": type(exc).__name__}
                rec["sign_ms"] = None
        else:
            try:                                                     # measure local signing cost; result discarded
                t_s = time.perf_counter()
                compile_tx(p["ixs"], p["user"], p["state"]["blockhash"], sign=True)
                rec["sign_ms"] = _ms(t_s)
            except Exception:  # noqa: BLE001
                rec["sign_ms"] = None
        rec["ready_ms"] = _ms(t_all)                                  # detect→ready-to-send cost of the direct path
        t_sim = time.perf_counter()
        sim = await simulate(_b64(tx), cl, signed=False, watch=p["watch"])
        rec["sim_ms"] = _ms(t_sim)
        rec.update(sim_ok=sim.get("ok"), sim_err=str(sim.get("err"))[:160] if sim.get("err") else None,
                   units=sim.get("units"), sim_transport_error=sim.get("transport_error"),
                   expected_out=p["expected_out"], min_out=p["min_out"], fees_bps=p["fees_bps"]["total"],
                   slippage_pct=round(slip * 100, 2), market_cap_sol=round(p["market_cap_lamports"] / 1e9, 3),
                   state=_st_view(p["state"]), amount=p["amount"])
        if sim.get("ok"):
            rec["sim_out"] = _sim_fill(side, sim, p["state"], p["priority_lamports"], int(p.get("tip_lamports") or 0))
        else:
            rec["logs_tail"] = sim.get("logs_tail")
        return rec
    except Exception as exc:  # noqa: BLE001
        rec["error"] = f"{type(exc).__name__}: {str(exc)[:100]}"
        return rec
    finally:
        if own:
            await cl.aclose()


async def shadow_finish(task: "asyncio.Future", jup: Dict[str, Any], *, side: str, mint: str, arm: Optional[str],
                        jup_ms: Optional[float], ref_price_sol: Optional[float] = None,
                        on_record: Optional[Any] = None, reason: str = "") -> Dict[str, Any]:
    """Pair the shadow simulation with what Jupiter actually filled; append to data/direct_pump.jsonl."""
    try:
        rec = await asyncio.wait_for(task, timeout=20)
    except Exception as exc:  # noqa: BLE001
        rec = {"side": side, "mint": mint, "error": f"shadow {type(exc).__name__}"}
    if reason:
        rec["reason"] = reason
    rec.update(kind="shadow", arm=arm, ts_ms=int(time.time() * 1000), jup_ok=bool(jup.get("ok")),
               jup_ms=jup_ms, jup_router=(jup.get("order") or {}).get("router"), jup_signature=jup.get("signature"))
    jo = int(jup.get("total_out") or 0)
    ji = int(jup.get("total_in") or 0)
    rec["jup_out"] = jo or None
    so = rec.get("sim_out")
    if so and jo:
        rec["direct_vs_jup_pct"] = round((so / jo - 1) * 100, 3)      # + = direct path gets more (tokens / SOL)
    if side == "buy" and so and rec.get("amount"):
        px = (rec["amount"] / 1e9) / (so / 1e6)                       # SOL per whole token (6 decimals)
        rec["direct_price_sol"] = px
        if ref_price_sol:
            rec["direct_vs_scout_pct"] = round((px / ref_price_sol - 1) * 100, 3)
        if jo and ji:
            jpx = (ji / 1e9) / (jo / 1e6)
            rec["jup_price_sol"] = jpx
            if ref_price_sol:
                rec["jup_vs_scout_pct"] = round((jpx / ref_price_sol - 1) * 100, 3)
    _stats[f"shadow_{side}"] += 1
    _stats[f"shadow_{side}_{'sim_ok' if rec.get('sim_ok') else 'ineligible' if rec.get('eligible') is False else 'sim_fail'}"] += 1
    log(rec)
    if on_record is not None:
        try:
            on_record(rec)
        except Exception:  # noqa: BLE001
            pass
    return rec


def log(rec: Dict[str, Any]) -> None:
    p = cfg()["log_path"]
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")
    except Exception as exc:  # noqa: BLE001
        logger.warning("pumpdirect log failed: %s", type(exc).__name__)


def _med(xs: List[float]) -> Optional[float]:
    xs = [x for x in xs if isinstance(x, (int, float))]
    return round(statistics.median(xs), 3) if xs else None


def summary(recent: int = 15) -> Dict[str, Any]:
    c = cfg()
    rows: List[Dict[str, Any]] = []
    try:
        with c["log_path"].open() as fh:
            for line in fh:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except FileNotFoundError:
        pass
    sh = [r for r in rows if r.get("kind") == "shadow"]
    live = [r for r in rows if r.get("kind") == "live"]

    def side_stats(side: str) -> Dict[str, Any]:
        rs = [r for r in sh if r.get("side") == side]
        el = [r for r in rs if r.get("eligible")]
        ok = [r for r in el if r.get("sim_ok")]
        cmp_ = [r["direct_vs_jup_pct"] for r in ok if r.get("direct_vs_jup_pct") is not None]
        return {"shadow_trades": len(rs), "on_curve_eligible": len(el), "sim_ok": len(ok),
                "sim_ok_rate": round(len(ok) / len(el), 3) if el else None,
                "sim_failures": [{"mint": r.get("mint"), "err": r.get("sim_err"), "logs": r.get("logs_tail")}
                                 for r in el if not r.get("sim_ok")][-5:],
                "ineligible_reasons": dict(collections.Counter(r.get("why") for r in rs if r.get("eligible") is False)),
                "median_ready_ms": _med([r.get("ready_ms") for r in el]),
                "median_fetch_ms": _med([r.get("fetch_ms") for r in el]),
                "median_sim_ms": _med([r.get("sim_ms") for r in el]),
                "median_jup_ms": _med([r.get("jup_ms") for r in rs]),
                "compared_with_jupiter": len(cmp_), "median_direct_vs_jup_pct": _med(cmp_),
                "direct_better_share": round(sum(1 for x in cmp_ if x >= 0) / len(cmp_), 3) if cmp_ else None,
                "median_direct_vs_scout_pct": _med([r.get("direct_vs_scout_pct") for r in ok]),
                "median_jup_vs_scout_pct": _med([r.get("jup_vs_scout_pct") for r in ok]),
                "median_expected_vs_sim_pct": _med([(r["sim_out"] / r["expected_out"] - 1) * 100 for r in ok
                                                    if r.get("sim_out") and r.get("expected_out")]),
                "median_units": _med([r.get("units") for r in ok])}
    b, s = side_stats("buy"), side_stats("sell")
    checks = {
        f"≥{c['golive_min_buys']} on-curve shadow buys simulated": (b["on_curve_eligible"] >= c["golive_min_buys"]),
        f"≥{c['golive_min_sells']} on-curve shadow sells simulated": (s["on_curve_eligible"] >= c["golive_min_sells"]),
        f"buy sim ok rate ≥ {c['golive_min_sim_ok']:.0%}": (b["sim_ok_rate"] or 0) >= c["golive_min_sim_ok"],
        f"sell sim ok rate ≥ {c['golive_min_sim_ok']:.0%}": (s["sim_ok_rate"] or 0) >= c["golive_min_sim_ok"],
        "median direct buy fill ≥ Jupiter's (tokens)": (b["median_direct_vs_jup_pct"] or -1) >= 0,
        "median direct sell fill ≥ −1% vs Jupiter (SOL)": (s["median_direct_vs_jup_pct"] if s["median_direct_vs_jup_pct"]
                                                          is not None else -99) >= -1.0,
        "curve quote matches simulation (|median| ≤ 1%)": abs(b["median_expected_vs_sim_pct"]
                                                               if b["median_expected_vs_sim_pct"] is not None else 99) <= 1.0,
        "median ready-to-send ≤ 400 ms": (b["median_ready_ms"] or 1e9) <= 400,
    }
    return {"ok": True, "enabled": c["enabled"], "mode": mode(), "slippage_pct": round(c["slippage_pct"] * 100, 2),
            "cu_limit": c["cu_limit"], "priority_lamports": c["priority_lamports"], "simulate_before_send": c["simulate"],
            "instruction": "pump buy_exact_quote_in_v3 / sell_v3 (17 accounts, TRADE_V3.md)",
            "buys": b, "sells": s, "live_trades": len(live),
            "go_live_checks": checks, "go_live_ready": all(checks.values()),
            "recent": [{k: r.get(k) for k in ("ts_ms", "side", "arm", "mint", "eligible", "why", "sim_ok", "sim_err",
                                               "ready_ms", "sim_ms", "jup_ms", "direct_vs_jup_pct",
                                               "direct_vs_scout_pct", "jup_vs_scout_pct")}
                       for r in sorted(sh, key=lambda x: -(x.get("ts_ms") or 0))[:recent]],
            "stats": dict(_stats)}
