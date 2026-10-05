import React from "react";
import { createRoot } from "react-dom/client";
import {
  Activity,
  BarChart3,
  Bot,
  CheckCircle2,
  Download,
  KeyRound,
  LogOut,
  Play,
  RefreshCw,
  Save,
  Settings,
  ShieldCheck,
  Terminal,
  XCircle
} from "lucide-react";
import { ActionMessage, AgentActivity, AgentAnalysis, AgentDecision, api, BacktestReport, Dashboard, HistoryBatchIngest, HistoryIngest, HistoryReadiness, LearningInsights, LearningProgress, LearningRule, LearningSummary, LogEntry, MarketCoin, Order, PerformanceGuard, Position, RlModel, ShadowTrade, StrategyOptimization, SystemStatus, TradeAnalytics, TradeChart, TradePostMortem, TradingAudit, TradingAuditDay, TradingAuditSymbol, TradingRun, TradingTick, UserSettings, WalkForwardReport } from "./api/client";
import "./styles.css";

type View = "dashboard" | "audit" | "market" | "agents" | "logs" | "settings";

const TRADING_SYMBOLS = [
  "ETH/USDT", "BNB/USDT", "SOL/USDT", "ADA/USDT", "LINK/USDT", "DOT/USDT",
  "TRX/USDT", "AAVE/USDT", "UNI/USDT", "ONDO/USDT"
];

function App() {
  const [view, setView] = React.useState<View>("dashboard");
  const [tokenReady, setTokenReady] = React.useState(Boolean(localStorage.getItem("token")));
  const [email, setEmail] = React.useState("");
  const [password, setPassword] = React.useState("");
  const [error, setError] = React.useState("");

  React.useEffect(() => {
    function onAuthExpired() {
      setTokenReady(false);
      setError("Сессия истекла. Войди заново.");
    }

    window.addEventListener("auth-expired", onAuthExpired);
    return () => window.removeEventListener("auth-expired", onAuthExpired);
  }, []);

  async function login(mode: "login" | "register") {
    try {
      setError("");
      const { data } = await api.post(`/auth/${mode}`, { email, password });
      localStorage.setItem("token", data.access_token);
      setTokenReady(true);
    } catch (err) {
      setError(readError(err));
    }
  }

  function logout() {
    localStorage.removeItem("token");
    setError("");
    setTokenReady(false);
  }

  if (!tokenReady) {
    return (
      <main className="auth-shell">
        <section className="auth-panel">
          <div>
            <div className="brand-mark"><Bot size={22} /> CryBotHunter</div>
            <h1>Крипто AI Трейдер</h1>
            <p>Панель управления торговым ботом с paper-режимом, проверками риска и Telegram-операциями.</p>
          </div>
          {error && <Alert tone="danger" text={error} />}
          <label className="field">
            Email
            <input value={email} onChange={(event) => setEmail(event.target.value)} placeholder="you@example.com" />
          </label>
          <label className="field">
            Пароль
            <input type="password" value={password} onChange={(event) => setPassword(event.target.value)} placeholder="Минимум 8 символов" />
          </label>
          <div className="action-row">
            <button className="btn primary flex-1" onClick={() => login("login")}><KeyRound size={16} /> Войти</button>
            <button className="btn flex-1" onClick={() => login("register")}>Регистрация</button>
          </div>
        </section>
      </main>
    );
  }

  return (
    <main className="app-shell">
      <nav className="topbar">
        <div className="topbar-inner">
          <div className="brand-mark"><Bot size={20} /> CryBotHunter</div>
          <div className="nav-group">
            <NavButton active={view === "dashboard"} onClick={() => setView("dashboard")} icon={<Activity size={16} />} label="Панель" />
            <NavButton active={view === "audit"} onClick={() => setView("audit")} icon={<BarChart3 size={16} />} label="Аудит 30д" />
            <NavButton active={view === "market"} onClick={() => setView("market")} icon={<BarChart3 size={16} />} label="Рынок" />
            <NavButton active={view === "agents"} onClick={() => setView("agents")} icon={<Bot size={16} />} label="Агенты" />
            <NavButton active={view === "logs"} onClick={() => setView("logs")} icon={<Terminal size={16} />} label="Логи" />
            <NavButton active={view === "settings"} onClick={() => setView("settings")} icon={<Settings size={16} />} label="Настройки" />
            <button className="icon-btn" onClick={logout} title="Выйти"><LogOut size={16} /></button>
          </div>
        </div>
      </nav>
      <div className="page">
        {view === "dashboard" && <DashboardView />}
        {view === "audit" && <TradingAuditView />}
        {view === "market" && <MarketView />}
        {view === "agents" && <AgentsView />}
        {view === "logs" && <LogsView />}
        {view === "settings" && <SettingsView />}
      </div>
    </main>
  );
}

function AgentsView() {
  const [analysis, setAnalysis] = React.useState<AgentAnalysis | null>(null);
  const [decisions, setDecisions] = React.useState<AgentDecision[]>([]);
  const [activity, setActivity] = React.useState<AgentActivity | null>(null);
  const [symbol, setSymbol] = React.useState("ETH/USDT");
  const [error, setError] = React.useState("");
  const [loading, setLoading] = React.useState(false);

  const load = React.useCallback(async () => {
    try {
      setError("");
      const [decisionResponse, activityResponse] = await Promise.all([
        api.get<AgentDecision[]>("/agents/decisions?limit=50"),
        api.get<AgentActivity>("/agents/activity")
      ]);
      setDecisions(decisionResponse.data);
      setActivity(activityResponse.data);
    } catch (err) {
      setError(readError(err));
    }
  }, []);

  React.useEffect(() => void load(), [load]);

  async function analyze() {
    try {
      setLoading(true);
      setError("");
      const encodedSymbol = encodeURIComponent(symbol);
      const { data } = await api.post<AgentAnalysis>(`/agents/analyze?symbol=${encodedSymbol}`);
      setAnalysis(data);
      await load();
    } catch (err) {
      setError(readError(err));
    } finally {
      setLoading(false);
    }
  }

  return (
    <section className="space-y-5">
      <Header title="AI-агенты" subtitle="Рыночные и риск-решения с полной историей проверок">
        <select className="agent-symbol-select" value={symbol} onChange={(event) => setSymbol(event.target.value)}>
          {TRADING_SYMBOLS.map((item) => <option key={item} value={item}>{item}</option>)}
        </select>
        <button className="btn primary" onClick={analyze} disabled={loading}><Bot size={16} /> {loading ? "Анализирую" : `Анализ ${symbol}`}</button>
      </Header>
      {error && <Alert tone="danger" text={error} />}
      <div className="metric-grid">
        <Metric label="Всего решений агентов" value={String(activity?.total_decisions ?? 0)} />
        <Metric label="Решений за 24 часа" value={String(activity?.decisions_24h ?? 0)} />
        <Metric label="Активных агентов" value={String(activity?.active_agents ?? 0)} />
        <Metric label="Одобрений комитета" value={String(activity?.committee_approvals ?? 0)} tone={(activity?.committee_approvals ?? 0) > 0 ? "good" : undefined} />
        <Metric label="Последняя активность" value={activity?.last_decision_at ? new Date(activity.last_decision_at).toLocaleString("ru-RU") : "Нет данных"} />
      </div>
      <AgentActivityTable activity={activity} />
      {analysis && (
        <div className="status-strip">
          <StatusItem label="Итоговое действие" value={translateAction(analysis.final_action)} good={analysis.approved} />
          <StatusItem label="Уверенность" value={`${fmt(analysis.final_confidence * 100)}%`} />
          <StatusItem label="Консенсус" value={`${fmt(analysis.consensus_score * 100)}%`} good={analysis.consensus_score >= 0.75} />
          <StatusItem label="Рыночный агент" value={translateAction(analysis.market.action)} good={analysis.market.action !== "WAIT"} />
          <StatusItem label="AI-советник" value={analysis.llm ? translateAction(analysis.llm.action) : "Выкл"} good={!analysis.llm || analysis.llm.action !== "WAIT"} />
        </div>
      )}
      {analysis && (
        <>
          <div className="two-col">
            <AgentCard decision={analysis.market} />
            {analysis.llm && <AgentCard decision={analysis.llm} />}
            <AgentCard decision={analysis.risk} />
          </div>
          <div className="table-wrap">
            <div className="table-title">Торговый комитет</div>
            <table>
              <thead><tr><th>Агент</th><th>Роль в соревновании</th><th>Рейтинг</th><th>Голос</th><th>Уверенность</th><th>Причина</th></tr></thead>
              <tbody>
                {analysis.committee.map((item) => (
                  <tr key={item.agent_name}>
                    <td>{translateAgentName(item.agent_name)}</td>
                    <td>{translateCompetitionStatus(typeof item.context.competition_status === "string" ? item.context.competition_status : null)}</td>
                    <td>{typeof item.context.performance_rating === "number" ? `${fmt(item.context.performance_rating * 100)}%` : "—"}</td>
                    <td><ActionPill action={item.action} /></td>
                    <td>{fmt(item.confidence * 100)}%</td>
                    <td>{item.rationale}</td>
                  </tr>
                ))}
                {!analysis.committee.length && <EmptyRow cols={6} text="Голосов комитета пока нет" />}
              </tbody>
            </table>
          </div>
        </>
      )}
      <div className="table-wrap">
        <div className="table-title">Последние решения агентов</div>
        <table>
          <thead><tr><th>Время</th><th>Агент</th><th>Пара</th><th>Действие</th><th>Уверенность</th><th>Обоснование</th></tr></thead>
          <tbody>
            {decisions.map((item, index) => (
              <tr key={`${item.agent_name}-${item.symbol}-${index}`}>
                <td>{item.created_at ? new Date(item.created_at).toLocaleString("ru-RU") : "-"}</td>
                <td>{translateAgentName(item.agent_name)}</td>
                <td>{item.symbol}</td>
                <td><ActionPill action={item.action} /></td>
                <td>{fmt(item.confidence * 100)}%</td>
                <td>{item.rationale}</td>
              </tr>
            ))}
            {!decisions.length && <EmptyRow cols={6} text="Решений агентов пока нет" />}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function AgentCard({ decision }: { decision: AgentDecision }) {
  return (
    <div className="panel-block">
      <div className="table-title">{translateAgentName(decision.agent_name)}</div>
      <div className="agent-card-body">
        <ActionPill action={decision.action} />
        <Metric label="Уверенность" value={`${fmt(decision.confidence * 100)}%`} />
        <p className="muted">{decision.rationale}</p>
      </div>
    </div>
  );
}

function ActionPill({ action }: { action: AgentDecision["action"] }) {
  const tone = action === "BUY" || action === "ALLOW" ? "buy" : action === "SELL" || action === "BLOCK" ? "sell" : "";
  return <span className={`pill ${tone}`}>{translateAction(action)}</span>;
}

function NavButton(props: { active: boolean; onClick: () => void; icon: React.ReactNode; label: string }) {
  return (
    <button className={`nav ${props.active ? "active" : ""}`} onClick={props.onClick} title={props.label}>
      {props.icon}
      <span>{props.label}</span>
    </button>
  );
}

function DashboardView() {
  const [data, setData] = React.useState<Dashboard | null>(null);
  const [orders, setOrders] = React.useState<Order[]>([]);
  const [optimizations, setOptimizations] = React.useState<StrategyOptimization[]>([]);
  const [learningRules, setLearningRules] = React.useState<LearningRule[]>([]);
  const [learningSummary, setLearningSummary] = React.useState<LearningSummary | null>(null);
  const [learningInsights, setLearningInsights] = React.useState<LearningInsights | null>(null);
  const [learningProgress, setLearningProgress] = React.useState<LearningProgress | null>(null);
  const [rlModels, setRlModels] = React.useState<RlModel[]>([]);
  const [shadowTrades, setShadowTrades] = React.useState<ShadowTrade[]>([]);
  const [postMortems, setPostMortems] = React.useState<TradePostMortem[]>([]);
  const [status, setStatus] = React.useState<SystemStatus | null>(null);
  const [guard, setGuard] = React.useState<PerformanceGuard | null>(null);
  const [backtest, setBacktest] = React.useState<BacktestReport | null>(null);
  const [walkForward, setWalkForward] = React.useState<WalkForwardReport | null>(null);
  const [historyResult, setHistoryResult] = React.useState<HistoryIngest | null>(null);
  const [batchHistory, setBatchHistory] = React.useState<HistoryBatchIngest | null>(null);
  const [readiness, setReadiness] = React.useState<HistoryReadiness[]>([]);
  const [run, setRun] = React.useState<TradingRun | null>(null);
  const [tick, setTick] = React.useState<TradingTick | null>(null);
  const [error, setError] = React.useState("");
  const [loading, setLoading] = React.useState(false);
  const [refreshing, setRefreshing] = React.useState(false);
  const loadSeq = React.useRef(0);

  const load = React.useCallback(async () => {
    const seq = loadSeq.current + 1;
    loadSeq.current = seq;
    const failures: string[] = [];

    async function request<T>(label: string, call: Promise<{ data: T }>, apply: (value: T) => void) {
      try {
        const response = await call;
        if (loadSeq.current === seq) {
          apply(response.data);
        }
      } catch (err) {
        if (loadSeq.current === seq) {
          failures.push(`${label}: ${readError(err)}`);
        }
      }
    }

    setError("");
    setRefreshing(true);
    await Promise.all([
      request<Dashboard>("Панель", api.get<Dashboard>("/dashboard"), setData),
      request<SystemStatus>("Статус", api.get<SystemStatus>("/trading/status"), setStatus),
      request<PerformanceGuard>("Защита", api.get<PerformanceGuard>("/trading/guard"), setGuard),
      request<BacktestReport>("Бэктест", api.get<BacktestReport>("/trading/backtest/sample"), setBacktest),
      request<Order[]>("Ордера", api.get<Order[]>("/orders"), setOrders),
      request<StrategyOptimization[]>("Оптимизация", api.get<StrategyOptimization[]>("/strategy-lab/results"), setOptimizations),
      request<HistoryReadiness[]>("Свечи", api.get<HistoryReadiness[]>("/market/history/readiness"), setReadiness),
      request<LearningRule[]>("Обучение", api.get<LearningRule[]>("/strategy-lab/learning-rules"), setLearningRules),
      request<LearningSummary>("Память", api.get<LearningSummary>("/strategy-lab/learning-summary"), setLearningSummary),
      request<LearningInsights>("Выводы обучения", api.get<LearningInsights>("/strategy-lab/learning-insights"), setLearningInsights),
      request<LearningProgress>("Прогресс обучения", api.get<LearningProgress>("/strategy-lab/learning-progress"), setLearningProgress),
      request<RlModel[]>("RL-модели", api.get<RlModel[]>("/strategy-lab/rl-models"), setRlModels),
      request<ShadowTrade[]>("Теневые сделки", api.get<ShadowTrade[]>("/strategy-lab/shadow-trades"), setShadowTrades),
      request<TradePostMortem[]>("Разбор ошибок", api.get<TradePostMortem[]>("/strategy-lab/post-mortems"), setPostMortems)
    ]);
    if (loadSeq.current === seq) {
      setRefreshing(false);
      if (failures.length) {
        setError(`Часть данных не загрузилась: ${failures[0]}`);
      }
    }
  }, []);

  React.useEffect(() => void load(), [load]);

  async function runTrading() {
    try {
      setLoading(true);
      setError("");
      const { data: result } = await api.post<TradingRun>("/trading/run-once");
      setRun(result);
      void load();
    } catch (err) {
      setError(readError(err));
    } finally {
      setLoading(false);
    }
  }

  async function managePositions() {
    try {
      setLoading(true);
      setError("");
      const { data: result } = await api.post<TradingTick>("/trading/tick");
      setTick(result);
      void load();
    } catch (err) {
      setError(readError(err));
    } finally {
      setLoading(false);
    }
  }

  async function loadHistoryAndBacktest() {
    try {
      setLoading(true);
      setError("");
      const symbol = encodeURIComponent("ETH/USDT");
      const { data: history } = await api.post<HistoryIngest>(`/market/history/ingest?symbol=${symbol}&timeframe=1h&limit=500`);
      setHistoryResult(history);
      const { data: report } = await api.post<BacktestReport>(`/trading/backtest?symbol=${symbol}&timeframe=1h&limit=500`);
      setBacktest(report);
    } catch (err) {
      setError(readError(err));
    } finally {
      setLoading(false);
    }
  }

  async function optimizeStrategy() {
    try {
      setLoading(true);
      setError("");
      const symbol = encodeURIComponent("ETH/USDT");
      const { data } = await api.post<StrategyOptimization[]>(`/strategy-lab/optimize?symbol=${symbol}&timeframe=1h&limit=500`);
      setOptimizations(data);
    } catch (err) {
      setError(readError(err));
    } finally {
      setLoading(false);
    }
  }

  async function runWalkForward() {
    try {
      setLoading(true);
      setError("");
      const symbol = encodeURIComponent("ETH/USDT");
      const { data } = await api.post<WalkForwardReport>(`/trading/backtest/walk-forward?symbol=${symbol}&timeframe=1h&limit=1000`);
      setWalkForward(data);
    } catch (err) {
      setError(readError(err));
    } finally {
      setLoading(false);
    }
  }

  async function ingestBatchHistory() {
    try {
      setLoading(true);
      setError("");
      const { data } = await api.post<HistoryBatchIngest>("/market/history/ingest/batch");
      setBatchHistory(data);
      setReadiness((await api.get<HistoryReadiness[]>("/market/history/readiness")).data);
    } catch (err) {
      setError(readError(err));
    } finally {
      setLoading(false);
    }
  }

  return (
    <section className="space-y-5">
      <Header title="Панель" subtitle="Портфель, риск-состояние и сводка работы бота">
        <button className="btn" onClick={() => void load()} disabled={refreshing}><RefreshCw size={16} /> {refreshing ? "Обновляется" : "Обновить"}</button>
        <button className="btn" onClick={loadHistoryAndBacktest} disabled={loading}><BarChart3 size={16} /> Бэктест ETH</button>
        <button className="btn" onClick={runWalkForward} disabled={loading}><BarChart3 size={16} /> Walk-forward</button>
        <button className="btn" onClick={ingestBatchHistory} disabled={loading}><RefreshCw size={16} /> Загрузить свечи</button>
        <button className="btn" onClick={optimizeStrategy} disabled={loading}><Settings size={16} /> Оптимизировать</button>
        <button className="btn" onClick={managePositions} disabled={loading}><Activity size={16} /> Проверить позиции</button>
        <button className="btn primary" onClick={runTrading} disabled={loading}><Play size={16} /> {loading ? "Запуск" : "Сканировать"}</button>
      </Header>
      {error && <Alert tone="danger" text={error} />}
      {status?.exchange_error && <Alert tone="danger" text={`Биржа: ${status.exchange_error}`} />}
      <div className="status-strip">
        <StatusItem label="Режим" value={status?.paper_trading ? "Paper-торговля" : "Live-торговля"} good={status?.paper_trading ?? true} />
        <StatusItem
          label="Биржа"
          value={status ? `${status.exchange} / ${status.exchange_market_type} / ${status.exchange_sandbox_enabled ? "sandbox" : "real"}` : "-"}
          good={status?.exchange_connected ?? true}
        />
        <StatusItem label="Telegram" value={status?.telegram_enabled ? `${status.telegram_chat_count} чат` : "Отключен"} good={Boolean(status?.telegram_enabled)} />
        <StatusItem label="Данные рынка" value={status?.real_market_data ? "Реальный рынок" : "Синтетика"} good={status?.real_market_data ?? false} />
        <StatusItem label="Открытые позиции" value={String(status?.open_positions ?? 0)} />
        <StatusItem
          label="Экспозиция"
          value={`${fmt(status?.gross_exposure_percent)}%`}
          good={(status?.gross_exposure_percent ?? 0) <= (status?.max_gross_exposure_percent ?? 300)}
        />
        <StatusItem label="Комитет" value={status?.ai_committee_enabled ? `${fmt((status.ai_committee_min_consensus ?? 0) * 100)}%` : "Выкл"} good={status?.ai_committee_enabled ?? true} />
        <StatusItem
          label="Защита"
          value={
            guard?.recovery_mode
              ? `Восстановление · риск ${fmt(guard.risk_multiplier * 100)}%`
              : guard?.retry_at
                ? `Пауза до ${new Date(guard.retry_at).toLocaleString("ru-RU")}`
                : guard?.allowed
                  ? "Разрешено"
                  : "Заблокировано"
          }
          good={guard?.allowed ?? true}
        />
      </div>
      <div className="metric-grid">
        <Metric
          label={data?.balance_source === "PAPER" ? "Paper-баланс" : "Баланс биржи"}
          value={`$${fmt(data?.balance)}`}
        />
        {data?.starting_balance != null && (
          <Metric
            label={`Изменение от $${fmt(data.starting_balance)}`}
            value={fmtSignedUsd(data.balance_change)}
            tone={(data.balance_change ?? 0) >= 0 ? "good" : "bad"}
          />
        )}
        {data?.starting_balance != null && (
          <Metric
            label="Доходность paper-баланса"
            value={fmtSignedPercent(data.balance_change_percent)}
            tone={(data.balance_change_percent ?? 0) >= 0 ? "good" : "bad"}
          />
        )}
        <Metric label="PnL за день" value={`$${fmt(data?.pnl_day)}`} tone={(data?.pnl_day ?? 0) >= 0 ? "good" : "bad"} />
        <Metric label="PnL за неделю" value={`$${fmt(data?.pnl_week)}`} />
        <Metric label="Win Rate за всё время" value={`${fmt(data?.analytics?.win_rate ?? data?.win_rate)}%`} />
        <Metric label="Закрыто сделок за всё время" value={String(data?.analytics?.closed_trades ?? data?.trades_count ?? 0)} />
      </div>
      <LearningProgressPanel data={learningProgress} />
      <ExperienceReplayPanel postMortems={postMortems} shadowTrades={shadowTrades} />
      <TradeAnalyticsPanel analytics={data?.analytics ?? null} />
      {run && (
        <div className="panel-block">
          <div className="table-title">Последний запуск: просканировано {run.scanned}, открыто {run.opened}, пропущено {run.skipped}</div>
          <DecisionList run={run} />
        </div>
      )}
      {tick && (
        <Alert
          tone={tick.closed > 0 ? "good" : "good"}
          text={`Менеджер позиций проверил ${tick.checked}, закрыл ${tick.closed}, обновил ${tick.updated.length}.`}
        />
      )}
      <div className="two-col">
        <PositionsTable data={data} onChanged={load} />
        <div className="panel-block">
          <div className="table-title">Бэктест</div>
          {historyResult && <p className="muted">Загружено {historyResult.inserted} новых свечей {historyResult.timeframe} для {historyResult.symbol}.</p>}
          <div className="mini-grid">
            <Metric label="Win Rate" value={`${fmt(backtest?.win_rate)}%`} />
            <Metric label="Profit Factor" value={fmt(backtest?.profit_factor)} />
            <Metric label="Сделки" value={String(backtest?.trades_count ?? 0)} />
            <Metric label="Общая прибыль" value={`$${fmt(backtest?.total_profit)}`} tone={(backtest?.total_profit ?? 0) >= 0 ? "good" : "bad"} />
            <Metric label="Макс. просадка" value={`$${fmt(backtest?.max_drawdown)}`} tone="bad" />
            <Metric label="Средняя прибыль" value={`$${fmt(backtest?.average_profit)}`} tone="good" />
          </div>
          {walkForward && (
            <>
              <div className="table-title mt-4">Walk-forward</div>
              <div className="mini-grid">
                <Metric label="Окна" value={`${walkForward.profitable_windows}/${walkForward.window_count}`} />
                <Metric label="WF прибыль" value={`$${fmt(walkForward.total_profit)}`} tone={walkForward.total_profit >= 0 ? "good" : "bad"} />
                <Metric label="Среднее окно" value={`$${fmt(walkForward.average_window_profit)}`} />
                <Metric label="Средний Win Rate" value={`${fmt(walkForward.average_win_rate)}%`} />
                <Metric label="Средний PF" value={fmt(walkForward.average_profit_factor)} />
                <Metric label="Худшая просадка" value={`$${fmt(walkForward.max_drawdown)}`} tone="bad" />
              </div>
            </>
          )}
        </div>
      </div>
      <TradeChartsPanel />
      <OrdersTable orders={orders} onChanged={load} />
      <LearningInsightsPanel data={learningInsights} />
      <LearningRulesTable items={learningRules} summary={learningSummary} />
      <RlModelsTable items={rlModels} />
      <ReadinessTable items={readiness} batch={batchHistory} />
      <OptimizationTable items={optimizations} />
    </section>
  );
}

function TradingAuditView() {
  const [data, setData] = React.useState<TradingAudit | null>(null);
  const [error, setError] = React.useState("");
  const [loading, setLoading] = React.useState(false);

  const load = React.useCallback(async () => {
    try {
      setLoading(true);
      setError("");
      const response = await api.get<TradingAudit>("/audit/trading-30d");
      setData(response.data);
    } catch (err) {
      setError(readError(err));
    } finally {
      setLoading(false);
    }
  }, []);

  React.useEffect(() => void load(), [load]);
  const total = data?.total;
  const conclusion = data?.final_conclusion;

  return (
    <section className="space-y-5">
      <Header title="Аудит торговли — 30 полных дней" subtitle="Только сохранённые торговые факты. Отсутствующие данные обозначены явно и не заменяются предположениями.">
        <button className="btn" onClick={() => void load()} disabled={loading}><RefreshCw size={16} /> {loading ? "Обновляется" : "Обновить отчёт"}</button>
      </Header>
      {error && <Alert tone="danger" text={error} />}
      {data && <>
        <div className="status-strip">
          <StatusItem label="Период" value={`${data.period.start} — ${data.period.end_exclusive}`} />
          <StatusItem label="Часовой пояс" value={data.timezone} />
          <StatusItem label="Торговых дней" value={String(data.period.trading_days ?? 0)} />
          <StatusItem label="Закрытых сделок" value={String(data.period.closed_trades ?? 0)} />
        </div>
        <div className="metric-grid">
          <Metric label="Net PnL, USDT" value={`$${fmt(total?.net_pnl)}`} tone={(total?.net_pnl ?? 0) >= 0 ? "good" : "bad"} />
          <Metric label="Win Rate" value={`${fmt(total?.win_rate)}%`} />
          <Metric label="Gross Profit, USDT" value={`$${fmt(total?.gross_profit)}`} tone="good" />
          <Metric label="Gross Loss, USDT" value={`$${fmt(total?.gross_loss)}`} tone="bad" />
          <Metric label="Profit Factor" value={formatProfitFactor(total?.profit_factor)} tone={(total?.profit_factor ?? 0) >= 1 ? "good" : "bad"} />
        </div>
        <AuditFacts title="Качество и границы расчёта" data={data.data_quality} />
        <AuditDailyTable rows={data.daily_results} />
        <AuditTimeline data={data.time_sequence} />
        <div className="two-col">
          <AuditFacts title="Дневной анализ" data={data.daily_analysis} />
          <AuditFacts title="До и после корректировок" data={data.before_after_changes} />
        </div>
        <AuditSymbolTable rows={data.by_symbol} />
        <div className="two-col">
          <AuditFacts title="Топ-5 по положительному PnL" data={{ pairs: data.top_symbols.positive }} />
          <AuditFacts title="Топ-5 по отрицательному PnL" data={{ pairs: data.top_symbols.negative }} />
        </div>
        <div className="two-col">
          <AuditFacts title="Причины убыточных сделок" data={data.loss_causes} />
          <AuditFacts title="Точка входа: MFE/MAE и раннее движение" data={data.entry_analysis} />
        </div>
        <div className="two-col">
          <AuditFacts title="Take Profit" data={data.take_profit} />
          <AuditFacts title="Stop Loss" data={data.stop_loss} />
        </div>
        <div className="two-col">
          <AuditFacts title="Капитал и риск" data={data.capital_and_risk} />
          <AuditFacts title="Одновременные позиции и корреляция" data={data.position_correlation} />
        </div>
        <AuditFacts title="Серии убытков" data={data.streaks} />
        <AuditDecisionAlgorithm data={data.decision_algorithm} />
        <AuditExternalSources data={data.external_sources} />
        <AuditFinal15 items={data.final_15} />
        <AuditConclusion data={conclusion} />
        <div className="table-wrap">
          <div className="table-title">Каких данных не хватает для достоверных выводов</div>
          <table>
            <thead><tr><th>Раздел</th><th>Причина</th></tr></thead>
            <tbody>
              {data.limitations.map((item) => <tr key={item.area}><td className="font-semibold">{item.area}</td><td>{item.message}</td></tr>)}
              {!data.limitations.length && <EmptyRow cols={2} text="Ограничения не зафиксированы" />}
            </tbody>
          </table>
        </div>
      </>}
    </section>
  );
}

function AuditDailyTable({ rows }: { rows: TradingAuditDay[] }) {
  return (
    <div className="table-wrap">
      <div className="table-title">Результат по каждому дню — все суммы в USDT</div>
      <table>
        <thead><tr><th>Дата</th><th>Сделок</th><th>Прибыльных</th><th>Убыточных</th><th>Win Rate</th><th>Gross Profit</th><th>Gross Loss</th><th>Net PnL</th><th>Средний PnL</th><th>Max DD</th><th>Profit Factor</th></tr></thead>
        <tbody>
          {rows.map((row) => <tr key={row.date}>
            <td className="font-semibold">{row.date}</td><td>{row.trades}</td><td>{row.profitable}</td><td>{row.losing}</td><td>{fmt(row.win_rate)}%</td>
            <td className="text-accent">${fmt(row.gross_profit)}</td><td className="text-danger">${fmt(row.gross_loss)}</td>
            <td className={row.net_pnl >= 0 ? "text-accent" : "text-danger"}>${fmt(row.net_pnl)}</td><td>${fmt(row.average_pnl)}</td><td>${fmt(row.max_drawdown)}</td><td>{formatProfitFactor(row.profit_factor)}</td>
          </tr>)}
          {!rows.length && <EmptyRow cols={11} text="Дневных записей нет" />}
        </tbody>
      </table>
    </div>
  );
}

function AuditTimeline({ data }: { data: Record<string, unknown> }) {
  const timeline = Array.isArray(data.timeline) ? data.timeline as Array<Record<string, unknown>> : [];
  const change = data.change_point;
  return <div className="table-wrap">
    <div className="table-title">Временная последовательность: День 1 → День 30</div>
    <div className="audit-timeline">
      {timeline.map((row) => {
        const pnl = Number(row.net_pnl ?? 0);
        return <div className={`audit-day ${pnl > 0 ? "positive" : pnl < 0 ? "negative" : "neutral"}`} key={String(row.day)}>
          <strong>День {String(row.day)}</strong><span>{String(row.date)}</span><span>PNL ${fmt(pnl)}</span><span>{String(row.trades)} сделок · {fmt(Number(row.win_rate ?? 0))}%</span>
        </div>;
      })}
    </div>
    <div className="audit-fact-line"><strong>Точка изменения:</strong> {auditValue(change)}</div>
  </div>;
}

function AuditSymbolTable({ rows }: { rows: TradingAuditSymbol[] }) {
  return <div className="table-wrap">
    <div className="table-title">Результат по каждой торговой паре — не сгруппировано по категориям</div>
    <table><thead><tr><th>Монета</th><th>Сделок</th><th>Win Rate</th><th>Gross Profit</th><th>Gross Loss</th><th>Net PnL</th><th>Avg PnL</th><th>Max Loss</th><th>Max Win</th><th>Profit Factor</th></tr></thead>
      <tbody>{rows.map((row) => <tr key={row.symbol}><td className="font-semibold">{row.symbol}</td><td>{row.trades}</td><td>{fmt(row.win_rate)}%</td><td className="text-accent">${fmt(row.gross_profit)}</td><td className="text-danger">${fmt(row.gross_loss)}</td><td className={row.net_pnl >= 0 ? "text-accent" : "text-danger"}>${fmt(row.net_pnl)}</td><td>${fmt(row.average_pnl)}</td><td>${fmt(row.max_loss ?? undefined)}</td><td>${fmt(row.max_win ?? undefined)}</td><td>{formatProfitFactor(row.profit_factor)}</td></tr>)}
      {!rows.length && <EmptyRow cols={10} text="Закрытых сделок за период нет" />}</tbody>
    </table>
  </div>;
}

function AuditDecisionAlgorithm({ data }: { data: Record<string, unknown> }) {
  const sequence = Array.isArray(data.sequence) ? data.sequence : [];
  const conditions = Array.isArray(data.simultaneous_conditions) ? data.simultaneous_conditions : [];
  const inputs = Array.isArray(data.inputs) ? data.inputs as Array<Record<string, unknown>> : [];
  return <div className="space-y-5">
    <div className="two-col"><AuditFacts title="Фактическая последовательность открытия позиции" data={{ source: data.source, steps: sequence }} /><AuditFacts title="Условия, которые должны выполняться одновременно" data={{ conditions }} /></div>
    <div className="table-wrap"><div className="table-title">Используемые данные и их влияние на решение</div><table><thead><tr><th>Категория</th><th>Параметр</th><th>Используется</th><th>Как влияет</th></tr></thead><tbody>{inputs.map((item, index) => <tr key={`${String(item.parameter)}-${index}`}><td>{String(item.category)}</td><td className="font-semibold">{String(item.parameter)}</td><td><span className={`pill ${item.used ? "buy" : "sell"}`}>{item.used ? "Да" : "Нет"}</span></td><td>{String(item.how)}</td></tr>)}{!inputs.length && <EmptyRow cols={4} text="Описание алгоритма недоступно" />}</tbody></table></div>
  </div>;
}

function AuditExternalSources({ data }: { data: TradingAudit["external_sources"] }) {
  return <div className="table-wrap"><div className="table-title">Внешние источники и влияние на вход/выход</div><table><thead><tr><th>Источник</th><th>Используется</th><th>Как часто</th><th>Какие данные</th><th>Вход</th><th>Выход</th></tr></thead><tbody>{(data.sources ?? []).map((item, index) => <tr key={`${String(item.source)}-${index}`}><td className="font-semibold">{String(item.source)}</td><td>{item.used ? "Да" : "Нет"}</td><td>{String(item.frequency)}</td><td>{String(item.data)}</td><td>{item.affects_entry ? "Да" : "Нет"}</td><td>{item.affects_exit ? "Да" : "Нет"}</td></tr>)}{!(data.sources ?? []).length && <EmptyRow cols={6} text="Сведения об источниках отсутствуют" />}</tbody></table><div className="audit-fact-line"><strong>Технический анализ без новостей:</strong> {data.answer ?? "—"}</div></div>;
}

function AuditFacts({ title, data }: { title: string; data: Record<string, unknown> }) {
  return <div className="panel-block"><div className="table-title">{title}</div><div className="audit-facts">{Object.entries(data).map(([key, value]) => <div key={key}><strong>{auditLabel(key)}</strong><span>{auditValue(value)}</span></div>)}</div></div>;
}

function AuditFinal15({ items }: { items: TradingAudit["final_15"] }) {
  return <div className="table-wrap"><div className="table-title">Итоговый отчёт в 15 пунктах</div><table><thead><tr><th>Вопрос</th><th>Фактический ответ</th></tr></thead><tbody>{items.map((item) => <tr key={item.question}><td className="font-semibold">{item.question}</td><td>{auditValue(item.answer)}</td></tr>)}{!items.length && <EmptyRow cols={2} text="Итоговые пункты недоступны" />}</tbody></table></div>;
}

function AuditConclusion({ data }: { data: TradingAudit["final_conclusion"] | undefined }) {
  if (!data) return null;
  return <div className="space-y-5"><AuditConclusionBlock title="ЧТО РАБОТАЕТ" items={data.what_works} /><AuditConclusionBlock title="ЧТО НЕ РАБОТАЕТ" items={data.what_does_not_work} /><div className="table-wrap"><div className="table-title">ЧТО НУЖНО ПРОВЕРИТЬ В ПЕРВУЮ ОЧЕРЕДЬ</div><table><thead><tr><th>Проблема</th><th>Доказательство</th><th>Предполагаемое влияние</th><th>Какие данные нужны</th></tr></thead><tbody>{data.check_first.map((item) => <tr key={item.problem}><td className="font-semibold">{item.problem}</td><td>{item.evidence}</td><td>{item.expected_impact}</td><td>{item.required_data}</td></tr>)}</tbody></table></div></div>;
}

function AuditConclusionBlock({ title, items }: { title: string; items: Array<{ statement: string; evidence: string }> }) {
  return <div className="panel-block"><div className="table-title">{title}</div><div className="audit-facts">{items.map((item, index) => <div key={`${item.statement}-${index}`}><strong>{item.statement}</strong><span>{item.evidence}</span></div>)}</div></div>;
}

function auditLabel(value: string) { return value.replace(/_/g, " "); }
function auditValue(value: unknown): string {
  if (value == null) return "—";
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") return String(value);
  if (Array.isArray(value)) return value.length ? value.map(auditValue).join(" · ") : "—";
  try { return JSON.stringify(value, null, 2); } catch { return "—"; }
}

function LearningProgressPanel({ data }: { data: LearningProgress | null }) {
  const milestones = data?.milestones ?? [];
  const blockers = data?.top_blockers_24h ?? [];
  const fleet = data?.rl_fleet;
  const rlCoverage = fleet?.target_pairs ? Math.round((fleet.active_pairs / fleet.target_pairs) * 100) : 0;
  return (
    <div className="panel-block learning-progress-panel">
      <div className="table-title table-title-row">
        <span>Прогресс обучения и поток решений</span>
        <span className={`pill ${data?.stage === "MATURE" ? "buy" : ""}`}>{learningStageLabel(data?.stage)}</span>
      </div>
      <div className="learning-progress-body">
        <div className="learning-progress-hero">
          <div>
            <span className="muted">Общий прогресс до устойчивой обучающей базы</span>
            <strong>{fmt(data?.overall_progress_percent)}%</strong>
          </div>
          <div className="learning-progress-track" aria-label="Прогресс обучения">
            <span style={{ width: `${Math.min(Math.max(data?.overall_progress_percent ?? 0, 0), 100)}%` }} />
          </div>
          <p className="muted">
            Следующая цель: {milestoneLabel(data?.next_milestone)}. Последний урок: {formatDateTime(data?.last_trade_closed_at)}.
          </p>
        </div>
        <div className="analytics-grid">
          <Metric label="Закрыто всего / 7д / 24ч" value={`${data?.closed_trades ?? 0} / ${data?.closed_7d ?? 0} / ${data?.closed_24h ?? 0}`} />
          <Metric label="Учебные позиции открыто / закрыто" value={`${data?.exploration_open_positions ?? 0} / ${data?.exploration_closed_trades ?? 0}`} />
          <Metric label="Сигналы 24ч" value={`${data?.signals_24h ?? 0}`} />
          <Metric label="Направленные / WAIT 24ч" value={`${data?.directional_signals_24h ?? 0} / ${data?.waits_24h ?? 0}`} />
          <Metric label="Сильные WAIT-кандидаты 24ч" value={`${data?.strong_waits_24h ?? 0}`} />
          <Metric label="Торговые / исключённые пары" value={`${data?.trading_symbols?.length ?? 0} / ${data?.excluded_symbols?.length ?? 0}`} />
          <Metric label="Решения агентов 24ч" value={`${data?.agent_decisions_24h ?? 0}`} />
          <Metric label="Правила / наблюдения" value={`${data?.learning_rules ?? 0} / ${data?.learning_observations ?? 0}`} />
          <Metric label="Post-mortem / исправимые ошибки" value={`${data?.bad_experiences ?? 0} / ${data?.avoidable_failures ?? 0}`} tone={(data?.avoidable_failures ?? 0) > 0 ? "bad" : undefined} />
          <Metric label="Дисциплинированные стопы" value={`${data?.disciplined_stop_losses ?? 0}`} tone={(data?.disciplined_stop_losses ?? 0) > 0 ? "good" : undefined} />
          <Metric label="Свечи готовы по парам" value={`${data?.candle_pairs_ready ?? 0} / ${data?.candle_pairs_total ?? 0}`} />
          <Metric label="Покрытие активных RL-пар" value={`${fleet?.active_pairs ?? 0} / ${fleet?.target_pairs ?? 0}`} tone={rlCoverage >= 80 ? "good" : "bad"} />
          <Metric label="Оптимизировано пар" value={`${data?.optimized_pairs ?? 0}`} />
          <Metric
            label="Performance guard"
            value={data?.guard_recovery_mode ? "Восстановление" : data?.guard_allowed ? "Разрешает" : "Пауза"}
            tone={data?.guard_allowed ? undefined : "bad"}
          />
        </div>
        <div className="rl-control-center">
          <div className="rl-control-header">
            <div>
              <span className="rl-control-kicker">RL CONTROL CENTER</span>
              <h3>Парк обучающихся моделей</h3>
              <p className="muted">
                Покрытие считается по торговым парам. Исторические попытки обучения показываются отдельно и больше не выглядят как «512 пар».
              </p>
            </div>
            <div className={`rl-coverage-orb ${rlCoverage >= 80 ? "ready" : ""}`}>
              <strong>{rlCoverage}%</strong>
              <span>покрытие</span>
            </div>
          </div>
          <div className="analytics-grid rl-fleet-grid">
            <Metric label="Активные пары / цель" value={`${fleet?.active_pairs ?? 0} / ${fleet?.target_pairs ?? 0}`} tone={rlCoverage >= 80 ? "good" : "bad"} />
            <Metric label="Активные модели" value={`${fleet?.active_models ?? 0}`} tone="good" />
            <Metric label="Теневые модели" value={`${fleet?.shadow_models ?? 0}`} />
            <Metric label="Всего экспериментов" value={`${fleet?.total_experiments ?? 0}`} />
            <Metric label="Успешных повышений" value={`${fleet?.promoted_experiments ?? 0} · ${fmt(fleet?.promotion_rate_percent)}%`} tone={(fleet?.promotion_rate_percent ?? 0) > 0 ? "good" : undefined} />
            <Metric label="Отклонено / архив" value={`${fleet?.rejected_models ?? 0} / ${fleet?.retired_models ?? 0}`} />
            <Metric label="Решения active / shadow за 24ч" value={`${fleet?.active_decisions_24h ?? 0} / ${fleet?.shadow_decisions_24h ?? 0}`} />
            <Metric label="Виртуальные позиции open / closed" value={`${fleet?.shadow_open_trades ?? 0} / ${fleet?.shadow_closed_trades ?? 0}`} />
            <Metric label="Shadow Win Rate / PnL" value={`${fmt(fleet?.shadow_win_rate)}% / $${fmt(fleet?.shadow_pnl)}`} tone={(fleet?.shadow_pnl ?? 0) >= 0 ? "good" : "bad"} />
            <Metric label="Последнее обучение" value={formatDateTime(fleet?.last_training_at)} />
          </div>
          <div className="rl-pair-coverage">
            <div>
              <strong>{fleet?.uncovered_pairs?.length ? "Пары в очереди на безопасное обучение" : "Все настроенные пары покрыты"}</strong>
              <span className="muted">Теневая модель не имеет права открывать сделки, пока не пройдёт validation.</span>
            </div>
            <div className="rl-pair-chips">
              {(fleet?.uncovered_pairs ?? []).map((symbol) => <span className="pill" key={symbol}>{symbol}</span>)}
              {!fleet?.uncovered_pairs?.length && <span className="pill buy">Готово</span>}
            </div>
          </div>
        </div>
        <div className="learning-milestones">
          {milestones.map((item) => (
            <div className="learning-milestone" key={item.key}>
              <div><span>{milestoneLabel(item.key)}</span><strong>{item.current} / {item.target}</strong></div>
              <div className="learning-progress-track"><span style={{ width: `${item.progress_percent}%` }} /></div>
            </div>
          ))}
        </div>
        <div className="learning-guard-note">
          <strong>Сейчас:</strong> {data?.guard_reason ?? "данные загружаются"}. Учебный контур работает только в paper-режиме и не ослабляет live-правила.
          {!!data?.excluded_symbols?.length && (
            <span> Исключено из новых входов, RL и shadow: <strong>{data.excluded_symbols.join(", ")}</strong>.</span>
          )}
        </div>
      </div>
      <div className="table-title">Почему входы чаще всего не открылись за 24 часа</div>
      <table>
        <thead><tr><th>Причина</th><th>Количество решений</th></tr></thead>
        <tbody>
          {blockers.map((item) => <tr key={item.reason}><td>{blockerLabel(item.reason)}</td><td>{item.count}</td></tr>)}
          {!blockers.length && <EmptyRow cols={2} text="Отказы ещё не накопились — бот продолжает сканирование" />}
        </tbody>
      </table>
    </div>
  );
}

function ExperienceReplayPanel({ postMortems, shadowTrades }: { postMortems: TradePostMortem[]; shadowTrades: ShadowTrade[] }) {
  return (
    <div className="experience-grid">
      <div className="table-wrap experience-card">
        <div className="table-title">BAD EXPERIENCE REPLAY · разбор убыточных сделок</div>
        <p className="muted">Каждая ошибка получает причину, поведенческий reward и приоритет повторного изучения. Правильный стоп отмечается отдельно и не считается плохой дисциплиной.</p>
        <table>
          <thead><tr><th>Сделка</th><th>Причина</th><th>Результат</th><th>Reward</th><th>Приоритет</th><th>Повторы</th><th>Главный урок</th></tr></thead>
          <tbody>
            {postMortems.map((item) => (
              <tr key={item.id}>
                <td><strong>{item.symbol}</strong><br /><span className="muted">#{item.position_id} · {formatDateTime(item.closed_at)}</span></td>
                <td><span className={`pill ${item.strategy_followed ? "buy" : "sell"}`}>{postMortemLabel(item.primary_label)}</span></td>
                <td className="text-danger">${fmt(item.pnl)} · {fmt(item.result_r)}R</td>
                <td className={item.shaped_reward >= 0 ? "text-accent" : "text-danger"}>{fmt(item.shaped_reward)}</td>
                <td>{fmt(item.priority)}</td>
                <td>{item.replay_count}</td>
                <td>{item.lessons[0] ?? "Пример сохранён; данных пока мало для точного вывода."}</td>
              </tr>
            ))}
            {!postMortems.length && <EmptyRow cols={7} text="Убыточных закрытых сделок после включения Post-Mortem пока нет" />}
          </tbody>
        </table>
      </div>
      <div className="table-wrap experience-card">
        <div className="table-title">SHADOW FORWARD TEST · виртуальные сделки без ордеров</div>
        <p className="muted">Теневая модель получает право торговать только после реальных forward-наблюдений: PnL, Profit Factor, Win Rate и просадка проверяются до повышения.</p>
        <table>
          <thead><tr><th>Модель</th><th>Пара</th><th>Сторона</th><th>Статус</th><th>PnL</th><th>Уверенность</th><th>Время</th></tr></thead>
          <tbody>
            {shadowTrades.map((item) => (
              <tr key={item.id}>
                <td>#{item.model_id}</td>
                <td><strong>{item.symbol}</strong></td>
                <td><span className={`pill ${item.side === "LONG" ? "buy" : "sell"}`}>{translateAction(item.side)}</span></td>
                <td><span className={`pill ${item.status === "OPEN" ? "" : item.pnl >= 0 ? "buy" : "sell"}`}>{translateStatus(item.status)}</span></td>
                <td className={item.pnl >= 0 ? "text-accent" : "text-danger"}>${fmt(item.pnl)}</td>
                <td>{fmt(item.confidence * 100)}%</td>
                <td>{formatDateTime(item.closed_at ?? item.entered_at)}</td>
              </tr>
            ))}
            {!shadowTrades.length && <EmptyRow cols={7} text="Теневые модели ещё не открыли виртуальные позиции" />}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function TradeAnalyticsPanel({ analytics }: { analytics: TradeAnalytics | null }) {
  const symbols = analytics?.by_symbol ?? [];
  const trades = analytics?.recent_trades ?? [];
  return (
    <>
      <div className="panel-block">
        <div className="table-title">Результат реальной работы бота за всё время</div>
        <div className="analytics-grid">
          <Metric label="Реализованный PnL" value={`$${fmt(analytics?.total_realized_pnl)}`} tone={(analytics?.total_realized_pnl ?? 0) >= 0 ? "good" : "bad"} />
          <Metric label="Открытый PnL" value={`$${fmt(analytics?.open_pnl)}`} tone={(analytics?.open_pnl ?? 0) >= 0 ? "good" : "bad"} />
          <Metric label="Итоговый PnL" value={`$${fmt(analytics?.net_pnl)}`} tone={(analytics?.net_pnl ?? 0) >= 0 ? "good" : "bad"} />
          <Metric label="Победы / убытки / 0" value={`${analytics?.wins ?? 0} / ${analytics?.losses ?? 0} / ${analytics?.breakeven ?? 0}`} />
          <Metric label="Profit Factor" value={formatProfitFactor(analytics?.profit_factor)} tone={(analytics?.profit_factor ?? 0) >= 1 ? "good" : "bad"} />
          <Metric label="Ожидание на сделку" value={`$${fmt(analytics?.expectancy)}`} tone={(analytics?.expectancy ?? 0) >= 0 ? "good" : "bad"} />
          <Metric label="Средняя прибыль" value={`$${fmt(analytics?.average_win)}`} tone="good" />
          <Metric label="Средний убыток" value={`$${fmt(analytics?.average_loss)}`} tone="bad" />
          <Metric label="Лучшая / худшая" value={`$${fmt(analytics?.best_trade)} / $${fmt(analytics?.worst_trade)}`} />
          <Metric label="Макс. серия W / L" value={`${analytics?.max_win_streak ?? 0} / ${analytics?.max_loss_streak ?? 0}`} />
        </div>
      </div>
      <div className="table-wrap">
        <div className="table-title">Качество сделок по парам</div>
        <table>
          <thead><tr><th>Пара</th><th>Сделки</th><th>W/L</th><th>Win Rate</th><th>PnL</th><th>Средняя</th><th>Profit Factor</th><th>Ожидание</th></tr></thead>
          <tbody>
            {symbols.map((item) => (
              <tr key={item.symbol}>
                <td className="font-semibold">{item.symbol}</td>
                <td>{item.trades}</td>
                <td>{item.wins}/{item.losses}</td>
                <td>{fmt(item.win_rate)}%</td>
                <td className={item.total_pnl >= 0 ? "text-accent" : "text-danger"}>${fmt(item.total_pnl)}</td>
                <td className={item.average_pnl >= 0 ? "text-accent" : "text-danger"}>${fmt(item.average_pnl)}</td>
                <td>{formatProfitFactor(item.profit_factor)}</td>
                <td className={item.expectancy >= 0 ? "text-accent" : "text-danger"}>${fmt(item.expectancy)}</td>
              </tr>
            ))}
            {!symbols.length && <EmptyRow cols={8} text="Закрытых сделок пока нет — статистика появится после первого выхода" />}
          </tbody>
        </table>
      </div>
      <div className="table-wrap">
        <div className="table-title">Понятная история последних сделок</div>
        <table>
          <thead><tr><th>Закрыта</th><th>Пара</th><th>Сторона</th><th>Результат</th><th>PnL</th><th>Доходность</th><th>Уверенность</th><th>Консенсус</th><th>Риск / R:R</th><th>Почему вошёл</th><th>Почему вышел</th></tr></thead>
          <tbody>
            {trades.map((item) => (
              <tr key={item.id}>
                <td>{item.closed_at ? new Date(item.closed_at).toLocaleString("ru-RU") : "-"}</td>
                <td className="font-semibold">{item.symbol}</td>
                <td><span className={`pill ${item.side === "LONG" ? "buy" : "sell"}`}>{translateAction(item.side)}</span></td>
                <td><span className={`pill ${item.result === "WIN" ? "buy" : item.result === "LOSS" ? "sell" : ""}`}>{translateTradeResult(item.result)}</span></td>
                <td className={item.pnl >= 0 ? "text-accent" : "text-danger"}>${fmt(item.pnl)}</td>
                <td className={item.return_percent >= 0 ? "text-accent" : "text-danger"}>{fmt(item.return_percent)}%</td>
                <td>{item.confidence == null ? "-" : `${fmt(item.confidence * 100)}%`}</td>
                <td>{item.consensus_score == null ? "-" : `${fmt(item.consensus_score * 100)}%`}</td>
                <td>{item.risk_percent == null ? "-" : `${fmt(item.risk_percent)}% / ${fmt(item.risk_reward_ratio ?? 0)}`}</td>
                <td title={item.decision_reason}>{item.entry_reasons.length ? item.entry_reasons.slice(0, 2).join("; ") : item.decision_reason || "Старая сделка без сохранённого объяснения"}</td>
                <td>{translateStatus(item.exit_reason ?? "-")}</td>
              </tr>
            ))}
            {!trades.length && <EmptyRow cols={11} text="История появится после закрытия позиции" />}
          </tbody>
        </table>
      </div>
    </>
  );
}

function LearningInsightsPanel({ data }: { data: LearningInsights | null }) {
  const insights = data?.insights ?? [];
  return (
    <div className="table-wrap">
      <div className="table-title">Что именно бот выучил</div>
      <div className="analytics-grid table-summary">
        <Metric label="Сделок-уроков" value={String(data?.learned_from_trades ?? 0)} />
        <Metric label="Обновлено правил" value={String(data?.rules_updated ?? 0)} />
        <Metric label="Надёжных паттернов" value={String(data?.strong_patterns ?? 0)} />
        <Metric label="Защитных выводов" value={String(data?.protective_patterns ?? 0)} tone={(data?.protective_patterns ?? 0) > 0 ? "bad" : undefined} />
        <Metric label="Прибыльных паттернов" value={String(data?.favorable_patterns ?? 0)} tone={(data?.favorable_patterns ?? 0) > 0 ? "good" : undefined} />
      </div>
      <table>
        <thead><tr><th>Вывод</th><th>Пара / scope</th><th>Сторона</th><th>Что заметил</th><th>Наблюдения</th><th>W/L</th><th>Win Rate</th><th>PnL</th><th>Уверенность</th><th>Как влияет</th></tr></thead>
        <tbody>
          {insights.map((item, index) => (
            <tr key={`${item.scope}-${item.side}-${item.feature_key}-${item.feature_value}-${index}`}>
              <td><span className={`pill ${item.impact === "PREFER" ? "buy" : item.impact === "AVOID" ? "sell" : ""}`}>{translateLearningImpact(item.impact)}</span></td>
              <td>{item.scope}</td>
              <td>{translateAction(item.side)}</td>
              <td>{translateFeature(item.feature_key)}: {translateFeatureValue(item.feature_value)}</td>
              <td>{item.observations}</td>
              <td>{item.wins}/{item.losses}</td>
              <td>{fmt(item.win_rate)}%</td>
              <td className={item.total_profit >= 0 ? "text-accent" : "text-danger"}>${fmt(item.total_profit)}</td>
              <td>{fmt(item.confidence * 100)}%</td>
              <td>{item.explanation}</td>
            </tr>
          ))}
          {!insights.length && <EmptyRow cols={10} text="После закрытых сделок здесь появятся конкретные выводы и их влияние на риск" />}
        </tbody>
      </table>
    </div>
  );
}

function ReadinessTable({ items, batch }: { items: HistoryReadiness[]; batch: HistoryBatchIngest | null }) {
  return (
    <div className="table-wrap">
      <div className="table-title">Готовность датасета</div>
      {batch && <p className="muted">Последняя пачка добавила свечей: {Object.values(batch.inserted).reduce((sum, value) => sum + value, 0)}.</p>}
      <table>
        <thead>
          <tr><th>Пара</th><th>Таймфрейм</th><th>Всего</th><th>Реальные</th><th>Синтетика</th><th>Покрытие</th><th>Статус</th><th>Последняя свеча</th></tr>
        </thead>
        <tbody>
          {items.map((item) => (
            <tr key={`${item.symbol}-${item.timeframe}`}>
              <td className="font-semibold">{item.symbol}</td>
              <td>{item.timeframe}</td>
              <td>{item.candles.toLocaleString()}</td>
              <td className="text-accent">{item.real_candles.toLocaleString()}</td>
              <td>{item.synthetic_candles.toLocaleString()}</td>
              <td>{fmt(item.coverage_percent)}%</td>
              <td><span className={`pill ${item.ready ? "buy" : ""}`}>{item.ready ? "Готово" : "Сбор"}</span></td>
              <td>{item.last_timestamp ? new Date(item.last_timestamp).toLocaleString() : "-"}</td>
            </tr>
          ))}
          {!items.length && <EmptyRow cols={8} text="Данных о готовности датасета пока нет" />}
        </tbody>
      </table>
    </div>
  );
}

function LearningRulesTable({ items, summary }: { items: LearningRule[]; summary: LearningSummary | null }) {
  return (
    <div className="table-wrap">
      <div className="table-title">Память бота об ошибках</div>
      {summary && (
        <div className="mini-grid table-summary">
          <Metric label="Правил" value={String(summary.total_rules)} />
          <Metric label="Наблюдений" value={String(summary.total_observations)} />
          <Metric label="WATCH" value={String(summary.watch_rules)} />
          <Metric label="WARN" value={String(summary.warn_rules)} tone={summary.warn_rules > 0 ? "bad" : undefined} />
          <Metric label="BLOCK" value={String(summary.block_rules)} tone={summary.block_rules > 0 ? "bad" : undefined} />
          <Metric label="W/L" value={`${summary.total_wins}/${summary.total_losses}`} />
        </div>
      )}
      <table>
        <thead>
          <tr><th>Риск</th><th>Scope</th><th>Сторона</th><th>Признак</th><th>Значение</th><th>Penalty</th><th>Уверенность</th><th>W/L</th><th>Итог</th><th>Причина</th></tr>
        </thead>
        <tbody>
          {items.map((item) => (
            <tr key={item.id}>
              <td><span className={`pill ${item.risk_level === "BLOCK" ? "sell" : item.risk_level === "WARN" ? "" : "buy"}`}>{translateRiskLevel(item.risk_level)}</span></td>
              <td>{item.scope}</td>
              <td>{translateAction(item.side)}</td>
              <td>{translateFeature(item.feature_key)}</td>
              <td>{translateFeatureValue(item.feature_value)}</td>
              <td className={item.penalty >= 2.5 ? "text-danger" : item.penalty > 0 ? "text-slate-700" : "text-accent"}>{fmt(item.penalty)}</td>
              <td>{fmt(item.confidence * 100)}%</td>
              <td>{item.wins}/{item.losses}</td>
              <td className={item.total_profit >= 0 ? "text-accent" : "text-danger"}>${fmt(item.total_profit)}</td>
              <td>{translateStatus(item.last_reason ?? "-")}</td>
            </tr>
          ))}
          {!items.length && <EmptyRow cols={10} text="Бот пока не накопил правил обучения. Они появятся после закрытых сделок." />}
        </tbody>
      </table>
    </div>
  );
}

function OptimizationTable({ items }: { items: StrategyOptimization[] }) {
  return (
    <div className="table-wrap">
      <div className="table-title">Лучшие конфиги Strategy Lab</div>
      <table>
        <thead>
          <tr><th>Пара</th><th>Проверка</th><th>Оценка</th><th>Стоп</th><th>Тейк</th><th>Трейл</th><th>Win Rate</th><th>Profit Factor</th><th>Проверка PF</th><th>Проверка PnL</th><th>Итого</th></tr>
        </thead>
        <tbody>
          {items.map((item, index) => {
            const robustness = item.parameters.robustness;
            const validationProfit = robustness?.validation_profit ?? 0;
            return (
              <tr key={`${item.symbol}-${item.score}-${index}`}>
                <td className="font-semibold">{item.symbol}</td>
                <td><span className={`pill ${robustness?.passed ? "buy" : "sell"}`} title={robustness?.reason ?? "Нет validation-проверки"}>{robustness?.passed ? "Прошел" : "Отклонен"}</span></td>
                <td>{fmt(item.score)}</td>
                <td>{fmt(item.parameters.stop_loss_percent)}%</td>
                <td>{fmt(item.parameters.take_profit_percent)}%</td>
                <td>{fmt(item.parameters.trailing_stop_percent)}%</td>
                <td>{fmt(item.win_rate)}%</td>
                <td>{fmt(item.profit_factor)}</td>
                <td>{fmt(robustness?.validation_profit_factor)}</td>
                <td className={validationProfit >= 0 ? "text-accent" : "text-danger"}>${fmt(validationProfit)}</td>
                <td className={item.total_profit >= 0 ? "text-accent" : "text-danger"}>${fmt(item.total_profit)}</td>
              </tr>
            );
          })}
          {!items.length && <EmptyRow cols={11} text="Запусти оптимизацию, чтобы получить конфиги стратегии" />}
        </tbody>
      </table>
    </div>
  );
}

function AgentActivityTable({ activity }: { activity: AgentActivity | null }) {
  const agents = activity?.agents ?? [];
  return (
    <div className="table-wrap">
      <div className="table-title">Как работают агенты</div>
      <table>
        <thead><tr><th>Агент</th><th>Статус</th><th>Рейтинг качества</th><th>Обучающих исходов</th><th>Успешность</th><th>Всего решений</th><th>24 часа</th><th>Средняя уверенность</th><th>BUY/SELL</th><th>ALLOW</th><th>BLOCK</th><th>WAIT</th><th>Последнее решение</th></tr></thead>
        <tbody>
          {agents.map((item) => (
            <tr key={item.agent_name}>
              <td className="font-semibold">{translateAgentName(item.agent_name)}</td>
              <td>{translateCompetitionStatus(item.competition_status)}</td>
              <td>{item.performance_rating == null ? "—" : `${fmt(item.performance_rating * 100)}%`}</td>
              <td>{item.performance_observations || "—"}</td>
              <td>{item.performance_win_rate == null ? "—" : `${fmt(item.performance_win_rate * 100)}%`}</td>
              <td>{item.decisions}</td>
              <td>{item.decisions_24h}</td>
              <td>{fmt(item.average_confidence * 100)}%</td>
              <td>{item.directional_votes}</td>
              <td>{item.approvals}</td>
              <td className={item.blocks > 0 ? "text-danger" : ""}>{item.blocks}</td>
              <td>{item.waits}</td>
              <td>{item.last_seen_at ? `${new Date(item.last_seen_at).toLocaleString("ru-RU")} · ${item.last_symbol} · ${translateAction(item.last_action)}` : "-"}</td>
            </tr>
          ))}
          {!agents.length && <EmptyRow cols={13} text="Агенты еще не накопили решений" />}
        </tbody>
      </table>
    </div>
  );
}

function RlModelsTable({ items }: { items: RlModel[] }) {
  return (
    <div className="table-wrap">
      <div className="table-title">RL-модели Stable Baselines3</div>
      <p className="muted table-explanation">«Активна» означает, что модель прошла validation и виртуальный forward-тест. «Тень» анализирует рынок, но не получает права влиять на входы.</p>
      <table>
        <thead>
          <tr><th>Пара</th><th>Модель</th><th>Статус</th><th>Обучение / проверка</th><th>Доходность</th><th>Profit Factor</th><th>Просадка</th><th>Сделки</th><th>Виртуальная проверка</th><th>Разбор ошибок</th><th>Источник</th><th>Причина статуса</th></tr>
        </thead>
        <tbody>
          {items.map((item) => (
            <tr key={item.id}>
              <td className="font-semibold">{item.symbol} / {item.timeframe}</td>
              <td>{item.algorithm} #{item.id}</td>
              <td>
                <span className={`pill ${item.is_active ? "buy" : item.status === "REJECTED" ? "sell" : ""}`}>
                  {translateRlModelStatus(item.status, item.is_active)}
                </span>
              </td>
              <td>{item.training_candles.toLocaleString()} / {item.validation_candles.toLocaleString()}</td>
              <td className={(item.metrics.return_percent ?? 0) >= 0 ? "text-accent" : "text-danger"}>{fmt(item.metrics.return_percent)}%</td>
              <td>{fmt(item.metrics.profit_factor)}</td>
              <td className="text-danger">{fmt(item.metrics.max_drawdown_percent)}%</td>
              <td>{item.metrics.trades ?? 0}</td>
              <td>
                {item.metrics.forward_status ? translateForwardStatus(item.metrics.forward_status) : "—"}
                {item.metrics.forward && <div className="muted">{item.metrics.forward.closed_trades ?? 0} сделок · PF {fmt(item.metrics.forward.profit_factor)} · ${fmt(item.metrics.forward.total_pnl)}</div>}
              </td>
              <td>{item.metrics.bad_experiences_seen ?? 0} примеров · {item.metrics.replay_weighted_candles ?? 0} свечей · {item.metrics.curriculum_stages?.length ?? 0} этапа</td>
              <td>{item.metrics.market_data_source === "ccxt" ? "Реальный рынок" : item.metrics.market_data_source ?? "-"}</td>
              <td>{item.metrics.promotion_reason ? translatePromotionReason(item.metrics.promotion_reason) : "—"}</td>
            </tr>
          ))}
          {!items.length && <EmptyRow cols={12} text="RL-моделей пока нет. Тренер ожидает достаточную историю реальных свечей." />}
        </tbody>
      </table>
    </div>
  );
}

function OrdersTable({ orders, onChanged }: { orders: Order[]; onChanged: () => Promise<void> }) {
  async function reconcile() {
    await api.post("/orders/reconcile");
    await onChanged();
  }
  return (
    <div className="table-wrap">
      <div className="table-title table-title-row">
        <span>Аудит исполнения</span>
        <button className="btn compact" onClick={reconcile}>Сверить</button>
      </div>
      <table>
        <thead>
          <tr><th>Время</th><th>Пара</th><th>Сторона</th><th>Статус</th><th>Исполнено</th><th>Средняя цена</th><th>Комиссия</th><th>Проскальзывание</th></tr>
        </thead>
        <tbody>
          {orders.map((order) => (
            <tr key={order.id}>
              <td>{new Date(order.created_at).toLocaleString()}</td>
              <td className="font-semibold">{order.symbol}</td>
              <td><span className={`pill ${order.side === "buy" ? "buy" : "sell"}`}>{translateAction(order.side)}</span></td>
              <td>{translateStatus(order.status)}</td>
              <td>{fmt(order.filled_amount)}</td>
              <td>${fmt(order.average_price ?? 0)}</td>
              <td>${fmt(order.fee)}</td>
              <td>${fmt(order.slippage)}</td>
            </tr>
          ))}
          {!orders.length && <EmptyRow cols={8} text="Ордеров пока нет" />}
        </tbody>
      </table>
    </div>
  );
}

function PositionsTable(props: { data: Dashboard | null; onChanged: () => Promise<void> }) {
  const positions = props.data?.active_positions ?? [];
  return (
    <div className="table-wrap">
      <div className="table-title">Активные позиции</div>
      <table>
        <thead>
          <tr><th>Монета</th><th>Сторона</th><th>Вход</th><th>Стоп</th><th>Тейк</th><th>PnL</th><th></th></tr>
        </thead>
        <tbody>
          {positions.map((position) => (
            <tr key={position.id}>
              <td className="font-semibold">{position.symbol}</td>
              <td><span className={`pill ${position.side === "LONG" ? "buy" : "sell"}`}>{translateAction(position.side)}</span></td>
              <td>${fmt(position.entry_price)}</td>
              <td>${fmt(position.stop)}</td>
              <td>${fmt(position.take)}</td>
              <td className={position.pnl >= 0 ? "text-accent" : "text-danger"}>${fmt(position.pnl)}</td>
              <td><button className="btn compact" onClick={async () => { await api.post(`/positions/${position.id}/close`); await props.onChanged(); }}>Закрыть</button></td>
            </tr>
          ))}
          {!positions.length && <EmptyRow cols={7} text="Активных позиций пока нет" />}
        </tbody>
      </table>
    </div>
  );
}

function DecisionList({ run }: { run: TradingRun }) {
  return (
    <div className="decision-list">
      {run.decisions.map((item) => (
        <div className="decision" key={item.symbol}>
          <span className={`pill ${item.signal === "BUY" ? "buy" : item.signal === "SELL" ? "sell" : ""}`}>{translateAction(item.signal)}</span>
          <strong>{item.symbol}</strong>
          <span>оценка {item.score}</span>
          <span className={item.action === "OPENED" ? "text-accent" : "text-slate-500"}>{translateAction(item.action)}</span>
          <span className="truncate">{item.reason}</span>
        </div>
      ))}
    </div>
  );
}

function MarketView() {
  const [coins, setCoins] = React.useState<MarketCoin[]>([]);
  const [error, setError] = React.useState("");
  const load = React.useCallback(async () => {
    try {
      setError("");
      setCoins((await api.get("/market/scan")).data);
    } catch (err) {
      setError(readError(err));
    }
  }, []);
  React.useEffect(() => void load(), [load]);
  return (
    <section className="space-y-5">
      <Header title="Сканер рынка" subtitle="Рейтинг, тренд и снимок индикаторов">
        <button className="btn" onClick={load}><RefreshCw size={16} /> Обновить</button>
      </Header>
      {error && <Alert tone="danger" text={error} />}
      <div className="table-wrap">
        <table>
          <thead>
            <tr><th>Монета</th><th>Цена</th><th>Объем 24ч</th><th>Изм.</th><th>RSI</th><th>Тренд</th><th>Режим</th><th>Рейтинг</th></tr>
          </thead>
          <tbody>
            {coins.map((coin) => (
              <tr key={coin.symbol}>
                <td className="font-semibold">{coin.symbol}</td>
                <td>${fmt(coin.price)}</td>
                <td>${fmt(coin.volume_24h)}</td>
                <td className={coin.price_change_percent >= 0 ? "text-accent" : "text-danger"}>{fmt(coin.price_change_percent)}%</td>
                <td>{fmt(coin.rsi)}</td>
                <td><span className={`pill ${coin.ema50 > coin.ema200 ? "buy" : "sell"}`}>{coin.ema50 > coin.ema200 ? "Бычий" : "Медвежий"}</span></td>
                <td><span className={`pill ${coin.regime === "TRENDING_UP" ? "buy" : coin.regime === "TRENDING_DOWN" || coin.regime === "HIGH_VOLATILITY" || coin.regime === "LOW_LIQUIDITY" ? "sell" : ""}`} title={coin.regime_reason}>{translateRegime(coin.regime)}</span></td>
                <td><span className="score">{coin.rating}</span></td>
              </tr>
            ))}
            {!coins.length && <EmptyRow cols={8} text="Рыночные данные пока не загружены" />}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function LogsView() {
  const [logs, setLogs] = React.useState<LogEntry[]>([]);
  const [error, setError] = React.useState("");
  const [exporting, setExporting] = React.useState(false);
  const [filter, setFilter] = React.useState<"all" | "trading" | "attention">("trading");
  const load = React.useCallback(async () => {
    try {
      setError("");
      setLogs((await api.get("/logs")).data);
    } catch (err) {
      setError(readError(err));
    }
  }, []);
  React.useEffect(() => void load(), [load]);
  async function downloadTradingAudit() {
    try {
      setExporting(true);
      setError("");
      const response = await api.get<Blob>("/logs/trading-audit", { responseType: "blob" });
      const disposition = String(response.headers["content-disposition"] ?? "");
      const filename = disposition.match(/filename="?([^";]+)"?/i)?.[1] ?? "crybothunter-trading-audit.zip";
      const url = URL.createObjectURL(response.data);
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
    } catch (err) {
      setError(readError(err));
    } finally {
      setExporting(false);
    }
  }
  const visibleLogs = logs.filter((log) => {
    const hasTradingEvent = typeof log.context?.event === "string";
    if (filter === "trading") return hasTradingEvent;
    if (filter === "attention") return ["WARNING", "ERROR"].includes(log.level.toUpperCase());
    return true;
  });
  return (
    <section className="space-y-5">
      <Header title="Логи" subtitle="Причины входов и отказов, контроль риска и жизненный цикл сделок">
        <button className="btn primary" onClick={downloadTradingAudit} disabled={exporting}>
          <Download size={16} /> {exporting ? "Готовим архив" : "Выгрузить аудит сделок"}
        </button>
        <button className="btn" onClick={load}><RefreshCw size={16} /> Обновить</button>
      </Header>
      {error && <Alert tone="danger" text={error} />}
      <Alert tone="good" text="Журнал объясняет, что произошло, почему система приняла решение и какие параметры риска были использованы. Технический исходник остаётся доступен для аудита, но не заменяет понятное описание." />
      <div className="log-filter" role="group" aria-label="Фильтр журнала">
        <button type="button" className={`log-filter-button ${filter === "trading" ? "active" : ""}`} onClick={() => setFilter("trading")}>Сделки и защита</button>
        <button type="button" className={`log-filter-button ${filter === "attention" ? "active" : ""}`} onClick={() => setFilter("attention")}>Требует внимания</button>
        <button type="button" className={`log-filter-button ${filter === "all" ? "active" : ""}`} onClick={() => setFilter("all")}>Все записи</button>
        <span className="muted">Показано: {visibleLogs.length} из {logs.length}</span>
      </div>
      <div className="table-wrap">
        <table>
          <thead><tr><th>Время</th><th>Уровень</th><th>Сообщение</th></tr></thead>
          <tbody>
            {visibleLogs.map((log) => {
              const presentation = describeLog(log);
              const summary = summarizeLogContext(log.context ?? {});
              return <tr key={log.id}>
                <td>{formatDateTime(log.created_at)}</td>
                <td><span className={`pill ${log.level.toUpperCase() === "ERROR" ? "sell" : log.level.toUpperCase() === "WARNING" ? "" : "buy"}`}>{translateLogLevel(log.level)}</span></td>
                <td>
                  <article className="log-card">
                    <div className="log-card-heading">
                      <strong>{presentation.title}</strong>
                      {presentation.event && <code>{presentation.event}</code>}
                    </div>
                    <p>{presentation.explanation}</p>
                  </article>
                  {summary.length > 0 && <div className="log-context-summary">{summary.join(" · ")}</div>}
                  <AgentLogDetails context={log.context ?? {}} />
                  <details className="log-context-details">
                      <summary>{Object.keys(log.context).length > 0 ? "Понятные параметры записи" : "Оригинальное техническое сообщение"}</summary>
                      <div className="log-original-message"><span>Исходное сообщение:</span> {log.message}</div>
                      {Object.keys(log.context).length > 0 && <>
                      <div className="log-context-grid">
                        {Object.entries(log.context).map(([key, value]) => isLogScalar(value) ? (
                          <div key={key}><span>{logFieldLabel(key)}</span><strong>{formatLogValue(key, value)}</strong></div>
                        ) : null)}
                      </div>
                      <details className="log-technical-details">
                        <summary>Технические поля для аудита</summary>
                        <pre>{JSON.stringify(log.context, null, 2)}</pre>
                      </details>
                      </>}
                  </details>
                </td>
              </tr>;
            })}
            {!visibleLogs.length && <EmptyRow cols={3} text={logs.length ? "Нет записей в выбранном фильтре" : "Логов пока нет"} />}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function describeLog(log: LogEntry) {
  const context = log.context ?? {};
  const event = typeof context.event === "string" ? context.event : "";
  const gate = typeof context.gate === "string" ? ` Проверка: ${translateLogGate(context.gate)}.` : "";
  const reason = typeof context.reason === "string" ? ` Причина: ${translateLogReason(context.reason)}.` : "";
  const labels: Record<string, { title: string; explanation: string }> = {
    ENTRY_CYCLE_BLOCKED: { title: "Новые входы временно остановлены", explanation: "Глобальная защита остановила текущий цикл, чтобы не увеличивать риск после слабых результатов." },
    ENTRY_OPENED: { title: "Позиция открыта", explanation: "Все обязательные проверки входа пройдены. В параметрах ниже сохранены объём, риск, SL, TP и рыночные подтверждения." },
    ENTRY_REJECTED: { title: "Ордер на вход не исполнен", explanation: "Сигнал был найден, но биржа не подтвердила исполнение безопасного объёма. Позиция не считается открытой." },
    ENTRY_SKIPPED: { title: "Вход пропущен защитой", explanation: "Бот сознательно не открыл позицию: хотя бы один обязательный фильтр не дал разрешение." },
    COMMITTEE_DECISION: { title: "Решение комитета агентов", explanation: "Независимые оценки направления и риска были объединены перед разрешением или блокировкой входа." },
    POSITION_PARTIALLY_CLOSED: { title: "Часть позиции зафиксирована", explanation: "Бот частично зафиксировал результат, оставив остаток позиции под защитой стоп-лосса." },
    POSITION_EXIT_PARTIALLY_FILLED: { title: "Выход исполнен частично", explanation: "Биржа исполнила только часть заявки на закрытие. Остаток позиции остаётся под управлением и контролем риска." },
    POSITION_CLOSED: { title: "Позиция закрыта", explanation: "Итоговый выход записан вместе с причиной, ценой, комиссией и фактическим PnL." },
    POSITION_CLOSE_FAILED: { title: "Закрытие позиции не подтверждено", explanation: "Биржа не сообщила корректное исполнение закрывающего ордера. Позиция не помечается закрытой без факта исполнения." },
    POSITION_MANAGEMENT_PAUSED: { title: "Управление позицией временно приостановлено", explanation: "Есть незавершённый ордер либо недоступны необходимые данные. Бот не отправляет конфликтующие распоряжения." },
    POSITION_PRICE_UNAVAILABLE: { title: "Нет безопасной цены для контроля позиции", explanation: "Проверка SL/TP остановлена для этого цикла, потому что текущая цена не была получена надёжно." },
    POSITION_TICKER_SNAPSHOT_FAILED: { title: "Не получен тикер позиции", explanation: "Снимок текущей цены не получен. Повтор будет выполнен в следующем цикле без изменения позиции." },
    POSITION_MARKET_SNAPSHOT_FAILED: { title: "Не получен рыночный снимок", explanation: "Дополнительные данные рынка недоступны. Бот сохраняет ошибку и не выдаёт их за нормальные данные." },
    BREAKEVEN_APPLIED: { title: "Стоп перенесён в безубыток", explanation: "После подтверждённого движения позиции защита перенесена к цене безубытка с учётом сохранённого правила." },
    BREAKEVEN_STOP_CONFIRMATION_PENDING: { title: "Подтверждение безубыточного стопа ожидается", explanation: "TP1 уже исполнен, но биржа ещё не подтвердила новый стоп. Локальный мониторинг контролирует возврат к цене защиты." },
    DYNAMIC_TAKE_PROFIT_EXTENDED: { title: "Целевой TP продлён", explanation: "Импульс сохраняется, поэтому часть позиции удерживается по динамическому плану, а защитный стоп зафиксирован." },
    DYNAMIC_TAKE_PROFIT_FAILED: { title: "Динамическая фиксация не исполнена", explanation: "Биржа не подтвердила операцию динамического TP. Позиция остаётся под существующей защитой до следующей проверки." },
    PARTIAL_TAKE_PROFIT_FAILED: { title: "Частичный TP не исполнен", explanation: "Фиксация части позиции не подтверждена биржей. Объём позиции не считается уменьшенным без фактического исполнения." },
    SECOND_TAKE_PROFIT_FILLED: { title: "Второй уровень прибыли исполнен", explanation: "Очередная часть позиции зафиксирована по плану каскадного выхода." },
    SECOND_TAKE_PROFIT_FAILED: { title: "Второй уровень прибыли не исполнен", explanation: "Биржа не подтвердила TP2. Остаток позиции продолжает контролироваться текущим стопом." },
    EMERGENCY_DRAWDOWN: { title: "Сработала аварийная защита просадки", explanation: "Новый риск остановлен, потому что достигнут установленный предел просадки." },
    SCALE_OUT_CANCELLED_MIN_NOTIONAL: { title: "Каскадный TP заменён единым выходом", explanation: "Частичные заявки были бы ниже минимального объёма биржи. Бот не отправил ошибочные ордера." },
    PROTECTIVE_STOP_CONFIRMED: { title: "Защитный стоп подтверждён биржей", explanation: "Для позиции создан или обновлён нативный защитный стоп. Его параметры сохранены в журнале." },
    PROTECTIVE_STOP_UNCONFIRMED: { title: "Защитный стоп не подтверждён", explanation: "Биржа не подтвердила защитный ордер. Бот включает локальный мониторинг и отмечает состояние как требующее внимания." },
    PROTECTIVE_STOP_CANCELLED: { title: "Предыдущий защитный стоп отменён", explanation: "Старый стоп отменён перед установкой обновлённой защиты, чтобы не осталось конфликтующих ордеров." },
    STOP_SLIPPAGE_RECORDED: { title: "Зафиксировано проскальзывание стопа", explanation: "Фактическая цена стоп-исполнения отличается от ожидаемой. Значение сохранено для анализа и обучения." },
    POST_MORTEM_CREATED: { title: "Создан разбор убыточной сделки", explanation: "Путь цены, MFE/MAE, исполнение и поведение стратегии сохранены для последующего анализа." },
    POST_MORTEM_FAILED: { title: "Разбор сделки не создан", explanation: "Закрытие позиции не отменяется: не удалось сохранить дополнительный аналитический разбор." },
    LEARNING_UPDATED: { title: "Результат учтён в обучении", explanation: "Факт закрытой сделки добавлен в память стратегии; это не означает автоматического изменения правил без проверок." },
    AGENT_LEARNING_UPDATED: { title: "Аналитики обучились на результате", explanation: "Прогноз каждого основного и теневого аналитика сопоставлен с фактическим PnL. Рейтинги обновлены плавно; при достаточной выборке лучший претендент может заменить основного аналитика." },
  };
  const item = labels[event];
  if (item) return { event, title: item.title, explanation: `${item.explanation}${gate}${reason}` };
  if (event) return { event, title: "Системное торговое событие", explanation: `Бот сохранил структурированную запись для контроля и аудита.${gate}${reason}` };
  return { event: "", title: "Техническая запись системы", explanation: "Это служебное сообщение без торгового кода события. Оригинальный текст и поля доступны в деталях." };
}

function AgentLogDetails({ context }: { context: Record<string, unknown> }) {
  const steps = Array.isArray(context.agent_steps) ? context.agent_steps : [];
  const results = Array.isArray(context.agent_results) ? context.agent_results : [];
  if (!steps.length && !results.length) return null;
  return (
    <div className="log-context-summary">
      {steps.map((raw, index) => {
        const item = raw && typeof raw === "object" ? raw as Record<string, unknown> : {};
        return <div key={`step-${index}`}>
          {translateAgentName(String(item["агент"] ?? "Агент"))}: {translateAction(String(item["действие"] ?? "—"))}, уверенность {fmt(Number(item["уверенность"] ?? 0) * 100)}%. {String(item["объяснение"] ?? "")}
        </div>;
      })}
      {results.map((raw, index) => {
        const item = raw && typeof raw === "object" ? raw as Record<string, unknown> : {};
        return <div key={`result-${index}`}>
          {translateAgentName(String(item.agent ?? "Агент"))}: прогноз {item.success ? "подтверждён" : "не подтвердился"}, новый рейтинг {fmt(Number(item.rating ?? 0) * 100)}%, наблюдений {String(item.observations ?? 0)}.
        </div>;
      })}
    </div>
  );
}

function isLogScalar(value: unknown): value is string | number | boolean {
  return typeof value === "string" || typeof value === "number" || typeof value === "boolean";
}

function summarizeLogContext(context: Record<string, unknown>) {
  const keys = ["symbol", "side", "signal", "gate", "position_id", "pnl", "entry_price", "exit_price", "stop", "take", "risk_percent", "order_status", "exit_reason"];
  return keys.flatMap((key) => isLogScalar(context[key]) ? [`${logFieldLabel(key)}: ${formatLogValue(key, context[key])}`] : []);
}

function logFieldLabel(key: string) {
  const labels: Record<string, string> = {
    event: "Код события", gate: "Проверка", symbol: "Пара", side: "Направление", signal: "Сигнал", lane: "Режим", score: "Оценка", rating: "Рейтинг", reason: "Причина", explanation: "Подробное объяснение", pnl: "PnL, USDT", position_id: "Позиция", order_id: "Ордер", order_status: "Статус ордера", execution_status: "Статус исполнения", requested_volume: "Запрошенный объём", filled_volume: "Исполненный объём", remaining_volume: "Остаток", entry_price: "Цена входа", exit_price: "Цена выхода", stop: "Stop Loss", take: "Take Profit", locked_stop: "Защитный SL", risk_percent: "Риск на сделку, %", daily_pnl: "PnL за день, USDT", daily_risk_limit: "Дневной лимит риска, USDT", reserved_stop_risk: "Риск открытых SL, USDT", candidate_stop_risk: "Риск новой сделки, USDT", exit_reason: "Причина выхода", exit_fee: "Комиссия выхода", partial_profit: "PnL части, USDT", previous_take: "Предыдущий TP", next_take: "Новый TP", extension_distance: "Шаг по ATR", win_rate: "Win Rate, %", total_profit: "Суммарный PnL, USDT", trades_checked: "Проверено сделок", evaluated_agents: "Оценено аналитиков"
  };
  return labels[key] ?? key.replace(/_/g, " ");
}

function formatLogValue(key: string, value: string | number | boolean) {
  if (typeof value === "boolean") return value ? "Да" : "Нет";
  if (typeof value === "number") return ["pnl", "daily_pnl", "daily_risk_limit", "reserved_stop_risk", "candidate_stop_risk", "partial_profit", "exit_fee"].includes(key) ? `$${fmt(value)}` : fmt(value);
  if (["side", "signal"].includes(key)) return translateAction(value);
  if (["order_status", "execution_status", "exit_reason"].includes(key)) return translateStatus(value);
  if (key === "gate") return translateLogGate(value);
  if (key === "reason") return translateLogReason(value);
  return value;
}

function translateLogLevel(value: string) {
  return ({ INFO: "Инфо", WARNING: "Внимание", ERROR: "Ошибка" } as Record<string, string>)[value.toUpperCase()] ?? value;
}

function translateLogGate(value: string) {
  const labels: Record<string, string> = {
    PERFORMANCE_GUARD: "общая защита результатов", SYMBOL_GUARD: "защита торговой пары", DAILY_RISK_BUDGET: "дневной риск-лимит", PRETRADE_QUALITY: "проверка качества стратегии", MICROSTRUCTURE: "стакан и поток сделок", EXECUTION: "исполнение на бирже", MARKET_QUALITY: "ликвидность и качество рынка", COMMITTEE: "комитет агентов", PAPER_LEARNING: "учебный paper-режим", LEARNING_MEMORY: "память прошлых сделок", RL_GATE: "RL-проверка", COOLDOWN: "пауза после результата", DIRECTIONAL_EXPOSURE: "концентрация по направлению", EXPOSURE: "лимит экспозиции", MAX_POSITIONS: "лимит открытых позиций", POSITION_ALREADY_OPEN: "позиция по паре уже открыта", VOLUME_CONFIRMATION: "подтверждение объёмом", PRICE_EXTENSION: "слишком растянутый вход", MARKET_REGIME: "режим рынка", VOLATILITY: "волатильность", STRATEGY_WAIT: "стратегия не подтвердила вход", RISK_MANAGER: "менеджер риска", PRICE_DATA: "надёжность цены", ENTRY_RULES: "правила входа"
  };
  return labels[value] ?? value.replace(/_/g, " ");
}

function translateLogReason(value: string) {
  if (/[А-Яа-яЁё]/.test(value)) return value;
  const normalized = value.toLowerCase();
  const matches: Array<[string, string]> = [
    ["performance guard", "глобальная защита ограничила новые входы"],
    ["symbol performance guard", "защита этой пары включила паузу или снижение риска"],
    ["strategy wait", "стратегия не получила достаточного подтверждения направления"],
    ["micro gate", "стакан и поток сделок не подтвердили вход"],
    ["market quality", "ликвидность, спред или качество рынка не соответствуют правилам"],
    ["pre-trade quality", "историческая walk-forward проверка не подтвердила качество"],
    ["rl", "RL-модель не дала достаточного подтверждения"],
    ["committee", "комитет агентов не набрал необходимый консенсус"],
    ["cooldown", "действует обязательная пауза после результата"],
    ["maximum open positions", "достигнут лимит одновременных позиций"],
    ["position already open", "по этой паре уже есть открытая позиция"],
    ["daily risk", "превышался бы дневной риск-лимит"],
    ["exposure", "превышался бы лимит концентрации капитала"],
    ["volume", "рассчитанный объём меньше допустимого или не подтверждён"],
    ["not filled", "биржа не подтвердила исполнение"],
  ];
  return matches.find(([needle]) => normalized.includes(needle))?.[1] ?? "Техническая причина сохранена в полях аудита";
}

function SettingsView() {
  const [settings, setSettings] = React.useState({
    exchange: "binance",
    api_key: "",
    secret_key: "",
    passphrase: "",
    api_key_masked: null as string | null,
    secret_key_masked: null as string | null,
    passphrase_masked: null as string | null,
    risk_percent: 1,
    daily_risk_percent: 3,
    max_positions: 3,
    min_rating: 80,
    scan_interval: "5m",
    stop_loss_percent: 1.5,
    take_profit_percent: 3,
    trailing_stop_percent: 0.8,
    atr_stop_multiplier: 1.5,
    risk_reward_ratio: 2,
    breakeven_trigger_r: 1,
    breakeven_offset_percent: 0.05,
    partial_take_profit_r: 1,
    partial_close_percent: 50
  });
  const [message, setMessage] = React.useState<ActionMessage | null>(null);
  const [error, setError] = React.useState("");
  const [saving, setSaving] = React.useState(false);

  React.useEffect(() => {
    void api.get<UserSettings>("/settings").then(({ data }) => {
      setSettings((current) => ({
        ...current,
        exchange: data.exchange,
        api_key_masked: data.api_key_masked ?? null,
        secret_key_masked: data.secret_key_masked ?? null,
        passphrase_masked: data.passphrase_masked ?? null,
        risk_percent: data.risk_percent,
        daily_risk_percent: data.daily_risk_percent,
        max_positions: data.max_positions,
        min_rating: data.min_rating,
        scan_interval: data.scan_interval,
        stop_loss_percent: data.stop_loss_percent,
        take_profit_percent: data.take_profit_percent,
        trailing_stop_percent: data.trailing_stop_percent,
        atr_stop_multiplier: data.atr_stop_multiplier,
        risk_reward_ratio: data.risk_reward_ratio,
        breakeven_trigger_r: data.breakeven_trigger_r,
        breakeven_offset_percent: data.breakeven_offset_percent,
        partial_take_profit_r: data.partial_take_profit_r,
        partial_close_percent: data.partial_close_percent
      }));
    }).catch((err) => setError(readError(err)));
  }, []);

  function update<K extends keyof typeof settings>(key: K, value: (typeof settings)[K]) {
    setSettings((current) => ({ ...current, [key]: value }));
  }

  async function save() {
    if (saving) {
      return;
    }

    try {
      setError("");
      setMessage(null);
      setSaving(true);
      const { api_key_masked, secret_key_masked, passphrase_masked, ...payload } = settings;
      void api_key_masked;
      void secret_key_masked;
      void passphrase_masked;
      const { data } = await api.put<UserSettings>("/settings", {
        ...payload,
        api_key: payload.api_key.trim() || undefined,
        secret_key: payload.secret_key.trim() || undefined,
        passphrase: payload.passphrase.trim() || undefined
      });
      setSettings((current) => ({
        ...current,
        exchange: data.exchange,
        api_key: "",
        secret_key: "",
        passphrase: "",
        api_key_masked: data.api_key_masked ?? current.api_key_masked,
        secret_key_masked: data.secret_key_masked ?? current.secret_key_masked,
        passphrase_masked: data.passphrase_masked ?? current.passphrase_masked,
        risk_percent: data.risk_percent,
        daily_risk_percent: data.daily_risk_percent,
        max_positions: data.max_positions,
        min_rating: data.min_rating,
        scan_interval: data.scan_interval,
        stop_loss_percent: data.stop_loss_percent,
        take_profit_percent: data.take_profit_percent,
        trailing_stop_percent: data.trailing_stop_percent,
        atr_stop_multiplier: data.atr_stop_multiplier,
        risk_reward_ratio: data.risk_reward_ratio,
        breakeven_trigger_r: data.breakeven_trigger_r,
        breakeven_offset_percent: data.breakeven_offset_percent,
        partial_take_profit_r: data.partial_take_profit_r,
        partial_close_percent: data.partial_close_percent
      }));
      setMessage({
        ok: true,
        message: data.api_key_masked && data.secret_key_masked
          ? `Ключи сохранены: ${data.api_key_masked}`
          : "Настройки сохранены. Ключи не менялись."
      });
    } catch (err) {
      setError(readError(err));
    } finally {
      setSaving(false);
    }
  }

  async function testTelegram() {
    try {
      setError("");
      setMessage((await api.post<ActionMessage>("/settings/telegram/test")).data);
    } catch (err) {
      setError(readError(err));
    }
  }

  async function testExchange() {
    try {
      setError("");
      setMessage(null);
      setSaving(true);
      setMessage((await api.post<ActionMessage>("/settings/exchange/test")).data);
    } catch (err) {
      setError(readError(err));
    } finally {
      setSaving(false);
    }
  }

  return (
    <section className="space-y-5">
      <Header title="Настройки" subtitle="Ключи биржи, риск-модель и проверка Telegram">
        <button className="btn primary" onClick={save} disabled={saving}>
          <Save size={16} /> {saving ? "Сохраняю..." : "Сохранить"}
        </button>
      </Header>
      {error && <Alert tone="danger" text={error} />}
      {message && <Alert tone={message.ok ? "good" : "danger"} text={message.message} />}
      <div className="settings-grid">
        <div className="panel-block">
          <div className="table-title">Подключение биржи</div>
          <p className="muted">
            Здесь подключается реальная биржа. Создай API key на выбранной бирже с доступом к торговле, вставь API Key и Secret Key, затем нажми «Сохранить».
          </p>
          <Alert
            tone={settings.api_key_masked && settings.secret_key_masked ? "good" : "danger"}
            text={settings.api_key_masked && settings.secret_key_masked ? `${exchangeLabel(settings.exchange)} подключена: ${settings.api_key_masked}` : `${exchangeLabel(settings.exchange)} еще не подключена: добавь API Key и Secret Key`}
          />
          <label className="field">Биржа<select value={settings.exchange} onChange={(event) => update("exchange", event.target.value)}><option value="binance">Binance</option><option value="bybit">Bybit</option><option value="okx">OKX</option><option value="kucoin">KuCoin</option><option value="gateio">Gate.io</option></select></label>
          <label className="field">API Key<input type="password" value={settings.api_key} onChange={(event) => update("api_key", event.target.value)} placeholder={settings.api_key_masked ?? `Вставь API Key из ${exchangeLabel(settings.exchange)}`} /></label>
          <label className="field">Secret Key<input type="password" value={settings.secret_key} onChange={(event) => update("secret_key", event.target.value)} placeholder={settings.secret_key_masked ?? `Вставь Secret Key из ${exchangeLabel(settings.exchange)}`} /></label>
          <label className="field">Passphrase<input type="password" value={settings.passphrase} onChange={(event) => update("passphrase", event.target.value)} placeholder={settings.passphrase_masked ?? "Нужна для OKX/KuCoin, для Binance/Gate.io обычно не нужна"} /></label>
          <button className="btn" onClick={testExchange} disabled={saving}><ShieldCheck size={16} /> Проверить биржу</button>
        </div>
        <div className="panel-block">
          <div className="table-title">Контроль риска</div>
          <label className="field">Риск на сделку<input type="number" value={settings.risk_percent} onChange={(event) => update("risk_percent", Number(event.target.value))} /></label>
          <label className="field">Дневной риск<input type="number" value={settings.daily_risk_percent} onChange={(event) => update("daily_risk_percent", Number(event.target.value))} /></label>
          <label className="field">Макс. позиций<input type="number" value={settings.max_positions} onChange={(event) => update("max_positions", Number(event.target.value))} /></label>
          <label className="field">Мин. рейтинг<input type="number" value={settings.min_rating} onChange={(event) => update("min_rating", Number(event.target.value))} /></label>
          <label className="field">Стоп-лосс<input type="number" value={settings.stop_loss_percent} onChange={(event) => update("stop_loss_percent", Number(event.target.value))} /></label>
          <label className="field">Тейк-профит<input type="number" value={settings.take_profit_percent} onChange={(event) => update("take_profit_percent", Number(event.target.value))} /></label>
          <label className="field">Трейлинг-стоп<input type="number" value={settings.trailing_stop_percent} onChange={(event) => update("trailing_stop_percent", Number(event.target.value))} /></label>
          <label className="field">ATR множитель стопа<input type="number" value={settings.atr_stop_multiplier} onChange={(event) => update("atr_stop_multiplier", Number(event.target.value))} /></label>
          <label className="field">Risk/Reward<input type="number" value={settings.risk_reward_ratio} onChange={(event) => update("risk_reward_ratio", Number(event.target.value))} /></label>
          <label className="field">Триггер безубытка R<input type="number" value={settings.breakeven_trigger_r} onChange={(event) => update("breakeven_trigger_r", Number(event.target.value))} /></label>
          <label className="field">Отступ безубытка<input type="number" value={settings.breakeven_offset_percent} onChange={(event) => update("breakeven_offset_percent", Number(event.target.value))} /></label>
          <label className="field">Частичный тейк R<input type="number" value={settings.partial_take_profit_r} onChange={(event) => update("partial_take_profit_r", Number(event.target.value))} /></label>
          <label className="field">TP1: частичное закрытие % (10–30)<input type="number" min="10" max="30" value={settings.partial_close_percent} onChange={(event) => update("partial_close_percent", Number(event.target.value))} /></label>
          <label className="field">Интервал скана<select value={settings.scan_interval} onChange={(event) => update("scan_interval", event.target.value)}><option value="1m">1m</option><option value="5m">5m</option><option value="15m">15m</option><option value="1h">1h</option></select></label>
        </div>
        <div className="panel-block">
          <div className="table-title">Telegram</div>
          <p className="muted">Токен и разрешенные chat ID настраиваются в переменных Railway. Проверь после деплоя telegram-worker.</p>
          <button className="btn" onClick={testTelegram}><ShieldCheck size={16} /> Отправить тест</button>
        </div>
      </div>
    </section>
  );
}

function Header(props: { title: string; subtitle: string; children?: React.ReactNode }) {
  return (
    <div className="header">
      <div>
        <h2>{props.title}</h2>
        <p>{props.subtitle}</p>
      </div>
      <div className="action-row">{props.children}</div>
    </div>
  );
}

function Metric(props: { label: string; value: string; tone?: "good" | "bad" }) {
  return <div className={`metric ${props.tone ?? ""}`}><span>{props.label}</span><strong>{props.value}</strong></div>;
}

function StatusItem(props: { label: string; value: string; good?: boolean }) {
  const icon = props.good === false ? <XCircle size={16} /> : <CheckCircle2 size={16} />;
  return <div className="status-item">{icon}<span>{props.label}</span><strong>{props.value}</strong></div>;
}

function Alert(props: { tone: "good" | "danger"; text: string }) {
  return <div className={`alert ${props.tone}`}>{props.text}</div>;
}

function EmptyRow(props: { cols: number; text: string }) {
  return <tr><td colSpan={props.cols} className="empty">{props.text}</td></tr>;
}

function fmt(value: number | undefined) {
  return Number(value ?? 0).toLocaleString("ru-RU", { maximumFractionDigits: 2 });
}

function fmtSignedUsd(value: number | null | undefined) {
  const number = Number(value ?? 0);
  return `${number >= 0 ? "+" : "−"}$${fmt(Math.abs(number))}`;
}

function fmtSignedPercent(value: number | null | undefined) {
  const number = Number(value ?? 0);
  return `${number >= 0 ? "+" : "−"}${fmt(Math.abs(number))}%`;
}

const TRADE_CHART_TIMEFRAMES = [
  { value: "1h", label: "1 час" },
  { value: "4h", label: "4 часа" },
  { value: "12h", label: "12 часов" },
  { value: "1d", label: "24 часа" }
] as const;
const OPEN_TRADE_CHART_REFRESH_MS = 5_000;

function TradeChartsPanel() {
  const [positions, setPositions] = React.useState<Position[]>([]);
  const [selectedId, setSelectedId] = React.useState<number | null>(null);
  const [timeframe, setTimeframe] = React.useState("1h");
  const [chart, setChart] = React.useState<TradeChart | null>(null);
  const [error, setError] = React.useState("");
  const [loading, setLoading] = React.useState(false);
  const liveRefreshInFlight = React.useRef(false);

  const loadPositions = React.useCallback(async () => {
    try {
      const { data } = await api.get<Position[]>("/positions?limit=80");
      setPositions(data);
      setSelectedId((current) => current && data.some((position) => position.id === current) ? current : data[0]?.id ?? null);
    } catch (err) {
      setError(readError(err));
    }
  }, []);

  React.useEffect(() => void loadPositions(), [loadPositions]);

  const loadChart = React.useCallback(async (quiet = false) => {
    if (!selectedId) {
      setChart(null);
      return;
    }
    try {
      if (!quiet) {
        setLoading(true);
        setError("");
      }
      const { data } = await api.get<TradeChart>(`/positions/${selectedId}/chart?timeframe=${timeframe}`);
      setChart(data);
    } catch (err) {
      if (!quiet) {
        setChart(null);
        setError(readError(err));
      }
    } finally {
      if (!quiet) setLoading(false);
    }
  }, [selectedId, timeframe]);

  const ingestSelectedHistory = React.useCallback(async () => {
    const position = positions.find((item) => item.id === selectedId);
    if (!position) return;
    try {
      setLoading(true);
      setError("");
      await api.post(`/market/history/ingest?symbol=${encodeURIComponent(position.symbol)}&timeframe=${timeframe}&limit=1000`);
      await loadChart();
    } catch (err) {
      setError(readError(err));
    } finally {
      setLoading(false);
    }
  }, [loadChart, positions, selectedId, timeframe]);

  React.useEffect(() => void loadChart(), [loadChart]);
  const selected = positions.find((position) => position.id === selectedId);

  React.useEffect(() => {
    if (selected?.status !== "OPEN") return;
    // Only the selected open position is polled.  The API merges factual
    // history with the latest public-exchange candle, so this is live market
    // data rather than a redrawing of the local candle cache.
    const timer = window.setInterval(() => {
      if (liveRefreshInFlight.current) return;
      liveRefreshInFlight.current = true;
      void Promise.all([loadPositions(), loadChart(true)]).finally(() => {
        liveRefreshInFlight.current = false;
      });
    }, OPEN_TRADE_CHART_REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [loadChart, loadPositions, selected?.id, selected?.status]);

  return (
    <div className="panel-block trade-chart-panel">
      <div className="trade-chart-heading">
        <div>
          <div className="table-title">График сделки</div>
          <p className="muted">Открытая сделка получает живые свечи биржи автоматически; закрытая — сохраняет фактическую историю входа, выхода, SL и TP.</p>
        </div>
        <div className="trade-chart-controls">
          <select value={selectedId ?? ""} onChange={(event) => setSelectedId(Number(event.target.value) || null)} aria-label="Выбор сделки">
            {!positions.length && <option value="">Сделок пока нет</option>}
            {positions.map((position) => (
              <option key={position.id} value={position.id}>
                #{position.id} · {position.symbol} · {position.status === "OPEN" ? "открыта" : position.exit_reason ?? "закрыта"}
              </option>
            ))}
          </select>
          <div className="trade-chart-timeframes" role="group" aria-label="Таймфрейм графика">
            {TRADE_CHART_TIMEFRAMES.map((item) => (
              <button
                key={item.value}
                type="button"
                className={`trade-chart-timeframe ${timeframe === item.value ? "active" : ""}`}
                onClick={() => setTimeframe(item.value)}
                aria-pressed={timeframe === item.value}
              >
                {item.label}
              </button>
            ))}
          </div>
          <button className="btn compact" onClick={() => void Promise.all([loadPositions(), loadChart()])} disabled={loading}>
            <RefreshCw size={15} /> {loading ? "Загрузка" : "Обновить"}
          </button>
          <button className="btn compact" onClick={() => void ingestSelectedHistory()} disabled={loading || !selected}>
            <Download size={15} /> Загрузить свечи
          </button>
        </div>
      </div>
      {error && <Alert tone="danger" text={error} />}
      {selected && !chart && !loading && <p className="muted trade-chart-empty">Загрузка графика для {selected.symbol} не дала данных.</p>}
      {chart && <TradePriceChart chart={chart} />}
    </div>
  );
}

function TradePriceChart({ chart }: { chart: TradeChart }) {
  const [zoom, setZoom] = React.useState(1);
  const [pan, setPan] = React.useState(0);
  const [hovered, setHovered] = React.useState<TradeChartGeometry["candles"][number] | null>(null);
  const drag = React.useRef<{ startX: number; startPan: number } | null>(null);
  const geometry = React.useMemo(() => buildTradeChartGeometry(chart, zoom, pan), [chart, pan, zoom]);
  const levelStyle: Record<TradeChart["levels"][number]["kind"], string> = {
    ENTRY: "entry",
    STOP: "stop",
    TAKE: "take",
    BREAKEVEN: "breakeven",
    EXIT: "exit"
  };

  React.useEffect(() => {
    setZoom(1);
    setPan(0);
    setHovered(null);
  }, [chart.position_id, chart.timeframe]);

  const changeZoom = (multiplier: number) => {
    setZoom((current) => Math.max(1, Math.min(16, current * multiplier)));
  };

  const updateHoveredCandle = (event: React.PointerEvent<HTMLDivElement>) => {
    const bounds = event.currentTarget.getBoundingClientRect();
    const pointerX = ((event.clientX - bounds.left) / Math.max(bounds.width, 1)) * geometry.width;
    const candle = geometry.candles.reduce((nearest, current) => (
      Math.abs(current.x - pointerX) < Math.abs(nearest.x - pointerX) ? current : nearest
    ));
    setHovered((current) => current?.timestamp === candle.timestamp ? current : candle);
  };

  const releaseDrag = (event: React.PointerEvent<HTMLDivElement>) => {
    drag.current = null;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
  };

  const lastCandle = chart.candles[chart.candles.length - 1];

  return (
    <div className="trade-chart-body">
      <div className="trade-chart-meta">
        <span className={`pill ${chart.side === "LONG" ? "buy" : "sell"}`}>{translateAction(chart.side)}</span>
        <strong>{chart.symbol}</strong>
        <span>{chart.status === "OPEN" ? "Открытая позиция" : "Закрытая позиция"}</span>
        <span>ТФ {chart.timeframe}</span>
        {chart.status === "OPEN" && (
          <span className={`trade-chart-live ${chart.live_market ? "" : "stale"}`}>
            ● {chart.live_market ? `рынок live · каждые ${OPEN_TRADE_CHART_REFRESH_MS / 1000} сек.` : "ожидание live-данных биржи"}
          </span>
        )}
        {chart.live_updated_at && <span>Получено: {new Date(chart.live_updated_at).toLocaleTimeString("ru-RU")}</span>}
        {lastCandle && <span>Последняя свеча: ${fmt(lastCandle.close)}</span>}
      </div>
      {chart.candles.length > 1 ? (
        <>
          <div className="trade-chart-toolbar">
            <span>Масштаб: {zoom.toFixed(zoom < 2 ? 1 : 0)}×</span>
            <button type="button" className="chart-tool" onClick={() => changeZoom(1 / 1.5)} disabled={zoom <= 1} aria-label="Отдалить график">−</button>
            <button type="button" className="chart-tool" onClick={() => changeZoom(1.5)} disabled={zoom >= 16} aria-label="Увеличить график">+</button>
            <button type="button" className="chart-tool chart-tool-fit" onClick={() => { setZoom(1); setPan(0); }} disabled={zoom === 1 && pan === 0}>Показать всё</button>
            <span className="muted">Колесо — масштаб; перетаскивание — перемещение по времени.</span>
          </div>
          <div
            className={`trade-chart-canvas ${drag.current ? "dragging" : ""}`}
            role="application"
            aria-label={`Интерактивный график сделки ${chart.symbol}`}
            onWheel={(event) => {
              event.preventDefault();
              changeZoom(event.deltaY < 0 ? 1.35 : 1 / 1.35);
            }}
            onPointerDown={(event) => {
              if (geometry.visibleCount >= chart.candles.length) return;
              drag.current = { startX: event.clientX, startPan: pan };
              event.currentTarget.setPointerCapture(event.pointerId);
            }}
            onPointerMove={(event) => {
              updateHoveredCandle(event);
              if (!drag.current) return;
              const bounds = event.currentTarget.getBoundingClientRect();
              const hiddenFraction = Math.max(1 - geometry.visibleCount / chart.candles.length, 0.0001);
              const nextPan = drag.current.startPan + (drag.current.startX - event.clientX) / Math.max(bounds.width, 1) / hiddenFraction;
              setPan(Math.max(0, Math.min(1, nextPan)));
            }}
            onPointerUp={releaseDrag}
            onPointerCancel={releaseDrag}
            onPointerLeave={() => { if (!drag.current) setHovered(null); }}
          >
            <svg viewBox={`0 0 ${geometry.width} ${geometry.height}`} preserveAspectRatio="none">
            <defs>
              <linearGradient id={`trade-area-${chart.position_id}`} x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor="#54e8ff" stopOpacity="0.25" />
                <stop offset="100%" stopColor="#54e8ff" stopOpacity="0" />
              </linearGradient>
            </defs>
            {geometry.priceTicks.map((tick) => (
              <g key={tick.price} className="trade-chart-axis">
                <line x1={geometry.plotLeft} x2={geometry.plotRight} y1={tick.y} y2={tick.y} className="trade-chart-grid" />
                <text x={geometry.plotRight + 10} y={tick.y + 4}>${fmt(tick.price)}</text>
              </g>
            ))}
            <line x1={geometry.plotLeft} x2={geometry.plotRight} y1={geometry.volumeBottom} y2={geometry.volumeBottom} className="trade-chart-volume-baseline" />
            <text x={geometry.plotLeft} y={geometry.volumeTop - 4} className="trade-chart-volume-label">ОБЪЁМ</text>
            {geometry.candles.map((candle) => (
              <rect key={`${candle.timestamp}-volume`} x={candle.x - candle.bodyWidth / 2} y={candle.volumeY} width={candle.bodyWidth} height={Math.max(geometry.volumeBottom - candle.volumeY, 1)} className={candle.close >= candle.open ? "trade-chart-volume up" : "trade-chart-volume down"} />
            ))}
            {geometry.candles.map((candle) => (
              <g key={candle.timestamp} className={candle.close >= candle.open ? "candle-up" : "candle-down"}>
                <title>{`${formatChartTimestamp(candle.timestamp)} UTC\nОткрытие ${fmt(candle.open)} · Максимум ${fmt(candle.high)} · Минимум ${fmt(candle.low)} · Закрытие ${fmt(candle.close)}\nОбъём ${fmt(candle.volume)}`}</title>
                <line x1={candle.x} x2={candle.x} y1={candle.highY} y2={candle.lowY} />
                <rect x={candle.x - candle.bodyWidth / 2} y={candle.bodyY} width={candle.bodyWidth} height={Math.max(candle.bodyHeight, 1.25)} rx="0.5" />
              </g>
            ))}
            <path d={geometry.areaPath} fill={`url(#trade-area-${chart.position_id})`} />
            <path d={geometry.closePath} className="trade-close-line" />
            {geometry.levels.map((level) => (
              <g key={level.key} className={`trade-level ${levelStyle[level.kind]}`}>
                <line x1={geometry.plotLeft} x2={geometry.plotRight} y1={level.y} y2={level.y} />
                <text x={geometry.plotLeft + 4} y={Math.max(level.y - 4, geometry.priceTop + 11)}>{level.label} ${fmt(level.price)}</text>
              </g>
            ))}
            {geometry.markers.map((marker) => (
              <g key={marker.key} className={`trade-marker ${marker.kind === "ENTRY" ? "entry" : "exit"}`}>
                <line x1={marker.x} x2={marker.x} y1={geometry.priceTop} y2={geometry.priceBottom} />
                <circle cx={marker.x} cy={marker.y} r="6" />
                <text x={marker.textX} y={marker.textY} textAnchor={marker.textAnchor}>{marker.label} · ${fmt(marker.price)}</text>
              </g>
            ))}
            {geometry.timeTicks.map((tick) => <text key={tick.timestamp} x={tick.x} y={geometry.height - 7} textAnchor="middle" className="trade-chart-time-label">{formatChartTimestamp(tick.timestamp)}</text>)}
          </svg>
          </div>
          {hovered && (
            <div className="trade-chart-hover" aria-live="polite">
              <strong>{formatChartTimestamp(hovered.timestamp)} UTC</strong>
              <span>O ${fmt(hovered.open)} · H ${fmt(hovered.high)} · L ${fmt(hovered.low)} · C ${fmt(hovered.close)} · V {fmt(hovered.volume)}</span>
            </div>
          )}
        </>
      ) : (
        <div className="trade-chart-empty">Недостаточно сохранённых свечей для линии графика.</div>
      )}
      <div className="trade-chart-levels">
        {chart.levels.map((level) => <span key={level.key} className={`trade-level-chip ${levelStyle[level.kind]}`}>{level.label}: ${fmt(level.price)}</span>)}
      </div>
      {chart.data_note && <p className="muted trade-chart-note">{chart.data_note}</p>}
    </div>
  );
}

type TradeChartGeometry = {
  width: number;
  height: number;
  plotLeft: number;
  plotRight: number;
  priceTop: number;
  priceBottom: number;
  volumeTop: number;
  volumeBottom: number;
  visibleCount: number;
  closePath: string;
  areaPath: string;
  candles: Array<{ timestamp: string; x: number; highY: number; lowY: number; bodyY: number; bodyHeight: number; bodyWidth: number; volumeY: number; open: number; high: number; low: number; close: number; volume: number }>;
  levels: Array<{ key: string; label: string; price: number; y: number; kind: TradeChart["levels"][number]["kind"] }>;
  markers: Array<{ key: string; label: string; kind: TradeChart["markers"][number]["kind"]; x: number; y: number; price: number; textX: number; textY: number; textAnchor: "start" | "end" }>;
  priceTicks: Array<{ price: number; y: number }>;
  timeTicks: Array<{ timestamp: string; x: number }>;
};

function buildTradeChartGeometry(chart: TradeChart, zoom: number, pan: number): TradeChartGeometry {
  const width = 1200;
  const height = 420;
  const plotLeft = 14;
  const plotRight = 1080;
  const priceTop = 16;
  const priceBottom = 310;
  const volumeTop = 338;
  const volumeBottom = 395;
  const visibleCount = Math.max(2, Math.min(chart.candles.length, Math.ceil(chart.candles.length / Math.max(zoom, 1))));
  const maxStart = Math.max(chart.candles.length - visibleCount, 0);
  const startIndex = Math.round(Math.max(0, Math.min(1, pan)) * maxStart);
  const visibleCandles = chart.candles.slice(startIndex, startIndex + visibleCount);
  const prices = [...visibleCandles.flatMap((candle) => [candle.high, candle.low]), ...chart.levels.map((level) => level.price), ...chart.markers.map((marker) => marker.price)].filter(Number.isFinite);
  const rawMin = Math.min(...prices);
  const rawMax = Math.max(...prices);
  const span = Math.max(rawMax - rawMin, Math.max(Math.abs(rawMax) * 0.002, 0.00000001));
  const minPrice = rawMin - span * 0.08;
  const maxPrice = rawMax + span * 0.08;
  const priceToY = (price: number) => priceBottom - ((price - minPrice) / (maxPrice - minPrice)) * (priceBottom - priceTop);
  const start = new Date(visibleCandles[0]?.timestamp ?? chart.markers[0]?.timestamp ?? Date.now()).getTime();
  const end = new Date(visibleCandles[visibleCandles.length - 1]?.timestamp ?? chart.markers[chart.markers.length - 1]?.timestamp ?? Date.now()).getTime();
  const timeSpan = Math.max(end - start, 1);
  const timeToX = (value: string) => plotLeft + Math.max(0, Math.min(1, (new Date(value).getTime() - start) / timeSpan)) * (plotRight - plotLeft);
  const bodyWidth = Math.max(2, Math.min(18, (plotRight - plotLeft) / Math.max(visibleCandles.length, 1) * 0.62));
  const maxVolume = Math.max(...visibleCandles.map((candle) => candle.volume), 1);
  const candles = visibleCandles.map((candle) => {
    const openY = priceToY(candle.open);
    const closeY = priceToY(candle.close);
    return {
      timestamp: candle.timestamp,
      x: timeToX(candle.timestamp),
      highY: priceToY(candle.high),
      lowY: priceToY(candle.low),
      bodyY: Math.min(openY, closeY),
      bodyHeight: Math.abs(openY - closeY),
      bodyWidth,
      volumeY: volumeBottom - (Math.max(candle.volume, 0) / maxVolume) * (volumeBottom - volumeTop),
      open: candle.open,
      high: candle.high,
      low: candle.low,
      close: candle.close,
      volume: candle.volume
    };
  });
  const closePath = candles.map((candle, index) => `${index ? "L" : "M"}${candle.x.toFixed(2)},${priceToY(candle.close).toFixed(2)}`).join(" ");
  const lastCandleGeometry = candles[candles.length - 1];
  const areaPath = closePath ? `${closePath} L${lastCandleGeometry?.x.toFixed(2)},${priceBottom} L${candles[0]?.x.toFixed(2)},${priceBottom} Z` : "";
  const markerPadding = timeSpan / Math.max(visibleCandles.length - 1, 1) * 0.6;
  const markerForViewport = chart.markers.filter((marker) => {
    const timestamp = new Date(marker.timestamp).getTime();
    return timestamp >= start - markerPadding && timestamp <= end + markerPadding;
  });
  return {
    width,
    height,
    plotLeft,
    plotRight,
    priceTop,
    priceBottom,
    volumeTop,
    volumeBottom,
    visibleCount,
    closePath,
    areaPath,
    candles,
    levels: chart.levels.map((level) => ({ ...level, y: priceToY(level.price) })),
    markers: markerForViewport.map((marker) => {
      const x = timeToX(marker.timestamp);
      const y = priceToY(marker.price);
      const textAnchor = x > plotRight - 175 ? "end" : "start";
      return {
        ...marker,
        x,
        y,
        textX: x + (textAnchor === "end" ? -10 : 10),
        textY: Math.max(priceTop + 13, Math.min(priceBottom - 8, y - 12)),
        textAnchor,
      };
    }),
    priceTicks: [0, 0.25, 0.5, 0.75, 1].map((fraction) => {
      const price = maxPrice - fraction * (maxPrice - minPrice);
      return { price, y: priceToY(price) };
    }),
    timeTicks: [...new Set([0, 0.25, 0.5, 0.75, 1].map((fraction) => (
      Math.min(visibleCandles.length - 1, Math.round((visibleCandles.length - 1) * fraction))
    )))].map((index) => {
      const candle = visibleCandles[index];
      return { timestamp: candle.timestamp, x: timeToX(candle.timestamp) };
    })
  };
}

function formatChartTimestamp(value: string) {
  const timestamp = new Date(value);
  if (Number.isNaN(timestamp.getTime())) return "—";
  return new Intl.DateTimeFormat("ru-RU", {
    timeZone: "UTC",
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit"
  }).format(timestamp);
}

function learningStageLabel(value?: LearningProgress["stage"]) {
  const labels: Record<string, string> = {
    COLLECTING: "Сбор данных",
    CALIBRATING: "Калибровка",
    LEARNING: "Активное обучение",
    MATURE: "Устойчивая база"
  };
  return value ? labels[value] ?? value : "Загрузка";
}

function milestoneLabel(value?: string | null) {
  const labels: Record<string, string> = {
    candle_coverage: "История свечей по всем парам",
    trade_lessons: "Закрытые сделки-уроки",
    memory_observations: "Наблюдения в памяти",
    active_rl_pairs: "Активные RL-модели по парам"
  };
  return value ? labels[value] ?? value : "все базовые цели выполнены";
}

function blockerLabel(value: string) {
  const labels: Record<string, string> = {
    POSITION_ALREADY_OPEN: "По паре уже есть открытая позиция",
    RECOVERY_POSITION_LIMIT: "Лимит обычных позиций в recovery-режиме",
    PERFORMANCE_GUARD: "Performance guard держит паузу",
    PAPER_LANE_CYCLE_LIMIT: "Не больше одной учебной сделки за цикл",
    PAPER_LANE_POSITION_LIMIT: "Заполнены учебные paper-слоты",
    COOLDOWN: "Активен cooldown после недавней сделки/убытка",
    MAX_POSITIONS: "Достигнут общий лимит открытых позиций",
    LOW_SCORE: "Недостаточный рейтинг сигнала",
    PRETRADE_QUALITY: "Walk-forward не подтвердил качество",
    RL_DISAGREEMENT: "RL-модель не согласна с направлением",
    LEARNING_MEMORY: "Память распознала слабый/убыточный паттерн",
    MARKET_QUALITY: "Недостаточная ликвидность или качество рынка",
    STRATEGY_WAIT: "Стратегии не хватило подтверждений направления",
    MICROSTRUCTURE: "Стакан и лента не подтвердили точку входа",
    COMMITTEE: "Ансамбль агентов не набрал консенсус 75%",
    DIRECTIONAL_EXPOSURE: "Слишком много позиций в одну сторону",
    EXPOSURE: "Лимит общей или парной экспозиции",
    OTHER: "Другая защитная проверка"
  };
  return labels[value] ?? value;
}

function formatDateTime(value?: string | null) {
  return value ? new Date(value).toLocaleString("ru-RU") : "ещё не было";
}

function exchangeLabel(value: string) {
  const labels: Record<string, string> = {
    binance: "Binance",
    bybit: "Bybit",
    okx: "OKX",
    kucoin: "KuCoin",
    gateio: "Gate.io"
  };
  return labels[value] ?? value;
}

function translateAgentName(value: string) {
  const labels: Record<string, string> = {
    MarketAnalystAgent: "Главный рыночный аналитик",
    LlmAdvisorAgent: "LLM-советник",
    RiskSupervisorAgent: "Инспектор риска",
    RegimeAgent: "Аналитик режима рынка",
    TrendAgent: "Трендовый аналитик",
    AdaptiveTrendAgent: "Адаптивный трендовый претендент",
    MomentumAgent: "Аналитик импульса",
    BreakoutAgent: "Аналитик пробоя-претендент",
    LiquidityAgent: "Инспектор ликвидности",
    VolatilityAgent: "Инспектор волатильности",
    DataQualityAgent: "Инспектор качества данных",
    EntryTimingAgent: "Инспектор тайминга входа",
    MicrostructureAgent: "Аналитик микроструктуры",
    TradeCommittee: "Торговый комитет",
    AgentOrchestrator: "Оркестратор агентов",
    PaperLearningScout: "Учебный разведчик",
    PaperLearningRiskGate: "Учебный инспектор риска",
    rl_policy: "Активная RL-модель",
    rl_shadow: "Теневая RL-модель",
  };
  return labels[value] ?? value;
}

function translateCompetitionStatus(value?: string | null) {
  return ({ CHAMPION: "Основной", CHALLENGER: "Претендент (в тени)", POOR: "Слабый аналитик" } as Record<string, string>)[value ?? ""] ?? "Защитный / базовый";
}

function translateAction(value: string) {
  const labels: Record<string, string> = {
    BUY: "Купить",
    buy: "Покупка",
    SELL: "Продать",
    sell: "Продажа",
    WAIT: "Ждать",
    ALLOW: "Разрешить",
    REDUCE_SIZE: "Уменьшить объем",
    BLOCK: "Блок",
    OPENED: "Открыто",
    SKIPPED: "Пропущено",
    LONG: "Лонг",
    SHORT: "Шорт"
  };
  return labels[value] ?? value;
}

function postMortemLabel(value: string) {
  const labels: Record<string, string> = {
    EARLY_EXIT_FROM_PROFIT: "Ранний выход после прибыли",
    HELD_AFTER_EARLY_INVALIDATION: "Удержание после инвалидирования",
    ENTRY_AGAINST_ORDER_FLOW: "Вход против стакана и ленты",
    LATE_ENTRY_EXHAUSTION: "Запоздалый вход в истощённый импульс",
    EXECUTION_COST_DAMAGE: "Издержки съели риск",
    VALID_STOP: "Правильный стоп по плану",
    UNCLASSIFIED_LOSS: "Причина уточняется"
  };
  return labels[value] ?? value.replace(/_/g, " ");
}

function translateStatus(value: string) {
  const labels: Record<string, string> = {
    NEW: "Новый",
    FILLED: "Исполнен",
    CANCELLED: "Отменен",
    FAILED: "Ошибка",
    OPEN: "Открыта",
    CLOSED: "Закрыта",
    STOP_LOSS: "Стоп-лосс",
    TAKE_PROFIT: "Тейк-профит"
  };
  return labels[value] ?? value;
}

function translateFeature(value: string) {
  const labels: Record<string, string> = {
    regime: "Режим",
    regime_score_bucket: "Сила режима",
    rsi_bucket: "RSI зона",
    atr_bucket: "ATR зона",
    trend_stack: "EMA структура",
    macd_direction: "MACD",
    rating_bucket: "Рейтинг",
    momentum_profile: "Профиль импульса",
    risk_profile: "Профиль риска",
    setup_signature: "Сетап",
    exit_reason: "Причина выхода",
    post_mortem_primary_label: "Главная причина ошибки",
    post_mortem_behavior: "Поведенческая ошибка",
    strategy_followed: "Соблюдение стратегии"
  };
  return labels[value] ?? value;
}

function translateRiskLevel(value: string) {
  const labels: Record<string, string> = {
    WATCH: "Наблюдать",
    WARN: "Риск",
    BLOCK: "Блок"
  };
  return labels[value] ?? value;
}

function translateFeatureValue(value: string): string {
  if (value.includes("|")) {
    return value.split("|").map((part) => translateFeatureValue(part)).join(" / ");
  }
  const labels: Record<string, string> = {
    TRENDING_UP: "Рост",
    TRENDING_DOWN: "Падение",
    HIGH_VOLATILITY: "Высокая волатильность",
    LOW_LIQUIDITY: "Низкая ликвидность",
    UNKNOWN: "Неизвестно",
    weak: "Слабый",
    medium: "Средний",
    strong: "Сильный",
    elite: "Элитный",
    oversold: "Перепроданность",
    bearish: "Медвежья",
    neutral: "Нейтральная",
    bullish: "Бычья",
    overbought: "Перекупленность",
    quiet: "Тихо",
    normal: "Норма",
    hot: "Горячо",
    extreme: "Экстрим",
    mixed: "Смешанная",
    positive: "Положительный",
    negative: "Отрицательный",
    flat: "Плоский"
  };
  return translateStatus(labels[value] ?? postMortemLabel(value));
}

function translateRegime(value: string) {
  const labels: Record<string, string> = {
    TRENDING_UP: "Рост",
    TRENDING_DOWN: "Падение",
    HIGH_VOLATILITY: "Высокая волатильность",
    LOW_LIQUIDITY: "Низкая ликвидность",
    RANGING: "Боковик",
    UNKNOWN: "Неизвестно"
  };
  return labels[value] ?? value;
}

function readError(err: unknown) {
  if (typeof err === "object" && err && "response" in err) {
    const response = (err as { response?: { status?: number; data?: { detail?: unknown } } }).response;
    if (response?.status === 401) {
      return "Сессия истекла. Войди заново.";
    }
    return formatErrorDetail(response?.data?.detail) ?? "Запрос не выполнен";
  }
  return "Запрос не выполнен";
}

function formatProfitFactor(value: number | null | undefined) {
  if (value == null) {
    return "∞";
  }
  return fmt(value);
}

function translateTradeResult(value: string) {
  const labels: Record<string, string> = {
    WIN: "Прибыль",
    LOSS: "Убыток",
    BREAKEVEN: "Безубыток"
  };
  return labels[value] ?? value;
}

function translateRlModelStatus(status: string, isActive: boolean) {
  if (isActive) return "Активна · прошла проверки";
  const labels: Record<string, string> = {
    SHADOW: "Тень · виртуальная проверка",
    RETIRED: "Архив · заменена/исключена",
    CANDIDATE: "Обучается",
    REJECTED: "Отклонена проверками",
    ACTIVE: "Активна"
  };
  return labels[status] ?? status;
}

function translateForwardStatus(value: string) {
  const labels: Record<string, string> = {
    PENDING: "Ожидает достаточных виртуальных сделок",
    PASSED: "Пройдена",
    FAILED: "Не пройдена",
    NONE: "Не запускалась"
  };
  return labels[value] ?? value;
}

function translatePromotionReason(value: string) {
  if (value === "passed") return "Все критерии validation пройдены";
  const replacements: Array<[string, string]> = [
    ["validation return below threshold", "доходность validation ниже порога"],
    ["validation return underperformed buy-and-hold", "результат хуже стратегии buy-and-hold"],
    ["too few profitable training seeds", "слишком мало успешных обучающих запусков"],
    ["validation profit factor below threshold", "Profit Factor validation ниже порога"],
    ["too few validation trades", "недостаточно сделок на validation"],
    ["validation drawdown above threshold", "просадка validation превышает предел"],
  ];
  let translated = value;
  for (const [source, target] of replacements) translated = translated.split(source).join(target);
  return translated;
}

function translateLearningImpact(value: string) {
  const labels: Record<string, string> = {
    PREFER: "Позитивный",
    WATCH: "Наблюдать",
    CAUTION: "Снизить риск",
    AVOID: "Избегать"
  };
  return labels[value] ?? value;
}

function formatErrorDetail(detail: unknown) {
  if (typeof detail === "string") {
    return translateError(detail) ?? detail;
  }
  if (Array.isArray(detail)) {
    return detail.map((item) => {
      if (typeof item === "object" && item && "msg" in item) {
        const validation = item as { loc?: Array<string | number>; msg?: string };
        const path = validation.loc?.filter((part) => part !== "body").join(".");
        return path ? `${path}: ${validation.msg}` : validation.msg;
      }
      return String(item);
    }).filter(Boolean).join("; ");
  }
  return undefined;
}

function translateError(value?: string) {
  if (!value) {
    return undefined;
  }
  const labels: Record<string, string> = {
    "Incorrect email or password": "Неверный email или пароль",
    "Email already registered": "Этот email уже зарегистрирован",
    "Registration failed": "Регистрация не удалась",
    "Invalid token": "Сессия истекла. Войди заново.",
    "Request failed": "Запрос не выполнен"
  };
  return labels[value] ?? value;
}

createRoot(document.getElementById("root")!).render(<App />);
