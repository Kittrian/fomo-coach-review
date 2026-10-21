"""Watched wallets: 6 S-tier scouts (primary), 5 fast traders + the kept FOMO wallets (confirmers).

Seed: data/scout_wallets.json, built from research/scout_wallets_candidates.json. Only full, verified
addresses are included; truncated ones are skipped (Cented's full address was re-verified on
kolexplorer.com/kol/cented on 2026-10-07).

Subscription key = the wallet's pump.fun `user_volume_accumulator` PDA (seeds ["user_volume_accumulator",
wallet], pump program), not the wallet. Verified 2026-10-07: top wallets receive ~13–55 dust txs PER SECOND
(e.g. Jijo 1000 signatures in <1 min) while their volume-accumulator PDA only appears in their own pump.fun
curve buys AND sells (~10–60/h). That makes free websockets viable.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from app.paths import data_dir
from app.pump.solana_pda import PUMP_AMM_PROGRAM_ID, PUMP_PROGRAM_ID, find_program_address, is_pubkey, pubkey_bytes

DATA_DIR = data_dir()
SEED_PATH = Path(os.getenv("SCOUT_WALLETS_PATH", str(DATA_DIR / "scout_wallets.json")))
CRYPTOHUB_PATH = Path(os.getenv("CRYPTOHUB_WALLETS_PATH", str(DATA_DIR / "cryptohub_wallets.json")))


def uva_pda(wallet: str) -> str:
    return find_program_address([b"user_volume_accumulator", pubkey_bytes(wallet)], PUMP_PROGRAM_ID)[0]


def amm_uva_pda(wallet: str) -> str:
    """v1.16: PumpSwap's own volume accumulator (same seeds, PumpSwap program). PumpSwap buy/sell(_v2) and
    `multi_hop_swap` pass this account instead of the pump.fun one (pump-public-docs MULTI_HOP_SWAP.md)."""
    return find_program_address([b"user_volume_accumulator", pubkey_bytes(wallet)], PUMP_AMM_PROGRAM_ID)[0]


def trigger_roles() -> set:
    """Roles that also get the PumpSwap-UVA logs sub + the wallet-lamports account sub (v1.16)."""
    return {r.strip() for r in os.getenv("SCOUT_WALLET_TRIGGER_ROLES", "scout,confirmer").split(",") if r.strip()}


@dataclass
class Watched:
    wallet: str
    label: str
    role: str          # scout | confirmer | fomo
    key: str           # subscription key (volume-accumulator PDA)
    amm_key: str = ""  # v1.16: PumpSwap volume-accumulator PDA
    buys: int = 0
    sells: int = 0
    last_ms: Optional[int] = None


class Roster:
    def __init__(self) -> None:
        self.by_wallet: Dict[str, Watched] = {}
        self.by_key: Dict[str, Watched] = {}
        self.skipped: List = []

    def add(self, wallet: str, label: str, role: str) -> Optional[Watched]:
        if not is_pubkey(wallet) or len(wallet) < 43 or wallet in self.by_wallet:
            return None
        w = Watched(wallet=wallet, label=label, role=role, key=uva_pda(wallet), amm_key=amm_uva_pda(wallet))
        self.by_wallet[wallet] = w
        self.by_key[w.key] = w
        self.by_key[w.amm_key] = w
        self.by_key[w.wallet] = w
        return w

    def sub_keys(self, w: Watched) -> List:
        """[(key, method, kind)] for one wallet. Every wallet: the pump.fun UVA logs sub (as before). Scouts and
        confirmers also get the PumpSwap UVA logs sub and an accountSubscribe on the wallet itself (lamport
        changes only, so dust/failed spam that mentions the wallet does not flood us)."""
        out = [(w.key, "logs", "pump_uva")]
        if w.role in trigger_roles():
            out += [(w.amm_key, "logs", "amm_uva"), (w.wallet, "account", "wallet")]
        elif w.role == "paper_v3":                     # v1.29: PumpSwap too, no lamports sub (cost)
            out += [(w.amm_key, "logs", "amm_uva")]
        elif w.role == "cryptohub" and os.getenv("CRYPTOHUB_AMM_SUBS", "true").strip().lower() in {"1", "true", "yes", "on"}:
            out += [(w.amm_key, "logs", "amm_uva")]    # v1.34: PumpSwap too (paper/log only), no lamports sub
        return out

    def load(self, path: Path = SEED_PATH) -> int:
        try:
            d = json.loads(path.read_text())
        except Exception:
            d = {"wallets": []}
        self.skipped = d.get("skipped", [])
        for r in d.get("wallets", []):
            a = (r.get("address") or "").strip()
            if "…" in a or "..." in a:
                self.skipped.append([r.get("label"), "truncated address"])
                continue
            self.add(a, r.get("label") or a[:6], "scout" if r.get("role") == "scout" else "confirmer")
        return len(self.by_wallet)

    def add_paper_v3_wallets(self) -> int:
        """v1.29: the paper_v3_new arm's 10 wallets (role paper_v3) + 7 near-misses (role watch). PAPER/log only:
        app.scout.engine routes these roles to app.paper_v3 before any scout-arm logic."""
        try:
            from app.paper_v3 import cfg as v3cfg
            c = v3cfg()
        except Exception:  # noqa: BLE001
            return 0
        if not c["enabled"]:
            return 0
        n = 0
        for a, h in c["targets"].items():
            n += bool(self.add(a, f"v3:{h}", "paper_v3"))
        for a, h in c["watch"].items():
            n += bool(self.add(a, f"watch:{h}", "watch"))
        return n

    def add_fomo_wallets(self) -> int:
        """Kept FOMO wallets (v1.12 sweep) as confirmers."""
        try:
            from app.fomo_onchain.wallets import watchlist
            if not watchlist.wallets:
                watchlist.load_seed()
            n = 0
            kept = [(w.wallet, w.handle) for w in watchlist.kept()]
            if not kept:   # sweep still running: fall back to the last saved sweep (any age)
                from app.fomo_onchain.wallets import STATE_PATH
                try:
                    saved = json.loads(STATE_PATH.read_text()).get("wallets", [])
                    kept = [(d["wallet"], d.get("handle") or d["wallet"][:6]) for d in saved if d.get("status") == "kept"]
                except Exception:
                    kept = []
            for wallet, handle in kept:
                if self.add(wallet, f"fomo:{handle}", "fomo"):
                    n += 1
            return n
        except Exception:
            return 0

    def add_cryptohub_wallets(self) -> int:
        """v1.34: cryptohub Discord labeled wallets (data/cryptohub_wallets.json), role `cryptohub`. PAPER/log only,
        live weight 0: app.scout.engine routes this role to the kol_confluence paper arm and returns before any scout /
        confirmer logic, so these wallets never trigger or confirm a live signal. Off: CRYPTOHUB_WATCH_ENABLED=false."""
        if os.getenv("CRYPTOHUB_WATCH_ENABLED", "true").strip().lower() not in {"1", "true", "yes", "on"}:
            return 0
        try:
            d = json.loads(CRYPTOHUB_PATH.read_text())
        except Exception:  # noqa: BLE001
            return 0
        cap = int(os.getenv("CRYPTOHUB_MAX_WALLETS", "0") or 0)
        n = 0
        for r in d.get("wallets", []):
            if cap and n >= cap:
                break
            if float(r.get("live_weight") or 0) != 0.0 or not r.get("paper_only", True):
                continue                                   # never accept a hand-edited live weight here
            a = (r.get("address") or "").strip()
            n += bool(self.add(a, f"ch:{r.get('label') or a[:6]}", "cryptohub"))
        return n

    def counts(self) -> Dict[str, int]:
        c: Dict[str, int] = {}
        for w in self.by_wallet.values():
            c[w.role] = c.get(w.role, 0) + 1
        return c


roster = Roster()
