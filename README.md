# Crypto AI Trader

MVP scaffold for an automated crypto trading system with FastAPI, PostgreSQL, Redis, React, Docker, market scoring, strategy evaluation, risk checks, paper trading, encrypted exchange credentials, logs, and ML/backtesting extension points.

## Stack

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
AI_COMMITTEE_MIN_CONSENSUS=0.66
MAX_GROSS_EXPOSURE_PERCENT=300
MAX_SYMBOL_EXPOSURE_PERCENT=100
TRADING_EXCLUDED_SYMBOLS=BTC/USDT
CANDLE_INGEST_SYMBOLS=ETH/USDT,BNB/USDT,SOL/USDT,XRP/USDT,ADA/USDT,DOGE/USDT,LINK/USDT,AVAX/USDT,DOT/USDT,LTC/USDT,TRX/USDT,AAVE/USDT,UNI/USDT,NEAR/USDT,FET/USDT,ONDO/USDT
MARKET_SCAN_SYMBOLS=ETH/USDT,BNB/USDT,SOL/USDT,XRP/USDT,ADA/USDT,DOGE/USDT,LINK/USDT,AVAX/USDT,DOT/USDT,LTC/USDT,TRX/USDT,AAVE/USDT,UNI/USDT,NEAR/USDT,FET/USDT,ONDO/USDT
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
PAPER_EXPLORATION_ENABLED=true
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
AI_COMMITTEE_MIN_CONSENSUS=0.66
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
TRADING_EXCLUDED_SYMBOLS=BTC/USDT
CANDLE_INGEST_SYMBOLS=ETH/USDT,BNB/USDT,SOL/USDT,XRP/USDT,ADA/USDT,DOGE/USDT,LINK/USDT,AVAX/USDT,DOT/USDT,LTC/USDT,TRX/USDT,AAVE/USDT,UNI/USDT,NEAR/USDT,FET/USDT,ONDO/USDT
MARKET_SCAN_SYMBOLS=ETH/USDT,BNB/USDT,SOL/USDT,XRP/USDT,ADA/USDT,DOGE/USDT,LINK/USDT,AVAX/USDT,DOT/USDT,LTC/USDT,TRX/USDT,AAVE/USDT,UNI/USDT,NEAR/USDT,FET/USDT,ONDO/USDT
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

`PAPER_EXPLORATION_ENABLED=true` enables a separate paper-only learning lane when the strict strategy returns `WAIT`. A candidate must clear the score threshold, a decisive indicator vote, market-quality, walk-forward, RL, cooldown, exposure, daily-loss, and drawdown gates. The lane can keep learning while the regular performance guard is cooling down, but it has independent recovery slots, obeys the configured `PAPER_EXPLORATION_MAX_PER_CYCLE` cap, and is hard-capped by `PAPER_EXPLORATION_MAX_RISK_PERCENT` even when an older deployment variable requests more risk. Every candidate and final allow/block result is recorded as `PaperLearningScout` and `PaperLearningRiskGate` agent activity. Exploratory outcomes update learning memory but do not distort the regular-strategy performance guard.

For exchange testnet execution, set `PAPER_TRADING=false`, `LIVE_TRADING_ENABLED=true`, and keep `EXCHANGE_SANDBOX_ENABLED=true`. Keep `ALLOW_LIVE_TRADING_WITHOUT_SANDBOX=false` until live execution is reviewed, tested, and deliberately approved.

For a live `trader-worker`, save the exchange credentials through the web settings. The worker decrypts those credentials for both its startup pre-flight and its trading loop, so Railway does not need a duplicate copy. The private pre-flight is enabled automatically in live mode. `API_KEY` and `API_SECRET` remain supported as deployment-level fallbacks when no credentials have been saved in the database:

```env
SAFETY_REQUIRE_API_CREDENTIALS=true
SAFETY_VALIDATE_PRIVATE_API=true
```

Do not add exchange secrets to the frontend or RL worker. In paper mode the pre-flight reads real public Binance time and `ETH/USDT` ticker data, but orders and balances remain virtual. `TRADING_EXCLUDED_SYMBOLS=BTC/USDT` removes Bitcoin from scans, candle ingestion, RL, shadow decisions, and all new entries while existing positions remain managed until their normal exit. In live mode pre-flight logs a prominent warning, requires both credentials, and validates them with `fetch_balance`. Invalid environment values, malformed exchange data, authentication failures, or exhausted network retries terminate the worker with exit code `1`, so Railway cannot start trading in a partially configured state. `SIGTERM` and `SIGINT` stop future loop iterations, interrupt PPO training through a Stable Baselines3 callback, and close tracked CCXT clients.

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
TRADING_EXCLUDED_SYMBOLS=BTC/USDT
CANDLE_INGEST_SYMBOLS=ETH/USDT,BNB/USDT,SOL/USDT,XRP/USDT,ADA/USDT,DOGE/USDT,LINK/USDT,AVAX/USDT,DOT/USDT,LTC/USDT,TRX/USDT,AAVE/USDT,UNI/USDT,NEAR/USDT,FET/USDT,ONDO/USDT
MARKET_SCAN_SYMBOLS=ETH/USDT,BNB/USDT,SOL/USDT,XRP/USDT,ADA/USDT,DOGE/USDT,LINK/USDT,AVAX/USDT,DOT/USDT,LTC/USDT,TRX/USDT,AAVE/USDT,UNI/USDT,NEAR/USDT,FET/USDT,ONDO/USDT
MARKET_SCAN_CONCURRENCY=3
CANDLE_INGEST_TIMEFRAMES=1h
RL_TRAINER_ENABLED=true
RL_GATE_ENABLED=true
RL_SYMBOLS=ETH/USDT,BNB/USDT,SOL/USDT,XRP/USDT,ADA/USDT,DOGE/USDT,LINK/USDT,AVAX/USDT,DOT/USDT,LTC/USDT,TRX/USDT,AAVE/USDT,UNI/USDT,NEAR/USDT,FET/USDT,ONDO/USDT
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

The RL service needs no Binance API key because OHLCV is public. Set its Railway Config File to `/backend/railway.rl.toml`; this selects `Dockerfile.rl`. Deploy it in the same Railway region that can reach Binance. Stable Baselines3 and CPU-only PyTorch are installed only by `Dockerfile.rl`; the web, trader, and Telegram images remain smaller.

PPO training runs outside the asyncio event loop, so `rl-worker` keeps publishing heartbeat updates while PyTorch is busy. `RL_SYMBOLS` defines the RL universe independently from the market scanner, while `RL_TRAINING_MAX_PER_CYCLE` limits heavy training attempts and lets missing pairs enter the queue gradually. Worker status reports expose the current pair, progress, active and shadow decisions, and deferred training totals.

Every new model starts as `SHADOW`, even after it passes chronological validation. Backtest promotion eligibility still requires benchmark edge, seed stability, return, profit factor, trade count, and drawdown gates, but actual `ACTIVE` promotion now additionally requires virtual forward trades to pass PnL, win-rate, profit-factor, and drawdown thresholds. Shadow trades include simulated fees, slippage, and market impact and never create exchange orders. Only a promoted `rl_policy` decision can block or confirm a real/paper strategy entry. The dashboard reports active pair coverage separately from experiment history and shows shadow forward PnL, trades, and promotion state.

RL training uses curriculum stages when clean contiguous trend and normal-volatility windows are available, then finishes on the complete market history. The reward function separates financial outcome from behavior: it penalizes churn, giving back an established unrealized edge, and holding through repeated adverse confirmation; it gives a small credit for strategy-aligned actions and disciplined invalidation exits. Loss post-mortems are replay-weighted in later training, capped by `BAD_REPLAY_MAX_WEIGHT` so one mistake cannot dominate the whole dataset.

Entry execution uses a two-level gatekeeper. The macro gate enforces market regime and direction; the micro gate reads public order-book depth, recent trades, spread, 10-minute momentum, and a clearly labelled iceberg proxy. Strong opposing book+tape flow blocks the entry, neutral or unavailable micro data reduces risk, and supportive consensus keeps normal risk. Configure it with `ENTRY_MICROSTRUCTURE_*`; `AI_COMMITTEE_MIN_CONSENSUS` defaults to `0.75`.

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
- Can open low-risk, clearly labelled exploratory paper positions from strong neutral setups so the learning loop can collect closed-trade outcomes without placing real orders.
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
- Supports an optional OpenAI-backed LLM advisor behind `LLM_PROVIDER=openai`; disagreements force WAIT rather than increasing risk.
- Provides panic/resume controls through API and Telegram.
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
