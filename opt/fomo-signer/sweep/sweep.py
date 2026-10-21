#!/usr/bin/env python3
"""fomo hourly profit sweep (VPS layer 2). Moves wallet SOL above the float (SWEEP_FLOAT_LAMPORTS, 1.0 SOL) to the cold
address held in fomo-signer's credential — the only non-trade transfer the signer will sign (intent=sweep: exactly one
System transfer to that address, wallet left ≥ float, at most once per ~hour).

Runs only when: SWEEP_ENABLED=true, the coach reports 0 open / 0 inflight / 0 pending positions, the kill switch and
the signer HALT are off, and the excess ≥ SWEEP_MIN_SOL. --dry-run: builds the real tx and gets the signer's verdict
(simulation included) but never signs or sends. Logs to SWEEP_LOG (JSONL). Never prints keys or RPC URLs."""
from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import time

import httpx

FEE_RESERVE = 10_000


def log(rec):
    rec = {"ts_ms": int(time.time() * 1000), **rec}
    print(json.dumps(rec, separators=(",", ":")))
    p = os.getenv("SWEEP_LOG", "")
    if p:
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, separators=(",", ":")) + "\n")


async def signer(req, path):
    r, w = await asyncio.wait_for(asyncio.open_unix_connection(path), 3)
    w.write((json.dumps(req) + "\n").encode())
    await w.drain()
    line = await asyncio.wait_for(r.readline(), 20)
    w.close()
    return json.loads(line)


async def rpc(c, url, method, params):
    j = (await c.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})).json()
    if "error" in j:
        raise RuntimeError(f"{method} rpc error {j['error'].get('code')}")
    return j["result"]


def coach_flat(coach_url: str):
    h = httpx.get(f"{coach_url}/health", timeout=8).json()
    l = h.get("live") or {}
    busy = {k: l.get(k) for k in ("open_positions", "inflight_buys", "pending_buys")}
    flat = all(isinstance(v, int) and v == 0 for v in busy.values())
    return flat, busy


async def main(dry: bool) -> int:
    from solders.hash import Hash  # type: ignore
    from solders.message import Message, to_bytes_versioned  # type: ignore
    from solders.pubkey import Pubkey  # type: ignore
    from solders.signature import Signature  # type: ignore
    from solders.system_program import TransferParams, transfer  # type: ignore
    from solders.transaction import Transaction  # type: ignore
    sock = os.getenv("SIGNER_SOCKET", "/run/fomo-signer/signer.sock")
    url = os.getenv("SWEEP_RPC_URL") or os.getenv("SIGNER_RPC_URL", "")
    wallet = os.getenv("SIGNER_WALLET", "")
    min_l = int(float(os.getenv("SWEEP_MIN_SOL", "0.05")) * 1e9)
    if not dry and os.getenv("SWEEP_ENABLED", "false").lower() not in ("1", "true", "yes"):
        log({"event": "skip", "why": "SWEEP_ENABLED is off"})
        return 0
    for f in (os.getenv("SWEEP_KILL_FILE", "/opt/fomo/state/KILL"),):
        if f and os.path.exists(f) and not dry:
            log({"event": "skip", "why": "kill switch on"})
            return 0
    try:
        flat, busy = coach_flat(os.getenv("SWEEP_COACH_URL", "http://127.0.0.1:8790"))
    except Exception as exc:  # noqa: BLE001
        log({"event": "skip", "why": f"coach unreachable ({type(exc).__name__})"})
        return 0
    if not flat:
        log({"event": "skip", "why": "positions open/inflight", **busy})
        return 0
    info = await signer({"op": "sweep_info"}, sock)
    if not info.get("ok"):
        log({"event": "skip", "why": "signer has no cold address"})
        return 1
    cold, float_l = info["cold_address"], int(info["float_lamports"])
    async with httpx.AsyncClient(timeout=15) as c:
        bal = int((await rpc(c, url, "getBalance", [wallet, {"commitment": "confirmed"}]))["value"])
        excess = bal - float_l - FEE_RESERVE
        if excess < min_l:
            log({"event": "skip", "why": "below threshold", "balance_sol": bal / 1e9, "excess_sol": max(0, excess) / 1e9})
            return 0
        bh = (await rpc(c, url, "getLatestBlockhash", [{"commitment": "confirmed"}]))["value"]["blockhash"]
        me = Pubkey.from_string(wallet)
        msg = Message.new_with_blockhash(
            [transfer(TransferParams(from_pubkey=me, to_pubkey=REDACTED(cold), lamports=excess))],
            me, Hash.from_string(bh))
        intent = {"kind": "sweep", "amount": excess, "tag": "hourly_sweep"}
        res = await signer({"op": "sign", "msg_b64": base64.b64encode(to_bytes_versioned(msg)).decode(),
                            "intent": intent, "dry_run": dry}, sock)
        rec = {"event": "dry_run" if dry else "sign", "excess_sol": excess / 1e9, "balance_sol": bal / 1e9,
               "ok": res.get("ok"), "signed": res.get("signed"), "reasons": res.get("reasons"), "signer_ms": res.get("ms")}
        if dry or not res.get("signed"):
            log(rec)
            return 0 if res.get("ok") else 1
        tx = Transaction.populate(msg, [Signature.from_bytes(base64.b64decode(res["signature_b64"]))])
        sig = await rpc(c, url, "sendTransaction", [base64.b64encode(bytes(tx)).decode(),
                                                     {"encoding": "base64", "preflightCommitment": "confirmed"}])
        for _ in range(60):
            st = (await rpc(c, url, "getSignatureStatuses", [[sig]]))["value"][0]
            if st and st.get("confirmationStatus") in ("confirmed", "finalized"):
                log({**rec, "event": "swept", "signature": sig, "err": st.get("err")})
                return 0 if st.get("err") is None else 1
            await asyncio.sleep(1)
        log({**rec, "event": "sent_unconfirmed", "signature": sig})
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main("--dry-run" in sys.argv)))
