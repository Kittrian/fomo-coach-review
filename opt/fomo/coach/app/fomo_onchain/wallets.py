"""Watchlist + per-wallet quality score from VERIFIED FOMO-co-signed activity.

Seed: data/fomo_top100_wallets.json (public fomoapi.io/top100 snapshot, 2026-09-09; order = all-time rank).
A wallet is KEPT only after we see ≥1 FOMO-co-signed trade it signed (sweep of its USDC token account's
recent history, or live); DROPPED when the sweep finds none. Score 0–100 (documented in `score()`).
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.fomo_onchain import USDC_MINT
from app.pump.solana_pda import associated_token_address, is_pubkey

from app.paths import data_dir  # noqa: E402
DATA_DIR = data_dir()
SEED_PATH = Path(os.getenv("FOMO_ONCHAIN_SEED_PATH", str(DATA_DIR / "fomo_top100_wallets.json")))
STATE_PATH = Path(os.getenv("FOMO_ONCHAIN_STATE_PATH", str(DATA_DIR / "fomo_onchain_wallets.json")))


@dataclass
class Wallet:
    wallet: str
    handle: str
    rank: int
    usdc_ata: str
    status: str = "pending"          # pending | kept | dropped
    drop_reason: Optional[str] = None
    swept_at_ms: Optional[int] = None
    sample_txs: int = 0              # parsed txs in sweep sample
    sweep_window_sigs: int = 0       # wallet signatures visible on the public RPC (≈18 h)
    cosigned_trades: int = 0         # sweep: FOMO co-signed buys/sells/xchain the wallet signed
    non_fomo_txs: int = 0            # sweep: signed by wallet but not via FOMO
    last_cosigned_ms: Optional[int] = None
    live_trades: int = 0             # live: verified co-signed trades since start
    live_buys: int = 0
    live_sells: int = 0
    buy_usd: float = 0.0
    sell_usd: float = 0.0
    handle_confirmed: Optional[bool] = None   # via fomoapi keyless join
    alt_handle: Optional[str] = None
    quality: float = 0.0
    flows: Dict[str, Dict[str, float]] = field(default_factory=dict)  # mint -> {in_usd, out_usd}

    def score(self, now_ms: Optional[int] = None) -> float:
        """activity 45 (co-signed trades, caps at 6) + recency 30 (<24h full, <72h 60%, <7d 30%)
        + FOMO purity 15 (co-signed / parsed) + live 10 (2 pts per live verified trade). 0 if none."""
        now = now_ms or int(time.time() * 1000)
        total = self.cosigned_trades + self.live_trades
        if total <= 0:
            return 0.0
        act = 45 * min(total / 6, 1.0)
        rec = 0.0
        if self.last_cosigned_ms:
            age_h = (now - self.last_cosigned_ms) / 3.6e6
            rec = 30 if age_h < 24 else 18 if age_h < 72 else 9 if age_h < 168 else 0
        parsed = self.cosigned_trades + self.non_fomo_txs
        pur = 15 * (self.cosigned_trades / parsed) if parsed else 7.5
        live = min(10, 2 * self.live_trades)
        return round(min(100.0, act + rec + pur + live), 1)

    def refresh(self) -> None:
        self.quality = self.score()

    def view(self) -> Dict[str, Any]:
        d = asdict(self)
        d.pop("flows", None)
        closed = {m: f for m, f in self.flows.items() if f.get("out_usd", 0) > 0 and f.get("in_usd", 0) > 0}
        d["observed_round_trip_usd"] = round(sum(f["in_usd"] - f["out_usd"] for f in closed.values()), 2)
        d["observed_round_trips"] = len(closed)
        return d


class Watchlist:
    def __init__(self) -> None:
        self.wallets: Dict[str, Wallet] = {}
        self.by_ata: Dict[str, str] = {}

    def load_seed(self, path: Path = SEED_PATH) -> int:
        try:
            rows = json.loads(path.read_text())
        except Exception:
            rows = []
        for i, r in enumerate(rows):
            w = (r.get("solana") or "").strip()
            if not is_pubkey(w) or w in self.wallets:
                continue
            ata = associated_token_address(w, USDC_MINT)
            self.wallets[w] = Wallet(wallet=w, handle=(r.get("handle") or w[:6]).strip(), rank=i + 1, usdc_ata=ata)
            self.by_ata[ata] = w
        self._merge_state()
        return len(self.wallets)

    def _merge_state(self) -> None:
        """Reuse a recent sweep (≤ FOMO_ONCHAIN_SWEEP_TTL_H) so restarts don't re-sweep everything."""
        ttl_ms = float(os.getenv("FOMO_ONCHAIN_SWEEP_TTL_H", "6")) * 3.6e6
        try:
            saved = json.loads(STATE_PATH.read_text())
        except Exception:
            return
        now = int(time.time() * 1000)
        for d in saved.get("wallets", []):
            w = self.wallets.get(d.get("wallet"))
            if not w or not d.get("swept_at_ms") or now - d["swept_at_ms"] > ttl_ms:
                continue
            for k in ("status", "drop_reason", "swept_at_ms", "sample_txs", "cosigned_trades", "non_fomo_txs", "sweep_window_sigs",
                      "last_cosigned_ms"):
                setattr(w, k, d.get(k))
            w.refresh()

    def save(self) -> None:
        try:
            STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = STATE_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps({"saved_ms": int(time.time() * 1000),
                                       "wallets": [w.view() for w in self.wallets.values()]}, default=str))
            tmp.replace(STATE_PATH)
        except Exception:
            pass

    def get(self, wallet: str) -> Optional[Wallet]:
        return self.wallets.get(wallet)

    def kept(self) -> List[Wallet]:
        return [w for w in self.wallets.values() if w.status == "kept"]

    def counts(self) -> Dict[str, int]:
        c = {"seeded": len(self.wallets), "kept": 0, "dropped": 0, "pending": 0}
        for w in self.wallets.values():
            c[w.status] = c.get(w.status, 0) + 1
        return c

    def apply_sweep(self, w: Wallet, parsed: List[Dict[str, Any]]) -> None:
        w.swept_at_ms = int(time.time() * 1000)
        w.sample_txs = len([p for p in parsed if p.get("kind") not in ("missing",)])
        fomo = [p for p in parsed if p.get("fomo") and p.get("kind") in ("buy", "sell", "xchain_buy", "xchain_sell_payout")]
        w.cosigned_trades = len(fomo)
        w.non_fomo_txs = len([p for p in parsed if p.get("kind") == "not_fomo" and p.get("wallet_signed")])
        if fomo:
            w.last_cosigned_ms = max(int((p.get("block_time") or 0) * 1000) for p in fomo) or w.last_cosigned_ms
            for p in fomo:
                self._flow(w, p)
        if w.cosigned_trades or w.live_trades:
            w.status, w.drop_reason = "kept", None
        else:
            w.status = "dropped"
            w.drop_reason = ("no FOMO co-signed trade signed by this wallet in %d sampled USDC trades" % w.sample_txs
                             if w.sample_txs else "no FOMO co-signed USDC trade in public-RPC history (~18 h)")
        w.refresh()

    def _flow(self, w: Wallet, p: Dict[str, Any]) -> None:
        m, usd = p.get("mint"), p.get("usd") or 0.0
        if not m or not usd:
            return
        f = w.flows.setdefault(m, {"in_usd": 0.0, "out_usd": 0.0})
        if p["kind"] == "buy":
            f["out_usd"] += usd
        elif p["kind"] == "sell":
            f["in_usd"] += usd

    def apply_live(self, w: Wallet, p: Dict[str, Any]) -> None:
        if not p.get("fomo") or p.get("kind") not in ("buy", "sell", "xchain_buy", "xchain_sell_payout"):
            return
        w.live_trades += 1
        if p["kind"] == "buy":
            w.live_buys += 1
            w.buy_usd += p.get("usd") or 0.0
        elif p["kind"] == "sell":
            w.live_sells += 1
            w.sell_usd += p.get("usd") or 0.0
        bt = int((p.get("block_time") or time.time()) * 1000)
        w.last_cosigned_ms = max(w.last_cosigned_ms or 0, bt)
        self._flow(w, p)
        if w.status != "kept":
            w.status, w.drop_reason = "kept", None
        w.refresh()


watchlist = Watchlist()
