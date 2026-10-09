# Crypto AI Trader

MVP scaffold for an automated crypto trading system with FastAPI, PostgreSQL, Redis, React, Docker, market scoring, strategy evaluation, risk checks, paper trading, encrypted exchange credentials, logs, and ML/backtesting extension points.

The primary trading venue is USDT-margined futures (`EXCHANGE_DEFAULT_TYPE=future`),
with isolated margin and configured leverage of 2 (capped at 3). Paper trading
remains enabled by default. Existing deployments must update their environment
explicitly: a previously saved `EXCHANGE_DEFAULT_TYPE=spot` overrides defaults.

Spot is optional and disabled by default (`SPOT_SECONDARY_ENABLED=false`).
Disabled spot mode blocks new entries but allows existing positions to exit.
There is no automatic fallback from futures to spot and no concurrent allocation
between the two markets yet. Enable spot only after separately validating its
strategy and accounting; the setting alone does not establish profitability.

## Stack

The **Статистика и аудит** page includes a calendar-day activity report at
`GET /api/v1/audit/activity?day=YYYY-MM-DD` (authenticated). It uses
Europe/Simferopol boundaries and the 00–07, 07–14 and 14–24 slices. Closed PnL,
entry skips, failed orders and recorded equity snapshots are separate measures.
Missing historical snapshots are returned as missing, never reconstructed from
today's unrealized PnL. The existing 30-day audit covers completed days only.
Three durable period reports are saved daily after 07:00, 14:00 and 00:00 local
time by the trader worker, independently of entry signals and pauses. A unique
database key prevents duplicates across restarts. Missed reports are assembled
from the persisted journal and marked `JOURNAL_BACKFILL`; this does not recreate
missing equity samples. The first launch covers yesterday and elapsed periods
today; later restarts catch up to 30 days of retained journal history.
Paper exits preserve the exact remaining simulated inventory even when it is
below today's venue minimum; live orders retain exchange minimum checks.

- Backend: Python 3.12, FastAPI, SQLAlchemy 2, Alembic, PostgreSQL, Redis, CCXT-ready services, Pandas, NumPy, APScheduler
- ML: XGBoost, LightGBM, Scikit-Learn, and an isolated Stable Baselines3 PPO training worker
- Frontend: React, TypeScript, TailwindCSS, Vite, Axios
- Infrastructure: Docker Compose, Nginx

## Run

1. Copy `.env.example` to `.env`.
2. Replace `JWT_SECRET` and `ENCRYPTION_KEY`.
3. Start services:

```bash
docker compose up --build
```

To start the complete background pipeline (Telegram, automatic trader,
candle ingestion, optimizer, and RL trainer), enable the shared worker
profile:

```bash
docker compose --profile workers up --build
```

Open:

- Frontend: http://localhost:5173
- Nginx gateway: http://localhost:8080
- API docs: http://localhost:8000/docs

## Deploy to Railway

Create one Railway project with these services:

- PostgreSQL database
- Redis database
- `backend` service from this GitHub repository with root directory `backend`
- `frontend` service from this GitHub repository with root directory `frontend`
- `telegram-bot` worker service from this GitHub repository with root directory `backend`
- `trader-worker` worker service from this GitHub repository with root directory `backend`
- `candle-worker` worker service from this GitHub repository with root directory `backend`
- `optimizer-worker` worker service from this GitHub repository with root directory `backend`
- `rl-worker` worker service from this GitHub repository with root directory `backend` and Dockerfile `Dockerfile.rl`

Backend variables:

```env
APP_PROCESS=web
ENVIRONMENT=production
DATABASE_URL=${{Postgres.DATABASE_URL}}
REDIS_URL=${{Redis.REDIS_URL}}
JWT_SECRET=replace-with-long-random-secret
REGISTRATION_ENABLED=false
ENCRYPTION_KEY=fernet-key-generated-for-production
PAPER_TRADING=true
LIVE_TRADING_ENABLED=false
EXCHANGE_SANDBOX_ENABLED=true
ALLOW_LIVE_TRADING_WITHOUT_SANDBOX=false
MARKET_DATA_MODE=ccxt
CORS_ORIGINS=https://your-frontend-domain.up.railway.app
TELEGRAM_BOT_TOKEN=123456:telegram-token-from-botfather
TELEGRAM_ALLOWED_CHAT_IDS=123456789
TELEGRAM_TRADE_REPORTS_ENABLED=true
TELEGRAM_CYCLE_REPORTS_ENABLED=true
TELEGRAM_CYCLE_REPORT_INTERVAL_MINUTES=15
TELEGRAM_OUTBOX_ENABLED=true
TELEGRAM_OUTBOX_RETRY_LIMIT=8
TELEGRAM_DAILY_REPORT_ENABLED=true
TELEGRAM_DAILY_REPORT_HOUR_UTC=18
TELEGRAM_DAILY_REPORT_MINUTE_UTC=0
WORKER_HEARTBEAT_ENABLED=true
WORKER_HEARTBEAT_INTERVAL_SECONDS=30
WORKER_HEARTBEAT_STALE_SECONDS=180
WORKER_HEARTBEAT_STARTUP_GRACE_SECONDS=600
WORKER_HEARTBEAT_LONG_TASK_GRACE_SECONDS=900
WORKER_HEARTBEAT_EXPECTED_WORKERS=trader-worker,candle-worker,rl-worker,optimizer-worker,telegram
TRADER_LOOP_SECONDS=60
LLM_PROVIDER=none
OPENAI_API_KEY=
LLM_MODEL=gpt-4.1-mini
AI_COMMITTEE_ENABLED=true
AI_COMMITTEE_MIN_CONSENSUS=0.75
MAX_GROSS_EXPOSURE_PERCENT=300
MAX_SYMBOL_EXPOSURE_PERCENT=100
TRADING_EXCLUDED_SYMBOLS=BTC/USDT,DOGE/USDT,AVAX/USDT,NEAR/USDT,LTC/USDT,FET/USDT,XRP/USDT
CANDLE_INGEST_SYMBOLS=ETH/USDT,BNB/USDT,SOL/USDT,ADA/USDT,LINK/USDT,DOT/USDT,TRX/USDT,AAVE/USDT,UNI/USDT,ONDO/USDT
MARKET_SCAN_SYMBOLS=ETH/USDT,BNB/USDT,SOL/USDT,ADA/USDT,LINK/USDT,DOT/USDT,TRX/USDT,AAVE/USDT,UNI/USDT,ONDO/USDT
MARKET_SCAN_CONCURRENCY=3
CANDLE_INGEST_TIMEFRAMES=1h
CANDLE_INGEST_LIMIT=500
CANDLE_INGEST_LOOP_SECONDS=300
CANDLE_DATASET_TARGET=5000
```

Frontend variables:

```env
VITE_API_BASE_URL=https://your-backend-domain.up.railway.app/api/v1
```

Telegram worker variables:

```env
APP_PROCESS=telegram
ENVIRONMENT=production
DATABASE_URL=${{Postgres.DATABASE_URL}}
REDIS_URL=${{Redis.REDIS_URL}}
JWT_SECRET=the-same-secret-as-backend
ENCRYPTION_KEY=the-same-fernet-key-as-backend
PAPER_TRADING=true
TELEGRAM_BOT_TOKEN=123456:telegram-token-from-botfather
TELEGRAM_ALLOWED_CHAT_IDS=123456789
```

Trader worker variables:

```env
APP_PROCESS=trader
ENVIRONMENT=production
DATABASE_URL=${{Postgres.DATABASE_URL}}
REDIS_URL=${{Redis.REDIS_URL}}
JWT_SECRET=the-same-secret-as-backend
ENCRYPTION_KEY=the-same-fernet-key-as-backend
PAPER_TRADING=true
LIVE_TRADING_ENABLED=false
EXCHANGE_SANDBOX_ENABLED=true
ALLOW_LIVE_TRADING_WITHOUT_SANDBOX=false
MARKET_DATA_MODE=ccxt
TELEGRAM_BOT_TOKEN=123456:telegram-token-from-botfather
TELEGRAM_ALLOWED_CHAT_IDS=123456789
TRADER_LOOP_SECONDS=60
TELEGRAM_TRADE_REPORTS_ENABLED=true
TELEGRAM_CYCLE_REPORTS_ENABLED=true
TELEGRAM_CYCLE_REPORT_INTERVAL_MINUTES=15
PAPER_EXPLORATION_ENABLED=false
PAPER_EXPLORATION_MIN_SCORE=60
PAPER_EXPLORATION_RISK_PERCENT=0.15
PAPER_EXPLORATION_MAX_RISK_PERCENT=0.15
PAPER_EXPLORATION_MAX_POSITIONS=2
PAPER_EXPLORATION_RECOVERY_SLOTS=2
PAPER_EXPLORATION_MAX_PER_CYCLE=2
PAPER_EXPLORATION_MIN_DIRECTIONAL_VOTES=4
PAPER_EXPLORATION_MIN_VOTE_MARGIN=2
PAPER_EXPLORATION_COOLDOWN_MINUTES=180
SAFETY_CHECK_ENABLED=true
SAFETY_CHECK_SYMBOL=ETH/USDT
SAFETY_RETRY_ATTEMPTS=5
SAFETY_RETRY_INITIAL_SECONDS=2
SAFETY_RETRY_MAX_SECONDS=30
AI_COMMITTEE_ENABLED=true
AI_COMMITTEE_MIN_CONSENSUS=0.75
MAX_GROSS_EXPOSURE_PERCENT=300
MAX_SYMBOL_EXPOSURE_PERCENT=100
```

Candle worker variables:

```env
APP_PROCESS=candles
ENVIRONMENT=production
DATABASE_URL=${{Postgres.DATABASE_URL}}
REDIS_URL=${{Redis.REDIS_URL}}
JWT_SECRET=the-same-secret-as-backend
ENCRYPTION_KEY=the-same-fernet-key-as-backend
PAPER_TRADING=true
MARKET_DATA_MODE=ccxt
TRADING_EXCLUDED_SYMBOLS=BTC/USDT,DOGE/USDT,AVAX/USDT,NEAR/USDT,LTC/USDT,FET/USDT,XRP/USDT
CANDLE_INGEST_SYMBOLS=ETH/USDT,BNB/USDT,SOL/USDT,ADA/USDT,LINK/USDT,DOT/USDT,TRX/USDT,AAVE/USDT,UNI/USDT,ONDO/USDT
MARKET_SCAN_SYMBOLS=ETH/USDT,BNB/USDT,SOL/USDT,ADA/USDT,LINK/USDT,DOT/USDT,TRX/USDT,AAVE/USDT,UNI/USDT,ONDO/USDT
MARKET_SCAN_CONCURRENCY=3
CANDLE_INGEST_TIMEFRAMES=1h,15m
CANDLE_INGEST_LIMIT=500
CANDLE_INGEST_LOOP_SECONDS=300
CANDLE_DATASET_TARGET=5000
```

Optimizer worker variables:

```env
APP_PROCESS=optimizer
ENVIRONMENT=production
DATABASE_URL=${{Postgres.DATABASE_URL}}
REDIS_URL=${{Redis.REDIS_URL}}
JWT_SECRET=the-same-secret-as-backend
ENCRYPTION_KEY=the-same-fernet-key-as-backend
STRATEGY_OPTIMIZER_WORKER_ENABLED=true
STRATEGY_OPTIMIZER_LOOP_SECONDS=21600
STRATEGY_OPTIMIZER_REFRESH_HOURS=24
STRATEGY_OPTIMIZER_LIMIT=800
STRATEGY_OPTIMIZER_TOP_N=5
```

`PAPER_TRADING=true` controls order execution only. Paper orders and balances remain virtual while `MARKET_DATA_MODE=ccxt` reads real public exchange prices and candles. The legacy value `MARKET_DATA_MODE=paper` is treated as the same real public feed for backward compatibility. Synthetic data is available only with the explicit value `MARKET_DATA_MODE=synthetic` and must never be used by the RL trainer.

`PAPER_EXPLORATION_ENABLED=true` enables a separate paper-only learning lane when the strict strategy returns `WAIT`. It is disabled by default because it deliberately samples weaker setups. A candidate must clear the score threshold, a decisive indicator vote, market-quality, walk-forward, RL, cooldown, exposure, daily-loss, and drawdown gates. The lane can keep learning while the regular performance guard is cooling down, but it has independent recovery slots, obeys the configured `PAPER_EXPLORATION_MAX_PER_CYCLE` cap, and is hard-capped by `PAPER_EXPLORATION_MAX_RISK_PERCENT` even when an older deployment variable requests more risk. Every candidate and final allow/block result is recorded as `PaperLearningScout` and `PaperLearningRiskGate` agent activity. Exploratory outcomes update learning memory but do not distort the regular-strategy performance guard.

The strict strategy requires current 24-hour quote volume to reach `STRATEGY_MIN_VOLUME_RATIO` of its rolling 24-hour average (default `1.05`) and rejects entries further than `STRATEGY_MAX_ENTRY_DISTANCE_ATR` from EMA20 (default `1.5`). This avoids treating missing volume history as confirmation or chasing an already extended move.

Live entry indicators use fully closed hourly candles; the active candle cannot create a temporary EMA, RSI, or MACD signal.

`BINANCE_EVENT_PRIORITY_ENABLED=true` checks Binance's official announcement feed for dated Launchpool and Launchpad events that explicitly use BNB. A confirmed pre-event/active event moves `BNB/USDT` to the front of strategy analysis and adds only a ranking bonus (`FINAL_SCORE = STRATEGY_SCORE + EVENT_SCORE + MARKET_SCORE`). It never changes BUY/SELL direction, `STRATEGY_SCORE`, risk, leverage, stops, or position size. Missing, stale, contradictory, or malformed source data fails closed to `UNKNOWN` with `EVENT_SCORE=0`; a post-event bonus is retained only when multiple BNB volume/volatility/momentum confirmations are present. Every cycle persists a `BNB_EVENT_EVALUATED` audit record with the event schedule and score components.

`SYMBOL_GUARD_*` adds a pair-level quarantine. After at least five closed trades, a pair whose recent win rate falls below 40% or whose recent net PnL is negative is paused for 24 hours. Its first retry is capped at 25% of normal risk, while healthy pairs remain eligible.

`DYNAMIC_TAKE_PROFIT_*` turns the initial ATR/R:R take-profit into a conservative profit ladder. At each reached target the bot closes `DYNAMIC_TAKE_PROFIT_PARTIAL_CLOSE_PERCENT` of the remaining position, locks the stop one ATR-based step behind the realised target, and moves the next target forward by that same step. The stop can only tighten; after `DYNAMIC_TAKE_PROFIT_MAX_EXTENSIONS` the remaining volume closes at the target. Set `DYNAMIC_TAKE_PROFIT_ENABLED=false` to retain a fixed final take-profit.

The first scale-out is now a cost-aware TP1: `Price_BU = Price_Entry × (1 + entry fee rate + expected exit fee rate + BREAKEVEN_SLIPPAGE_BUFFER_BPS / 10,000)` for a long (the signs are reversed for a short). It closes 10–30% only after the exchange confirms the fill, then moves the stop to that same Price_BU. TP2 closes 30–40% at `SCALE_OUT_TP2_DISTANCE_PERCENT` from entry until a persisted support/resistance detector is available; TP3 remains the ATR/strategy target. Before each partial order, the engine checks exchange precision, min amount, exchange min cost, and `MIN_EXIT_NOTIONAL_USDT`. If a partial is too small, it sends no invalid API order and preserves one monolithic final TP instead. For Binance, OKX and Bybit derivatives, an active reduce-only stop is cancelled and replaced after every confirmed TP/stop update; the new exchange order ID is recorded before it is trusted. If replacement is not confirmed by `PROTECTIVE_STOP_REPLACE_TIMEOUT_SECONDS`, the local monitor sends a market exit only when price returns to the protected stop. Spot mode deliberately retains that local monitor, because a generic spot stop cannot safely be reduce-only. Stop-fill logs retain actual execution slippage in the trade context and structured learning event; one observation does not automatically alter a live slippage buffer.

`DAILY_RISK_RESERVE_ENABLED=true` reserves the distance from every open position to its current stop, plus the candidate's planned stop loss, before a new entry is allowed. The bot rejects an entry if the worst case of all stops would exceed the configured daily-risk budget.

For exchange testnet execution, set `PAPER_TRADING=false`, `LIVE_TRADING_ENABLED=true`, and keep `EXCHANGE_SANDBOX_ENABLED=true`. Keep `ALLOW_LIVE_TRADING_WITHOUT_SANDBOX=false` until live execution is reviewed, tested, and deliberately approved.

For a live `trader-worker`, save the exchange credentials through the web settings. The worker decrypts those credentials for both its startup pre-flight and its trading loop, so Railway does not need a duplicate copy. The private pre-flight is enabled automatically in live mode. `API_KEY` and `API_SECRET` remain supported as deployment-level fallbacks when no credentials have been saved in the database:

```env
SAFETY_REQUIRE_API_CREDENTIALS=true
SAFETY_VALIDATE_PRIVATE_API=true
```

Do not add exchange secrets to the frontend or RL worker. In paper mode the pre-flight reads real public Binance time and `ETH/USDT` ticker data, but orders and balances remain virtual. `TRADING_EXCLUDED_SYMBOLS` removes listed pairs from scans, candle ingestion, RL, shadow decisions, and all new entries while existing positions remain managed until their normal exit. DOGE/USDT, AVAX/USDT, NEAR/USDT, LTC/USDT, FET/USDT, and XRP/USDT are also hard-excluded by the application, so an outdated Railway symbol list cannot restore them. In live mode pre-flight logs a prominent warning, requires both credentials, and validates them with `fetch_balance`. Invalid environment values, malformed exchange data, authentication failures, or exhausted network retries terminate the worker with exit code `1`, so Railway cannot start trading in a partially configured state. `SIGTERM` and `SIGINT` stop future loop iterations, interrupt PPO training through a Stable Baselines3 callback, and close tracked CCXT clients.

RL worker variables:

```env
APP_PROCESS=rl
ENVIRONMENT=production
DATABASE_URL=${{Postgres.DATABASE_URL}}
REDIS_URL=${{Redis.REDIS_URL}}
JWT_SECRET=the-same-secret-as-backend
ENCRYPTION_KEY=the-same-fernet-key-as-backend
PAPER_TRADING=true
LIVE_TRADING_ENABLED=false
MARKET_DATA_MODE=ccxt
DEFAULT_EXCHANGE=binance
TRADING_EXCLUDED_SYMBOLS=BTC/USDT,DOGE/USDT,AVAX/USDT,NEAR/USDT,LTC/USDT,FET/USDT,XRP/USDT
CANDLE_INGEST_SYMBOLS=ETH/USDT,BNB/USDT,SOL/USDT,ADA/USDT,LINK/USDT,DOT/USDT,TRX/USDT,AAVE/USDT,UNI/USDT,ONDO/USDT
MARKET_SCAN_SYMBOLS=ETH/USDT,BNB/USDT,SOL/USDT,ADA/USDT,LINK/USDT,DOT/USDT,TRX/USDT,AAVE/USDT,UNI/USDT,ONDO/USDT
MARKET_SCAN_CONCURRENCY=3
CANDLE_INGEST_TIMEFRAMES=1h
RL_TRAINER_ENABLED=true
RL_GATE_ENABLED=true
RL_SYMBOLS=ETH/USDT,BNB/USDT,SOL/USDT,ADA/USDT,LINK/USDT,DOT/USDT,TRX/USDT,AAVE/USDT,UNI/USDT,ONDO/USDT
RL_TRAINING_MAX_PER_CYCLE=1
RL_TRAINING_TIMESTEPS=20000
RL_TRAINING_LIMIT=5000
RL_MIN_TRAINING_CANDLES=2000
RL_TRAINING_SEEDS=7,29
RL_REFRESH_HOURS=24
RL_REJECTED_RETRY_HOURS=6
RL_PREDICTION_LOOP_SECONDS=300
SAFETY_CHECK_ENABLED=true
SAFETY_CHECK_SYMBOL=ETH/USDT
SAFETY_RETRY_ATTEMPTS=5
SAFETY_RETRY_INITIAL_SECONDS=2
SAFETY_RETRY_MAX_SECONDS=30
RL_VALIDATION_PERCENT=25
RL_MIN_VALIDATION_RETURN_PERCENT=0
RL_MIN_EXCESS_RETURN_PERCENT=0
RL_MIN_PROFITABLE_SEED_RATIO=0.5
RL_MIN_VALIDATION_PROFIT_FACTOR=1.05
RL_MIN_VALIDATION_TRADES=5
RL_MAX_VALIDATION_DRAWDOWN_PERCENT=15
RL_GATE_MIN_CONFIDENCE=0.55
RL_GATE_MAX_AGE_HOURS=6
RL_WAIT_RISK_MULTIPLIER=0.5
RL_CURRICULUM_ENABLED=true
RL_BEHAVIOR_PENALTY=0.35
RL_STRATEGY_ADHERENCE_BONUS=0.03
SHADOW_TRADE_NOTIONAL=100
SHADOW_TRADE_MIN_CONFIDENCE=0.55
SHADOW_FORWARD_MIN_TRADES=5
SHADOW_FORWARD_MIN_PROFIT_FACTOR=1.1
SHADOW_FORWARD_MIN_WIN_RATE=40
SHADOW_FORWARD_MIN_PNL=0
SHADOW_FORWARD_MAX_DRAWDOWN_PERCENT=8
SHADOW_FORWARD_MAX_TRIAL_DAYS=7
```

The RL service needs no Binance API key because OHLCV is public. Set its Railway Config File to `/backend/railway.rl.toml`; this selects `Dockerfile.rl`. Deploy it in the same Railway region that can reach Binance. `Dockerfile.rl` is preferred because it keeps the RL packages isolated. As a deployment safety net, the standard backend image now also contains Stable Baselines3 and CPU-only PyTorch: an RL worker configured with the standard Dockerfile will start instead of repeatedly failing with `ModuleNotFoundError`.

PPO training runs outside the asyncio event loop, so `rl-worker` keeps publishing heartbeat updates while PyTorch is busy. `RL_SYMBOLS` defines the RL universe independently from the market scanner, while `RL_TRAINING_MAX_PER_CYCLE` limits heavy training attempts and lets missing pairs enter the queue gradually. Worker status reports expose the current pair, progress, active and shadow decisions, and deferred training totals.

Every new model starts as `SHADOW`, even after it passes chronological validation. Backtest promotion eligibility still requires benchmark edge, seed stability, return, profit factor, trade count, and drawdown gates, but actual `ACTIVE` promotion now additionally requires virtual forward trades to pass PnL, win-rate, profit-factor, and drawdown thresholds. Shadow trades include simulated fees, slippage, and market impact and never create exchange orders. Only a promoted `rl_policy` decision can block or confirm a real/paper strategy entry. The dashboard reports active pair coverage separately from experiment history and shows shadow forward PnL, trades, and promotion state.

RL training uses curriculum stages when clean contiguous trend and normal-volatility windows are available, then finishes on the complete market history. The reward function separates financial outcome from behavior: it penalizes churn, giving back an established unrealized edge, and holding through repeated adverse confirmation; it gives a small credit for strategy-aligned actions and disciplined invalidation exits. Loss post-mortems are replay-weighted in later training, capped by `BAD_REPLAY_MAX_WEIGHT` so one mistake cannot dominate the whole dataset.

Entry execution uses a two-level gatekeeper. The macro gate enforces market regime and direction; the micro gate reads public order-book depth, recent trades, spread, 10-minute momentum, and a clearly labelled iceberg proxy. By default, an unavailable feed, fewer than two independent microstructure sources (`ENTRY_MICROSTRUCTURE_MIN_SOURCES=2`), or insufficient directional consensus blocks the entry; this prevents blind entries during exchange/API degradation and confirmation from a single noisy source. `ENTRY_MICROSTRUCTURE_REQUIRE_DATA=false` and `ENTRY_MICROSTRUCTURE_NEUTRAL_ENTRIES_ENABLED=true` restore the previous reduced-risk paper-experiment behavior. Configure it with `ENTRY_MICROSTRUCTURE_*`; `AI_COMMITTEE_MIN_CONSENSUS` defaults to `0.75`.

Trading logs persist a structured context for every entry, rejection, protection update, partial close, and final exit. It includes the event code, decision cycle, gate, entry signal, market snapshot, and the safe microstructure summary; the Logs panel exposes it under `Детали записи`, and the trading-audit CSV exports it as JSON.

Open positions are managed from exchange tickers directly and do not wait for an hourly candle/indicator refresh. The price selector uses the bid for a long and the ask for a short when no last price is available, so stop checks stay conservative. Ticker and price-data failures are recorded in the structured log.

After every closed losing position, `trade_post_mortems` stores the 30-minute pre-entry/position path, entry and exit microstructure, MFE/MAE, fees/slippage, behavior labels, shaped reward, lesson, and replay priority. Correct stop discipline receives credit even when financial PnL is negative. The dashboard and Telegram close report explain the result; `/api/v1/strategy-lab/post-mortems` and `/api/v1/strategy-lab/shadow-trades` expose the auditable records.

Only the `backend` and `frontend` services need public domains. Worker services should remain private. The web process runs Alembic migrations by default; background workers skip migrations to avoid concurrent schema upgrades. Override this only with an explicit `RUN_MIGRATIONS=true`.

Registration is owner-only by default: the first account can register on an empty database, then `/auth/register` is closed. Temporarily set `REGISTRATION_ENABLED=true` only when deliberately adding another trusted operator.

You can run only the critical pre-flight locally or in a Railway shell with `python -m app.safety_manager`. The `rl` and `trader` entry points run it automatically before starting their work loops; Stable Baselines3 is imported only after the check passes.

Use `POST /api/v1/market/history/ingest` to persist OHLCV candles for one symbol, `POST /api/v1/market/history/ingest/batch` for configured symbols, and `GET /api/v1/market/history/readiness` to inspect dataset coverage for backtesting and future ML datasets.

Use `POST /api/v1/strategy-lab/optimize` to run a parameter grid search against stored candles and save the strongest strategy configurations. Use `GET /api/v1/strategy-lab/results` to show the latest optimizer results in the dashboard.

Use `POST /api/v1/agents/analyze` to run the AI Trade Committee: market, trend, momentum, liquidity, volatility, optional LLM, and risk agents. Every agent decision is stored in `agent_decisions` for auditability.

Set `LLM_PROVIDER=openai` and `OPENAI_API_KEY` to enable the optional LLM advisor. It can only advise through structured JSON; it cannot open trades directly.

Use `GET /health/deep` to check database, Redis, panic state, paper mode, market data mode, and LLM provider.

Use `POST /api/v1/trading/panic` to pause new entries and `POST /api/v1/trading/resume` to resume them. Telegram supports `/panic` and `/resume`.

Dynamic risk defaults are configured with `MAX_POSITION_SIZE_PERCENT=25` and `MAX_DRAWDOWN_PERCENT=5`. Closed trades are mirrored asynchronously to SQLite at `TRADE_MEMORY_SQLITE_PATH=data/trade_memory.sqlite3`; PostgreSQL remains the primary transactional database.

Generate public domains for the backend and frontend services under Settings -> Networking. Keep `PAPER_TRADING=true` until live exchange execution has been reviewed and tested.

Generate `ENCRYPTION_KEY` with:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

The backend Dockerfile runs migrations on startup and listens on Railway's `$PORT`. The frontend Dockerfile builds static assets and serves them through Nginx on Railway's `$PORT`.

## Telegram Bot

Create a bot with Telegram `@BotFather`, copy the bot token, and set it as `TELEGRAM_BOT_TOKEN`.

To find your chat ID:

1. Temporarily leave `TELEGRAM_ALLOWED_CHAT_IDS` empty.
2. Deploy the `telegram-bot` Railway service.
3. Send `/chatid` to your bot.
4. Copy the returned number into `TELEGRAM_ALLOWED_CHAT_IDS`.
5. Redeploy the `telegram-bot` service.

Supported commands:

- `/start`
- `/chatid`
- `/status`
- `/balance`
- `/stats`
- `/guard`
- `/positions`
- `/report`
- `/trades`
- `/why`
- `/learning`
- `/reconcile`
- `/panic`
- `/resume`
- `/stop`

## Current MVP Behavior

- Uses paper trading by default through `PAPER_TRADING=true`.
- Opens exploratory paper positions from strong neutral setups only when explicitly enabled, so the standard configuration trades strict, validated signals only.
- Blocks non-sandbox live exchange execution unless `ALLOW_LIVE_TRADING_WITHOUT_SANDBOX=true` is deliberately set.
- Supports a dedicated `APP_PROCESS=trader` worker that loops automatically, manages open positions, and scans for new entries.
- Registers/logs in users with JWT.
- Stores exchange API keys encrypted and returns only masked values.
- Uses real public exchange market data in paper mode and records candle provenance so synthetic rows cannot enter RL training.
- Produces BUY, SELL, or WAIT signals based on EMA/RSI/price/volume/rating rules.
- Detects market regimes such as trending, ranging, high volatility, and low liquidity before accepting entries.
- Applies risk checks before opening paper positions.
- Blocks entries when portfolio or single-symbol exposure exceeds configured limits.
- Uses the AI Trade Committee as an optional final entry gate before opening positions.
- Requires a macro-regime plus microstructure gate using order-book imbalance, time-and-sales flow, spread, and short momentum before execution.
- Opens positions with ATR-aware stop/take planning and moves stops to breakeven after configured R-multiple progress.
- Produces a normalized `[-1, 1]` RSI/ATR/SMA market-context vector suitable for Stable Baselines3 observations.
- Caps each new position by both loss budget and configured deposit percentage.
- Switches to `Only Close`, sends a critical Telegram alert, and emergency-closes open positions when portfolio drawdown reaches the configured threshold.
- Takes partial profit at a configured R-multiple, reduces remaining exposure, and lets the rest run with breakeven/trailing logic.
- Sends detailed Russian Telegram reports for worker startup, every configured cycle interval, paper/live entries, risk plan, current PnL, protection changes, partial profit, closing reason, final result, learning update, and worker/exchange errors.
- Persists Telegram notifications in a deduplicated outbox, retries transient delivery failures with exponential backoff, and resumes partially delivered text/photo reports without duplicating the successful part.
- Adds the latest 48 real Binance 1h candles, entry, current price, stop loss, and take profit to each position card when market data is available.
- Records worker heartbeats and sends one alert when a worker becomes stale plus a recovery notice when it resumes; expected workers that never start are reported as `MISSING`, while retired worker rows are ignored through `WORKER_HEARTBEAT_EXPECTED_WORKERS`.
- Uses renewable, owner-token Redis leases for every trading mutation. Automatic cycles, manual scans, ticks, and manual closes share one lock, so an expired old process cannot delete a newer worker's lease or submit a competing order.
- Provides Telegram `/health` diagnostics for PostgreSQL, Redis, Binance market data, the notification queue, and every worker heartbeat.
- Sends one deduplicated daily Telegram portfolio report with a generated JPEG card (18:00 UTC by default), including PnL, positions, learning, workers, and delivery-queue health.
- Returns an execution report for every manual scan: scanned, opened, skipped, and decision reasons.
- Manages open positions through `/api/v1/trading/tick`: current price, floating PnL, stop loss, take profit, trailing stop, and close reasons.
- Stores every execution attempt in `orders`, including status, filled amount, average price, fee, and paper slippage.
- Reconciles local order state through `POST /api/v1/orders/reconcile` and Telegram `/reconcile`.
- Runs strategy backtests through `/api/v1/trading/backtest` using stored candles with volatility-, liquidity-, latency-, fee-, slippage-, and market-impact costs.
- Runs walk-forward backtests through `/api/v1/trading/backtest/walk-forward` to validate optimized parameters on unseen windows.
- Runs Strategy Lab optimization through `/api/v1/strategy-lab/optimize` and stores top strategy configurations.
- Trains multiple seeded PPO candidates with curriculum learning and Bad Experience Replay, validates them chronologically with realistic costs, then requires a profitable virtual forward test before activation.
- Creates structured post-mortems for every loss and distinguishes avoidable behavior from a correctly executed stop.
- Publishes promoted PPO decisions through the shared database; the trading engine uses them only as a veto or risk reducer behind deterministic risk controls.
- Provides safe AI Trade Committee decisions through `/api/v1/agents/analyze`; agents vote, veto weak setups, and audit every decision while deterministic risk checks remain the gate.
- Runs online competition inside the trend and momentum roles: a shadow challenger observes the same setups, learns from closed-trade PnL and confidence calibration, earns a quality rating, and can replace a weaker champion only after at least 20 evaluated outcomes, a 10-point rating advantage, and a seven-day promotion cooldown. Safety veto agents are never displaced by this competition.
- Requires calibrated support from at least two independent directional families. Correlated indicators from one family cannot manufacture consensus. Dedicated data-quality, entry-timing, liquidity, volatility, and microstructure agents retain hard veto authority without voting on direction.
- Adds five independent pre-entry controls: portfolio correlation (including BTC/BNB references), estimated round-trip execution cost, 15m/1h/4h structure, per-symbol/regime confidence drift, and confirmed event risk. They may veto or reduce risk but never invent a BUY/SELL direction. Launchpool/Launchpad alone still changes only BNB analysis priority.
- Shows Russian agent names, detailed Russian rationales, per-agent committee steps, learning outcomes, quality ratings, and champion/challenger/weak-analyst status in the UI and structured logs.
- Supports an optional OpenAI-backed LLM advisor behind `LLM_PROVIDER=openai`; disagreements force WAIT rather than increasing risk.
- Provides panic/resume controls through API and Telegram.
- Sends the completed daily Telegram report after local midnight in `Europe/Simferopol`, with separate `00:00–07:00`, `07:00–14:00`, and `14:00–24:00` sections covering entries, exits, wins/losses, realized PnL, fees, slippage, symbols, and exit reasons.
- Provides deep health checks through `/health/deep`.
- Blocks new entries through a performance guard when recent win rate, loss streak, or total profit falls below thresholds.
- Automatically leaves a performance-guard deadlock after a cooldown by allowing one reduced-risk recovery position; a new loss starts the cooldown again.
- Provides system status, sample backtest metrics, and Telegram test notification API.
- Persists positions, trades, signals, settings, and logs in PostgreSQL and mirrors completed trades into asynchronous SQLite memory.
- Exposes dashboard, market, logs, settings, positions, and trading endpoints.

## Next Production Steps

- Wire `ExchangeClient` to authenticated CCXT clients for Binance and Bybit.
- Accumulate a longer multi-timeframe real-market dataset and monitor model drift across market regimes.
- Expand Strategy Lab with multi-symbol optimization, ML feature selection, and automated model promotion rules.
- Replace Telegram polling with webhook mode if lower latency is needed.
- Add pytest coverage for strategy, risk manager, auth, and trading engine.


Pre-trade futures validation now checks the candidate BUY/SELL direction separately,
uses the same Paper exploration signal function as the trading engine, and evaluates
the selected stop/take/trailing settings instead of a separately optimized strategy.
It reads up to 2160 completed candles and anchors the last out-of-sample window to
the newest completed candle. Historical checks run off the asynchronous worker loop.
Existing risk floors, cooldowns and live/Paper mode controls remain enforced; a lack
of passing candidates does not cause an automatic forced entry. Cycle summaries
prioritize blockers on directional candidates rather than masking them with WAITs.


Trailing protection preserves the initial ATR stop until the favourable move reaches
`TRAILING_ACTIVATION_R` (default 1.0, measured against initial stop distance). The
activation value is recorded with new positions; already tightened stops never widen.
Pre-trade validation uses the same ATR multiplier, reward ratio and activation value.
OHLCV backtests apply closing-based trailing changes from the next candle, fill stop
gaps at the opening price and resolve ambiguous intrabar stop/target hits stop-first.
These tests remain approximations: hourly bars do not reproduce tick paths, partial
fills, funding payments or the full dynamic partial-exit lifecycle.
