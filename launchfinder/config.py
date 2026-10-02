from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("DATA_DIR", ROOT / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

PUMP_API = "https://frontend-api-v3.pump.fun"
DEX_API = "https://api.dexscreener.com"
FXTWITTER_API = "https://api.fxtwitter.com"
GITHUB_API = "https://api.github.com"

PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
MIGRATE_PROGRAM = "39azUYFWPz3VHgKCf3VChUwbpURdCHRxjWVowf5jUJjg"
PUMPSWAP_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
# Raydium LaunchLab (deploy / initialize_v2 / migrate). Not AMM pool_ray.
RAYDIUM_LAUNCHPAD_PROGRAM = "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj"

# Typical Pump.fun graduation market cap used as a reference baseline.
GRADUATION_MCAP_USD = 69_000.0


def env_str(name: str, default: str = "") -> str:
    """Railway service-to-service refs resolve missing keys to ''."""
    raw = os.getenv(name)
    if raw is None:
        return default
    stripped = raw.strip()
    return stripped if stripped else default


def env_float(name: str, default: str) -> float:
    return float(env_str(name, default))


def env_int(name: str, default: str) -> int:
    return int(env_str(name, default))


@dataclass(frozen=True)
class Settings:
    helius_api_key: str = env_str("HELIUS_API_KEY")
    solana_rpc_url: str = env_str("SOLANA_RPC_URL")
    solana_ws_url: str = env_str("SOLANA_WS_URL")
    twitter_bearer: str = env_str("TWITTER_BEARER_TOKEN")
    github_token: str = env_str("GITHUB_TOKEN")
    gmgn_api_key: str = env_str("GMGN_API_KEY")
    fomo_api_key: str = env_str("FOMO_API_KEY")
    # Bitquery streaming GraphQL — RH Uni V4 Initialize (ROUTE-class).
    # https://streaming.bitquery.io/graphql  token from account.bitquery.io
    bitquery_api_token: str = env_str("BITQUERY_API_TOKEN") or env_str("BITQUERY_TOKEN")
    openai_api_key: str = env_str("OPENAI_API_KEY")
    telegram_bot_token: str = env_str("TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str = env_str("TELEGRAM_CHAT_ID")
    discord_webhook_url: str = env_str("DISCORD_WEBHOOK_URL")
    alert_min_p: float = env_float("ALERT_MIN_P", "0.7")
    # Late-bloom funnel: watch a migrate this many hours and Telegram
    # when the live tape looks good even if entry p(good) was weak.
    bloom_watch_hours: float = env_float("BLOOM_WATCH_HOURS", "168")
    bloom_min_promise: float = env_float("BLOOM_MIN_PROMISE", "0.58")
    # Bloom / Doing well X rescan. Paid mention counts only on that
    # small live set — not every Hunt name.
    live_social_seconds: float = env_float("LIVE_SOCIAL_SECONDS", "720")
    database_url: str = env_str("DATABASE_URL", f"sqlite:///{DATA_DIR / 'launchfinder.db'}")
    poll_seconds: float = env_float("POLL_SECONDS", "12")
    # Hunt / Doing-well last + Live. Not a per-trade firehose.
    hunt_last_seconds: float = env_float("HUNT_LAST_SECONDS", "60")
    # Phase 1/2 batch refits on the decision ledger (entry + live models).
    batch_fit_seconds: float = env_float("BATCH_FIT_SECONDS", "3600")
    batch_fit_delay_seconds: float = env_float("BATCH_FIT_DELAY_SECONDS", "600")
    # Sol Hunt holder refresh (Helius DAS token accounts). Cards per cycle and
    # DAS pages per hour (10 credits a page). 300 pages/h ~ 72k credits/day —
    # a 10M-credit Developer month still leaves ~8M for ingest / RPC / WS.
    sol_holder_tape_cap: int = env_int("SOL_HOLDER_TAPE_CAP", "8")
    sol_holder_das_pages_per_hour: int = env_int("SOL_HOLDER_DAS_PAGES_PER_HOUR", "300")
    # Live FOMO door is keyless /v2/alerts (free). 10 min is enough
    # to catch a new app-trending name without burning credits.
    fomo_poll_seconds: float = env_float("FOMO_POLL_SECONDS", "600")
    # Keyed FOMO /ws/alerts — drop spam below this USD notional (paper-safe learn).
    fomo_alerts_min_usd: float = env_float("FOMO_ALERTS_MIN_USD", "2000")
    fomo_flow_cluster_min_traders: int = env_int("FOMO_FLOW_CLUSTER_MIN_TRADERS", "3")
    fomo_flow_cluster_window_min: int = env_int("FOMO_FLOW_CLUSTER_WINDOW_MIN", "30")
    fomo_trader_score_window_days: int = env_int("FOMO_TRADER_SCORE_WINDOW_DAYS", "7")
    fomo_trader_min_buys: int = env_int("FOMO_TRADER_MIN_BUYS", "5")
    fomo_trader_cluster_min_traders: int = env_int("FOMO_TRADER_CLUSTER_MIN_TRADERS", "5")
    # Keyed GET /v2/users/id/{id} on persist_fomo_alert. Default off — WS
    # messages are free; user-profile REST burns FOMO credits. Opt in only.
    fomo_user_wallet_lookup: bool = env_str("FOMO_USER_WALLET_LOOKUP", "0").lower() in {
        "1",
        "true",
        "on",
        "yes",
    }
    # A "good" token must reach this multiple of its entry mcap without a
    # liquidity collapse. The desk hunts 5-50x runners, not 2-3x bounces.
    win_multiple: float = env_float("WIN_MULTIPLE", "5.0")
    backfill_limit: int = env_int("BACKFILL_LIMIT", "20")
    # Same Railway process, separate /rh dashboard. Off to pause RH GMGN spend.
    robinhood_enabled: bool = env_str("ROBINHOOD_ENABLED", "1").lower() not in {"0", "false", "off"}
    host: str = env_str("HOST", "0.0.0.0")
    port: int = env_int("PORT", "8080")
    log_level: str = env_str("LOG_LEVEL", "INFO")
    # all = one process (local / tests). api = desk + webhooks.
    # worker = poll + tape + bloom. Split so /health never waits on Dex.
    role: str = env_str("LAUNCHFINDER_ROLE", "all").lower() or "all"

    def runs_worker(self) -> bool:
        return self.role in {"all", "worker"}

    @property
    def rpc_url(self) -> str:
        if self.solana_rpc_url:
            return self.solana_rpc_url
        if self.helius_api_key:
            return f"https://mainnet.helius-rpc.com/?api-key={self.helius_api_key}"
        return "https://api.mainnet-beta.solana.com"

    @property
    def ws_url(self) -> str:
        if self.solana_ws_url:
            return self.solana_ws_url
        if self.helius_api_key:
            return f"wss://mainnet.helius-rpc.com/?api-key={self.helius_api_key}"
        return ""

    @property
    def has_helius(self) -> bool:
        return bool(self.helius_api_key)

    @property
    def has_x(self) -> bool:
        """True only when paid X is enabled and a bearer is present.

        Bearer alone does not spend — ``X_PAID_ENABLED`` must be on.
        """
        paid = env_str("X_PAID_ENABLED", "0").lower() in {"1", "true", "on", "yes"}
        return paid and bool(self.twitter_bearer)

    @property
    def has_gmgn(self) -> bool:
        return bool(self.gmgn_api_key)

    @property
    def has_fomo(self) -> bool:
        return bool(self.fomo_api_key)

    @property
    def has_bitquery(self) -> bool:
        return bool(self.bitquery_api_token)


settings = Settings()
