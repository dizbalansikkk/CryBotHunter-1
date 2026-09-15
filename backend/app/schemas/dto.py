from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class SettingsIn(BaseModel):
    exchange: Literal["binance", "bybit", "okx", "kucoin", "gateio"] = "binance"
    api_key: str | None = None
    secret_key: str | None = None
    passphrase: str | None = None
    risk_percent: float = Field(ge=0.1, le=10)
    daily_risk_percent: float = Field(ge=0.1, le=25)
    max_positions: int = Field(ge=1, le=50)
    min_rating: int = Field(ge=0, le=100)
    scan_interval: Literal["1m", "5m", "15m", "1h"]
    stop_loss_percent: float = Field(ge=0.1, le=25)
    take_profit_percent: float = Field(ge=0.1, le=100)
    trailing_stop_percent: float = Field(default=0.8, ge=0, le=20)
    atr_stop_multiplier: float = Field(default=1.5, ge=0.1, le=10)
    risk_reward_ratio: float = Field(default=2.0, ge=0.2, le=20)
    breakeven_trigger_r: float = Field(default=1.0, ge=0.1, le=10)
    breakeven_offset_percent: float = Field(default=0.05, ge=0, le=5)
    partial_take_profit_r: float = Field(default=1.0, ge=0.1, le=10)
    partial_close_percent: float = Field(default=50.0, ge=1, le=90)


class SettingsOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    exchange: str
    api_key_masked: str | None = None
    secret_key_masked: str | None = None
    passphrase_masked: str | None = None
    risk_percent: float
    daily_risk_percent: float
    max_positions: int
    min_rating: int
    scan_interval: str
    stop_loss_percent: float
    take_profit_percent: float
    trailing_stop_percent: float
    atr_stop_multiplier: float
    risk_reward_ratio: float
    breakeven_trigger_r: float
    breakeven_offset_percent: float
    partial_take_profit_r: float
    partial_close_percent: float


class MarketCoin(BaseModel):
    symbol: str
    price: float
    volume_24h: float
    price_change_percent: float
    atr: float
    rsi: float
    ema20: float
    ema50: float
    ema200: float
    macd: float
    funding_rate: float = 0
    open_interest: float = 0
    bid: float | None = None
    ask: float | None = None
    spread_bps: float = 0
    rating: int
    regime: str = "UNKNOWN"
    regime_score: int = 0
    regime_reason: str = ""
    sma: float = 0
    market_context: dict = Field(default_factory=dict)


class StrategySignal(BaseModel):
    symbol: str
    signal: Literal["BUY", "SELL", "WAIT"]
    score: int
    reasons: list[str] = []


class TradingDecision(BaseModel):
    symbol: str
    signal: Literal["BUY", "SELL", "WAIT"]
    score: int
    action: Literal["OPENED", "SKIPPED"]
    reason: str


class TradingRunOut(BaseModel):
    scanned: int
    opened: int
    skipped: int
    decisions: list[TradingDecision]


class PositionUpdateOut(BaseModel):
    id: int
    symbol: str
    side: str
    entry_price: float = 0
    previous_price: float
    current_price: float
    volume: float = 0
    pnl: float
    status: str
    exit_reason: str | None = None
    stop: float
    take: float


class TradingTickOut(BaseModel):
    checked: int
    closed: int
    updated: list[PositionUpdateOut]


class SystemStatusOut(BaseModel):
    paper_trading: bool
    market_data_mode: str = "ccxt"
    real_market_data: bool = True
    exchange: str
    exchange_connected: bool = True
    exchange_error: str | None = None
    exchange_market_type: str = "spot"
    exchange_sandbox_enabled: bool = True
    telegram_enabled: bool
    telegram_chat_count: int
    open_positions: int
    daily_pnl: float
    panic_paused: bool = False
    panic_reason: str | None = None
    ai_committee_enabled: bool = True
    ai_committee_min_consensus: float = 0.66
    gross_exposure: float = 0
    gross_exposure_percent: float = 0
    max_gross_exposure_percent: float = 300
    max_symbol_exposure_percent: float = 100


class PerformanceGuardOut(BaseModel):
    allowed: bool
    reason: str
    trades_checked: int
    win_rate: float
    loss_streak: int
    total_profit: float
    recovery_mode: bool = False
    risk_multiplier: float = 1.0
    retry_at: datetime | None = None


class BacktestOut(BaseModel):
    win_rate: float
    profit_factor: float
    sharpe_ratio: float
    max_drawdown: float
    average_profit: float
    average_loss: float
    trades_count: int = 0
    total_profit: float = 0


class WalkForwardWindowOut(BaseModel):
    index: int
    train_start: str
    train_end: str
    test_start: str
    test_end: str
    parameters: dict[str, float]
    train_profit: float
    test_profit: float
    test_win_rate: float
    test_profit_factor: float
    test_max_drawdown: float
    test_trades_count: int


class WalkForwardOut(BaseModel):
    windows: list[WalkForwardWindowOut]
    window_count: int
    profitable_windows: int
    total_profit: float
    average_window_profit: float
    average_win_rate: float
    average_profit_factor: float
    max_drawdown: float


class HistoryIngestOut(BaseModel):
    symbol: str
    timeframe: str
    inserted: int


class HistoryBatchIngestOut(BaseModel):
    inserted: dict[str, int]


class HistoryReadinessOut(BaseModel):
    symbol: str
    timeframe: str
    candles: int
    real_candles: int = 0
    synthetic_candles: int = 0
    target: int
    coverage_percent: float
    ready: bool
    first_timestamp: datetime | None = None
    last_timestamp: datetime | None = None


class StrategyOptimizationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int | None = None
    symbol: str
    timeframe: str
    parameters: dict
    score: float
    win_rate: float
    profit_factor: float
    max_drawdown: float
    total_profit: float
    trades_count: int
    created_at: datetime | None = None


class RlModelOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    symbol: str
    timeframe: str
    algorithm: str
    status: str
    is_active: bool
    training_candles: int
    validation_candles: int
    metrics: dict
    feature_schema: dict
    created_at: datetime | None = None


class ShadowTradeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    model_id: int
    symbol: str
    timeframe: str
    side: str
    status: str
    entry_price: float
    current_price: float
    volume: float
    stop: float
    take: float
    fee: float
    slippage: float
    pnl: float
    confidence: float
    entry_context: dict
    exit_reason: str | None = None
    entered_at: datetime
    closed_at: datetime | None = None


class TradePostMortemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    position_id: int
    symbol: str
    side: str
    pnl: float
    planned_risk: float
    result_r: float
    shaped_reward: float
    priority: float
    primary_label: str
    behavior_labels: list[str] = Field(default_factory=list)
    strategy_followed: bool
    market_snapshot: dict
    execution_snapshot: dict
    reward_components: dict
    lessons: list[str] = Field(default_factory=list)
    entered_at: datetime
    closed_at: datetime
    replay_count: int
    created_at: datetime | None = None


class LearningRuleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    scope: str
    side: str
    feature_key: str
    feature_value: str
    penalty: float
    observations: int
    wins: int
    losses: int
    total_profit: float
    confidence: float = 0
    risk_level: str = "WATCH"
    last_reason: str | None = None
    updated_at: datetime | None = None


class LearningSummaryOut(BaseModel):
    total_rules: int
    watch_rules: int
    warn_rules: int
    block_rules: int
    total_observations: int
    total_losses: int
    total_wins: int


class LearningInsightOut(BaseModel):
    impact: Literal["PREFER", "WATCH", "CAUTION", "AVOID"]
    scope: str
    side: str
    feature_key: str
    feature_value: str
    observations: int
    wins: int
    losses: int
    win_rate: float
    total_profit: float
    effective_penalty: float
    confidence: float
    risk_level: str
    explanation: str
    last_reason: str | None = None


class LearningInsightsOut(BaseModel):
    learned_from_trades: int
    rules_updated: int
    strong_patterns: int
    protective_patterns: int
    favorable_patterns: int
    insights: list[LearningInsightOut] = Field(default_factory=list)


class LearningMilestoneOut(BaseModel):
    key: str
    current: int
    target: int
    progress_percent: float
    complete: bool


class TradeBlockerOut(BaseModel):
    reason: str
    count: int


class RlFleetOut(BaseModel):
    target_pairs: int
    target_models: int
    active_pairs: int
    active_models: int
    shadow_models: int
    rejected_models: int
    retired_models: int
    candidate_models: int
    total_experiments: int
    promoted_experiments: int
    promotion_rate_percent: float
    active_decisions_24h: int
    shadow_decisions_24h: int
    shadow_open_trades: int = 0
    shadow_closed_trades: int = 0
    shadow_win_rate: float = 0.0
    shadow_pnl: float = 0.0
    uncovered_pairs: list[str] = Field(default_factory=list)
    last_training_at: datetime | None = None


class LearningProgressOut(BaseModel):
    stage: Literal["COLLECTING", "CALIBRATING", "LEARNING", "MATURE"]
    overall_progress_percent: float
    next_milestone: str | None = None
    closed_trades: int
    closed_24h: int
    closed_7d: int
    open_positions: int
    exploration_open_positions: int
    exploration_closed_trades: int
    exploration_closed_24h: int
    signals_24h: int
    directional_signals_24h: int
    waits_24h: int
    strong_waits_24h: int
    trading_symbols: list[str]
    excluded_symbols: list[str]
    agent_decisions_24h: int
    learning_rules: int
    learning_observations: int
    bad_experiences: int
    avoidable_failures: int
    disciplined_stop_losses: int
    active_rl_pairs: int
    trained_rl_models: int
    rl_fleet: RlFleetOut
    optimized_pairs: int
    candle_pairs_ready: int
    candle_pairs_total: int
    guard_allowed: bool
    guard_recovery_mode: bool
    guard_reason: str
    last_signal_at: datetime | None = None
    last_trade_closed_at: datetime | None = None
    last_learning_at: datetime | None = None
    last_post_mortem_at: datetime | None = None
    last_agent_decision_at: datetime | None = None
    milestones: list[LearningMilestoneOut] = Field(default_factory=list)
    top_blockers_24h: list[TradeBlockerOut] = Field(default_factory=list)


class ActionMessage(BaseModel):
    ok: bool
    message: str


class AgentDecisionOut(BaseModel):
    agent_name: str
    symbol: str
    action: Literal["BUY", "SELL", "WAIT", "ALLOW", "REDUCE_SIZE", "BLOCK"]
    confidence: float
    rationale: str
    context: dict = Field(default_factory=dict)
    created_at: datetime | None = None


class AgentActivityItemOut(BaseModel):
    agent_name: str
    decisions: int
    decisions_24h: int
    average_confidence: float
    directional_votes: int
    approvals: int
    blocks: int
    waits: int
    last_action: str
    last_symbol: str
    last_seen_at: datetime | None = None


class AgentActivityOut(BaseModel):
    total_decisions: int
    decisions_24h: int
    active_agents: int
    committee_approvals: int
    last_decision_at: datetime | None = None
    agents: list[AgentActivityItemOut] = Field(default_factory=list)


class LlmAdvice(BaseModel):
    action: Literal["BUY", "SELL", "WAIT"]
    confidence: float = Field(ge=0, le=1)
    rationale: str
    invalid_if: list[str] = []


class AgentAnalysisOut(BaseModel):
    symbol: str
    market: AgentDecisionOut
    llm: AgentDecisionOut | None = None
    risk: AgentDecisionOut
    committee: list[AgentDecisionOut] = Field(default_factory=list)
    consensus_score: float = 0
    final_action: Literal["BUY", "SELL", "WAIT", "BLOCK"]
    final_confidence: float
    approved: bool


class MlPrediction(BaseModel):
    symbol: str
    long_probability: int
    short_probability: int


class PositionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    symbol: str
    side: str
    entry_price: float
    current_price: float
    volume: float
    stop: float
    take: float
    initial_risk: float
    breakeven_applied: bool
    breakeven_trigger_r: float
    breakeven_offset_percent: float
    partial_take_profit_r: float
    partial_close_percent: float
    partial_taken: bool
    trailing_stop_percent: float
    highest_price: float
    lowest_price: float
    pnl: float
    entry_context: dict = Field(default_factory=dict)
    status: str
    exit_reason: str | None = None
    entered_at: datetime
    closed_at: datetime | None = None


class OrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    exchange_order_id: str | None = None
    symbol: str
    side: str
    order_type: str
    status: str
    requested_amount: float
    filled_amount: float
    requested_price: float | None = None
    average_price: float | None = None
    fee: float
    slippage: float
    created_at: datetime


class LogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    level: str
    message: str
    created_at: datetime


class TradeHistoryItemOut(BaseModel):
    id: int
    symbol: str
    side: str
    entered_at: datetime | None = None
    closed_at: datetime | None = None
    entry_price: float
    exit_price: float
    pnl: float
    return_percent: float
    result: Literal["WIN", "LOSS", "BREAKEVEN"]
    exit_reason: str | None = None
    duration_minutes: int | None = None
    confidence: float | None = None
    consensus_score: float | None = None
    signal_score: int | None = None
    risk_percent: float | None = None
    risk_reward_ratio: float | None = None
    paper_exploration: bool = False
    entry_reasons: list[str] = Field(default_factory=list)
    decision_reason: str = ""


class SymbolPerformanceOut(BaseModel):
    symbol: str
    trades: int
    wins: int
    losses: int
    win_rate: float
    total_pnl: float
    average_pnl: float
    profit_factor: float | None = None
    expectancy: float


class TradeAnalyticsOut(BaseModel):
    closed_trades: int
    open_positions: int
    wins: int
    losses: int
    breakeven: int
    win_rate: float
    total_realized_pnl: float
    open_pnl: float
    net_pnl: float
    gross_profit: float
    gross_loss: float
    profit_factor: float | None = None
    expectancy: float
    average_win: float
    average_loss: float
    best_trade: float
    worst_trade: float
    max_win_streak: int
    max_loss_streak: int
    by_symbol: list[SymbolPerformanceOut] = Field(default_factory=list)
    recent_trades: list[TradeHistoryItemOut] = Field(default_factory=list)


class DashboardOut(BaseModel):
    balance: float
    pnl_day: float
    pnl_week: float
    win_rate: float
    trades_count: int
    active_positions: list[PositionOut]
    analytics: TradeAnalyticsOut
