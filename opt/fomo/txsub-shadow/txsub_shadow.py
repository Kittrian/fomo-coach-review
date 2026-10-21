#!/usr/bin/env python3
"""fomo txsub shadow (v1.38b): READ-ONLY copied-wallet detector on Helius Enhanced WebSocket transactionSubscribe
(processed, full tx). Never signs, never trades, never touches the coach. For every scout/confirmer wallet tx:
  - logs sig, wallet, tx slot, our receive time and the head slot at receive  -> compare with the coach's
    logsSubscribe->getTransaction path (data/scout_detections.jsonl recv_ms, joined by sig in the report script);
  - for pump.fun bonding-curve BUYS: watches the curve account (accountSubscribe, processed) and records the curve
    price at the wallet's slot +0/+2/+4/+10  -> paper latency budgets (what each saved block is worth).
Output: /opt/fomo/coach/data/txsub_shadow.jsonl. Key: HELIUS_API_KEY from a systemd credential (never logged)."""
from __future__ import annotations

import asyncio
import base64
import json
import os
import struct
import time
from typing import Any, Dict, List, Optional

import httpx
import websockets

DATA = os.getenv("TXSUB_DATA_DIR", "/opt/fomo/coach/data")
OUT = os.path.join(DATA, "txsub_shadow.jsonl")
PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
WSOL = "So11111111111111111111111111111111111111112"
BUDGETS = (0, 2, 4, 10)
ROLES = set((os.getenv("TXSUB_ROLES") or "scout,confirmer").split(","))
WATCH_S = float(os.getenv("TXSUB_CURVE_WATCH_S", "30"))
MAX_CURVES = int(os.getenv("TXSUB_MAX_CURVES", "20"))


def now_ms() -> int:
    return int(time.time() * 1000)


def log(rec: Dict[str, Any]) -> None:
    rec.setdefault("ts_ms", now_ms())
    with open(OUT, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, separators=(",", ":")) + "\n")


def wallets() -> Dict[str, str]:
    d = json.load(open(os.path.join(DATA, "scout_wallets.json")))
    return {w["address"]: w.get("label") or w["address"][:6] for w in d.get("wallets", [])
            if w.get("role") in ROLES and w.get("address")}


def curve_price(data_b64: str) -> Optional[Dict[str, Any]]:
    try:
        b = base64.b64decode(data_b64)
        vtok, vsol, rtok, rsol, supply = struct.unpack_from("<QQQQQ", b, 8)
        complete = bool(b[48]) if len(b) > 48 else None
        return {"price": vsol / vtok if vtok else None, "vsol": vsol, "vtok": vtok, "complete": complete}
    except Exception:  # noqa: BLE001
        return None


def parse_tx(res: Dict[str, Any], watched: Dict[str, str]) -> Optional[Dict[str, Any]]:
    """transactionNotification result (jsonParsed) -> trade view for the watched signer, or None."""
    tx = res.get("transaction") or {}
    meta = tx.get("meta") or {}
    msg = (tx.get("transaction") or {}).get("message") or {}
    keys = [k["pubkey"] if isinstance(k, dict) else k for k in msg.get("accountKeys") or []]
    signers = [k["pubkey"] for k in msg.get("accountKeys") or [] if isinstance(k, dict) and k.get("signer")]
    w = next((s for s in signers if s in watched), None) or next((k for k in keys if k in watched), None)
    if not w or meta.get("err") is not None:
        return None
    def bal(arr):
        out: Dict[str, int] = {}
        for t in arr or []:
            if t.get("owner") == w and t.get("mint") != WSOL:
                out[t["mint"]] = out.get(t["mint"], 0) + int(t["uiTokenAmount"]["amount"])
        return out
    pre, post = bal(meta.get("preTokenBalances")), bal(meta.get("postTokenBalances"))
    deltas = {m: post.get(m, 0) - pre.get(m, 0) for m in set(pre) | set(post)}
    deltas = {m: d for m, d in deltas.items() if d}
    if not deltas:
        return {"wallet": w, "mint": None, "side": None}
    mint, d = max(deltas.items(), key=lambda kv: abs(kv[1]))
    curve = None
    ixs = list(msg.get("instructions") or [])
    for inner in meta.get("innerInstructions") or []:
        ixs += inner.get("instructions") or []
    for ix in ixs:
        if ix.get("programId") == PUMP and len(ix.get("accounts") or []) > 3 and ix["accounts"][2] == mint:
            curve = ix["accounts"][3]
            break
    return {"wallet": w, "mint": mint, "side": "buy" if d > 0 else "sell", "token_delta": d, "curve": curve}


class Shadow:
    def __init__(self, key: str):
        self.key = key
        self.ws_url = os.getenv("TXSUB_WS_BASE", "wss://mainnet.helius-rpc.com/") + "?api-key=" + key
        self.rpc_url = os.getenv("TXSUB_RPC_BASE", "https://mainnet.helius-rpc.com/") + "?api-key=" + key
        self.head = 0
        self.rid = 10
        self.pending: Dict[int, Any] = {}
        self.subs: Dict[int, Dict[str, Any]] = {}        # account sub id -> watch
        self.watches: List[Dict[str, Any]] = []
        self.stats = {"tx": 0, "trades": 0, "budgets": 0, "reconnects": 0}

    async def send(self, ws, method: str, params: list) -> int:
        self.rid += 1
        await ws.send(json.dumps({"jsonrpc": "2.0", "id": self.rid, "method": method, "params": params}))
        return self.rid

    async def initial_price(self, w: Dict[str, Any]) -> None:
        try:
            async with httpx.AsyncClient(timeout=4.0) as cl:
                r = await cl.post(self.rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
                                                      "params": [w["curve"], {"encoding": "base64", "commitment": "processed"}]})
                j = r.json().get("result") or {}
            cp = curve_price(((j.get("value") or {}).get("data") or [None])[0] or "")
            if cp and cp["price"]:
                w["updates"].append((int((j.get("context") or {}).get("slot") or 0), cp["price"], now_ms()))
        except Exception as exc:  # noqa: BLE001
            w["err"] = type(exc).__name__

    def settle(self, force: bool = False) -> List[Dict[str, Any]]:
        done = []
        for w in self.watches:
            for n in BUDGETS:
                if str(n) in w["prices"]:
                    continue
                target = w["slot"] + n
                ups = [u for u in w["updates"] if u[0] <= target]
                if self.head > target or force:              # slot passed: price = last state at/before it
                    if ups:
                        u = max(ups, key=lambda u: (u[0], u[2]))
                        w["prices"][str(n)] = u[1]
                        w["price_slots"][str(n)] = u[0]
            if len(w["prices"]) == len(BUDGETS) or force or time.time() - w["t0"] > WATCH_S:
                done.append(w)
        for w in done:
            self.watches.remove(w)
            p0 = w["prices"].get("0")
            log({"kind": "budget", "sig": w["sig"], "wallet": w["wallet"], "label": w["label"], "mint": w["mint"],
                 "curve": w["curve"], "slot": w["slot"], "recv_ms": w["recv_ms"], "head_at_recv": w["head_at_recv"],
                 "prices": w["prices"], "price_slots": w["price_slots"],
                 "vs_slot0_pct": {k: round(100 * (v / p0 - 1), 3) for k, v in w["prices"].items()} if p0 else None,
                 "n_updates": len(w["updates"]), "err": w.get("err")})
            self.stats["budgets"] += 1
        return done

    async def run(self) -> None:
        while True:
            try:
                await self.session()
            except Exception as exc:  # noqa: BLE001
                log({"kind": "error", "error": type(exc).__name__, "detail": str(exc).replace(self.key, "<k>")[:160]})
            self.stats["reconnects"] += 1
            await asyncio.sleep(2)

    async def session(self) -> None:
        watched = wallets()
        async with websockets.connect(self.ws_url, open_timeout=10, ping_interval=20, max_size=2 ** 24) as ws:
            await self.send(ws, "transactionSubscribe", [{"accountInclude": list(watched), "failed": False, "vote": False},
                                                         {"commitment": "processed", "encoding": "jsonParsed",
                                                          "transactionDetails": "full", "maxSupportedTransactionVersion": 1}])
            await self.send(ws, "slotSubscribe", [])
            log({"kind": "start", "wallets": len(watched), "budgets": BUDGETS})
            last_hb = time.time()
            sub_ids: Dict[int, str] = {}
            while True:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                except asyncio.TimeoutError:
                    raw = None
                t_recv = now_ms()
                if raw:
                    m = json.loads(raw)
                    meth = m.get("method")
                    if meth == "slotNotification":
                        self.head = max(self.head, int(m["params"]["result"]["slot"]))
                    elif meth == "transactionNotification":
                        self.stats["tx"] += 1
                        res = m["params"]["result"]
                        slot = int(res.get("slot") or 0)
                        sig = res.get("signature")
                        tv = parse_tx(res, watched)
                        if tv:
                            self.stats["trades"] += 1
                            log({"kind": "detect", "sig": sig, "slot": slot, "recv_ms": t_recv, "head_at_recv": self.head,
                                 "slot_lag": (self.head - slot) if self.head else None, "label": watched.get(tv["wallet"]),
                                 **tv})
                            if tv.get("side") == "buy" and tv.get("curve") and len(self.watches) < MAX_CURVES:
                                w = {"sig": sig, "wallet": tv["wallet"], "label": watched.get(tv["wallet"]),
                                     "mint": tv["mint"], "curve": tv["curve"], "slot": slot, "recv_ms": t_recv,
                                     "head_at_recv": self.head, "t0": time.time(), "updates": [], "prices": {},
                                     "price_slots": {}}
                                self.watches.append(w)
                                rid = await self.send(ws, "accountSubscribe", [tv["curve"], {"encoding": "base64",
                                                                                            "commitment": "processed"}])
                                self.pending[rid] = w
                                asyncio.ensure_future(self.initial_price(w))
                    elif meth == "accountNotification":
                        sid = m["params"]["subscription"]
                        w = self.subs.get(sid)
                        if w is not None:
                            r = m["params"]["result"]
                            cp = curve_price((r.get("value") or {}).get("data", [""])[0])
                            if cp and cp["price"]:
                                w["updates"].append((int(r["context"]["slot"]), cp["price"], t_recv))
                    elif "id" in m and m["id"] in self.pending and "result" in m:
                        w = self.pending.pop(m["id"])
                        self.subs[int(m["result"])] = w
                        sub_ids[int(m["result"])] = w["sig"]
                for w in self.settle():
                    for sid, s in list(sub_ids.items()):
                        if s == w["sig"]:
                            await self.send(ws, "accountUnsubscribe", [sid])
                            sub_ids.pop(sid, None)
                            self.subs.pop(sid, None)
                if time.time() - last_hb > 60:
                    last_hb = time.time()
                    log({"kind": "heartbeat", "head": self.head, "watches": len(self.watches), **self.stats})


def main() -> None:
    key = os.getenv("HELIUS_API_KEY", "").strip()
    if not key:
        raise SystemExit("txsub shadow: no HELIUS_API_KEY credential")
    asyncio.run(Shadow(key).run())


if __name__ == "__main__":
    main()
