#!/usr/bin/env python3
"""fomo tripwire (VPS layer 3; also runs on the Mac as a cheap watchdog). Polls the wallet's signatures and trips on:

  unknown_tx      a transaction our wallet SIGNED whose signature is not in the known set (Mac: the coach ledger
                  data/live_trades.jsonl; VPS: fomo-signer's decisions.jsonl) after a grace period → key in other hands
  big_outflow     any transaction that takes more than TRIP_MAX_TX_OUTFLOW_SOL from the wallet (known or not)
  signer_attack   (VPS) ≥ TRIP_REFUSAL_BURST dangerous signer refusals in 10 min (destination / program / authority)
  integrity       (VPS) the signer's code or launcher changed vs. the baseline hashes

On trip (TRIP_MODE): log → always; alert file into TRIP_ALERT_SPOOL (the Telegram watcher sends it to Saved Messages);
kill → also create the coach kill-switch file; full → also create the signer HALT file and stop fomo-signer.
report = log + alert only. TRIP_UNKNOWN_TX_ACTION=alert makes unknown_tx alert-only in any mode; ignore/off skips it entirely (no log, no alert). Reads only public chain data; never holds or prints keys / RPC URLs."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

import httpx

DANGEROUS = ("non-allowed destination", "program not allowed", "Approve", "SetAuthority", "durable-nonce",
             "assign/allocate", "change owner", "delegate", "close authority", "rent to someone else",
             "SOL value would fall")


def env_file(path: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    try:
        for ln in Path(os.path.expanduser(path)).read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, v = ln.split("=", 1)
                out[k.strip().removeprefix("export ").strip()] = v.strip().strip("'\"")
    except OSError:
        pass
    return out


def cfg() -> Dict[str, Any]:
    e = dict(env_file(os.getenv("TRIP_COACH_ENV", ""))) if os.getenv("TRIP_COACH_ENV") else {}
    g = lambda k, d="": os.getenv(k) or d  # noqa: E731
    return {
        "wallet": g("TRIP_WALLET", os.getenv("SIGNER_WALLET") or e.get("SOLANA_WALLET", "")),
        "rpc_urls": [u for u in [g("TRIP_RPC_URL", "https://solana-rpc.publicnode.com"),
                                 g("TRIP_RPC_FALLBACK", os.getenv("SIGNER_RPC_URL") or e.get("SOLANA_RPC_URL", ""))] if u],
        "mode": g("TRIP_MODE", "report"),
        "known_files": [os.path.expanduser(p) for p in g("TRIP_KNOWN_FILES").split(",") if p.strip()],
        "signer_log": os.path.expanduser(g("TRIP_SIGNER_LOG")),
        "kill_file": os.path.expanduser(g("TRIP_KILL_FILE", e.get("LIVE_KILL_FILE", ""))),
        "halt_file": os.path.expanduser(g("TRIP_HALT_FILE")),
        "spool": os.path.expanduser(g("TRIP_ALERT_SPOOL")),
        "state": os.path.expanduser(g("TRIP_STATE", "~/.fomo-tripwire.json")),
        "log": os.path.expanduser(g("TRIP_LOG", "~/.fomo-tripwire.jsonl")),
        "grace_s": float(g("TRIP_GRACE_S", "120")),
        "poll_s": float(g("TRIP_POLL_S", "20")),
        "max_outflow_lamports": int(float(g("TRIP_MAX_TX_OUTFLOW_SOL", "0.6")) * 1e9),
        "refusal_burst": int(g("TRIP_REFUSAL_BURST", "3")),
        "integrity_paths": [p for p in g("TRIP_INTEGRITY_PATHS").split(",") if p.strip()],
        "integrity_baseline": os.path.expanduser(g("TRIP_INTEGRITY_BASELINE")),
        "stop_units": [u for u in g("TRIP_STOP_UNITS").split(",") if u.strip()],
        # Andrew Oct 14 8:43 PM CT: unknown_tx (wallet signed a tx the signer did not make) → alert only (log + spool),
        # never kill / HALT / stop units. "trip" restores the old behaviour. Other trip kinds are unaffected.
        "unknown_tx_action": (g("TRIP_UNKNOWN_TX_ACTION", "trip").strip().lower() or "trip"),
        # Andrew Oct 19 4:30 PM CT (FOMOANT): big_outflow (wallet transfer out) -> alert only (log + spool), never pause.
        "big_outflow_action": (g("TRIP_BIG_OUTFLOW_ACTION", "trip").strip().lower() or "trip"),
        "label": g("TRIP_LABEL", "fomo"),
    }


def _sigs_in(obj: Any, out: Set[str]) -> None:
    if isinstance(obj, dict):
        for v in obj.values():
            _sigs_in(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _sigs_in(v, out)
    elif isinstance(obj, str) and 80 <= len(obj) <= 90 and obj.isalnum():
        out.add(obj)


def known_signatures(files: Iterable[str]) -> Set[str]:
    """Every base58 signature-looking string in the given JSONL/JSON files (ledger rows, signer decisions)."""
    out: Set[str] = set()
    for f in files:
        try:
            with open(f, encoding="utf-8") as fh:
                for ln in fh:
                    try:
                        _sigs_in(json.loads(ln), out)
                    except ValueError:
                        pass
        except OSError:
            pass
    return out


def tx_view(tx: Dict[str, Any], wallet: str) -> Dict[str, Any]:
    """Is our wallet a signer? SOL change for our wallet (incl. fee if we paid)."""
    msg = (tx.get("transaction") or {}).get("message") or {}
    keys = [k if isinstance(k, str) else k.get("pubkey") for k in msg.get("accountKeys") or []]
    nsig = ((msg.get("header") or {}).get("numRequiredSignatures")) or 0
    meta = tx.get("meta") or {}
    delta = 0
    if wallet in keys:
        i = keys.index(wallet)
        pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
        if i < len(pre) and i < len(post):
            delta = int(post[i]) - int(pre[i])
    return {"we_signed": wallet in keys[:nsig], "fee_payer_us": bool(keys) and keys[0] == wallet,
            "sol_delta": delta, "err": meta.get("err")}


def refusal_burst(signer_log: str, now_ms: int, window_ms: int = 600_000) -> int:
    n = 0
    try:
        with open(signer_log, encoding="utf-8") as fh:
            lines = fh.readlines()[-500:]
    except OSError:
        return 0
    for ln in lines:
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if r.get("ok") is False and now_ms - int(r.get("ts_ms") or 0) < window_ms and \
                any(d in x for x in r.get("reasons") or [] for d in DANGEROUS):
            n += 1
    return n


def hashes(paths: List[str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for root in paths:
        p = Path(root)
        files = [p] if p.is_file() else sorted(x for x in p.rglob("*") if x.is_file() and "__pycache__" not in x.parts)
        for f in files:
            out[str(f)] = hashlib.sha256(f.read_bytes()).hexdigest()
    return out


class Tripwire:
    def __init__(self, c: Dict[str, Any], rpc=None):
        self.c = c
        self.rpc = rpc or self._rpc
        self.state = self._load()
        self.client = httpx.Client(timeout=15.0)

    def _load(self) -> Dict[str, Any]:
        try:
            return json.loads(Path(self.c["state"]).read_text())
        except Exception:  # noqa: BLE001
            return {"last_sig": None, "pending": {}, "tripped": []}

    def _save(self) -> None:
        p = Path(self.c["state"])
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state))
        os.replace(tmp, p)

    def _rpc(self, method: str, params: List[Any]) -> Any:
        last: Optional[Exception] = None
        for attempt, url in enumerate(self.c["rpc_urls"] * 2):
            if attempt >= len(self.c["rpc_urls"]):
                time.sleep(1.5)                                   # second round after a short backoff (429s)
            try:
                r = self.client.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
                j = r.json()
                if "error" in j:
                    raise RuntimeError(f"{method} rpc error")
                return j.get("result")
            except Exception as exc:  # noqa: BLE001
                last = exc
        raise RuntimeError(f"{method} failed on all RPCs ({type(last).__name__})")

    def log(self, rec: Dict[str, Any]) -> None:
        rec = {"ts_ms": int(time.time() * 1000), **rec}
        with open(self.c["log"], "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, separators=(",", ":")) + "\n")

    def trip(self, kind: str, detail: Dict[str, Any], *, soft: bool = False) -> None:
        key = f"{kind}:{detail.get('signature') or detail.get('path') or ''}"
        if key in self.state.setdefault("tripped", []):
            return
        self.state["tripped"] = (self.state["tripped"] + [key])[-200:]
        mode = "report" if soft else self.c["mode"]        # soft: a FAILED unknown tx can't move funds → alert only
        actions = ["log"]
        if mode in ("kill", "full") and self.c["kill_file"]:
            Path(self.c["kill_file"]).parent.mkdir(parents=True, exist_ok=True)
            Path(self.c["kill_file"]).write_text(f"tripwire {kind} {time.strftime('%Y-%m-%d %H:%M:%S %Z')}\n")
            actions.append("kill_switch")
        if mode == "full":
            if self.c["halt_file"]:
                try:
                    Path(self.c["halt_file"]).write_text(f"tripwire {kind}\n")
                    actions.append("signer_halt_file")
                except OSError:
                    pass
            for u in self.c["stop_units"]:
                if subprocess.run(["systemctl", "stop", u], capture_output=True).returncode == 0:
                    actions.append(f"stopped:{u}")
        text = (f"🚨 TRIPWIRE [{self.c['label']}] {kind}\n"
                + "\n".join(f"{k}: {v}" for k, v in detail.items() if v is not None)
                + f"\nactions: {', '.join(actions)} (mode {mode})"
                + ("\nKill switch is ON — remove the KILL file only after checking the wallet." if "kill_switch" in actions else ""))
        if self.c["spool"]:
            try:
                d = Path(self.c["spool"])
                d.mkdir(parents=True, exist_ok=True)
                f = d / f"trip-{int(time.time() * 1000)}.json"
                f.write_text(json.dumps({"text": text, "kind": kind}))
                actions.append("alert_spooled")
            except OSError:
                pass
        self.log({"event": "TRIP", "kind": kind, "detail": detail, "actions": actions, "mode": mode})

    def check_chain(self, now: Optional[float] = None) -> List[str]:
        now = now or time.time()
        w = self.c["wallet"]
        params: Dict[str, Any] = {"limit": 50}
        if self.state.get("last_sig"):
            params["until"] = self.state["last_sig"]
        sigs = self.rpc("getSignaturesForAddress", [w, params]) or []
        if not self.state.get("last_sig"):                       # first run: baseline, don't judge history
            self.state["last_sig"] = sigs[0]["signature"] if sigs else None
            self.log({"event": "baseline", "last_sig_set": bool(sigs)})
            return []
        if sigs:
            self.state["last_sig"] = sigs[0]["signature"]
        pend = self.state.setdefault("pending", {})
        for s in sigs:
            pend.setdefault(s["signature"], {"seen": now, "block_time": s.get("blockTime")})
        if not pend:
            return []
        known = known_signatures(self.c["known_files"] + ([self.c["signer_log"]] if self.c["signer_log"] else []))
        tripped: List[str] = []
        for sig, meta in list(pend.items()):
            if now - meta["seen"] < self.c["grace_s"] and sig not in known:
                continue
            tx = self.rpc("getTransaction", [sig, {"encoding": "json", "maxSupportedTransactionVersion": 1,
                                                   "commitment": "confirmed"}])
            if tx is None:
                if now - meta["seen"] > 900:
                    pend.pop(sig, None)
                continue
            pend.pop(sig, None)
            v = tx_view(tx, w)
            out = -v["sol_delta"]
            all_sigs = set((tx.get("transaction") or {}).get("signatures") or []) | {sig}
            is_known = bool(all_sigs & known)          # JupiterZ: tx id is the MM's sig; ours is the 2nd
            if v["we_signed"] and not is_known and self.c.get("unknown_tx_action") in ("ignore", "off"):
                continue                               # Andrew Oct 14 9:45 PM CT: unknown_tx ignored entirely
            if v["we_signed"] and not is_known:
                self.trip("unknown_tx", {"signature": sig, "sol_out": round(out / 1e9, 4), "failed": v["err"] is not None},
                          soft=v["err"] is not None or self.c.get("unknown_tx_action") == "alert")
                tripped.append(sig)
            elif out > self.c["max_outflow_lamports"] and v["err"] is None:
                self.trip("big_outflow", {"signature": sig, "sol_out": round(out / 1e9, 4), "known": is_known},
                          soft=self.c.get("big_outflow_action") == "alert")
                tripped.append(sig)
        return tripped

    def check_signer(self) -> None:
        if self.c["signer_log"]:
            n = refusal_burst(self.c["signer_log"], int(time.time() * 1000))
            if n >= self.c["refusal_burst"]:
                self.trip("signer_attack", {"dangerous_refusals_10min": n, "path": "burst-" + time.strftime("%Y%m%d%H")})
        if self.c["integrity_paths"] and self.c["integrity_baseline"]:
            base_p = Path(self.c["integrity_baseline"])
            cur = hashes(self.c["integrity_paths"])
            if not base_p.exists():
                base_p.write_text(json.dumps(cur))
                self.log({"event": "integrity_baseline", "files": len(cur)})
                return
            base = json.loads(base_p.read_text())
            changed = sorted(set(k for k in set(base) | set(cur) if base.get(k) != cur.get(k)))
            if changed:
                self.trip("integrity", {"changed": ", ".join(changed[:5]), "path": "|".join(changed[:5])})

    def run_once(self) -> None:
        try:
            self.check_chain()
        except Exception as exc:  # noqa: BLE001
            self.log({"event": "error", "where": "chain", "error": type(exc).__name__})
        try:
            self.check_signer()
        except Exception as exc:  # noqa: BLE001
            self.log({"event": "error", "where": "signer", "error": type(exc).__name__})
        self._save()


def main() -> None:
    c = cfg()
    if not c["wallet"]:
        sys.exit("tripwire: no wallet configured")
    t = Tripwire(c)
    t.log({"event": "start", "mode": c["mode"], "known_files": len(c["known_files"]), "label": c["label"]})
    once = "--once" in sys.argv
    while True:
        t.run_once()
        if once:
            break
        time.sleep(c["poll_s"])


if __name__ == "__main__":
    main()
