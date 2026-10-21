from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(_ROOT / ".env")


def _truthy(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    host: str = os.getenv("HOST", "0.0.0.0")
    port: int = int(os.getenv("PORT", "8787"))
    demo_mode: bool = _truthy("DEMO_MODE", "true")
    ingest_secret: str = os.getenv("INGEST_SECRET", "").strip()

    fomoapi_key: str = os.getenv("FOMOAPI_KEY", "").strip()
    fomoapi_ws_url: str = os.getenv("FOMOAPI_WS_URL", "wss://api.fomoapi.io/ws").strip()

    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    telegram_min_score: int = int(os.getenv("TELEGRAM_MIN_SCORE", "75"))

    helius_api_key: str = os.getenv("HELIUS_API_KEY", "").strip()
    public_base_url: str = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    helius_webhook_path: str = os.getenv("HELIUS_WEBHOOK_PATH", "/api/ingest/helius")

    birdeye_api_key: str = os.getenv("BIRDEYE_API_KEY", "").strip()
    bubblemaps_api_key: str = os.getenv("BUBBLEMAPS_API_KEY", "").strip()
    discord_webhook_url: str = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    debot_narrative_enabled: bool = _truthy("DEBOT_NARRATIVE_ENABLED", "true")

    cluster_window_minutes: int = int(os.getenv("CLUSTER_WINDOW_MINUTES", "180"))
    min_wallet_convergence: int = int(os.getenv("MIN_WALLET_CONVERGENCE", "3"))

    # TypeSafe Jev via Cloudflare Workers AI (recommend-only gate)
    cloudflare_account_id: str = os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip()
    cloudflare_api_token: str = os.getenv("CLOUDFLARE_API_TOKEN", "").strip()
    jev_gate_enabled: bool = _truthy("JEV_GATE_ENABLED", "true")

    # Jupiter Order & Execute — confirm-to-execute; OFF by default
    # Docs: https://developers.jup.ag/docs/swap/order-and-execute
    # Keyless (0.5 RPS) or optional free portal key via x-api-key.
    jupiter_exec_enabled: bool = _truthy("JUPITER_EXEC_ENABLED", "false")
    jupiter_api_key: str = os.getenv("JUPITER_API_KEY", "").strip()
    solana_rpc_url: str = (
        os.getenv("SOLANA_RPC_URL", "").strip()
        or "https://api.mainnet-beta.solana.com"
    )
    solana_keypair_path: str = os.getenv("SOLANA_KEYPAIR_PATH", "").strip()
    # Watch/quote-only public pubkey (no private key). Used as Jupiter `taker` for unsigned txs.
    solana_wallet: str = (
        os.getenv("SOLANA_WALLET", "").strip()
        or os.getenv("JUPITER_USER_PUBLIC_KEY", "").strip()
    )

    # QP Data Mesh — free enrichment + rule risk gate (GET only; 2 rps/IP public limit)
    mesh_enabled: bool = _truthy("MESH_ENABLED", "true")
    mesh_base_url: str = os.getenv("MESH_BASE_URL", "https://quantumpools.io/api/mesh").strip().rstrip("/")
    mesh_timeout_s: float = float(os.getenv("MESH_TIMEOUT_S", "4.0"))
    mesh_rps: float = float(os.getenv("MESH_RPS", "1.5"))
    mesh_burst: float = float(os.getenv("MESH_BURST", "4"))

    # Fluffy pump→dump recommend-only plan (see PLAN.md) — never auto-executes
    plan_entry_sol_min: float = float(os.getenv("PLAN_ENTRY_SOL_MIN", "0.40"))
    plan_entry_sol_max: float = float(os.getenv("PLAN_ENTRY_SOL_MAX", "0.40"))
    plan_entry_sol_default: float = float(os.getenv("PLAN_ENTRY_SOL_DEFAULT", "0.40"))
    plan_tp1_mult: float = float(os.getenv("PLAN_TP1_MULT", "2"))
    plan_tp1_frac: float = float(os.getenv("PLAN_TP1_FRAC", "0.50"))
    plan_tp2_mult: float = float(os.getenv("PLAN_TP2_MULT", "3"))
    plan_tp2_frac: float = float(os.getenv("PLAN_TP2_FRAC", "0.25"))
    plan_tp3_mult: float = float(os.getenv("PLAN_TP3_MULT", "5"))
    plan_tp3_frac: float = float(os.getenv("PLAN_TP3_FRAC", "0.125"))
    plan_trail_pct: float = float(os.getenv("PLAN_TRAIL_PCT", "0.25"))
    plan_hard_stop_pct: float = float(os.getenv("PLAN_HARD_STOP_PCT", "0.35"))

    @property
    def fomoapi_configured(self) -> bool:
        return bool(self.fomoapi_key)

    @property
    def telegram_configured(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @property
    def helius_configured(self) -> bool:
        return bool(self.helius_api_key)

    @property
    def helius_webhook_ready(self) -> bool:
        return bool(self.helius_api_key and self.public_base_url.startswith("https://"))

    @property
    def birdeye_configured(self) -> bool:
        return bool(self.birdeye_api_key)

    @property
    def bubblemaps_configured(self) -> bool:
        return bool(self.bubblemaps_api_key)

    @property
    def discord_configured(self) -> bool:
        return bool(self.discord_webhook_url)

    @property
    def jev_configured(self) -> bool:
        return bool(self.cloudflare_account_id and self.cloudflare_api_token)

    @property
    def jupiter_api_key_configured(self) -> bool:
        return bool(self.jupiter_api_key)

    @property
    def solana_keypair_configured(self) -> bool:
        p = self.solana_keypair_path
        return bool(p) and Path(p).expanduser().is_file()

    @property
    def solana_wallet_configured(self) -> bool:
        w = self.solana_wallet
        # base58 Solana pubkey ~32–44 chars; never treat as a secret
        if not (32 <= len(w) <= 44):
            return False
        return "/" not in w and " " not in w and "\\" not in w


settings = Settings()
