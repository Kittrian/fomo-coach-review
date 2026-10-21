"""fomo-signer policy: decides whether a transaction message may be signed for the coach's stated intent.

Two layers, both must pass:
  1. static   decode the message (legacy or v0, ALTs resolved by the caller): our wallet is a signer, fee payer rules,
              program allowlist, no durable nonce, no Assign/Allocate of the wallet, System transfers from the wallet
              only to our own wSOL ATA / a Jito tip account (intent=tip) / the cold address (intent=sweep), no token
              Approve / SetAuthority / Burn by our wallet, CloseAccount only back to our wallet, priority-fee cap.
  2. effects  pre-state (getMultipleAccounts) vs simulateTransaction post-state of every writable account + wallet:
              our SOL value (wallet + our token accounts' lamports) may fall by at most the intent's cap + fees +
              rent slack; only the intent's mints may change on our token accounts; a token account we own may not
              change owner, gain a delegate or a foreign close authority; the wallet stays a plain System account.

Hard limits come from the signer's own config (never from the request). Pure functions; no I/O here."""
from __future__ import annotations

import os
import struct
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

SYSTEM = "11111111111111111111111111111111"
TOKEN = "REDACTED"
TOKEN22 = "REDACTED"
ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
COMPUTE_BUDGET = "ComputeBudget111111111111111111111111111111"
WSOL = "So11111111111111111111111111111111111111112"
TOKEN_PROGRAMS = (TOKEN, TOKEN22)

# Programs a swap may invoke at top level. Extend with SIGNER_EXTRA_PROGRAMS (comma list) — never from the request.
DEFAULT_SWAP_PROGRAMS = {
    COMPUTE_BUDGET, SYSTEM, TOKEN, TOKEN22, ATA_PROGRAM,
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",   # Jupiter aggregator v6 (metis)
    "61DFfeTKM7trxYcPQCM78bJ794ddZprZpAwAnLiwTpYH",  # JupiterZ RFQ order engine (seen in shadow /order)
    "DF1ow4tspfHX9JwWJsAb9epbkA8hmpSEAtxXy1V27QBH",  # DFlow (seen in live fills)
    "6m2CDdhRgxpH4WjvdzxAYbGxwdGUz5MziiL5jek2kBma",  # OKX DEX aggregation router v2
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",   # pump.fun bonding curve (direct path)
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",   # pump AMM
    "L2TExMFKdjpN9kozasaurPirfHy9P8sbXoAN1qA3S95",   # Lighthouse assertions (Jupiter guard)
    "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr",   # memo v2
    "Memo1UhkJRfHyvLMcVucJwxXeuD728EqVDDwQDxFMNo",   # memo v1
}
JITO_TIP_ACCOUNTS = {
    "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5", "HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe",
    "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY", "ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt6iGPaS49",
    "DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh", "ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt",
    "DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL", "3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT",
}
# v1.26 Helius Sender tip accounts (mainnet-beta), pinned from https://www.helius.dev/docs/sending-transactions/sender
# on 2026-10-09. A buy/sell may carry exactly ONE System transfer from our wallet to one of these, ≤ max_tip_lamports
# (SIGNER_MAX_TIP_LAMPORTS, hard ceiling TIP_CEILING_LAMPORTS) and no other SOL transfer from the wallet.
HELIUS_SENDER_TIP_ACCOUNTS = frozenset({
    "4ACfpUFoaSD9bfPdeu6DBt89gB6ENTeHBXCAi87NhDEE", "D2L6yPZ2FmmmTKPgzaMKdhu6EWZcTpLy1Vhx8uvZe7NZ",
    "9bnz4RShgq1hAnLnZbP8kbgBg1kEmcJBYQq3gQbmnSta", "5VY91ws6B2hMmBFRsXkoAAdsPHBJwRfBht4DXox3xkwn",
    "2nyhqdwKcJZR2vcqCyrYsaPVdAnFoJjiksCXJ7hfEYgD", "2q5pghRs6arqVjRvT5gfgWfWcHWmw1ZuCzphgd5KfWGJ",
    "wyvPkWjVZz1M8fHQnMMCDTQDbkManefNNhweYk5WkcF", "3KCKozbAaF75qEU33jtzozcJ29yJuaLJTy2jFdzUY8bT",
    "4vieeGHPYPG2MmyPRcYjdiDmmhN3ww7hsFNap8pVN3Ey", "4TQLFNWK8AovT1gFvda5jfw2oJeRMKEmw7aH6MGBJ3or",
})
TIP_CEILING_LAMPORTS = 1_000_000                    # 0.001 SOL absolute ceiling, whatever the env says
KINDS = ("buy", "sell", "tip", "sweep")
LAMPORTS = 1_000_000_000


def _i(name: str, d: int) -> int:
    try:
        return int(float(os.getenv(name, str(d))))
    except ValueError:
        return d


def _b(name: str, d: bool) -> bool:
    v = os.getenv(name)
    return d if v is None else v.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    wallet: str = ""
    cold_address: str = ""
    max_buy_lamports: int = 400_000_000           # = LIVE_MAX_SOL_PER_TRADE 0.40
    max_priority_lamports: int = 2_000_000          # = LIVE_MAX_PRIORITY_LAMPORTS
    max_tip_lamports: int = 5_000                   # v1.26: = chosen Sender tip (SWQOS-only); ceiling 0.001 SOL
    rent_slack_lamports: int = 5_000_000            # PDAs/ATAs the route creates that we don't own (pump volume acc.)
    sell_loss_slack_lamports: int = 0
    float_lamports: int = 1_000_000_000             # sweep leaves ≥ 1.0 SOL
    allow_external_fee_payer: bool = False          # JupiterZ / gasless; still bound by the effects check
    programs: set = field(default_factory=lambda: set(DEFAULT_SWAP_PROGRAMS))
    max_signs_per_min: int = 20
    max_buys_per_hour: int = 30
    max_buy_lamports_24h: int = 6 * LAMPORTS
    allow_stray_buy_tokens: bool = False             # FOMOANT 2026-10-19: buy may RECEIVE extra mints (warn, not refuse)

    @classmethod
    def from_env(cls) -> "Config":
        c = cls(wallet=os.getenv("SIGNER_WALLET", "").strip(), cold_address=os.getenv("SWEEP_COLD_ADDRESS", "").strip(),
                max_buy_lamports=_i("SIGNER_MAX_BUY_LAMPORTS", 400_000_000),
                max_priority_lamports=_i("SIGNER_MAX_PRIORITY_LAMPORTS", 2_000_000),
                max_tip_lamports=max(0, min(_i("SIGNER_MAX_TIP_LAMPORTS", 5_000), TIP_CEILING_LAMPORTS)),
                rent_slack_lamports=_i("SIGNER_RENT_SLACK_LAMPORTS", 5_000_000),
                float_lamports=_i("SWEEP_FLOAT_LAMPORTS", 1_000_000_000),
                allow_external_fee_payer=_b("SIGNER_ALLOW_EXTERNAL_FEE_PAYER", False),
                max_signs_per_min=_i("SIGNER_MAX_SIGNS_PER_MIN", 20),
                max_buys_per_hour=_i("SIGNER_MAX_BUYS_PER_HOUR", 30),
                max_buy_lamports_24h=_i("SIGNER_MAX_BUY_LAMPORTS_24H", 6 * LAMPORTS),
                allow_stray_buy_tokens=_b("SIGNER_ALLOW_STRAY_BUY_TOKENS", False))
        extra = [p.strip() for p in os.getenv("SIGNER_EXTRA_PROGRAMS", "").split(",") if p.strip()]
        c.programs |= set(extra)
        return c


# ---------------------------------------------------------------- message decoding

def ata(owner: str, mint: str, token_program: str = TOKEN) -> str:
    from solders.pubkey import Pubkey  # type: ignore
    pk, _ = Pubkey.find_program_address(
        [bytes(Pubkey.from_string(owner)), bytes(Pubkey.from_string(token_program)), bytes(Pubkey.from_string(mint))],
        Pubkey.from_string(ATA_PROGRAM))
    return str(pk)


def decode_message(msg_bytes: bytes):
    from solders.message import from_bytes_versioned  # type: ignore
    return from_bytes_versioned(msg_bytes)


def alt_keys(msg) -> List[str]:
    """Lookup-table accounts referenced by a v0 message (legacy → [])."""
    return [str(l.account_key) for l in (getattr(msg, "address_table_lookups", None) or [])]


def parse_alt(data: bytes) -> List[str]:
    from solders.pubkey import Pubkey  # type: ignore
    body = data[56:]
    return [str(Pubkey.from_bytes(body[i:i + 32])) for i in range(0, len(body) - len(body) % 32, 32)]


def resolve(msg, alts: Dict[str, List[str]]) -> Tuple[List[str], List[bool]]:
    """Full account list (static + ALT writable + ALT readonly) and writable flags, Solana's ordering."""
    static = [str(k) for k in msg.account_keys]
    h = msg.header
    s, rs, ru = h.num_required_signatures, h.num_readonly_signed_accounts, h.num_readonly_unsigned_accounts
    n = len(static)
    w = [(i < s - rs) if i < s else (i < n - ru) for i in range(n)]
    lw: List[str] = []
    lr: List[str] = []
    for l in (getattr(msg, "address_table_lookups", None) or []):
        table = alts.get(str(l.account_key))
        if table is None:
            raise ValueError(f"lookup table {str(l.account_key)[:8]}… not resolved")
        for idx in bytes(l.writable_indexes):
            if idx >= len(table):
                raise ValueError("lookup index out of range")
            lw.append(table[idx])
        for idx in bytes(l.readonly_indexes):
            if idx >= len(table):
                raise ValueError("lookup index out of range")
            lr.append(table[idx])
    return static + lw + lr, w + [True] * len(lw) + [False] * len(lr)


# ---------------------------------------------------------------- static checks

@dataclass
class Static:
    violations: List[str]
    fee_payer_external: bool
    priority_lamports: int
    num_signatures: int
    programs: List[str]
    writable: List[str]
    tip_lamports: int = 0                           # v1.26 Helius Sender tip inside a buy/sell


def _u32(b: bytes, o: int = 0) -> int:
    return struct.unpack_from("<I", b, o)[0] if len(b) >= o + 4 else -1


def _u64(b: bytes, o: int) -> int:
    return struct.unpack_from("<Q", b, o)[0] if len(b) >= o + 8 else -1


def check_static(msg, keys: List[str], writable: List[bool], wallet: str, intent: Dict[str, Any], cfg: Config) -> Static:
    v: List[str] = []
    kind = intent.get("kind")
    if kind not in KINDS:
        v.append(f"unknown intent kind {kind!r}")
    n_sig = msg.header.num_required_signatures
    signers = keys[:n_sig]
    if wallet not in signers:
        v.append("our wallet is not a required signer")
    external = bool(keys) and keys[0] != wallet
    if external and not (cfg.allow_external_fee_payer and kind in ("buy", "sell")):
        v.append("fee payer is not our wallet (external fee payer not allowed)")
    if n_sig > 2:
        v.append(f"{n_sig} required signers (max 2)")
    amount = int(intent.get("amount") or 0)
    in_m, out_m = intent.get("input_mint"), intent.get("output_mint")
    if kind == "buy":
        if in_m != WSOL or not out_m or out_m == WSOL:
            v.append("buy must be WSOL → token")
        if amount <= 0 or amount > cfg.max_buy_lamports:
            v.append(f"buy amount {amount} outside (0, {cfg.max_buy_lamports}]")
    elif kind == "sell":
        if out_m != WSOL or not in_m or in_m == WSOL:
            v.append("sell must be token → WSOL")
        if amount <= 0:
            v.append("sell amount must be > 0")
    elif kind == "sweep":
        if not cfg.cold_address:
            v.append("sweep: no cold address configured")
    allowed_programs = {COMPUTE_BUDGET, SYSTEM} if kind in ("tip", "sweep") else cfg.programs
    own_wsol = {ata(wallet, WSOL, TOKEN), ata(wallet, WSOL, TOKEN22)} if wallet else set()
    own_atas = set(own_wsol)
    for m in (in_m, out_m):
        if m and m != WSOL and wallet:
            own_atas |= {ata(wallet, m, TOKEN), ata(wallet, m, TOKEN22)}
    cu_limit: Optional[int] = None
    cu_price = 0
    progs: List[str] = []
    sys_transfers = 0
    sender_tips = 0
    tip_lamports = 0
    tip_cap = min(cfg.max_tip_lamports, TIP_CEILING_LAMPORTS)
    for ix in msg.instructions:
        prog = keys[ix.program_id_index]
        progs.append(prog)
        acc = [keys[i] for i in bytes(ix.accounts)]
        data = bytes(ix.data)
        if prog not in allowed_programs:
            v.append(f"program not allowed: {prog}")
            continue
        if prog == COMPUTE_BUDGET:
            if data[:1] == b"\x02":
                cu_limit = _u32(data, 1)
            elif data[:1] == b"\x03":
                cu_price = _u64(data, 1)
        elif prog == SYSTEM:
            tag = _u32(data)
            touches_wallet = wallet in acc
            if tag == 2:                                    # Transfer
                lam = _u64(data, 4)
                src, dst = (acc + ["", ""])[:2]
                if src == wallet:
                    sys_transfers += 1
                    if kind == "tip" and dst in JITO_TIP_ACCOUNTS and 0 < lam <= tip_cap:
                        pass
                    elif kind in ("buy", "sell") and dst in HELIUS_SENDER_TIP_ACCOUNTS:
                        sender_tips += 1
                        if 0 < lam <= tip_cap:
                            tip_lamports += lam
                        else:
                            v.append(f"sender tip {lam} lamports outside (0, {tip_cap}]")
                    elif kind == "sweep" and dst == cfg.cold_address and lam == amount and lam > 0:
                        pass
                    elif kind == "buy" and dst in own_wsol and lam <= amount:
                        pass
                    else:
                        v.append(f"SOL transfer from wallet to non-allowed destination {dst[:8]}… ({lam} lamports)")
            elif tag in (4, 5, 6, 7, 12):
                v.append("durable-nonce instruction")
            elif tag in (1, 8, 9, 10, 11) and touches_wallet:
                v.append(f"system instruction {tag} (assign/allocate/seed transfer) on our wallet")
            elif tag in (0, 3):
                lam = _u64(data, 4) if tag == 0 else -1
                owner = data[20:52] if tag == 0 else b""
                if acc and acc[0] == wallet:
                    from solders.pubkey import Pubkey  # type: ignore
                    ok_owner = tag == 0 and len(owner) == 32 and str(Pubkey.from_bytes(owner)) in TOKEN_PROGRAMS
                    if not (ok_owner and 0 <= lam <= 3_000_000):
                        v.append("CreateAccount funded by our wallet (non-token or > rent)")
            elif tag not in (0, 2, 3) and touches_wallet:
                v.append(f"unknown system instruction {tag}")
        elif prog in TOKEN_PROGRAMS:
            tag = data[0] if data else -1
            if tag in (3, 12):                              # Transfer / TransferChecked
                src, dst, auth = (acc[0], acc[1], acc[2]) if tag == 3 else (acc[0], acc[2], acc[3])
                if auth == wallet and dst not in own_atas:
                    v.append(f"token transfer by our wallet to non-own account {dst[:8]}…")
            elif tag in (4, 13):                            # Approve / ApproveChecked
                owner = acc[2] if tag == 4 else acc[3]
                if owner == wallet:
                    v.append("token Approve (delegate) by our wallet")
            elif tag == 6:                                  # SetAuthority
                if len(acc) > 1 and acc[1] == wallet:
                    v.append("token SetAuthority by our wallet")
            elif tag in (8, 15):                            # Burn / BurnChecked
                if len(acc) > 2 and acc[2] == wallet:
                    v.append("token Burn by our wallet")
            elif tag == 9:                                  # CloseAccount
                if len(acc) > 2 and acc[2] == wallet and acc[1] != wallet:
                    v.append("CloseAccount sends our rent to someone else")
            elif tag in (1, 5, 16, 17, 18, 22):             # init / revoke / syncNative / immutable owner
                pass
            elif wallet in acc:
                v.append(f"token instruction {tag} involving our wallet")
        elif prog == ATA_PROGRAM:
            tag = data[0] if data else 0
            if tag == 2:
                v.append("ATA RecoverNested")
            elif len(acc) > 2 and acc[0] == wallet and acc[2] != wallet:
                v.append("we pay for someone else's token account")
    if sender_tips > 1:
        v.append(f"{sender_tips} sender tips (max 1)")
    if sender_tips and sys_transfers > sender_tips:
        v.append("sender tip combined with another SOL transfer from our wallet")
    if kind in ("tip", "sweep") and sys_transfers != 1:
        v.append(f"{kind} must contain exactly one SOL transfer (found {sys_transfers})")
    limit = cu_limit if cu_limit is not None and cu_limit >= 0 else 1_400_000
    priority = (cu_price * limit + 999_999) // 1_000_000 if cu_price > 0 else 0
    cap = cfg.max_priority_lamports
    ask = int(intent.get("max_priority_lamports") or 0)
    if ask > 0:
        cap = min(cap, ask)
    if priority > cap:
        v.append(f"priority fee {priority} > cap {cap}")
    return Static(violations=v, fee_payer_external=external, priority_lamports=priority, num_signatures=n_sig,
                  programs=progs, writable=[k for k, w in zip(keys, writable) if w], tip_lamports=tip_lamports)


# ---------------------------------------------------------------- effects (simulation) checks

def parse_token(acct: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not acct or acct.get("owner") not in TOKEN_PROGRAMS:
        return None
    d = acct.get("data") or b""
    if len(d) < 165:
        return None
    from solders.pubkey import Pubkey  # type: ignore
    return {"mint": str(Pubkey.from_bytes(d[0:32])), "owner": str(Pubkey.from_bytes(d[32:64])),
            "amount": _u64(d, 64), "delegate": _u32(d, 72) == 1,
            "close_auth": str(Pubkey.from_bytes(d[133:165])) if _u32(d, 129) == 1 else None}


def check_effects(pre: Dict[str, Optional[Dict[str, Any]]], post: Dict[str, Optional[Dict[str, Any]]], wallet: str,
                  intent: Dict[str, Any], st: Static, cfg: Config) -> Tuple[List[str], Dict[str, Any]]:
    v: List[str] = []
    warn: List[str] = []
    kind = intent.get("kind")
    amount = int(intent.get("amount") or 0)
    pw, qw = pre.get(wallet), post.get(wallet)
    if not pw or not qw:
        return ["wallet state missing from simulation"], {}
    if qw.get("owner") != SYSTEM or len(qw.get("data") or b"") != 0:
        v.append("wallet would stop being a plain System account")
    sol_delta = int(qw["lamports"]) - int(pw["lamports"])
    token_delta: Dict[str, int] = {}
    for addr in set(pre) | set(post):
        if addr == wallet:
            continue
        a, b = pre.get(addr), post.get(addr)
        ta, tb = parse_token(a), parse_token(b)
        ours_pre = bool(ta and ta["owner"] == wallet)
        ours_post = bool(tb and tb["owner"] == wallet)
        if ours_pre and b and not ours_post:
            v.append(f"our token account {addr[:8]}… would change owner/type")
        if ours_post:
            if tb["delegate"] and not (ta and ta["delegate"]):
                v.append(f"delegate set on our token account {addr[:8]}…")
            if tb["close_auth"] and tb["close_auth"] != wallet:
                v.append(f"foreign close authority on our token account {addr[:8]}…")
        if ours_pre or ours_post:
            sol_delta += (int(b["lamports"]) if b else 0) - (int(a["lamports"]) if a else 0)
            mint = (tb or ta)["mint"]
            if mint != WSOL:
                d = (tb["amount"] if ours_post else 0) - (ta["amount"] if ours_pre else 0)
                if d:
                    token_delta[mint] = token_delta.get(mint, 0) + d
    loss = -sol_delta
    base_fee = 0 if st.fee_payer_external else 5000 * st.num_signatures
    fees = base_fee + st.priority_lamports + (st.tip_lamports if kind in ("buy", "sell") else 0)
    if kind == "buy":
        cap = amount + fees + cfg.rent_slack_lamports
        out_m = intent.get("output_mint")
        if token_delta.get(out_m, 0) <= 0:
            v.append("buy yields no output tokens")
        for m, d in token_delta.items():
            if m != out_m:
                if d > 0 and cfg.allow_stray_buy_tokens:
                    warn.append(f"stray token received on buy (mint {m[:8]}…, {d}) - allowed")
                else:
                    v.append(f"unexpected token change on our account (mint {m[:8]}…, {d})")
    elif kind == "sell":
        cap = fees + cfg.rent_slack_lamports + cfg.sell_loss_slack_lamports
        in_m = intent.get("input_mint")
        for m, d in token_delta.items():
            if m == in_m:
                if -d > amount:
                    v.append(f"sell spends {-d} tokens > intent {amount}")
                if d > 0:
                    v.append("sell increases the input token")
            else:
                v.append(f"unexpected token change on our account (mint {m[:8]}…, {d})")
    elif kind == "tip":
        cap = cfg.max_tip_lamports + fees
        if token_delta:
            v.append("tip changes token balances")
    elif kind == "sweep":
        cap = amount + fees
        if int(qw["lamports"]) < cfg.float_lamports:
            v.append(f"sweep would leave {int(qw['lamports'])} < float {cfg.float_lamports}")
        if token_delta:
            v.append("sweep changes token balances")
    else:
        cap = 0
    if loss > cap:
        v.append(f"our SOL value would fall by {loss} lamports > allowed {cap}")
    return v, {"sol_delta": sol_delta, "loss_cap": cap, "token_delta": token_delta, "fees": fees,
               **({"warnings": warn} if warn else {})}
