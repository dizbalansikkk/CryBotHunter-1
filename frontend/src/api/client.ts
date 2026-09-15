import axios from "axios";

export const api = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL ?? "/api/v1"
});

api.interceptors.request.use((config) => {
  const token = localStorage.getItem("token");
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

api.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error?.response?.status === 401) {
      localStorage.removeItem("token");
      window.dispatchEvent(new Event("auth-expired"));
    }
    return Promise.reject(error);
  }
);

export type Position = {
  id: number;
  symbol: string;
  side: string;
  entry_price: number;
  current_price: number;
  volume: number;
  stop: number;
  take: number;
  initial_risk: number;
  breakeven_applied: boolean;
  breakeven_trigger_r: number;
  breakeven_offset_percent: number;
  partial_take_profit_r: number;
  partial_close_percent: number;
  partial_taken: boolean;
  trailing_stop_percent: number;
  highest_price: number;
  lowest_price: number;
  pnl: number;
  entry_context: Record<string, unknown>;
  status: string;
  exit_reason?: string | null;
  entered_at: string;
  closed_at?: string | null;
};

export type TradeHistoryItem = {
  id: number;
  symbol: string;
  side: string;
  entered_at?: string | null;
  closed_at?: string | null;
  entry_price: number;
  exit_price: number;
  pnl: number;
  return_percent: number;
  result: "WIN" | "LOSS" | "BREAKEVEN";
  exit_reason?: string | null;
  duration_minutes?: number | null;
  confidence?: number | null;
  consensus_score?: number | null;
  signal_score?: number | null;
  risk_percent?: number | null;
  risk_reward_ratio?: number | null;
  paper_exploration: boolean;
  entry_reasons: string[];
  decision_reason: string;
};

export type SymbolPerformance = {
  symbol: string;
  trades: number;
  wins: number;
  losses: number;
  win_rate: number;
  total_pnl: number;
  average_pnl: number;
  profit_factor?: number | null;
  expectancy: number;
};

export type TradeAnalytics = {
  closed_trades: number;
  open_positions: number;
  wins: number;
  losses: number;
  breakeven: number;
  win_rate: number;
  total_realized_pnl: number;
  open_pnl: number;
  net_pnl: number;
  gross_profit: number;
  gross_loss: number;
  profit_factor?: number | null;
  expectancy: number;
  average_win: number;
  average_loss: number;
  best_trade: number;
  worst_trade: number;
  max_win_streak: number;
  max_loss_streak: number;
  by_symbol: SymbolPerformance[];
  recent_trades: TradeHistoryItem[];
};

export type Order = {
  id: number;
  exchange_order_id?: string | null;
  symbol: string;
  side: string;
  order_type: string;
  status: string;
  requested_amount: number;
  filled_amount: number;
  requested_price?: number | null;
  average_price?: number | null;
  fee: number;
  slippage: number;
  created_at: string;
};

export type Dashboard = {
  balance: number;
  pnl_day: number;
  pnl_week: number;
  win_rate: number;
  trades_count: number;
  active_positions: Position[];
  analytics: TradeAnalytics;
};

export type TradingDecision = {
  symbol: string;
  signal: "BUY" | "SELL" | "WAIT";
  score: number;
  action: "OPENED" | "SKIPPED";
  reason: string;
};

export type TradingRun = {
  scanned: number;
  opened: number;
  skipped: number;
  decisions: TradingDecision[];
};

export type TradingTick = {
  checked: number;
  closed: number;
  updated: Array<{
    id: number;
    symbol: string;
    side: string;
    entry_price: number;
    previous_price: number;
    current_price: number;
    volume: number;
    pnl: number;
    status: string;
    exit_reason?: string | null;
    stop: number;
    take: number;
  }>;
};

export type SystemStatus = {
  paper_trading: boolean;
  market_data_mode: string;
  real_market_data: boolean;
  exchange: string;
  exchange_connected: boolean;
  exchange_error?: string | null;
  exchange_market_type: string;
  exchange_sandbox_enabled: boolean;
  telegram_enabled: boolean;
  telegram_chat_count: number;
  open_positions: number;
  daily_pnl: number;
  ai_committee_enabled: boolean;
  ai_committee_min_consensus: number;
  gross_exposure: number;
  gross_exposure_percent: number;
  max_gross_exposure_percent: number;
  max_symbol_exposure_percent: number;
};

export type PerformanceGuard = {
  allowed: boolean;
  reason: string;
  trades_checked: number;
  win_rate: number;
  loss_streak: number;
  total_profit: number;
  recovery_mode: boolean;
  risk_multiplier: number;
  retry_at: string | null;
};

export type BacktestReport = {
  win_rate: number;
  profit_factor: number;
  sharpe_ratio: number;
  max_drawdown: number;
  average_profit: number;
  average_loss: number;
  trades_count: number;
  total_profit: number;
};

export type WalkForwardReport = {
  windows: Array<{
    index: number;
    train_start: string;
    train_end: string;
    test_start: string;
    test_end: string;
    parameters: Record<string, number>;
    train_profit: number;
    test_profit: number;
    test_win_rate: number;
    test_profit_factor: number;
    test_max_drawdown: number;
    test_trades_count: number;
  }>;
  window_count: number;
  profitable_windows: number;
  total_profit: number;
  average_window_profit: number;
  average_win_rate: number;
  average_profit_factor: number;
  max_drawdown: number;
};

export type HistoryIngest = {
  symbol: string;
  timeframe: string;
  inserted: number;
};

export type HistoryBatchIngest = {
  inserted: Record<string, number>;
};

export type HistoryReadiness = {
  symbol: string;
  timeframe: string;
  candles: number;
  real_candles: number;
  synthetic_candles: number;
  target: number;
  coverage_percent: number;
  ready: boolean;
  first_timestamp?: string | null;
  last_timestamp?: string | null;
};

export type StrategyOptimizationRobustness = {
  passed?: boolean;
  reason?: string;
  validation_profit?: number;
  validation_profit_factor?: number;
  validation_win_rate?: number;
  validation_trades?: number;
  validation_max_drawdown?: number;
  train_profit?: number;
  overfit_ratio?: number;
  consistency?: number;
};

export type StrategyOptimizationParameters = {
  stop_loss_percent?: number;
  take_profit_percent?: number;
  trailing_stop_percent?: number;
  risk_per_trade?: number;
  robustness?: StrategyOptimizationRobustness;
};

export type StrategyOptimization = {
  id?: number | null;
  symbol: string;
  timeframe: string;
  parameters: StrategyOptimizationParameters;
  score: number;
  win_rate: number;
  profit_factor: number;
  max_drawdown: number;
  total_profit: number;
  trades_count: number;
  created_at?: string | null;
};

export type RlModel = {
  id: number;
  symbol: string;
  timeframe: string;
  algorithm: string;
  status: "ACTIVE" | "REJECTED" | "RETIRED" | string;
  is_active: boolean;
  training_candles: number;
  validation_candles: number;
  metrics: {
    return_percent?: number;
    max_drawdown_percent?: number;
    profit_factor?: number;
    trades?: number;
    buy_hold_return_percent?: number;
    passed?: boolean;
    promotion_reason?: string;
    market_data_source?: string;
    seed?: number;
  };
  feature_schema: Record<string, unknown>;
  created_at?: string | null;
};

export type LearningRule = {
  id: number;
  scope: string;
  side: string;
  feature_key: string;
  feature_value: string;
  penalty: number;
  observations: number;
  wins: number;
  losses: number;
  total_profit: number;
  confidence: number;
  risk_level: "WATCH" | "WARN" | "BLOCK";
  last_reason?: string | null;
  updated_at?: string | null;
};

export type LearningSummary = {
  total_rules: number;
  watch_rules: number;
  warn_rules: number;
  block_rules: number;
  total_observations: number;
  total_losses: number;
  total_wins: number;
};

export type LearningInsight = {
  impact: "PREFER" | "WATCH" | "CAUTION" | "AVOID";
  scope: string;
  side: string;
  feature_key: string;
  feature_value: string;
  observations: number;
  wins: number;
  losses: number;
  win_rate: number;
  total_profit: number;
  effective_penalty: number;
  confidence: number;
  risk_level: string;
  explanation: string;
  last_reason?: string | null;
};

export type LearningInsights = {
  learned_from_trades: number;
  rules_updated: number;
  strong_patterns: number;
  protective_patterns: number;
  favorable_patterns: number;
  insights: LearningInsight[];
};

export type LearningMilestone = {
  key: string;
  current: number;
  target: number;
  progress_percent: number;
  complete: boolean;
};

export type TradeBlocker = {
  reason: string;
  count: number;
};

export type LearningProgress = {
  stage: "COLLECTING" | "CALIBRATING" | "LEARNING" | "MATURE";
  overall_progress_percent: number;
  next_milestone?: string | null;
  closed_trades: number;
  closed_24h: number;
  closed_7d: number;
  open_positions: number;
  exploration_open_positions: number;
  exploration_closed_trades: number;
  exploration_closed_24h: number;
  signals_24h: number;
  directional_signals_24h: number;
  waits_24h: number;
  agent_decisions_24h: number;
  learning_rules: number;
  learning_observations: number;
  active_rl_pairs: number;
  trained_rl_models: number;
  optimized_pairs: number;
  candle_pairs_ready: number;
  candle_pairs_total: number;
  guard_allowed: boolean;
  guard_recovery_mode: boolean;
  guard_reason: string;
  last_signal_at?: string | null;
  last_trade_closed_at?: string | null;
  last_learning_at?: string | null;
  last_agent_decision_at?: string | null;
  milestones: LearningMilestone[];
  top_blockers_24h: TradeBlocker[];
};

export type ActionMessage = {
  ok: boolean;
  message: string;
};

export type UserSettings = {
  exchange: string;
  api_key_masked?: string | null;
  secret_key_masked?: string | null;
  passphrase_masked?: string | null;
  risk_percent: number;
  daily_risk_percent: number;
  max_positions: number;
  min_rating: number;
  scan_interval: string;
  stop_loss_percent: number;
  take_profit_percent: number;
  trailing_stop_percent: number;
  atr_stop_multiplier: number;
  risk_reward_ratio: number;
  breakeven_trigger_r: number;
  breakeven_offset_percent: number;
  partial_take_profit_r: number;
  partial_close_percent: number;
};

export type AgentDecision = {
  agent_name: string;
  symbol: string;
  action: "BUY" | "SELL" | "WAIT" | "ALLOW" | "REDUCE_SIZE" | "BLOCK";
  confidence: number;
  rationale: string;
  context: Record<string, unknown>;
  created_at?: string | null;
};

export type AgentActivityItem = {
  agent_name: string;
  decisions: number;
  decisions_24h: number;
  average_confidence: number;
  directional_votes: number;
  approvals: number;
  blocks: number;
  waits: number;
  last_action: string;
  last_symbol: string;
  last_seen_at?: string | null;
};

export type AgentActivity = {
  total_decisions: number;
  decisions_24h: number;
  active_agents: number;
  committee_approvals: number;
  last_decision_at?: string | null;
  agents: AgentActivityItem[];
};

export type AgentAnalysis = {
  symbol: string;
  market: AgentDecision;
  llm?: AgentDecision | null;
  risk: AgentDecision;
  committee: AgentDecision[];
  consensus_score: number;
  final_action: "BUY" | "SELL" | "WAIT" | "BLOCK";
  final_confidence: number;
  approved: boolean;
};

export type MarketCoin = {
  symbol: string;
  price: number;
  volume_24h: number;
  price_change_percent: number;
  rsi: number;
  ema50: number;
  ema200: number;
  rating: number;
  regime: string;
  regime_score: number;
  regime_reason: string;
};

export type LogEntry = {
  id: number;
  level: string;
  message: string;
  created_at: string;
};
