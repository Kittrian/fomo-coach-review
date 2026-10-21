#!/usr/bin/env python3
"""v1.49 paper_tgpump — PAPER-ONLY Telegram pump-group watcher (Sky, Oct 20 2026 8:27 PM CT). Separate service
(fomo-tgpump.service); the coach process is not touched.

Sources: public channel previews https://t.me/s/<channel> (no login), polled ~every 2 s each, dedupe by message id.
New coin address in a post (base58, validated on-chain as an SPL / Token-2022 mint) → one paper trade per mint:
  * entry 0.10 SOL after detect + the measured live signal→land delay (app.paper_realism params); price =
    pump.fun curve (our size simulated on reserves) → PumpSwap pool → Jupiter read-only quote; never below the market
    price at detect; live fee bps + priority + tip per leg.
  * exits side by side on the same position: t30, t60, t120, t300 and trail30 (30% off the high, armed at entry,
    max 10 min). Exit price = on-chain/Jupiter mark × live generic sell ratio (paper_realism.exit_px).
  * curve: price + mcap every ~2 s for 10 min after the post (kind=curve_tick); at +11 min a backfill of the mint's
    transactions from 10 min BEFORE the post to 10 min after (kind=pre_curve: per-tx time, signer, SOL & token delta,
    implied price; plus a pre_summary of how many wallets / SOL bought before the post = insider front-running).
Log: data/paper_tgpump.jsonl, every row tagged with "channel". PAPER ONLY: never imports the signer, never sends a
transaction, never touches live positions/caps/kill switch; "paper_tgpump" is not in app.live.config.ARMS and the
service exits if it ever appears in LIVE_ARMS. Off: systemctl disable --now fomo-tgpump.
"""
import html, json, os, random, re, sys, threading, time, urllib.request, urllib.error, base64
sys.path.insert(0, "/opt/fomo/coach")
ARM = "paper_tgpump"
if ARM in {a.strip() for a in os.getenv("LIVE_ARMS", "").split(",")}:
    sys.exit("paper_tgpump must never be a live arm")
from app.paper_realism import params, entry_fill, entry_position, exit_px, proceeds_lamports
from app.pump.curve import decode_curve, curve_price_sol, decode_pool, token_amount, PUMP_PROGRAM_ID, PUMP_AMM_PROGRAM_ID
from app.pump.solana_pda import bonding_curve_pda, canonical_pumpswap_pool

CHANNELS = [c.strip() for c in os.getenv("TGPUMP_CHANNELS", "pumpsonfomo,memecoinwhalespump,launchmemecoins,SolanaInsiderTerminal,weeklymemelaunches,Fluffyalpha").split(",") if c.strip()]
POLL_S = float(os.getenv("TGPUMP_POLL_S", "2"))
SIZE = float(os.getenv("TGPUMP_SIZE_SOL", "0.10"))
EXITS = (30, 60, 120, 300)
TRAIL, TRAIL_MAX_S = 0.30, 600
DATA = "/opt/fomo/coach/data"
LOG = f"{DATA}/paper_tgpump.jsonl"
STATE = f"{DATA}/paper_tgpump_state.json"
RPC = os.getenv("SOLANA_RPC_URL") or "https://api.mainnet-beta.solana.com"
WSOL = "So11111111111111111111111111111111111111112"
TOKEN_PROGS = {"TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"}
B58 = re.compile(r"(?<![1-9A-HJ-NP-Za-km-z/])[1-9A-HJ-NP-Za-km-z]{32,44}(?![1-9A-HJ-NP-Za-km-z])")
UA = "Mozilla/5.0 (fomo paper watcher; polite 2s poll)"
_lock = threading.Lock()


def now_ms(): return int(time.time() * 1000)


def log(kind, **r):
    with _lock, open(LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts_ms": now_ms(), "kind": kind, "arm": ARM, "paper": True, **r}, default=str) + "\n")


def rpc(m, p, tries=3):
    for a in range(tries):
        try:
            rq = urllib.request.Request(RPC, json.dumps({"jsonrpc": "2.0", "id": 1, "method": m, "params": p}).encode(),
                                        {"Content-Type": "application/json"})
            j = json.load(urllib.request.urlopen(rq, timeout=10))
            if "result" in j: return j["result"]
        except Exception: pass
        time.sleep(0.3 * (a + 1))
    return None


def acct(pk):
    v = (rpc("getAccountInfo", [pk, {"encoding": "base64", "commitment": "processed"}]) or {}).get("value")
    if not v: return None
    return {"owner": v.get("owner"), "data": base64.b64decode(v["data"][0]) if v.get("data") else b""}


def jup(inp, out, amt):
    try:
        u = f"https://lite-api.jup.ag/swap/v1/quote?inputMint={inp}&outputMint={out}&amount={int(amt)}&slippageBps=2500"
        j = json.load(urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": UA}), timeout=8))
        return int(j["outAmount"]) if j.get("outAmount") else None
    except Exception:
        return None


DEC = {}
def decimals(mint):
    if mint not in DEC:
        a = acct(mint)
        DEC[mint] = a["data"][44] if a and len(a["data"]) > 44 else 6
    return DEC[mint]


def price(mint):
    """{'px': SOL per whole token, 'src', 'curve'|'pool'?} or None. Read-only."""
    try:
        a = acct(bonding_curve_pda(mint))
        if a and a["owner"] == PUMP_PROGRAM_ID:
            c = decode_curve(a["data"])
            if c.get("quote_is_sol", True) and not c.get("complete"):
                px = curve_price_sol(c)
                if px: return {"px": px, "src": "curve", "curve": c, "ms": now_ms()}
        pa = acct(canonical_pumpswap_pool(mint))
        if pa and pa["owner"] == PUMP_AMM_PROGRAM_ID:
            p = decode_pool(pa["data"])
            r = rpc("getMultipleAccounts", [[p["base_vault"], p["quote_vault"]], {"encoding": "base64", "commitment": "processed"}])
            vals = (r or {}).get("value") or []
            if len(vals) == 2 and all(vals):
                base = token_amount(base64.b64decode(vals[0]["data"][0]))
                eff = token_amount(base64.b64decode(vals[1]["data"][0])) + p["virtual_quote_reserves"]
                if base > 0:
                    return {"px": (eff / 1e9) / (base / 1e6), "src": "pumpswap", "ms": now_ms(),
                            "pool": {"base_reserve": base, "effective_quote_reserves": eff}}
    except Exception:
        pass
    d = decimals(mint)
    out = jup(WSOL, mint, 0.01 * 1e9)            # small probe = spot-ish mark
    if out: return {"px": 0.01 / (out / 10 ** d), "src": "jupiter", "ms": now_ms()}
    return None


def is_mint(addr):
    a = acct(addr)
    # SPL mint layout: 82 bytes (Token-2022 may be longer); byte 45 = is_initialized
    return bool(a and a["owner"] in TOKEN_PROGS and len(a["data"]) >= 82 and a["data"][45] == 1)


def holders(base, mint):
    """Creator wallet (signer of the mint's oldest tx) + supply held by creator and the top accounts. Read-only."""
    try:
        sigs, bf, oldest = [], None, None
        for _ in range(30):
            p = {"limit": 1000}
            if bf: p["before"] = bf
            s = rpc("getSignaturesForAddress", [mint, p]) or []
            if not s: break
            oldest = s[-1]; bf = s[-1]["signature"]
            if len(s) < 1000: break
        creator = None
        if oldest:
            tx = rpc("getTransaction", [oldest["signature"], {"encoding": "json", "maxSupportedTransactionVersion": 0}])
            if tx:
                k = tx["transaction"]["message"]["accountKeys"][0]
                creator = k if isinstance(k, str) else k.get("pubkey")
        sup = float(((rpc("getTokenSupply", [mint]) or {}).get("value") or {}).get("uiAmount") or 0)
        top = ((rpc("getTokenLargestAccounts", [mint]) or {}).get("value") or [])[:20]
        rows = []
        for t in top:
            ai = (rpc("getAccountInfo", [t["address"], {"encoding": "jsonParsed"}]) or {}).get("value") or {}
            owner = (((ai.get("data") or {}).get("parsed") or {}).get("info") or {}).get("owner")
            amt = float(t.get("uiAmount") or 0)
            rows.append({"owner": owner, "pct": round(100 * amt / sup, 3) if sup else None})
        cre_pct = None
        if creator:
            r = rpc("getTokenAccountsByOwner", [creator, {"mint": mint}, {"encoding": "jsonParsed"}]) or {}
            amt = sum(float(v["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"] or 0) for v in r.get("value") or [])
            cre_pct = round(100 * amt / sup, 3) if sup else None
        log("holders", creator=creator, creator_created_ms=(oldest or {}).get("blockTime") and oldest["blockTime"] * 1000,
            creator_pct=cre_pct, top=rows, top10_pct_ex_pool=round(sum(r["pct"] or 0 for r in rows[1:11]), 3),
            supply=sup, **base)
    except Exception as e:
        log("error", where="holders", err=type(e).__name__, **base)


def mcap_sol(mint, px):
    try:
        s = rpc("getTokenSupply", [mint]) or {}
        return round(float(s["value"]["uiAmount"]) * px, 3)
    except Exception:
        return None


# ---------------- trade ----------------
def trade(ch, mid, mint, post_ms, det_ms):
    P = params(ARM)
    base = {"channel": ch, "msg_id": mid, "mint": mint, "post_ms": post_ms, "detect_ms": det_ms,
            "detect_lag_ms": det_ms - post_ms if post_ms else None}
    p0 = price(mint)
    supply_m = None
    if p0:
        supply_m = mcap_sol(mint, 1.0)
        log("detect_mark", px=p0["px"], src=p0["src"], mcap_sol=supply_m and round(supply_m * p0["px"], 3), **base)
    threading.Thread(target=holders, args=(dict(base, at="detect"), mint), daemon=True).start()
    time.sleep(max(0.0, P["lat_ms"] / 1000))
    oc = price(mint)
    if not oc:
        log("skip", why="no price after live delay", **base); return
    d = decimals(mint)
    if oc["src"] == "jupiter":
        out = jup(WSOL, mint, SIZE * 1e9)
        if not out:
            log("skip", why="no jupiter quote for size", **base); return
        px = SIZE / (out / 10 ** d) * (1 + P["fee_bps"] / 1e4)
        src = "jupiter_quote"
    else:
        ef = entry_fill(ARM, None, onchain=oc, size_sol=SIZE, P=P)
        if not ef.get("ok"):
            log("skip", why=ef.get("reason"), **base); return
        px, src = float(ef["px"]), ef.get("src")
    if p0: px = max(px, p0["px"])                   # never below the market at detect
    spent, tokens = entry_position(P, px, SIZE)
    t0 = time.time()
    log("open", entry_px=px, entry_src=src, spent_lamports=spent, lat_ms=P["lat_ms"],
        entry_after_post_ms=now_ms() - post_ms if post_ms else None, **base)
    hi, done, curve = px, {}, []
    while time.time() - t0 < TRAIL_MAX_S + 5:
        m = price(mint)
        el = time.time() - t0
        if m:
            mk = m["px"]; hi = max(hi, mk)
            curve.append((round(el, 1), mk))
            log("curve_tick", t_after_post_s=round((now_ms() - post_ms) / 1000, 1) if post_ms else None, px=mk,
                src=m["src"], mcap_sol=round(supply_m * mk, 3) if supply_m else None, **base)
            for e in EXITS:
                if f"t{e}" not in done and el >= e:
                    done[f"t{e}"] = (mk, "time_exit", el)
            if "trail30" not in done and (mk <= hi * (1 - TRAIL) or el >= TRAIL_MAX_S):
                done["trail30"] = (mk, "trail" if mk <= hi * (1 - TRAIL) else "trail_max_hold", el)
        if len(done) == len(EXITS) + 1: break
        time.sleep(max(0.2, 2.0 - (time.time() - t0 - el)))
    for tr in [f"t{e}" for e in EXITS] + ["trail30"]:
        mk, why, el = done.get(tr, (None, "no price (counted -100%)", None))
        xp = exit_px(P, "time_exit", mk, px) if mk else 0.0
        got = proceeds_lamports(P, tokens, xp) if mk else 0
        pnl = (got - spent) / 1e9
        log("close", track=tr, exit_px=xp, mark_px=mk, exit_reason=why, held_s=el and round(el, 1),
            spent_lamports=spent, received_lamports=got, pnl_sol=round(pnl, 6), ret=round(pnl / SIZE, 4),
            entry_px=px, high_px=hi, **base)
    # backfill the curve from 10 min before the post to 10 min after
    wait = (post_ms or det_ms) / 1000 + 660 - time.time()
    if wait > 0: time.sleep(wait)
    backfill(base, mint, (post_ms or det_ms) / 1000, d)
    holders(dict(base, at="post+11m"), mint)


def backfill(base, mint, post_s, d):
    try:
        sigs, bf = [], None
        for _ in range(20):
            p = {"limit": 1000}
            if bf: p["before"] = bf
            s = rpc("getSignaturesForAddress", [mint, p]) or []
            sigs += s
            if len(s) < 1000 or (s[-1].get("blockTime") or 0) < post_s - 600: break
            bf = s[-1]["signature"]
        win = [x for x in sigs if not x.get("err") and x.get("blockTime") and post_s - 600 <= x["blockTime"] <= post_s + 600]
        pre = [x for x in win if x["blockTime"] < post_s]
        post = [x for x in win if x["blockTime"] >= post_s]
        pick = pre[:400] + (random.sample(post, 200) if len(post) > 200 else post)   # all pre-post txs (up to 400)
        buyers, sol_in, first = set(), 0.0, None
        for x in sorted(pick, key=lambda x: x["slot"]):
            tx = rpc("getTransaction", [x["signature"], {"encoding": "json", "maxSupportedTransactionVersion": 0,
                                                         "commitment": "confirmed"}])
            if not tx: continue
            m = tx.get("meta") or {}
            keys = tx["transaction"]["message"]["accountKeys"]
            signer = keys[0] if isinstance(keys[0], str) else keys[0].get("pubkey")
            sol_d = ((m.get("postBalances") or [0])[0] - (m.get("preBalances") or [0])[0]) / 1e9
            def tok(bal):
                return sum(float(b["uiTokenAmount"]["uiAmount"] or 0) for b in (bal or [])
                           if b.get("mint") == mint and b.get("owner") == signer)
            tok_d = tok(m.get("postTokenBalances")) - tok(m.get("preTokenBalances"))
            ipx = abs(sol_d) / abs(tok_d) if tok_d and abs(sol_d) > 0.0005 else None
            rel = x["blockTime"] - post_s
            log("pre_curve", t_rel_post_s=rel, slot=tx["slot"], sig=x["signature"], signer=signer,
                sol_delta=round(sol_d, 6), token_delta=tok_d, implied_px=ipx, side="buy" if tok_d > 0 else "sell" if tok_d < 0 else None, **base)
            if rel < 0 and tok_d > 0:
                buyers.add(signer); sol_in += max(0.0, -sol_d); first = first if first is not None else rel
        log("pre_summary", txs_in_window=len(win), txs_before_post=len(pre), txs_after_post=len(post),
            buyers_before_post=len(buyers), sol_bought_before_post=round(sol_in, 4), first_buy_rel_post_s=first,
            backfilled=len(pick), **base)
    except Exception as e:
        log("error", where="backfill", err=type(e).__name__, **base)


# ---------------- watcher ----------------
MSG = re.compile(r'data-post="([^"/]+)/(\d+)"(.*?)(?=data-post="|\Z)', re.S)
def fetch(ch):
    rq = urllib.request.Request(f"https://t.me/s/{ch}", headers={"User-Agent": UA})
    return urllib.request.urlopen(rq, timeout=10).read().decode("utf-8", "replace")


def parse(h):
    out = []
    for m in MSG.finditer(h):
        body = m.group(3)
        t = re.search(r'tgme_widget_message_text[^>]*>(.*?)</div>', body, re.S)
        txt = html.unescape(re.sub(r"<br\s*/?>", "\n", t.group(1)) if t else "")
        txt = re.sub(r"<a [^>]*href=\"[^\"]*\"[^>]*>(.*?)</a>", r" \1 ", txt)
        txt = re.sub(r"<[^>]+>", " ", txt)
        txt = re.sub(r"https?://\S+", " ", txt)
        dt = re.search(r'datetime="([^"]+)"', body)
        out.append((int(m.group(2)), txt, dt.group(1) if dt else None))
    return out


def iso_ms(s):
    from datetime import datetime
    try: return int(datetime.fromisoformat(s).timestamp() * 1000)
    except Exception: return None


def main():
    st = {}
    try: st = json.load(open(STATE))
    except Exception: pass
    st.setdefault("last_id", {}); st.setdefault("mints", [])
    seen_m = set(st["mints"]); back = {c: POLL_S for c in CHANNELS}; nxt = {c: 0 for c in CHANNELS}
    log("start", channels=CHANNELS, poll_s=POLL_S)
    while True:
        for ch in CHANNELS:
            if time.time() < nxt[ch]: continue
            try:
                msgs = parse(fetch(ch)); back[ch] = POLL_S
            except urllib.error.HTTPError as e:
                back[ch] = min(60, back[ch] * 2); log("poll_error", channel=ch, code=e.code); msgs = []
            except Exception as e:
                back[ch] = min(60, back[ch] * 2); msgs = []
            nxt[ch] = time.time() + back[ch] + random.uniform(0, 0.4)
            last = st["last_id"].get(ch)
            first_run = last is None
            new = [m for m in msgs if last is None or m[0] > last]
            if msgs: st["last_id"][ch] = max(st["last_id"].get(ch) or 0, max(m[0] for m in msgs))
            det = now_ms()
            for mid, txt, dt in sorted(new):
                for addr in dict.fromkeys(B58.findall(txt)):
                    if addr in seen_m or addr == WSOL: continue
                    if not is_mint(addr): continue
                    seen_m.add(addr); st["mints"].append(addr)
                    if first_run:
                        log("seed_old_mint", channel=ch, msg_id=mid, mint=addr, post_ms=iso_ms(dt)); continue
                    pm = iso_ms(dt)
                    log("post", channel=ch, msg_id=mid, mint=addr, post_ms=pm, detect_ms=det,
                        detect_lag_ms=det - pm if pm else None, text=txt.strip()[:300])
                    threading.Thread(target=trade, args=(ch, mid, addr, pm, det), daemon=True).start()
            if new:
                json.dump(st, open(STATE + ".tmp", "w")); os.replace(STATE + ".tmp", STATE)
        time.sleep(0.2)


if __name__ == "__main__":
    main()
