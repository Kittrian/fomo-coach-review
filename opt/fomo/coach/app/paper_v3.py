"""v1.29 `paper_v3_new` — PAPER-ONLY 7-day copy test of the 10 vetted wallets from wallets_v3 (Sky, Oct 9 5:18 PM CT).

Same machinery and guarantees as fomo_top_paper (app.paper_fomo_top): never imports the signer, never calls Jupiter,
never touches live positions / caps / loss limit / kill switch. Simulated PAPER_V3_SIZE_SOL (0.20) entries, the SAME
rug check + mesh rule as live (verdict recorded as would_live_buy), DexScreener marks.
Detection: the scout websocket source (pump.fun + PumpSwap volume-accumulator logs subs) with role `paper_v3`; these
wallets never trigger scout_solo / scout_confirmed and never confirm anything (app.scout.engine routes them here first).
Tracks: live_exit (current live scout exit without trader-sells), follow_wallet (+ exit when the copied wallet sells),
half2x_c30 (exit-variants HALF2X_C30: live pre-2x incl. wallet sell, half at 2x, 30 s candle stop on the rest).
Per entry: the copied wallet's own fill price vs our simulated entry (fill_vs_trader_pct).
Near-miss wallets (role `watch`) are logged only (kind=watch_trade), never traded.
"""
from __future__ import annotations

import os
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, Optional

from app.paper_fomo_top import PaperFomoTop, _ct, _f
from app.paths import data_dir

ARM = "paper_v3_new"
TRACKS = ("live_exit", "follow_wallet", "half2x_c30")
DEFAULT_WALLETS = ("A3W8psibkTUvjxs4LRscbnjux6TFDXdvD4m4GsGpQ2KJ:Numer0,3L8RAxLkvwkz4CgHivaVRtq19741FAdGLk5DgjRfc1fW:Inquixit,"
                   "CkPFGv2Wv1vwdWjtXioEgb8jhZQfs3eVZez3QCetu7xD:Lynk,8AtQ4ka3dgtrH1z4Uq3Tm4YdMN3cK5RRj1eKuGNnvenm:peacefuldestroy,"
                   "CEUA7zVoDRqRYoeHTP58UHU6TR8yvtVbeLrX1dppqoXJ:Tom,GvTXquDAJbfrFrEGTPRPUj3WM5Bzub5yeTFzkLVHpjfD:brunowsky,"
                   "EjtQrPTbcMevStBkpnjsH23NfUCMhGHusTYsHuGVQZp2:Zef,2W14ahXD3XBfWJchQ4K5NLXmguWWcTTUTuHDhEzeuvP3:Veloce,"
                   "YvEsBWpHK5PJ6Q8m4YrocwKeWys1NG67pbgi73UPnuX:Gucci,5B52w1ZW9tuwUduueP5J7HXz5AcGfruGoX6YoAudvyxG:Yenni")
# v1.30 whales (whales_v4 near-misses, Sky approved Oct 9 11:18 PM CT): PAPER ONLY, with an entry cap: skip the paper
# entry if the market is already > PAPER_V3_WHALE_MAX_CHASE_PCT above the whale's own fill (whales move price).
DEFAULT_WHALES = ("8AtQ4ka3dgtrH1z4Uq3Tm4YdMN3cK5RRj1eKuGNnvenm:peacefuldestroy,"
                  "4Be9CvxqHW6BYiRAxW9Q3xu1ycTMWaL5z8NX4HR3ha7t:Mitch")
DEFAULT_WATCH = ("2net6etAtTe3Rbq2gKECmQwnzcKVXRaLcHy2Zy1iCiWz:rambo,8zLw4WyAeZbYEDJTP3H48pa927cSQ46ymDH4LVr6tfM7:8zLw4W,"
                 "LeenseyyUU3ccdBPCFCrrZ8oKU2B3T2uToGGZ7eVABY:Leens,5dzH7gh5FjtrxUwtfBufJyTBA4fyCUGheZsdYQsE9vag:Hermes,"
                 "CUKFKdJw7F91bZBbtAJMrLLxqtVigVSJhx644EFnCnw:Baraka,CxgPWvH2GoEDENELne2XKAR2z2Fr4shG2uaeyqZceGve:narc,"
                 "FsG3BaPmRTdSrPaivbgJsFNCCa8cPfkUtk8VLWXkHpHP:Reljoo")


def _pairs(s: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for part in s.split(","):
        a, _, h = part.strip().partition(":")
        if a.strip():
            out[a.strip()] = (h or a[:6]).strip()
    return out


def cfg() -> Dict[str, Any]:
    whales = _pairs(os.getenv("PAPER_V3_WHALE_WALLETS", DEFAULT_WHALES))
    return {
        "enabled": os.getenv("PAPER_V3_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"},
        "targets": {**_pairs(os.getenv("PAPER_V3_WALLETS", DEFAULT_WALLETS)), **whales},
        "whales": whales,
        "whale_max_chase_pct": _f("PAPER_V3_WHALE_MAX_CHASE_PCT", 25),
        "watch": _pairs(os.getenv("PAPER_V3_WATCH_WALLETS", DEFAULT_WATCH)),
        "size_sol": _f("PAPER_V3_SIZE_SOL", 0.20),
        "days": _f("PAPER_V3_DAYS", 7),
        "cost_bps": int(_f("PAPER_V3_COST_BPS", 150)),
        "mark_s": _f("PAPER_V3_MARK_S", 5),
        "mesh_timeout_s": _f("PAPER_V3_MESH_TIMEOUT_S", 8),
        "max_open": int(_f("PAPER_V3_MAX_OPEN", 60)),
        "log_path": Path(os.getenv("PAPER_V3_LOG_PATH", str(data_dir() / "paper_v3.jsonl"))),
        "state_path": Path(os.getenv("PAPER_V3_STATE_PATH", str(data_dir() / "paper_v3_state.json"))),
    }


class PaperV3(PaperFomoTop):
    arm = ARM
    tracks = TRACKS

    def __init__(self) -> None:
        super().__init__()
        self._candles: Dict[str, Any] = {}

    def cfg(self) -> Dict[str, Any]:
        return cfg()

    # ---------------- events from the scout websocket source ----------------
    def on_scout_trade(self, wallet: str, label: str, is_buy: bool, mint: str, *, price_sol: Optional[float],
                       sol: float, sig: str, recv_ms: int, venue: Optional[str] = None) -> Optional[str]:
        c = self.cfg()
        if wallet in c["watch"]:
            if self.active():
                self.stats["watch_trades"] += 1
                self._log("watch_trade", wallet=wallet[:6], handle=c["watch"][wallet], side="buy" if is_buy else "sell",
                          mint=mint, sol=round(sol, 6), price_sol=price_sol, signature=sig, venue=venue)
            return "watch"
        if wallet not in c["targets"]:
            return None
        h = c["targets"][wallet]
        if not is_buy:
            return self.on_wallet_sell(wallet, h, mint, signature=sig, block_ms=recv_ms)
        from app.fomo_onchain import market
        su = market.sol_usd()
        res = {"mint": mint, "usd": round(sol * su, 2) if su else None,
               "fill_price_usd": price_sol * su if price_sol and su else None, "trader_price_sol": price_sol,
               "trader_sol": round(sol, 6), "venue": venue}
        return self.on_wallet_buy(wallet, h, res, signature=sig, block_ms=recv_ms,
                                  detect_lag_ms=max(0, int(time.time() * 1000) - recv_ms))

    def _entry_gate(self, wallet, handle, mint, res, px):  # type: ignore[override]
        """v1.30 whale cap: skip (and log) a whale-copied paper entry if price is already > cap above the whale's fill."""
        c = self.cfg()
        if wallet not in c["whales"]:
            return None
        tp = res.get("trader_price_sol")
        chase = round((px / tp - 1) * 100, 2) if tp else None
        if chase is not None and chase <= c["whale_max_chase_pct"]:
            return None
        why = (f"whale cap: price +{chase}% above whale fill (> {c['whale_max_chase_pct']:g}%)" if chase is not None
               else "whale cap: whale fill price unknown")
        self.stats["skipped_whale_cap"] += 1
        self._log("skip", mint=mint, handle=handle, wallet=wallet[:6], reason=why, whale_cap=True,
                  trader_price_sol=tp, price_sol=px, chase_pct=chase, cap_pct=c["whale_max_chase_pct"])
        return why

    async def _enter(self, pid, wallet, handle, mint, res, signature, block_ms, detect_lag_ms) -> None:  # type: ignore[override]
        await super()._enter(pid, wallet, handle, mint, res, signature, block_ms, detect_lag_ms)
        b = self.book.get(pid)
        if not b:
            return
        tp = res.get("trader_price_sol")
        lt = b["tracks"]["live_exit"]
        b["meta"].update(trader_price_sol=tp, trader_sol=res.get("trader_sol"), venue=res.get("venue"),
                         fill_vs_trader_pct=round((lt.entry_price_sol / tp - 1) * 100, 2) if tp else None)
        from app.scout import exit_variants as xv
        b["meta"]["xv"] = asdict(xv.Tracker(signal_id=pid, arm=ARM, mint=mint, variant="HALF2X_C30", lat_s=0,
                                            entry_ms=lt.entry_ms, entry_price_sol=lt.entry_price_sol,
                                            scout_wallet=wallet, high=lt.entry_price_sol))
        rt = b["tracks"].get("real_live_exit")                       # v1.37 realistic twin of the HALF2X_C30 track
        if rt is not None:
            b["meta"]["xv_real"] = asdict(xv.Tracker(signal_id=pid, arm=ARM, mint=mint, variant="HALF2X_C30", lat_s=0,
                                                     entry_ms=rt.entry_ms, entry_price_sol=rt.entry_price_sol,
                                                     scout_wallet=wallet, high=rt.entry_price_sol))
        self._log("fill_vs_trader", id=pid, mint=mint, handle=handle, trader_price_sol=tp,
                  entry_price_sol=lt.entry_price_sol, mark_price_sol=b["meta"].get("mark_price_sol"),
                  entry_src=b["meta"].get("entry_src"), fill_vs_trader_pct=b["meta"]["fill_vs_trader_pct"])
        self.save()

    def on_wallet_sell(self, wallet, handle, mint, **kw):  # type: ignore[override]
        pid = self.seen_mints.get(mint)
        b = self.book.get(pid or "")
        if b and wallet in b["meta"]["wallets"] and b["meta"].get("xv"):
            b["meta"]["xv"]["trader_sold"] = True
        if b and wallet in b["meta"]["wallets"] and b["meta"].get("xv_real"):
            b["meta"]["xv_real"]["trader_sold"] = True
        return super().on_wallet_sell(wallet, handle, mint, **kw)

    # ---------------- HALF2X_C30 track (exit-variants rule, applied to a LivePos for accounting) ----------------
    def _custom_track(self, pid: str, b: Dict[str, Any], track: str, p: Any, now: int) -> bool:
        if track not in ("half2x_c30", "real_half2x_c30"):
            return False
        from app.scout import exit_variants as xv
        real = track == "real_half2x_c30"
        key = "xv_real" if real else "xv"
        d = b["meta"].get(key)
        px = p.last_price_sol
        if not d or not px:
            return True
        t = xv.Tracker(**d)
        cd = self._candles.setdefault(pid + (":r" if real else ""), xv.Candles())
        cd.update(px, now, [xv.STRUCT["HALF2X_C30"]["tf"]])
        n0 = len(t.exits)
        P = xv.params()
        xv.evaluate(t, px, now, P, cd)
        if real and b["meta"].get("real_drain") and t.status == "open" and not t.exits[n0:]:
            t.status = "closed"
            t.exits.append({"ts_ms": now, "reason": "rug_flag_liquidity_drain", "frac": t.rem,
                            "mark_mult": round(px / p.entry_price_sol, 4), "fill_mult": round(px / p.entry_price_sol, 4)})
            t.rem = 0
        for ex in t.exits[n0:]:
            sell_px = ex["fill_mult"] / (1 - P["haircut"]) * p.entry_price_sol     # pre-haircut fill; our cost model
            tokens = p.tokens_left if t.rem <= 0 else min(p.tokens_left, int(p.tokens_initial * ex["frac"]))
            if tokens <= 0:
                continue
            if real:                                                  # v1.37 live sell ratio + live fees
                mark = (ex.get("mark_mult") or ex["fill_mult"]) * p.entry_price_sol
                self._real_sell(pid, b, track, p, ex["reason"], tokens, mark, now)
                continue
            basis = p.cost_basis(tokens)
            lam = self._proceeds(tokens, sell_px)
            p.tokens_left -= tokens
            p.received_lamports += lam
            fill = {"ts_ms": now, "reason": ex["reason"], "tokens": tokens, "sol": round(lam / 1e9, 6),
                    "pnl_sol": round((lam - basis) / 1e9, 6), "price_sol": sell_px,
                    "multiple": round(sell_px / p.entry_price_sol, 4)}
            p.sells.append(fill)
            self._log("sell", id=pid, track=track, mint=p.mint, **fill)
        if real and (t.status != "open" or p.tokens_left <= 0):
            self._real_close(pid, b, track, p, (t.exits[-1]["reason"] if t.exits else "closed"), now)
            self._candles.pop(pid + ":r", None)
            b["meta"][key] = asdict(t)
            return True
        if t.status != "open" or p.tokens_left <= 0:
            p.status, p.exit_reason, p.closed_ms = "closed", (t.exits[-1]["reason"] if t.exits else "closed"), now
            pn = p.pnl()
            self.stats[f"closed_{track}"] += 1
            self._log("close", id=pid, track=track, mint=p.mint, symbol=b["meta"].get("symbol"),
                      handles=b["meta"]["handles"], reason=p.exit_reason, pnl_sol=pn["pnl_sol"], pnl_pct=pn["pnl_pct"],
                      hold_s=round((now - p.entry_ms) / 1000), would_live_buy=b["meta"]["would_live_buy"],
                      fill_vs_trader_pct=b["meta"].get("fill_vs_trader_pct"),
                      max_multiple=round((p.high_price_sol or 0) / p.entry_price_sol, 4) if p.entry_price_sol else None)
            self._candles.pop(pid, None)
        b["meta"][key] = asdict(t)
        return True

    def summary(self, recent: int = 15) -> Dict[str, Any]:  # type: ignore[override]
        s = super().summary(recent)
        rows = []
        try:
            import json
            with self.cfg()["log_path"].open() as fh:
                rows = [json.loads(x) for x in fh if '"fill_vs_trader"' in x or '"watch_trade"' in x]
        except Exception:  # noqa: BLE001
            pass
        fv: Dict[str, list] = {}
        for r in rows:
            if r.get("kind") == "fill_vs_trader" and r.get("fill_vs_trader_pct") is not None:
                fv.setdefault(r.get("handle"), []).append(r["fill_vs_trader_pct"])

        def med(v):
            v = sorted(v)
            return round(v[len(v) // 2], 2) if v else None
        for h, row in s["by_handle"].items():
            row["fill_vs_trader_median_pct"] = med(fv.get(h, []))
            row["fills"] = len(fv.get(h, []))
        s.update(fill_vs_trader_median_pct=med([x for v in fv.values() for x in v]),
                 watch_only=[{"handle": h, "wallet": f"{a[:4]}…{a[-4:]}"} for a, h in self.cfg()["watch"].items()],
                 watch_trades=sum(1 for r in rows if r.get("kind") == "watch_trade"),
                 exit=("live_exit = current live scout exit (no trader-sells); follow_wallet = + exit on wallet sell; "
                       "half2x_c30 = exit-variants HALF2X_C30 (live pre-2x incl. wallet sell, half at 2x, 30 s candle "
                       "stop −3% on the rest)"),
                 detection="scout websocket (pump.fun + PumpSwap volume-accumulator logs), role paper_v3",
                 promotion_rule="≥20 paper fills, positive P&L without the best trade, median fill-vs-trader ≤15%")
        return s


paper = PaperV3()
