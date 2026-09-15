from functools import lru_cache

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Crypto AI Trader"
    environment: str = "development"
    api_prefix: str = "/api/v1"

    database_url: str = Field(default="postgresql+asyncpg://trader:trader@postgres:5432/trader")
    redis_url: str = Field(default="redis://redis:6379/0")

    jwt_secret: str = Field(default="change-me-in-production")
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60
    registration_enabled: bool = False
    encryption_key: str = Field(default="replace-with-valid-fernet-key")

    cors_origins_raw: str = Field(default="http://localhost:5173,http://localhost:8080", validation_alias="CORS_ORIGINS")
    default_exchange: str = "binance"
    exchange_default_type: str = "spot"
    paper_trading: bool = True
    live_trading_enabled: bool = False
    exchange_sandbox_enabled: bool = True
    allow_live_trading_without_sandbox: bool = False
    market_data_mode: str = "ccxt"
    paper_fee_rate: float = 0.0004
    paper_slippage_bps: float = 2.0
    exchange_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("EXCHANGE_API_KEY", "API_KEY"),
    )
    exchange_secret_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("EXCHANGE_SECRET_KEY", "EXCHANGE_API_SECRET", "API_SECRET"),
    )
    exchange_passphrase: str | None = Field(
        default=None,
        validation_alias=AliasChoices("EXCHANGE_PASSPHRASE", "API_PASSPHRASE"),
    )
    log_retention_days: int = 90
    telegram_bot_token: str | None = None
    telegram_allowed_chat_ids_raw: str = Field(default="", validation_alias="TELEGRAM_ALLOWED_CHAT_IDS")
    telegram_trade_reports_enabled: bool = True
    telegram_cycle_reports_enabled: bool = True
    telegram_cycle_report_interval_minutes: int = 5
    telegram_outbox_enabled: bool = True
    telegram_outbox_retry_limit: int = 8
    telegram_outbox_batch_size: int = 20
    telegram_outbox_retention_days: int = 7
    telegram_daily_report_enabled: bool = True
    telegram_daily_report_hour_utc: int = 18
    telegram_daily_report_minute_utc: int = 0
    worker_heartbeat_enabled: bool = True
    worker_heartbeat_interval_seconds: int = 30
    worker_heartbeat_stale_seconds: int = 180
    worker_heartbeat_startup_grace_seconds: int = 600
    worker_heartbeat_long_task_grace_seconds: int = 900
    trader_loop_seconds: int = 60
    llm_provider: str = "none"
    openai_api_key: str | None = None
    llm_model: str = "gpt-4.1-mini"
    llm_timeout_seconds: int = 20
    trading_panic_key: str = "trading:panic"
    guard_min_trades: int = 5
    guard_min_win_rate: float = 35.0
    guard_max_loss_streak: int = 3
    guard_min_total_profit: float = -50.0
    guard_recovery_enabled: bool = True
    guard_recovery_cooldown_hours: float = 6.0
    guard_recovery_risk_multiplier: float = 0.25
    guard_recovery_max_positions: int = 1
    ai_committee_enabled: bool = True
    ai_committee_min_consensus: float = 0.66
    max_gross_exposure_percent: float = 300.0
    max_symbol_exposure_percent: float = 100.0
    max_position_size_percent: float = 25.0
    max_drawdown_percent: float = 5.0
    trade_memory_sqlite_path: str = "data/trade_memory.sqlite3"
    max_same_side_positions: int = 2
    directional_risk_reduction_start: int = 1
    directional_risk_multiplier: float = 0.5
    pretrade_quality_enabled: bool = True
    pretrade_quality_min_candles: int = 420
    pretrade_quality_min_profit_factor: float = 1.1
    pretrade_quality_min_win_rate: float = 42.0
    pretrade_quality_min_profitable_windows_percent: float = 50.0
    pretrade_quality_min_trades: int = 3
    pretrade_quality_min_risk_multiplier: float = 0.35
    market_quality_min_quote_volume: float = 100_000_000.0
    market_quality_max_spread_bps: float = 25.0
    market_quality_max_price_change_percent: float = 18.0
    market_quality_min_risk_multiplier: float = 0.5
    market_scan_concurrency: int = 3
    market_scan_symbols_raw: str = Field(
        default="BTC/USDT,ETH/USDT,BNB/USDT,SOL/USDT,XRP/USDT,ADA/USDT,DOGE/USDT,LINK/USDT,AVAX/USDT,DOT/USDT,LTC/USDT,TRX/USDT",
        validation_alias="MARKET_SCAN_SYMBOLS",
    )
    loss_cooldown_enabled: bool = True
    loss_cooldown_symbol_hours: float = 6.0
    loss_cooldown_global_hours: float = 3.0
    loss_cooldown_loss_streak: int = 2
    loss_cooldown_min_loss: float = 0.0
    paper_exploration_enabled: bool = False
    paper_exploration_min_score: int = 65
    paper_exploration_risk_percent: float = 0.15
    paper_exploration_max_risk_percent: float = 0.15
    paper_exploration_max_positions: int = 5
    paper_exploration_recovery_slots: int = 2
    paper_exploration_max_per_cycle: int = 1
    paper_exploration_min_directional_votes: int = 5
    paper_exploration_min_vote_margin: int = 2
    paper_exploration_cooldown_minutes: int = 240
    learning_progress_target_trades: int = 30
    learning_progress_target_observations: int = 100
    strategy_optimizer_apply_enabled: bool = True
    strategy_optimizer_min_profit_factor: float = 1.05
    strategy_optimizer_min_trades: int = 3
    strategy_optimizer_max_age_days: int = 14
    strategy_optimizer_worker_enabled: bool = True
    strategy_optimizer_loop_seconds: int = 21600
    strategy_optimizer_refresh_hours: int = 24
    strategy_optimizer_limit: int = 800
    strategy_optimizer_top_n: int = 5
    strategy_optimizer_validation_enabled: bool = True
    strategy_optimizer_require_validation_pass: bool = True
    strategy_optimizer_validation_percent: float = 30.0
    strategy_optimizer_validation_min_candles: int = 220
    strategy_optimizer_min_validation_trades: int = 2
    strategy_optimizer_min_validation_profit_factor: float = 1.0
    strategy_optimizer_min_validation_win_rate: float = 35.0
    strategy_optimizer_min_validation_profit: float = 0.0
    strategy_optimizer_max_overfit_ratio: float = 8.0
    candle_ingest_symbols_raw: str = Field(
        default="BTC/USDT,ETH/USDT,BNB/USDT,SOL/USDT,XRP/USDT,ADA/USDT,DOGE/USDT,LINK/USDT,AVAX/USDT,DOT/USDT,LTC/USDT,TRX/USDT",
        validation_alias="CANDLE_INGEST_SYMBOLS",
    )
    candle_ingest_timeframes_raw: str = Field(default="1h", validation_alias="CANDLE_INGEST_TIMEFRAMES")
    candle_ingest_limit: int = 500
    candle_ingest_loop_seconds: int = 300
    candle_dataset_target: int = 5_000
    rl_trainer_enabled: bool = True
    rl_gate_enabled: bool = True
    rl_training_timesteps: int = 20_000
    rl_training_limit: int = 5_000
    rl_min_training_candles: int = 2_000
    rl_training_seeds_raw: str = Field(default="7,29", validation_alias="RL_TRAINING_SEEDS")
    rl_refresh_hours: float = 24.0
    rl_rejected_retry_hours: float = 6.0
    rl_prediction_loop_seconds: int = 300
    rl_validation_percent: float = 25.0
    rl_min_validation_return_percent: float = 0.0
    rl_min_validation_profit_factor: float = 1.05
    rl_min_validation_trades: int = 5
    rl_max_validation_drawdown_percent: float = 15.0
    rl_gate_min_confidence: float = 0.55
    rl_gate_max_age_hours: float = 6.0
    rl_wait_risk_multiplier: float = 0.5

    @property
    def cors_origins(self) -> list[str]:
        return _parse_csv(self.cors_origins_raw)

    @property
    def telegram_allowed_chat_ids(self) -> list[int]:
        return [int(item) for item in _parse_csv(self.telegram_allowed_chat_ids_raw)]

    @property
    def candle_ingest_symbols(self) -> list[str]:
        return _parse_csv(self.candle_ingest_symbols_raw)

    @property
    def market_scan_symbols(self) -> list[str]:
        return _parse_csv(self.market_scan_symbols_raw)

    @property
    def candle_ingest_timeframes(self) -> list[str]:
        return _parse_csv(self.candle_ingest_timeframes_raw)

    @property
    def uses_live_market_data(self) -> bool:
        # "paper" used to mean synthetic data. Keep it as a compatibility
        # alias for public exchange data; execution safety is controlled only
        # by PAPER_TRADING/LIVE_TRADING_ENABLED.
        return self.market_data_mode.strip().lower() != "synthetic"

    @property
    def rl_training_seeds(self) -> list[int]:
        values: list[int] = []
        for item in _parse_csv(self.rl_training_seeds_raw):
            try:
                values.append(int(item))
            except ValueError:
                continue
        return values or [7]


@lru_cache
def get_settings() -> Settings:
    return Settings()


def _parse_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def async_database_url(url: str) -> str:
    if url.startswith("postgresql+asyncpg://"):
        return url
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+asyncpg://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


def sync_database_url(url: str) -> str:
    if url.startswith("postgresql+asyncpg://"):
        return url.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1)
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+psycopg://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url
