"""v1.17 rug / bundle checks — they can only make buying STRICTER (never a reason to buy).

Pre-buy (every arm, run alongside the mesh check):
  bundle    same-slot launch bundle: buys in the creation slot (+LIVE_RUG_BUNDLE_SLOTS) by wallets other than the creator
  holdings  creator + linked wallets (launch-window buyers; same fee payer) still hold > X% of supply right now
  top10     top holders % from Jupiter's token audit (bonding curve / pools excluded by Jupiter)
  dev       the creator already sold most of the launch buy, or launched many coins (Jupiter devMints)
  rugger    the creator is on our local rugger list (data/rugger_creators.json, built from our own losing trades and
            rug flags — app/live/rugcheck.build_rugger_list)

Timing rule (LIVE_RUG_TIMEOUT_S, default 1.5 s): a check whose data is not back in time is
  * Telegram arms (fluffy_pump_dump, zyrox): REFUSED (fail closed) — these signals are public, a second of delay
    costs little, and they are the ones most often farmed by bundlers;
  * scout arms and everything else: that ONE check is skipped and logged as `unknown`; the other checks still apply.
  A check that does not APPLY is `na` and never refuses: no pump.fun curve (other launchpads), or a launch window
  deeper than LIVE_RUG_MAX_SIG_PAGES x 1000 trades (holdings then falls back to the creator's balance from Jupiter).
    Scout entries are time-sensitive (the edge decays within seconds) and every v1.15/v1.16 gate (mesh, age, mcap,
    curve status, simulate, reserve, caps) still runs.

While a position is open (LiveEngine.rug_watch): creator or linked wallets dumping (balance drop) or the curve / pool
liquidity being pulled → the existing rug-flag sell path (force_exit with a "rug" signal, which closes even runners).

Every per-check flag: LIVE_RUG_CHECK_<NAME>=false turns that one check off. LIVE_RUG_CHECKS_ENABLED=false turns all off.
"""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

import httpx

from app.paths import data_dir, data_path
from app.pump.events import parse_all_logs, parse_inner_events
from app.pump.solana_pda import (TOKEN_2022_PROGRAM_ID, TOKEN_PROGRAM_ID, associated_token_address,
                                 bonding_curve_pda)

logger = logging.getLogger("coach.live.rugcheck")

CHECKS = ("bundle", "holdings", "top10", "dev", "rugger")
NEW_CHECKS = ("curve_floor", "creator_cap")        # v1.21: default OFF in code (LIVE_RUG_CHECK_<NAME>=true turns on)
ALL_CHECKS = CHECKS + NEW_CHECKS                   # new checks only appear in results while switched on
TG_FAIL_CLOSED_ARMS = ("fluffy_pump_dump", "zyrox")
PUMP_SUPPLY_RAW = 1_000_000_000 * 10**6
JUP_TOKENS = "REDACTED"


def _env(name: str, d: str) -> str:
    return os.getenv(name, d).strip()


def _f(name: str, d: float) -> float:
    try:
        return float(_env(name, str(d)))
    except ValueError:
        return d


def _on(name: str, d: str = "true") -> bool:
    return _env(name, d).lower() in {"1", "true", "yes", "on"}


def cfg() -> Dict[str, Any]:
    """Thresholds: see the v1.17 handoff for the evidence behind each default."""
    return {
        "enabled": _on("LIVE_RUG_CHECKS_ENABLED"),
        "checks": {c: _on(f"LIVE_RUG_CHECK_{c.upper()}", "false" if c in NEW_CHECKS else "true") for c in ALL_CHECKS},
        "min_curve_sol": _f("LIVE_RUG_MIN_CURVE_SOL", 5.0),               # v1.21 real SOL in the curve at buy
        "creator_max_pct": _f("LIVE_RUG_CREATOR_MAX_PCT", 10.0),          # v1.21 creator wallet(s) hold now
        "timeout_s": _f("LIVE_RUG_TIMEOUT_S", 1.5),
        "bundle_slots": int(_f("LIVE_RUG_BUNDLE_SLOTS", 1)),               # creation slot + this many slots
        "bundle_max_buyers": int(_f("LIVE_RUG_BUNDLE_MAX_BUYERS", 3)),     # > this many non-creator buyers → refuse
        "bundle_max_supply_pct": _f("LIVE_RUG_BUNDLE_MAX_SUPPLY_PCT", 15.0),   # launch-window buys (excl. creator)
        "holdings_max_pct": _f("LIVE_RUG_HOLDINGS_MAX_PCT", 20.0),         # creator + linked, now
        "top10_max_pct": _f("LIVE_RUG_TOP10_MAX_PCT", 45.0),
        "dev_sold_max_frac": _f("LIVE_RUG_DEV_SOLD_MAX_FRAC", 0.5),        # creator sold > half the launch buy
        "dev_max_mints": int(_f("LIVE_RUG_DEV_MAX_MINTS", 0)),             # serial-launcher cut, 0 = off (evidence: 3 of 4 smug winners' devs had >100 mints)
        "max_pages": int(_f("LIVE_RUG_MAX_SIG_PAGES", 1)),               # 1 page (≤1000 trades) keeps the check ~0.2–0.5 s; deeper → n/a
        "max_launch_txs": int(_f("LIVE_RUG_MAX_LAUNCH_TXS", 8)),
        "rpc_url": _env("LIVE_RUG_RPC_URL", "") or None,
        # open positions
        "watch_enabled": _on("LIVE_RUG_WATCH_ENABLED"),
        "watch_s": _f("LIVE_RUG_WATCH_S", 3.0),
        "dump_frac": _f("LIVE_RUG_DUMP_FRAC", 0.5),         # creator+linked sold ≥ half of what they held at entry
        "dump_min_pct": _f("LIVE_RUG_DUMP_MIN_SUPPLY_PCT", 2.0),   # …and that was ≥ 2% of supply
        "drain_frac": _f("LIVE_DRAIN_FRAC", 0.5),           # curve/pool quote reserves fell ≥ 50% from the peak
    }


# ---------------- local rugger list ----------------
def rugger_path() -> Path:
    return data_path("RUGGER_CREATORS_PATH", "rugger_creators.json")


_rug_cache: Dict[str, Any] = {"t": 0.0, "set": set()}


def ruggers(max_age_s: float = 60.0) -> set:
    if time.time() - _rug_cache["t"] < max_age_s:
        return _rug_cache["set"]
    try:
        d = json.loads(rugger_path().read_text())
        s = {c for c in (d.get("creators") or {})}
    except Exception:  # noqa: BLE001
        s = set()
    _rug_cache.update(t=time.time(), set=s)
    return s


def _jsonl(p: Path):
    if not p.exists():
        return
    with p.open() as fh:
        for line in fh:
            try:
                yield json.loads(line)
            except ValueError:
                continue


def build_rugger_list(*, now_ms: Optional[int] = None) -> Dict[str, Any]:
    """Creators of coins that hurt us: live/paper/scout positions closed by a rug flag or a hard stop with ≤ -50%,
    learning-log candidates that fell ≥ 80% by +60 min, and scout_filters deployers already flagged with rugs.
    Creator addresses come from the learning log / scout filters (no RPC). Excluded rows are ignored.
    Adds only; an entry is never removed automatically (edit the file to un-list)."""
    now_ms = now_ms or int(time.time() * 1000)
    dd = data_dir()
    creator_of: Dict[str, str] = {}
    for r in _jsonl(dd / "candidates.jsonl"):
        c = (r.get("features") or {}).get("creator")
        if c and r.get("mint"):
            creator_of[r["mint"]] = c
    try:
        sf = json.loads((dd / "scout_filters.json").read_text())
        for dep, info in (sf.get("deployers") or {}).items():
            for m in (info or {}).get("tokens") or []:
                creator_of.setdefault(m, dep)
    except Exception:  # noqa: BLE001
        sf = {}
    bad: Dict[str, List[str]] = collections.defaultdict(list)    # mint -> why
    # live ledger (read-only)
    for r in _jsonl(dd / "live_trades.jsonl"):
        if r.get("kind") == "close":
            reason = str(r.get("reason") or "")
            pct = ((r.get("pnl") or {}).get("pnl_pct")) or 0
            if "rug" in reason or "creator" in reason or (reason.startswith("hard_stop") and pct <= -50):
                bad[r["mint"]].append(f"live {reason} {pct}%")
    for r in _jsonl(dd / "scout_trades.jsonl"):
        if r.get("kind") != "pos" or r.get("excluded") or (r.get("pos") or {}).get("excluded"):
            continue
        p = r.get("pos") or {}
        pct = (r.get("pnl") or {}).get("pnl_pct") or 0
        if p.get("status") == "closed" and (("rug" in str(p.get("exit_reason"))) or pct <= -50):
            bad[p["mint"]].append(f"paper {p.get('exit_reason')} {pct}%")
    excluded = {r.get("id") for r in _jsonl(dd / "paper_trades.jsonl") if r.get("kind") == "excluded"}
    for r in _jsonl(dd / "paper_trades.jsonl"):
        if r.get("kind") == "close" and r.get("id") not in excluded:
            pct = (r.get("pnl") or {}).get("pnl_pct") or 0
            if "rug" in str(r.get("exit_reason")) or pct <= -50:
                bad[r["mint"]].append(f"paper {r.get('exit_reason')} {pct}%")
    try:
        from app.learning.outcomes import path as out_path
        for o in _jsonl(out_path()):
            if o.get("kind") == "outcome" and int(o.get("horizon_s") or 0) == 3600 and (o.get("ret") or 0) <= -0.8:
                bad[o["mint"]].append(f"shadow +60m {o['ret']:+.0%}")
    except Exception:  # noqa: BLE001
        pass
    prev = {}
    try:
        prev = json.loads(rugger_path().read_text()).get("creators") or {}
    except Exception:  # noqa: BLE001
        pass
    creators: Dict[str, Any] = dict(prev)
    for m, why in bad.items():
        c = creator_of.get(m)
        if not c:
            continue
        e = creators.setdefault(c, {"mints": [], "why": [], "added_ms": now_ms})
        if m not in e["mints"]:
            e["mints"].append(m)
            e["why"] = (e["why"] + why)[-10:]
    for dep, info in ((sf or {}).get("deployers") or {}).items():
        if (info or {}).get("rugs"):
            e = creators.setdefault(dep, {"mints": [], "why": [], "added_ms": now_ms})
            for m in info["rugs"]:
                if m not in e["mints"]:
                    e["mints"].append(m)
                    e["why"] = (e["why"] + ["scout_filters rug"])[-10:]
    out = {"updated_ms": now_ms, "creators": creators,
           "note": "Built from our own losing trades and rug flags (v1.17). Adds only; edit to un-list."}
    p = rugger_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(out, indent=1))
    tmp.replace(p)
    _rug_cache["t"] = 0.0
    return {"creators": len(creators), "new": len(creators) - len(prev), "bad_mints": len(bad),
            "bad_mints_without_creator": sum(1 for m in bad if m not in creator_of)}


# ---------------- data gathering ----------------
RpcFn = Callable[[str, List[Any]], Awaitable[Any]]


def parse_launch(txs: List[Tuple[Dict[str, Any], Optional[Dict[str, Any]]]], mint: str,
                 creator: Optional[str]) -> Dict[str, Any]:
    """Pure: launch-window txs [(sig_info, tx_json)] → buyers / creator buy / bundle facts."""
    buyers: Dict[str, Dict[str, Any]] = {}
    payers: Dict[str, set] = collections.defaultdict(set)
    dev_tokens = 0
    for info, tx in txs:
        if not tx:
            continue
        meta = tx.get("meta") or {}
        evs = parse_all_logs(meta.get("logMessages") or [])
        if not any(e.get("kind") == "pump_trade" for e in evs):
            evs += parse_inner_events(meta)
        keys = (((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or [])
        payer = keys[0] if keys and isinstance(keys[0], str) else ((keys[0] or {}).get("pubkey") if keys else None)
        for e in evs:
            if e.get("kind") != "pump_trade" or e.get("mint") != mint or not e.get("is_buy"):
                continue
            creator = creator or e.get("creator")
            u = e.get("user")
            if u == creator:
                dev_tokens += int(e.get("token_amount") or 0)
                continue
            b = buyers.setdefault(u, {"user": u, "tokens": 0, "sol": 0, "slot": info.get("slot"), "payer": payer})
            b["tokens"] += int(e.get("token_amount") or 0)
            b["sol"] += int(e.get("sol_amount") or 0)
            if payer:
                payers[payer].add(u)
    linked_by_payer = sorted({u for p, us in payers.items() for u in us if len(us) > 1 or (p != u and p != creator)})
    tok = sum(b["tokens"] for b in buyers.values())
    return {"creator": creator, "dev_buy_tokens": dev_tokens, "dev_buy_pct": round(dev_tokens / PUMP_SUPPLY_RAW * 100, 3),
            "bundle_buyers": len(buyers), "bundle_tokens": tok,
            "bundle_supply_pct": round(tok / PUMP_SUPPLY_RAW * 100, 3),
            "bundle_sol": round(sum(b["sol"] for b in buyers.values()) / 1e9, 4),
            "shared_payer_wallets": len(linked_by_payer),
            "linked": sorted(buyers), "buyers": list(buyers.values())[:20]}


async def launch_profile(mint: str, rpc: RpcFn, *, creator: Optional[str] = None, max_pages: int = 2,
                         bundle_slots: int = 1, max_txs: int = 8, before: Optional[str] = None) -> Dict[str, Any]:
    """Creation slot from the bonding-curve signature history (oldest first) + the launch-window txs.
    `before` (a known later signature) lets the offline evidence study start paging from that point."""
    pda = bonding_curve_pda(mint)
    sigs: List[Dict[str, Any]] = []
    reached = False
    for _ in range(max_pages):
        opts: Dict[str, Any] = {"limit": 1000, "commitment": "confirmed"}
        if before:
            opts["before"] = before
        res = await rpc("getSignaturesForAddress", [pda, opts])
        if res is None:
            return {"ok": False, "reason": "rpc"}
        sigs += res
        if len(res) < 1000:
            reached = True
            break
        before = res[-1]["signature"]
    if not sigs:
        return {"ok": False, "reason": "no history"}
    if not reached:
        return {"ok": False, "reason": f"history deeper than {max_pages * 1000} txs"}
    sigs = sigs[::-1]                                       # oldest first
    s0 = sigs[0].get("slot")
    window = [s for s in sigs if s.get("slot") is not None and s0 is not None and s["slot"] <= s0 + bundle_slots
              and s.get("err") is None]
    fetch = window[:max_txs]

    async def one(s):
        return s, await rpc("getTransaction", [s["signature"], {"encoding": "json", "commitment": "confirmed",
                                                                "maxSupportedTransactionVersion": 1}])
    got = await asyncio.gather(*(one(s) for s in fetch))
    if any(tx is None for _, tx in got):
        return {"ok": False, "reason": "launch tx fetch failed"}
    out = parse_launch(list(got), mint, creator)
    out.update(ok=True, create_slot=s0, create_s=sigs[0].get("blockTime"), window_txs=len(window),
               fetched=len(fetch), truncated=len(window) > len(fetch), tx_count=len(sigs))
    return out


async def holdings_now(mint: str, owners: List[str], rpc: RpcFn) -> Optional[Dict[str, int]]:
    """Current raw token balance per owner (both token programs' ATAs) via one getMultipleAccounts call."""
    owners = [o for o in dict.fromkeys(owners) if o][:48]
    if not owners:
        return {}
    atas = []
    for o in owners:
        for prog in (TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID):
            atas.append((o, associated_token_address(o, mint, prog)))
    res = await rpc("getMultipleAccounts", [[a for _, a in atas], {"encoding": "base64", "commitment": "processed",
                                                                   "dataSlice": {"offset": 64, "length": 8}}])
    if res is None:
        return None
    import base64
    bal: Dict[str, int] = collections.defaultdict(int)
    for (o, _), v in zip(atas, (res or {}).get("value") or []):
        if not v:
            continue
        try:
            raw = base64.b64decode((v.get("data") or [""])[0])
            bal[o] += int.from_bytes(raw[:8], "little")
        except Exception:  # noqa: BLE001
            continue
    return {o: bal.get(o, 0) for o in owners}


async def jup_audit(mint: str, client: httpx.AsyncClient) -> Optional[Dict[str, Any]]:
    r = await client.get(JUP_TOKENS, params={"query": mint}, timeout=4.0)
    if r.status_code != 200:
        return None
    for t in r.json() or []:
        if t.get("id") == mint:
            a = t.get("audit") or {}
            return {"top_holders_pct": a.get("topHoldersPercentage"), "dev_balance_pct": a.get("devBalancePercentage"),
                    "dev_mints": a.get("devMints"), "dev": t.get("dev"), "holder_count": t.get("holderCount"),
                    "mint_authority_disabled": a.get("mintAuthorityDisabled"),
                    "freeze_authority_disabled": a.get("freezeAuthorityDisabled"), "launchpad": t.get("launchpad")}
    return {}


# ---------------- evaluation (pure) ----------------
# A launch profile that could not be built for a STRUCTURAL reason is "n/a", not "unknown":
#   * "no history"        → the mint has no pump.fun bonding curve (another launchpad / an old token): nothing to bundle-check
#   * "history deeper …"  → thousands of trades already; the launch window is beyond the free-RPC page budget
# n/a never refuses (on any arm). "unknown" = the data did not come back (timeout / RPC error) → Telegram arms refuse.
NA_LAUNCH_PREFIXES = ("no history", "history deeper")


def _launch_state(L: Optional[Dict[str, Any]]) -> str:
    if L and L.get("ok"):
        return "ok"
    if L and str(L.get("reason") or "").startswith(NA_LAUNCH_PREFIXES):
        return "na"
    return "unknown"


def evaluate(facts: Dict[str, Any], c: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """facts: {"launch":{...}|None, "holdings":{owner:raw}|None, "jup":{...}|None, "creator":str|None}
    → per check {"status": pass|fail|unknown|na|off, "why", "value"}. Pure (tests + replay)."""
    out: Dict[str, Dict[str, Any]] = {}
    L, H, J = facts.get("launch"), facts.get("holdings"), facts.get("jup")
    ls = _launch_state(L)
    creator = facts.get("creator") or (L or {}).get("creator") or (J or {}).get("dev")
    on = c["checks"]

    def put(name, status, why=None, value=None):
        out[name] = {"status": status if on.get(name, True) else "off", "why": why, "value": value}

    # bundle
    if ls != "ok":
        put("bundle", ls, (L or {}).get("reason") or "launch data not back in time")
    else:
        nb, sp = L["bundle_buyers"], L["bundle_supply_pct"]
        bad = nb > c["bundle_max_buyers"] or sp > c["bundle_max_supply_pct"]
        put("bundle", "fail" if bad else "pass",
            f"launch bundle: {nb} wallet(s) bought {sp:.1f}% of supply in the creation slot(+{c['bundle_slots']})"
            + (" (truncated)" if L.get("truncated") else ""), {"buyers": nb, "supply_pct": sp})
    # holdings: creator + linked (launch window) now; creator-only from Jupiter when the launch window is n/a
    if ls == "ok" and H is not None:
        own = [creator] + list(L.get("linked") or []) if creator else list(L.get("linked") or [])
        tot = sum(H.get(o, 0) for o in own if o)
        pct = tot / PUMP_SUPPLY_RAW * 100
        put("holdings", "fail" if pct > c["holdings_max_pct"] else "pass",
            f"creator + {len(L.get('linked') or [])} linked wallet(s) hold {pct:.1f}% of supply", round(pct, 2))
    elif J and J.get("dev_balance_pct") is not None and ls != "unknown":
        pct = float(J["dev_balance_pct"])
        put("holdings", "fail" if pct > c["holdings_max_pct"] else "pass",
            f"creator holds {pct:.1f}% of supply (Jupiter audit; linked wallets n/a)", round(pct, 2))
    elif ls == "na" and J is not None:
        put("holdings", "na", "launch window n/a and Jupiter reports no creator balance")
    else:
        put("holdings", "unknown", "holder balances not back in time")
    # top holders (Jupiter audit)
    if not J or J.get("top_holders_pct") is None:
        put("top10", "unknown" if not J else "na", "Jupiter audit unavailable" if not J else "Jupiter has no holder data")
    else:
        t = float(J["top_holders_pct"])
        put("top10", "fail" if t > c["top10_max_pct"] else "pass", f"top holders {t:.1f}% (Jupiter audit)", round(t, 2))
    # dev: sold most of the launch buy (and, only if LIVE_RUG_DEV_MAX_MINTS > 0, a serial launcher)
    dev_why, dev_bad, dev_state = [], False, ("unknown" if ls == "unknown" else "na")
    if ls == "ok" and not L.get("dev_buy_tokens"):
        dev_state = "pass"                                  # no dev buy at all: nothing to dump
    if ls == "ok" and L.get("dev_buy_tokens") and H is not None and creator:
        dev_state = "pass"
        sold = 1 - H.get(creator, 0) / L["dev_buy_tokens"]
        if sold > c["dev_sold_max_frac"]:
            dev_bad = True
            dev_why.append(f"creator sold {sold:.0%} of the launch buy")
    if c["dev_max_mints"] > 0 and J and J.get("dev_mints") is not None:
        dev_state = "pass" if dev_state != "unknown" else dev_state
        if int(J["dev_mints"]) > c["dev_max_mints"]:
            dev_bad = True
            dev_why.append(f"creator launched {J['dev_mints']} coins (serial launcher)")
    put("dev", "fail" if dev_bad else dev_state,
        "; ".join(dev_why) or {"pass": "dev ok", "na": "no launch window (n/a)",
                               "unknown": "dev data not back in time"}[dev_state])
    # local rugger list
    if creator:
        hit = creator in ruggers()
        put("rugger", "fail" if hit else "pass", "creator on our rugger list" if hit else "creator not listed", creator)
    elif J is not None or ls == "na":
        put("rugger", "na", "creator not identifiable")
    else:
        put("rugger", "unknown", "creator unknown (data not back in time)")
    # v1.21 curve floor + creator cap (fresh bonding-curve read; only judged while on a SOL pump curve)
    C = facts.get("curve")
    for name in NEW_CHECKS:
        if not on.get(name):
            continue
        if C is None:
            put(name, "unknown", "bonding curve not back in time")
        elif not C.get("on_curve"):
            put(name, "na", "not a pump.fun bonding-curve coin")
        elif C.get("complete"):
            put(name, "na", "curve complete (graduated)")
        elif not C.get("quote_is_sol", True):
            put(name, "na", "non-SOL quote curve")
        elif name == "curve_floor":
            sol = float(C.get("quote_sol") or 0.0)
            put(name, "fail" if sol < c["min_curve_sol"] else "pass",
                f"{sol:.2f} SOL in the curve (min {c['min_curve_sol']:g})", round(sol, 3))
        else:
            hold = C.get("creator_holdings")
            if hold is None:
                put(name, "unknown", "creator balance not back in time")
            else:
                pct = sum(hold.values()) / PUMP_SUPPLY_RAW * 100
                put(name, "fail" if pct > c["creator_max_pct"] else "pass",
                    f"creator wallet(s) hold {pct:.1f}% of supply (max {c['creator_max_pct']:g}%)", round(pct, 2))
    return out


class RugChecker:
    def __init__(self) -> None:
        self.http: Optional[httpx.AsyncClient] = None
        self.stats: Dict[str, int] = collections.defaultdict(int)
        self.recent: collections.deque = collections.deque(maxlen=100)

    async def client(self) -> httpx.AsyncClient:
        if self.http is None or self.http.is_closed:
            self.http = httpx.AsyncClient(timeout=6.0, limits=httpx.Limits(max_connections=12))
        return self.http

    async def rpc(self, method: str, params: List[Any]) -> Any:
        from app.pump import rpc as _rpc
        try:
            return await _rpc.call(method, params, client=await self.client(), url=cfg()["rpc_url"], timeout=4.0)
        except Exception:  # noqa: BLE001
            self.stats["rpc_errors"] += 1
            return None

    async def gather_facts(self, mint: str, *, creator: Optional[str], c: Dict[str, Any],
                           timeout_s: float) -> Dict[str, Any]:
        """Launch profile → holdings, and the Jupiter audit, concurrently; whatever is not back by timeout_s is None."""
        facts: Dict[str, Any] = {"launch": None, "holdings": None, "jup": None, "creator": creator}

        async def launch_and_holdings():
            L = await launch_profile(mint, self.rpc, creator=creator, max_pages=c["max_pages"],
                                     bundle_slots=c["bundle_slots"], max_txs=c["max_launch_txs"])
            facts["launch"] = L
            if L.get("ok"):
                cr = creator or L.get("creator")
                facts["creator"] = facts["creator"] or cr
                facts["holdings"] = await holdings_now(mint, ([cr] if cr else []) + list(L.get("linked") or []), self.rpc)

        async def jup():
            try:
                facts["jup"] = await jup_audit(mint, await self.client())
            except Exception:  # noqa: BLE001
                facts["jup"] = None

        async def curve_and_creator():                       # v1.21 one getAccountInfo + creator balances
            from app.pump import rpc as _rpc
            from app.pump.curve import decode_curve
            from app.pump.solana_pda import PUMP_PROGRAM_ID
            res = await self.rpc("getAccountInfo", [bonding_curve_pda(mint),
                                                    {"encoding": "base64", "commitment": "processed"}])
            if res is None:
                return
            acc = _rpc._decode_account(res.get("value") if isinstance(res, dict) else None)
            if not acc or acc.get("owner") != PUMP_PROGRAM_ID:
                facts["curve"] = {"on_curve": False}
                return
            cv = decode_curve(acc["data"])
            C = {"on_curve": True, "complete": cv.get("complete"), "quote_is_sol": cv.get("quote_is_sol", True),
                 "quote_sol": (cv.get("real_quote_reserves") or 0) / 1e9, "creator": cv.get("creator")}
            facts["curve"] = C
            if c["checks"].get("creator_cap"):
                owners = list(dict.fromkeys(x for x in (cv.get("creator"), creator) if x))
                C["creator_holdings"] = (await holdings_now(mint, owners, self.rpc)) if owners else {}

        tasks = []
        if any(c["checks"].get(k) for k in NEW_CHECKS):
            tasks.append(asyncio.ensure_future(curve_and_creator()))
        if any(c["checks"].get(k) for k in ("bundle", "holdings", "dev", "rugger")):
            tasks.append(asyncio.ensure_future(launch_and_holdings()))
        if any(c["checks"].get(k) for k in ("top10", "dev", "rugger")):
            tasks.append(asyncio.ensure_future(jup()))
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=timeout_s)
            for t in pending:
                t.cancel()
            for t in done:
                if t.exception() is not None:
                    self.stats["fact_errors"] += 1
        if not facts["creator"] and facts.get("jup"):
            facts["creator"] = facts["jup"].get("dev")
        return facts

    async def prebuy(self, mint: str, arm: str, *, creator: Optional[str] = None) -> Dict[str, Any]:
        """→ {"ok", "refuse":[reasons], "unknown":[checks], "checks":{...}, "elapsed_ms"}. Never raises."""
        c = cfg()
        t0 = time.perf_counter()
        if not c["enabled"]:
            return {"ok": True, "refuse": [], "unknown": [], "checks": {}, "disabled": True, "elapsed_ms": 0}
        try:
            facts = await self.gather_facts(mint, creator=creator, c=c, timeout_s=c["timeout_s"])
            res = evaluate(facts, c)
        except Exception as exc:  # noqa: BLE001
            self.stats["errors"] += 1
            res = {k: {"status": "unknown" if c["checks"].get(k) else "off", "why": f"error {type(exc).__name__}"}
                   for k in CHECKS + tuple(k for k in NEW_CHECKS if c["checks"].get(k))}
            facts = {}
        strict = arm in TG_FAIL_CLOSED_ARMS
        refuse = [f"rugcheck {k}: {v['why']}" for k, v in res.items() if v["status"] == "fail"]
        unknown = [k for k, v in res.items() if v["status"] == "unknown"]
        na = [k for k, v in res.items() if v["status"] == "na"]
        strict_unknown = [k for k in unknown if k not in NEW_CHECKS]   # v1.21 checks never fail closed on unknown
        if strict and strict_unknown:
            refuse += [f"rugcheck {k}: unknown within {c['timeout_s']:g}s — Telegram arm fails closed"
                       for k in strict_unknown]
        out = {"ok": not refuse, "refuse": refuse, "unknown": unknown, "na": na, "strict": strict, "checks": res,
               "elapsed_ms": round((time.perf_counter() - t0) * 1000),
               "linked": ((facts.get("launch") or {}).get("linked") or [])[:20],
               "creator": facts.get("creator"),
               "launch_holdings": {o: v for o, v in (facts.get("holdings") or {}).items()},
               "curve": facts.get("curve")}
        self.stats["checked"] += 1
        self.stats["refused" if refuse else "passed"] += 1
        for k in unknown:
            self.stats[f"unknown_{k}"] += 1
        for k in na:
            self.stats[f"na_{k}"] += 1
        self.recent.appendleft({"ts_ms": int(time.time() * 1000), "mint": mint, "arm": arm, "ok": out["ok"],
                                "refuse": refuse[:3], "unknown": unknown, "elapsed_ms": out["elapsed_ms"]})
        return out

    def summary(self) -> Dict[str, Any]:
        c = cfg()
        return {"enabled": c["enabled"], "checks": c["checks"], "timeout_s": c["timeout_s"],
                "min_curve_sol": c["min_curve_sol"], "creator_max_pct": c["creator_max_pct"],
                "watch_enabled": c["watch_enabled"], "stats": dict(self.stats), "recent": list(self.recent)[:10],
                "rugger_creators": len(ruggers())}


checker = RugChecker()
