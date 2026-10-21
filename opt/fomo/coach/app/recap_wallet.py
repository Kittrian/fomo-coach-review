"""v1.17.4 Day Recap wallet panel — READ-ONLY, public addresses only, off the hot path.

GET /api/recap/wallet?date=YYYY-MM-DD → balances, token holdings (SPL Token + Token-2022), day start→now change,
fees/rent, closable empty token accounts, recent transactions, balance series.

Wallets:
* the bot's trading wallet = the PUBLIC address in env SOLANA_WALLET (or JUPITER_USER_PUBLIC_KEY). This module never
  reads SOLANA_KEYPAIR_PATH, never loads a keypair, and never returns any key material or file path;
* optional watch-only public addresses: RECAP_WATCH_WALLETS="Label=Address,Address2" (default empty).

Network: keyless public RPCs only (RECAP_WALLET_RPC_URLS, default publicnode then api.mainnet-beta — publicnode
blocks getTokenAccountsByOwner so that call starts at mainnet-beta). Never Helius. DEX Screener / Jupiter lite for
prices. Everything is cached (wallet state ≥ 45 s, prices 60 s, per-signature tx summaries forever) and soft-fails.
Snapshots: a low-priority loop appends {ts_ms, wallet, sol} to data/wallet_snapshots.jsonl every 5 min.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import time
from datetime import date as _date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Query

from app.paths import data_path

logger = logging.getLogger("coach.recap.wallet")
router = APIRouter()

WSOL = "So11111111111111111111111111111111111111112"
TOKEN_PROGRAMS = {"spl": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "t22": "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"}
DEX_URL = "https://api.dexscreener.com/tokens/v1/solana/"
JUP_PRICE_URL = "https://lite-api.jup.ag/price/v3?ids="
B58 = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
STABLES = {"EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"}


def _f(name: str, d: float) -> float:
    try:
        return float(os.getenv(name, str(d)).strip())
    except ValueError:
        return d


def _net_enabled() -> bool:
    return os.getenv("RECAP_NET_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")


def rpc_urls() -> List[str]:
    v = os.getenv("RECAP_WALLET_RPC_URLS", "").strip()
    urls = [u.strip() for u in v.split(",") if u.strip()] if v else \
        ["https://solana-rpc.publicnode.com", "https://api.mainnet-beta.solana.com"]
    return [u for u in urls if "helius" not in u.lower() and "api-key" not in u.lower() and "api_key" not in u.lower()]


def short(a: Optional[str], n: int = 4) -> str:
    return f"{a[:n]}…{a[-n:]}" if a and len(a) > 2 * n + 1 else (a or "")


def wallets() -> List[Dict[str, str]]:
    """Public addresses only. Never touches keypair files."""
    out: List[Dict[str, str]] = []
    tw = (os.getenv("SOLANA_WALLET", "").strip() or os.getenv("JUPITER_USER_PUBLIC_KEY", "").strip())
    if B58.match(tw or ""):
        out.append({"address": tw, "label": "Bot trading wallet", "role": "trading"})
    for i, part in enumerate(x.strip() for x in os.getenv("RECAP_WATCH_WALLETS", "").split(",")):
        if not part:
            continue
        label, _, addr = part.rpartition("=")
        addr = addr.strip()
        if B58.match(addr) and all(w["address"] != addr for w in out):
            out.append({"address": addr, "label": (label.strip() or f"Watch wallet {i + 1}")[:40], "role": "watch"})
    return out[:6]


def _sol(lamports: Optional[int]) -> Optional[float]:
    return None if lamports is None else round(lamports / 1e9, 9)


# ------------------------------------------------------------------ state
class WalletBook:
    def __init__(self) -> None:
        self.state: Dict[str, Dict[str, Any]] = {}       # address → last refresh result
        self.sigs: Dict[str, Dict[str, Dict[str, Any]]] = {}  # address → sig → {slot, blockTime, err}
        self.tx: Dict[str, Dict[str, Any]] = {}          # f"{address}:{sig}" → summary (immutable once final)
        self.prices: Dict[str, Tuple[float, Optional[Dict[str, Any]]]] = {}
        self.sol_usd: Tuple[float, Optional[float]] = (0.0, None)
        self._tasks: Dict[str, asyncio.Task] = {}
        self.window: Dict[str, int] = {}                 # address → earliest day start (unix s) fetched so far
        self._loaded = False
        self.stats: Dict[str, int] = {"rpc_calls": 0, "rpc_errors": 0, "price_calls": 0, "price_errors": 0}
        self._snap_task: Optional[asyncio.Task] = None
        self.last_snapshot_ms: Optional[int] = None

    # ---- persistence (tx summaries only: public chain data)
    def cache_path(self) -> Path:
        return data_path("RECAP_WALLET_CACHE_PATH", "recap_wallet_cache.json")

    def load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            j = json.loads(self.cache_path().read_text())
            self.tx = {k: v for k, v in (j.get("tx") or {}).items() if isinstance(v, dict)}
            for k, v in self.tx.items():
                addr, _, sig = k.partition(":")
                self.sigs.setdefault(addr, {})[sig] = {"slot": v.get("slot"), "blockTime": v.get("blockTime"), "err": v.get("err")}
        except Exception:  # noqa: BLE001
            pass

    def save(self) -> None:
        try:
            p = self.cache_path()
            tmp = p.with_suffix(".tmp")
            keep = dict(sorted(self.tx.items(), key=lambda kv: -(kv[1].get("blockTime") or 0))[:3000])
            tmp.write_text(json.dumps({"tx": keep}))
            tmp.replace(p)
        except Exception:  # noqa: BLE001
            pass

    # ---- network helpers
    async def rpc(self, c: Any, method: str, params: List[Any], prefer_last: bool = False) -> Any:
        urls = rpc_urls()
        if prefer_last:
            urls = urls[::-1]
        for u in urls:
            self.stats["rpc_calls"] += 1
            try:
                r = await c.post(u, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
                j = r.json() if r.status_code == 200 else None
                if isinstance(j, dict) and "result" in j:
                    return j["result"]
            except Exception:  # noqa: BLE001
                pass
            self.stats["rpc_errors"] += 1
        return None

    async def _prices(self, c: Any, mints: List[str]) -> None:
        now = time.time()
        need = [m for m in dict.fromkeys(mints) if m and (m not in self.prices or now - self.prices[m][0] > 60)]
        for i in range(0, len(need), 30):
            chunk = need[i:i + 30]
            self.stats["price_calls"] += 1
            best: Dict[str, Dict[str, Any]] = {}
            try:
                r = await c.get(DEX_URL + ",".join(chunk))
                pairs = r.json() if r.status_code == 200 else []
            except Exception:  # noqa: BLE001
                pairs = []
                self.stats["price_errors"] += 1
            for p in pairs if isinstance(pairs, list) else []:
                if not isinstance(p, dict):
                    continue
                bt = p.get("baseToken") or {}
                m = bt.get("address")
                liq = float(((p.get("liquidity") or {}).get("usd")) or 0)
                if m in chunk and (m not in best or liq > best[m]["liquidity_usd"]):
                    try:
                        pu = float(p.get("priceUsd")) if p.get("priceUsd") else None
                    except (TypeError, ValueError):
                        pu = None
                    best[m] = {"symbol": bt.get("symbol"), "name": bt.get("name"), "price_usd": pu, "liquidity_usd": liq}
            for m in chunk:
                self.prices[m] = (now, best.get(m))
        # SOL/USD (DEX Screener wSOL, fallback Jupiter lite price)
        if now - self.sol_usd[0] > 60 or self.sol_usd[1] is None:
            px = (self.prices.get(WSOL) or (0, None))[1]
            v = (px or {}).get("price_usd")
            if not v:
                try:
                    r = await c.get(JUP_PRICE_URL + WSOL)
                    v = float(((r.json() or {}).get(WSOL) or {}).get("usdPrice") or 0) or None
                except Exception:  # noqa: BLE001
                    v = None
            if v:
                self.sol_usd = (now, float(v))

    # ---- tx summaries
    def _summarize(self, addr: str, sig: str, tx: Dict[str, Any], ledger_sigs: Dict[str, Tuple[str, Optional[str]]]) -> Dict[str, Any]:
        meta = tx.get("meta") or {}
        keys = ((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or []
        pk = [k.get("pubkey") if isinstance(k, dict) else k for k in keys]
        idx = pk.index(addr) if addr in pk else None
        pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
        pre_w = pre[idx] if idx is not None and idx < len(pre) else None
        post_w = post[idx] if idx is not None and idx < len(post) else None
        fee_payer = idx == 0
        fee = int(meta.get("fee") or 0) if fee_payer else 0
        created = closed = 0
        for i, (a, b) in enumerate(zip(pre, post)):
            if i == idx:
                continue
            if a == 0 and 1_400_000 <= b <= 2_500_000:            # new token account (rent-exempt minimum)
                created += b
            elif b == 0 and 1_400_000 <= a <= 2_500_000:
                closed += a
        tdelta: Dict[str, float] = {}
        for side, sign in (("preTokenBalances", -1), ("postTokenBalances", 1)):
            for t in meta.get(side) or []:
                if t.get("owner") == addr:
                    ui = (t.get("uiTokenAmount") or {}).get("uiAmount") or 0.0
                    tdelta[t.get("mint")] = tdelta.get(t.get("mint"), 0.0) + sign * float(ui)
        tdelta = {m: v for m, v in tdelta.items() if abs(v) > 1e-12}
        delta = (post_w - pre_w) if pre_w is not None and post_w is not None else None
        kind = "other"
        mint = None
        if meta.get("err") is not None:
            kind = "failed"
        elif sig in ledger_sigs:
            kind, mint = ledger_sigs[sig]
        elif tdelta:
            mint, v = max(tdelta.items(), key=lambda kv: abs(kv[1]))
            kind = "buy" if v > 0 and (delta or 0) < 0 else "sell" if v < 0 and (delta or 0) > 0 else \
                "token in" if v > 0 else "token out"
        elif delta is not None:
            net = delta + fee
            if closed and not created and net > 0:
                kind = "close accounts"
            elif net > 0:
                kind = "transfer in"
            elif net < 0:
                kind = "transfer out"
            else:
                kind = "fee only"
        return {"sig": sig, "slot": tx.get("slot"), "blockTime": tx.get("blockTime"), "err": meta.get("err") is not None,
                "pre": pre_w, "post": post_w, "delta": delta, "fee": fee, "fee_payer": fee_payer,
                "rent_paid": created if fee_payer else 0, "rent_recovered": closed if fee_payer else 0,
                "token_deltas": tdelta, "type": kind, "mint": mint}

    async def refresh(self, addr: str, start_s: int, ledger_sigs: Dict[str, Tuple[str, Optional[str]]]) -> None:
        import httpx
        self.load()
        out: Dict[str, Any] = {"errors": []}
        try:
            async with httpx.AsyncClient(timeout=8.0, headers={"user-agent": "fomo-signal-coach-recap"}) as c:
                bal = await self.rpc(c, "getBalance", [addr, {"commitment": "confirmed"}])
                out["lamports"] = (bal or {}).get("value") if isinstance(bal, dict) else None
                if out["lamports"] is None:
                    out["errors"].append("balance unavailable")
                # token accounts (publicnode blocks this method → start at the last URL)
                accts: List[Dict[str, Any]] = []
                for prog, pid in TOKEN_PROGRAMS.items():
                    res = await self.rpc(c, "getTokenAccountsByOwner", [addr, {"programId": pid},
                                                                          {"encoding": "jsonParsed", "commitment": "confirmed"}],
                                         prefer_last=True)
                    if res is None:
                        out["errors"].append(f"token accounts ({prog}) unavailable")
                        continue
                    for a in (res or {}).get("value") or []:
                        info = ((((a.get("account") or {}).get("data") or {}).get("parsed") or {}).get("info")) or {}
                        ta = info.get("tokenAmount") or {}
                        accts.append({"account": a.get("pubkey"), "program": prog, "mint": info.get("mint"),
                                      "amount": float(ta.get("uiAmount") or 0.0), "decimals": ta.get("decimals"),
                                      "rent_lamports": (a.get("account") or {}).get("lamports") or 0,
                                      "state": info.get("state")})
                out["accounts"] = accts
                # signatures: newest pages back to the requested day start (cap 5 pages)
                known = self.sigs.setdefault(addr, {})
                before = None
                for _ in range(5):
                    params: Dict[str, Any] = {"limit": 200}
                    if before:
                        params["before"] = before
                    page = await self.rpc(c, "getSignaturesForAddress", [addr, params])
                    if not isinstance(page, list):
                        out["errors"].append("signatures unavailable")
                        break
                    overlap = False
                    for s in page:
                        if s.get("signature") in known:
                            overlap = True
                        known[s["signature"]] = {"slot": s.get("slot"), "blockTime": s.get("blockTime"), "err": s.get("err") is not None}
                    if len(page) < 200 or not page:
                        break
                    oldest = page[-1]
                    if (oldest.get("blockTime") or 0) < start_s and overlap:
                        break
                    if (oldest.get("blockTime") or 0) < start_s - 86400:
                        break
                    before = oldest.get("signature")
                # tx details: recent 20 + everything since day start + one before it (for the start balance)
                order = sorted(known.items(), key=lambda kv: -((kv[1].get("slot") or 0)))
                want = [s for s, _ in order[:20]]
                in_day = [s for s, v in order if (v.get("blockTime") or 0) >= start_s]
                prior = [s for s, v in order if (v.get("blockTime") or 0) < start_s][:1]
                need = [s for s in dict.fromkeys(want + in_day + prior) if f"{addr}:{s}" not in self.tx][:120]
                sem = asyncio.Semaphore(3)

                async def one(sig: str) -> None:
                    async with sem:
                        tx = await self.rpc(c, "getTransaction", [sig, {"encoding": "jsonParsed", "commitment": "confirmed",
                                                                         "maxSupportedTransactionVersion": 0}])
                        if isinstance(tx, dict):
                            self.tx[f"{addr}:{sig}"] = self._summarize(addr, sig, tx, ledger_sigs)
                        await asyncio.sleep(0.1)
                if need:
                    await asyncio.gather(*(one(s) for s in need))
                    self.save()
                await self._prices(c, [WSOL] + [a["mint"] for a in accts if a.get("amount")])
        except Exception as exc:  # noqa: BLE001
            out["errors"].append(f"refresh failed: {type(exc).__name__}")
        out["refreshed_ms"] = int(time.time() * 1000)
        prev = self.state.get(addr) or {}
        if out.get("lamports") is None and prev.get("lamports") is not None:   # keep last good numbers
            out["lamports"] = prev["lamports"]
            out.setdefault("accounts", prev.get("accounts"))
            out["stale"] = True
        self.state[addr] = out

    def ensure(self, addr: str, start_s: int, ledger_sigs: Dict[str, Tuple[str, Optional[str]]],
               min_age_s: float) -> Optional[asyncio.Task]:
        """Start a background refresh if the cached state is older than min_age_s or doesn't reach back to start_s."""
        if not _net_enabled():
            return None
        st = self.state.get(addr) or {}
        fresh = bool(st.get("refreshed_ms")) and time.time() * 1000 - st["refreshed_ms"] < min_age_s * 1000
        covered = self.window.get(addr, 10 ** 12) <= start_s
        if fresh and covered:
            return None
        t = self._tasks.get(addr)
        if t and not t.done():
            return t
        try:
            t = asyncio.get_running_loop().create_task(self.refresh(addr, start_s, ledger_sigs))
        except RuntimeError:
            return None
        self._tasks[addr] = t
        self.window[addr] = min(self.window.get(addr, 10 ** 12), start_s)
        return t

    # ---- snapshots (low priority)
    def snapshot_path(self) -> Path:
        return data_path("RECAP_WALLET_SNAPSHOTS_PATH", "wallet_snapshots.jsonl")

    def snapshots_enabled(self) -> bool:
        if "_pytest" in sys.modules and os.getenv("RECAP_WALLET_SNAPSHOTS_FORCE") != "true":
            return False
        return _net_enabled() and os.getenv("RECAP_WALLET_SNAPSHOTS", "true").strip().lower() not in ("0", "false", "no", "off")

    def start(self) -> None:
        if not self.snapshots_enabled() or (self._snap_task and not self._snap_task.done()):
            return
        try:
            self._snap_task = asyncio.get_running_loop().create_task(self._snap_loop())
        except RuntimeError:
            pass

    async def stop(self) -> None:
        t = self._snap_task
        if t and not t.done():
            t.cancel()
            try:
                await t
            except BaseException:  # noqa: BLE001
                pass

    async def _snap_loop(self) -> None:
        import httpx
        await asyncio.sleep(_f("RECAP_WALLET_SNAPSHOT_DELAY_S", 45))
        while True:
            try:
                ws = wallets()
                if ws:
                    async with httpx.AsyncClient(timeout=6.0) as c:
                        rows = []
                        for w in ws:
                            bal = await self.rpc(c, "getBalance", [w["address"], {"commitment": "confirmed"}])
                            lam = (bal or {}).get("value") if isinstance(bal, dict) else None
                            if lam is not None:
                                rows.append({"ts_ms": int(time.time() * 1000), "wallet": w["address"], "role": w["role"],
                                             "sol": _sol(lam)})
                    if rows:
                        p = self.snapshot_path()
                        p.parent.mkdir(parents=True, exist_ok=True)
                        with p.open("a", encoding="utf-8") as fh:
                            fh.write("".join(json.dumps(r) + "\n" for r in rows))
                        self.last_snapshot_ms = rows[-1]["ts_ms"]
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.info("wallet snapshot soft-failed: %s", type(exc).__name__)
            await asyncio.sleep(max(60.0, _f("RECAP_WALLET_SNAPSHOT_S", 300)))


book = WalletBook()


# ------------------------------------------------------------------ view building (pure, from caches)
def _ledger_sigs(ledger_rows: List[Dict[str, Any]]) -> Dict[str, Tuple[str, Optional[str]]]:
    out: Dict[str, Tuple[str, Optional[str]]] = {}
    for r in ledger_rows:
        if r.get("kind") == "buy" and r.get("buy_sig"):
            out[r["buy_sig"]] = ("buy", r.get("mint"))
        elif r.get("kind") == "sell" and r.get("signature"):
            out[r["signature"]] = ("sell", r.get("mint"))
    return out


def _snapshots(addr: str, start_ms: int, end_ms: int) -> List[Dict[str, Any]]:
    from app.recap import read_jsonl
    return [r for r in read_jsonl(book.snapshot_path(), start_ms=start_ms, end_ms=end_ms, must_contain=addr, tag="wsnap")
            if r.get("wallet") == addr and isinstance(r.get("sol"), (int, float))]


def build_wallet_view(w: Dict[str, str], start_ms: int, end_ms: int, is_today: bool,
                      sym_hint: Dict[str, str]) -> Dict[str, Any]:
    addr = w["address"]
    st = book.state.get(addr) or {}
    sol_usd = book.sol_usd[1]
    lam = st.get("lamports")
    tokens, empty, dust = [], [], 0
    for a in st.get("accounts") or []:
        px = (book.prices.get(a["mint"]) or (0, None))[1] or {}
        price = 1.0 if a["mint"] in STABLES else px.get("price_usd")
        usd = round(a["amount"] * price, 4) if price is not None else None
        row = {"mint": a["mint"], "symbol": px.get("symbol") or sym_hint.get(a["mint"]) or short(a["mint"]),
               "amount": a["amount"], "decimals": a.get("decimals"), "price_usd": price, "usd": usd,
               "program": a["program"], "account": a.get("account"), "rent_sol": _sol(a.get("rent_lamports")),
               "liquidity_usd": px.get("liquidity_usd")}
        if a["amount"] <= 0:
            empty.append(row)
            continue
        row["dust"] = usd is None or usd < _f("RECAP_WALLET_DUST_USD", 0.5)
        dust += 1 if row["dust"] else 0
        tokens.append(row)
    tokens.sort(key=lambda t: -(t["usd"] or 0))
    sol = _sol(lam)
    tok_usd = round(sum(t["usd"] or 0 for t in tokens), 2)
    # ---- day window from tx summaries
    txs = [v for k, v in book.tx.items() if k.startswith(addr + ":")]
    txs.sort(key=lambda t: ((t.get("slot") or 0), t.get("blockTime") or 0))
    s_s, e_s = start_ms // 1000, end_ms // 1000
    in_day = [t for t in txs if s_s <= (t.get("blockTime") or 0) < e_s]
    before = [t for t in txs if (t.get("blockTime") or 0) < s_s]
    start_sol = end_sol = None
    src = None
    if in_day and in_day[0].get("pre") is not None:
        start_sol, src = _sol(in_day[0]["pre"]), "tx history"
    elif before and before[-1].get("post") is not None:
        start_sol, src = _sol(before[-1]["post"]), "tx history"
    snaps = _snapshots(addr, start_ms, end_ms)
    if start_sol is None and snaps:
        start_sol, src = snaps[0]["sol"], "first snapshot of the day"
    if is_today and sol is not None:
        end_sol = sol
        if start_sol is None and not in_day:
            start_sol, src = sol, "no transactions today"
    elif in_day and in_day[-1].get("post") is not None:
        end_sol = _sol(in_day[-1]["post"])
    elif start_sol is not None:
        end_sol = start_sol
    series = [[t["blockTime"] * 1000, _sol(t["post"])] for t in in_day if t.get("post") is not None and t.get("blockTime")]
    series += [[r["ts_ms"], r["sol"]] for r in snaps]
    series.sort()
    if start_sol is not None:
        series.insert(0, [start_ms if not in_day else min(start_ms + 1, in_day[0]["blockTime"] * 1000 - 1), start_sol])
    if is_today and sol is not None:
        series.append([int(time.time() * 1000), sol])
    fees = sum(t.get("fee") or 0 for t in in_day)
    rent_paid = sum(t.get("rent_paid") or 0 for t in in_day)
    rent_back = sum(t.get("rent_recovered") or 0 for t in in_day)
    t_in = sum((t.get("delta") or 0) + (t.get("fee") or 0) for t in in_day if t.get("type") == "transfer in")
    t_out = sum((t.get("delta") or 0) + (t.get("fee") or 0) for t in in_day if t.get("type") == "transfer out")
    types: Dict[str, int] = {}
    for t in in_day:
        types[t["type"]] = types.get(t["type"], 0) + 1
    change = round(end_sol - start_sol, 9) if start_sol is not None and end_sol is not None else None
    recent = sorted(txs, key=lambda t: -((t.get("slot") or 0)))[:20]
    sigs_known = sorted((book.sigs.get(addr) or {}).items(), key=lambda kv: -((kv[1].get("slot") or 0)))[:20]
    rmap = {t["sig"]: t for t in recent}
    recent_rows = []
    for sig, meta in sigs_known:
        t = rmap.get(sig) or book.tx.get(f"{addr}:{sig}")
        bt = (t or {}).get("blockTime") or meta.get("blockTime")
        mint = (t or {}).get("mint")
        recent_rows.append({"sig": sig, "ts_ms": bt * 1000 if bt else None,
                            "type": (t or {}).get("type") or ("failed" if meta.get("err") else "pending lookup"),
                            "sol_delta": _sol((t or {}).get("delta")), "fee_sol": _sol((t or {}).get("fee")),
                            "mint": mint, "symbol": ((book.prices.get(mint) or (0, None))[1] or {}).get("symbol")
                            or sym_hint.get(mint or "") or (short(mint) if mint else None)})
    closable = round(sum(e["rent_sol"] or 0 for e in empty), 9)
    dust_rent = round(sum(t["rent_sol"] or 0 for t in tokens if t["dust"]), 9)
    return {
        "label": w["label"], "role": w["role"], "address": addr, "short": short(addr),
        "solscan": f"https://solscan.io/account/{addr}",
        "sol": sol, "sol_usd_value": round(sol * sol_usd, 2) if sol is not None and sol_usd else None,
        "tokens_usd": tok_usd, "total_usd": round((sol or 0) * (sol_usd or 0) + tok_usd, 2) if sol_usd else None,
        "tokens": tokens, "dust_count": dust, "empty_accounts": empty,
        "rent": {"closable_empty_sol": closable, "empty_accounts": len(empty), "dust_accounts": dust,
                 "dust_rent_sol": dust_rent,
                 "note": "Empty token accounts each hold ~0.002 SOL rent that closing them would return. "
                         "Shown only — the coach takes no action."},
        "day": {"start_sol": start_sol, "end_sol": end_sol, "change_sol": change,
                "change_pct": round(change / start_sol * 100, 2) if change is not None and start_sol else None,
                "source": src, "tx_count": len(in_day), "types": types, "fees_sol": _sol(fees),
                "rent_paid_sol": _sol(rent_paid), "rent_recovered_sol": _sol(rent_back),
                "transfers_in_sol": _sol(t_in), "transfers_out_sol": _sol(t_out),
                "series": series[-400:], "snapshots": len(snaps), "end_is_now": bool(is_today)},
        "recent": recent_rows,
        "refreshed_ms": st.get("refreshed_ms"), "stale": bool(st.get("stale")), "errors": st.get("errors") or [],
    }


async def get_wallet_recap(d: _date, wait_s: float = 6.0) -> Dict[str, Any]:
    from app.recap import TZ, day_bounds, ledger_path, read_jsonl, enrich
    start_ms, end_ms = day_bounds(d)
    is_today = d == datetime.now(TZ).date()
    ws = wallets()
    ledger = read_jsonl(ledger_path(), tag="ledger")
    lsigs = _ledger_sigs(ledger)
    tasks = [t for t in (book.ensure(w["address"], start_ms // 1000, lsigs, _f("RECAP_WALLET_REFRESH_S", 45)) for w in ws) if t]
    if tasks and wait_s > 0 and not all((book.state.get(w["address"]) or {}).get("refreshed_ms") for w in ws):
        try:
            await asyncio.wait_for(asyncio.shield(asyncio.gather(*tasks)), timeout=wait_s)
        except Exception:  # noqa: BLE001
            pass
    sym_hint = {m: (v[1] or {}).get("symbol") for m, v in enrich.dex.items() if v[1] and (v[1] or {}).get("symbol")}
    views = [build_wallet_view(w, start_ms, end_ms, is_today, sym_hint) for w in ws]
    return {"ok": True, "read_only": True, "date": d.isoformat(), "is_today": is_today, "sol_usd": book.sol_usd[1],
            "wallets": views,
            "config": {"trading_wallet_env": "SOLANA_WALLET (public address only)",
                       "watch_wallets_env": "RECAP_WATCH_WALLETS=Label=Address,Address2 (optional, public addresses)",
                       "watch_wallets": sum(1 for w in ws if w["role"] == "watch"),
                       "snapshots": {"enabled": book.snapshots_enabled(), "every_s": max(60.0, _f("RECAP_WALLET_SNAPSHOT_S", 300)),
                                     "last_ms": book.last_snapshot_ms},
                       "refresh_s": _f("RECAP_WALLET_REFRESH_S", 45), "net": _net_enabled()},
            "stats": dict(book.stats),
            "note": None if ws else "No wallet configured: set SOLANA_WALLET (public address) or RECAP_WATCH_WALLETS."}


def wallet_brief() -> Optional[Dict[str, Any]]:
    """Cache-only one-liner for the recap's Coach's take (no network)."""
    for w in wallets():
        if w["role"] == "trading":
            st = book.state.get(w["address"]) or {}
            if st.get("lamports") is not None:
                return {"sol": _sol(st["lamports"]), "short": short(w["address"]), "sol_usd": book.sol_usd[1]}
    return None


@router.get("/api/recap/wallet")
async def recap_wallet(date: Optional[str] = Query(default=None, description="YYYY-MM-DD (America/Chicago); default today")) -> Dict[str, Any]:
    from app.recap import parse_day
    return await get_wallet_recap(parse_day(date))
