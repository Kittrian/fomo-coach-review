"""fomo-signer: the only process that can produce a wallet signature. Coach → unix socket (SO_PEERCRED-checked uid)
→ JSON {op:"sign", msg_b64, intent, dry_run} → static policy → ALT resolve + pre-state + simulation → effects policy
→ rate/24h limits → sign (live) or verdict only (shadow). One JSON line per request/response.

SIGNER_MODE=shadow (default): never signs anything, returns the verdict. live: signs when every check passes.
HALT file (SIGNER_HALT_FILE) present → refuses everything (tripwire sets it). Decisions → SIGNER_STATE_DIR/decisions.jsonl
(no tx bytes, no keys)."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import pwd
import socket
import struct
import time
from collections import deque
from typing import Any, Dict, List, Optional

from . import policy
from .chain import Chain

log = logging.getLogger("fomo-signer")
MAX_LINE = 256 * 1024


class Limits:
    """Signing-rate and 24h buy-volume limits, persisted so a restart can't reset them."""

    def __init__(self, cfg: policy.Config, path: str):
        self.cfg, self.path = cfg, path
        self.ev: deque = deque()
        try:
            with open(path, encoding="utf-8") as fh:
                for r in json.load(fh):
                    self.ev.append(tuple(r))
        except Exception:  # noqa: BLE001
            pass

    def _prune(self, now: float) -> None:
        while self.ev and now - self.ev[0][0] > 86400:
            self.ev.popleft()

    def check(self, kind: str, lamports: int, now: Optional[float] = None) -> Optional[str]:
        now = now or time.time()
        self._prune(now)
        if sum(1 for t, _, _ in self.ev if now - t < 60) >= self.cfg.max_signs_per_min:
            return "rate limit: signs per minute"
        if kind == "buy":
            if sum(1 for t, k, _ in self.ev if k == "buy" and now - t < 3600) >= self.cfg.max_buys_per_hour:
                return "rate limit: buys per hour"
            if sum(l for _, k, l in self.ev if k == "buy") + lamports > self.cfg.max_buy_lamports_24h:
                return "24h buy volume cap"
        if kind == "sweep" and any(k == "sweep" and now - t < 3000 for t, k, _ in self.ev):
            return "sweep already signed this hour"
        return None

    def record(self, kind: str, lamports: int, now: Optional[float] = None) -> None:
        self.ev.append((now or time.time(), kind, int(lamports)))
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(list(self.ev), fh)
        os.replace(tmp, self.path)


class Signer:
    def __init__(self, cfg: policy.Config, backend, chain: Chain, *, mode: str, state_dir: str, halt_file: str):
        self.cfg, self.backend, self.chain = cfg, backend, chain
        self.mode = mode if mode in ("shadow", "live") else "shadow"
        self.state_dir, self.halt_file = state_dir, halt_file
        self.limits = Limits(cfg, os.path.join(state_dir, "limits.json"))
        if backend is not None and cfg.wallet and backend.pubkey != cfg.wallet:
            raise RuntimeError("keypair does not match SIGNER_WALLET")
        self.wallet = cfg.wallet or (backend.pubkey if backend else "")
        self.cfg.wallet = self.wallet
        self.stats: Dict[str, int] = {}

    def _bump(self, k: str) -> None:
        self.stats[k] = self.stats.get(k, 0) + 1

    def _log(self, rec: Dict[str, Any]) -> None:
        try:
            with open(os.path.join(self.state_dir, "decisions.jsonl"), "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, separators=(",", ":"), default=str) + "\n")
        except OSError:
            pass

    async def evaluate(self, msg_bytes: bytes, intent: Dict[str, Any]) -> Dict[str, Any]:
        from solders.signature import Signature  # type: ignore
        from solders.transaction import VersionedTransaction  # type: ignore
        from solders.message import to_bytes_versioned  # type: ignore
        msg = policy.decode_message(msg_bytes)
        if type(msg).__name__ not in ("Message", "MessageV0"):
            return {"ok": False, "reasons": [f"unsupported message version {type(msg).__name__}"]}
        if to_bytes_versioned(msg) != msg_bytes:
            return {"ok": False, "reasons": ["non-canonical message bytes"]}
        t_a = time.perf_counter()
        alts = await self.chain.alts(policy.alt_keys(msg)) if policy.alt_keys(msg) else {}
        t_alts = round((time.perf_counter() - t_a) * 1000, 1)
        keys, writable = policy.resolve(msg, alts)
        st = policy.check_static(msg, keys, writable, self.wallet, intent, self.cfg)
        out: Dict[str, Any] = {"static_violations": st.violations, "programs": sorted(set(st.programs)),
                               "priority_lamports": st.priority_lamports, "external_fee_payer": st.fee_payer_external}
        if st.violations:
            return {**out, "ok": False, "reasons": st.violations}
        unsigned = VersionedTransaction.populate(msg, [Signature.default()] * msg.header.num_required_signatures)
        addrs = list(dict.fromkeys([self.wallet] + st.writable))
        t_s = time.perf_counter()
        pre_t = asyncio.create_task(self.chain.accounts(addrs))
        sim = await self.chain.simulate(base64.b64encode(bytes(unsigned)).decode(), addrs)
        pre = await pre_t
        out.update(sim_err=sim["err"], units=sim["units"],
                   stage_ms={"alts": t_alts, "sim": round((time.perf_counter() - t_s) * 1000, 1)})
        if sim["err"] is not None:
            return {**out, "ok": False, "reasons": [f"simulation failed: {str(sim['err'])[:160]}"],
                    "logs_tail": sim["logs_tail"]}
        ev, eff = policy.check_effects(pre, sim["post"], self.wallet, intent, st, self.cfg)
        out["effects"] = eff
        if ev:
            return {**out, "ok": False, "reasons": ev}
        return {**out, "ok": True, "reasons": [], "_unsigned": bytes(unsigned)}

    async def handle(self, req: Dict[str, Any]) -> Dict[str, Any]:
        op = req.get("op")
        if op == "pubkey":
            return {"ok": True, "pubkey": self.wallet}
        if op == "sweep_info":
            return {"ok": bool(self.cfg.cold_address), "cold_address": self.cfg.cold_address,
                    "float_lamports": self.cfg.float_lamports}
        if op == "sender_tips":                          # v1.26: the pinned Helius Sender tip list + cap (read-only)
            cap = min(self.cfg.max_tip_lamports, policy.TIP_CEILING_LAMPORTS)
            return {"ok": cap > 0, "accounts": sorted(policy.HELIUS_SENDER_TIP_ACCOUNTS), "max_tip_lamports": cap}
        if op == "health":
            return {"ok": True, "mode": self.mode, "halted": os.path.exists(self.halt_file),
                    "backend": getattr(self.backend, "name", None), "wallet": self.wallet, "stats": self.stats,
                    "external_fee_payer_allowed": self.cfg.allow_external_fee_payer}
        if op != "sign":
            return {"ok": False, "error": "unknown op"}
        t0 = time.perf_counter()
        intent = req.get("intent") or {}
        dry = bool(req.get("dry_run")) or self.mode == "shadow"
        rec: Dict[str, Any] = {"ts_ms": int(time.time() * 1000), "kind": intent.get("kind"),
                               "in": intent.get("input_mint"), "out": intent.get("output_mint"),
                               "amount": intent.get("amount"), "router": intent.get("router"),
                               "tag": str(intent.get("tag") or "")[:40], "dry_run": dry, "mode": self.mode}
        if os.path.exists(self.halt_file):
            res = {"ok": False, "signed": False, "reasons": ["signer halted (tripwire)"]}
        else:
            try:
                msg_bytes = base64.b64decode(req.get("msg_b64") or "", validate=True)
                res = await self.evaluate(msg_bytes, intent)
            except Exception as exc:  # noqa: BLE001
                res = {"ok": False, "reasons": [f"evaluation error {type(exc).__name__}: {str(exc)[:120]}"]}
            unsigned = res.pop("_unsigned", None)
            res["signed"] = False
            if res.get("ok") and not dry:
                lam = int(intent.get("amount") or 0) if intent.get("kind") in ("buy", "sweep") else 0
                why = self.limits.check(intent.get("kind"), lam)
                if why:
                    res.update(ok=False, reasons=[why])
                else:
                    try:
                        t_k = time.perf_counter()
                        sig = await self.backend.sign(msg_bytes, unsigned)
                        res.setdefault("stage_ms", {})["sign"] = round((time.perf_counter() - t_k) * 1000, 2)
                        self.limits.record(intent.get("kind"), lam)
                        from solders.signature import Signature  # type: ignore
                        res.update(signed=True, signature_b64=base64.b64encode(sig).decode(),
                                   signature=str(Signature.from_bytes(sig)))
                    except Exception as exc:  # noqa: BLE001
                        res.update(ok=False, reasons=[f"backend sign failed: {type(exc).__name__}"])
        res["ms"] = round((time.perf_counter() - t0) * 1000, 1)
        self._bump(f"{'pass' if res.get('ok') else 'refuse'}_{intent.get('kind')}")
        rec.update({k: res.get(k) for k in ("ok", "signed", "reasons", "programs", "priority_lamports",
                                            "external_fee_payer", "effects", "sim_err", "ms", "signature", "stage_ms")})
        self._log(rec)
        return res


def peer_uid(sock: socket.socket) -> int:
    creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    return struct.unpack("3i", creds)[1]


async def serve(signer: Signer, path: str, allowed_uids: List[int]) -> None:
    async def client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        sock = writer.get_extra_info("socket")
        try:
            uid = peer_uid(sock)
        except OSError:
            uid = -1
        if uid not in allowed_uids:
            log.warning("refused connection from uid %s", uid)
            writer.close()
            return
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    req = json.loads(line)
                    res = await signer.handle(req if isinstance(req, dict) else {})
                except json.JSONDecodeError:
                    res = {"ok": False, "error": "bad json"}
                writer.write((json.dumps(res, separators=(",", ":"), default=str) + "\n").encode())
                await writer.drain()
        except (ConnectionError, asyncio.LimitOverrunError, ValueError):
            pass
        finally:
            writer.close()

    if os.path.exists(path):
        os.unlink(path)
    old = os.umask(0o117)
    server = await asyncio.start_unix_server(client, path=path, limit=MAX_LINE)
    os.umask(old)
    os.chmod(path, 0o660)
    log.info("fomo-signer listening (%s mode, backend %s)", signer.mode, getattr(signer.backend, "name", None))
    async with server:
        await server.serve_forever()


def main() -> None:
    from . import backends
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)      # v1.40c: INFO request lines leaked the RPC key
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    cfg = policy.Config.from_env()
    backend = backends.from_env()
    state = os.getenv("SIGNER_STATE_DIR", "/var/lib/fomo-signer")
    signer = Signer(cfg, backend, Chain(), mode=os.getenv("SIGNER_MODE", "shadow").strip().lower(), state_dir=state,
                    halt_file=os.getenv("SIGNER_HALT_FILE", os.path.join(state, "HALT")))
    uids = [pwd.getpwnam(u.strip()).pw_uid for u in os.getenv("SIGNER_ALLOWED_USERS", "fomo").split(",") if u.strip()]
    rd = os.getenv("RUNTIME_DIRECTORY", "/run/fomo-signer").split(":")[0]
    asyncio.run(serve(signer, os.path.join(rd, "signer.sock"), uids))


if __name__ == "__main__":
    main()
