from contextlib import asynccontextmanager
import time

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address
from starlette.responses import JSONResponse
from sqlalchemy import text
from redis.asyncio import Redis

from app.api.router import api_router
from app.core.config import get_settings
from app.db.session import AsyncSessionLocal
from app.services.control import TradingControlService
from app.services.scheduler import create_scheduler

settings = get_settings()
limiter = Limiter(key_func=get_remote_address, default_limits=["120/minute"])
scheduler = create_scheduler()


@asynccontextmanager
async def lifespan(_: FastAPI):
    scheduler.start()
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.state.limiter = limiter
app.add_middleware(SlowAPIMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(_: Request, exc: RateLimitExceeded) -> JSONResponse:
    return JSONResponse(status_code=429, content={"detail": str(exc)})


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/deep")
async def deep_health() -> dict[str, object]:
    checks: dict[str, object] = {"status": "ok"}
    try:
        async with AsyncSessionLocal() as db:
            await db.execute(text("select 1"))
        checks["database"] = "ok"
    except Exception as exc:
        checks["status"] = "degraded"
        checks["database"] = f"error: {exc.__class__.__name__}"
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        await redis.ping()
        checks["redis"] = "ok"
        if settings.scalping_enabled:
            from app.services.market_stream import PREFIX
            stamps = await redis.mget([PREFIX + 'health:book', PREFIX + 'health:tape'])
            checks["scalping_streams"] = {kind + "_age_seconds": round(time.time()-float(stamp), 2) if stamp else None
                for kind, stamp in zip(("book", "tape"), stamps)}
    except Exception as exc:
        checks["status"] = "degraded"
        checks["redis"] = f"error: {exc.__class__.__name__}"
    finally:
        await redis.aclose()
    control = TradingControlService()
    try:
        paused, reason = await control.is_paused()
    finally:
        await control.close()
    checks["panic_paused"] = paused
    checks["panic_reason"] = reason
    checks["paper_trading"] = settings.paper_trading
    checks["live_trading_enabled"] = settings.live_trading_enabled
    checks["exchange_sandbox_enabled"] = settings.exchange_sandbox_enabled
    checks["allow_live_trading_without_sandbox"] = settings.allow_live_trading_without_sandbox
    checks["scalping_enabled"] = settings.scalping_enabled
    checks["atr_risk_policy_enabled"] = settings.atr_risk_policy_enabled and not settings.scalping_enabled
    checks["experimental_exit_limits_enabled"] = settings.experimental_exit_limits_enabled and not settings.atr_risk_policy_enabled and not settings.scalping_enabled
    if settings.scalping_enabled:
        checks["risk_policy"] = {"mode": "PAPER_ORDER_BOOK_TAPE", "risk_percent": settings.scalping_risk_percent,
            "stop_bps": settings.scalping_stop_bps, "daily_loss_percent": 2.5,
            "loop_seconds": settings.scalping_loop_seconds, "timeout_seconds": settings.scalping_timeout_seconds,
            "max_hold_seconds": settings.scalping_max_hold_seconds, "live_entries_allowed": False,
            "entry": "SIMULATED_POST_ONLY_QUEUE", "hourly_rl_votes": False}
    elif settings.atr_risk_policy_enabled:
        checks["risk_policy"] = {"risk_percent": 1, "atr_stop_multiplier": 2,
            "atr_take_multiplier": 5, "daily_loss_percent": 2.5,
            "day_timezone": settings.trading_day_timezone, "live_entries_allowed": False}
    if settings.experimental_exit_limits_enabled and not settings.atr_risk_policy_enabled and not settings.scalping_enabled:
        from app.services.exit_experiment import MAX_LOSS, TARGET_PROFIT
        checks["exit_experiment"] = {"max_loss_usdt": MAX_LOSS, "target_profit_usdt": TARGET_PROFIT,
            "live_entries_allowed": False, "protection": "PAPER_LOCAL_SL_TP" if settings.paper_trading else "LIVE_BLOCKED"}
    checks["market_data_mode"] = settings.market_data_mode
    checks["llm_provider"] = settings.llm_provider
    checks["agent_learning_policy"] = {"trust_window": 50, "reward_version": 3,
        "feature_schema_version": 3, "validation": "PURGED_WALK_FORWARD_90D_30D",
        "liquidation_zone_provider_configured": False,
        "new_rl_training": "BLOCKED_UNTIL_POINT_IN_TIME_FEATURE_COVERAGE"}
    if settings.scalping_enabled:
        checks["scalping_learning_policy"] = {"reward_version": 2, "offline_only": True,
            "requires_complete_queue_tape_replay": True, "phase_1_min_sharpe": settings.scalp_training_min_sharpe,
            "out_of_sample_net_expectancy_required": True, "walk_forward_folds": 2,
            "cash_reward": "TOXIC_FLAT_ONLY", "automatic_execution_authority": False}
    return checks


app.include_router(api_router, prefix=settings.api_prefix)
