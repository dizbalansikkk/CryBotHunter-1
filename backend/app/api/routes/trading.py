import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user
from app.core.config import get_settings
from app.db.session import get_db
from app.models.entities import Position, User, UserSettings
from app.schemas.dto import ActionMessage, BacktestOut, PerformanceGuardOut, SystemStatusOut, TradingRunOut, TradingTickOut, WalkForwardOut
from app.services.backtesting import BacktestingService
from app.services.control import TradingControlService
from app.services.history import HistoricalDataService
from app.services.locks import RedisLockManager, TRADING_CYCLE_LOCK
from app.services.performance_guard import PerformanceGuardService
from app.services.pnl import PnlMetricsService
from app.services.risk_manager import RiskManager, RiskSettings
from app.services.exchange import ExchangeClient, exchange_error_message
from app.services.trading_engine import TradingEngine

router = APIRouter(prefix="/trading", tags=["trading"])
logger = logging.getLogger(__name__)


@router.post("/run-once", response_model=TradingRunOut)
async def run_once(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)) -> TradingRunOut:
    runtime_settings = get_settings()
    paused, _reason = await _trading_control_status()
    if paused:
        return TradingRunOut(scanned=0, opened=0, skipped=0, decisions=[])
    user_settings = (await db.execute(select(UserSettings).where(UserSettings.user_id == user.id))).scalar_one()
    risk_settings = RiskSettings(
        balance=1000,
        risk_percent=user_settings.risk_percent,
        daily_risk_percent=user_settings.daily_risk_percent,
        max_positions=user_settings.max_positions,
        min_rating=user_settings.min_rating,
        stop_loss_percent=user_settings.stop_loss_percent,
        take_profit_percent=user_settings.take_profit_percent,
        trailing_stop_percent=user_settings.trailing_stop_percent,
        atr_stop_multiplier=user_settings.atr_stop_multiplier,
        risk_reward_ratio=user_settings.risk_reward_ratio,
        breakeven_trigger_r=user_settings.breakeven_trigger_r,
        breakeven_offset_percent=user_settings.breakeven_offset_percent,
        partial_take_profit_r=user_settings.partial_take_profit_r,
        partial_close_percent=user_settings.partial_close_percent,
    )
    locks = RedisLockManager()
    try:
        async with locks.lock(TRADING_CYCLE_LOCK, ttl_seconds=55) as acquired:
            if not acquired:
                return TradingRunOut(scanned=0, opened=0, skipped=0, decisions=[])
            exchange = ExchangeClient.from_user_settings(user_settings)
            engine = TradingEngine(exchange)
            try:
                return await engine.run_once(db, risk_settings, timeframe=user_settings.scan_interval)
            except Exception as exc:
                logger.exception("Trading scan failed")
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=exchange_error_message(
                        exc,
                        exchange=user_settings.exchange,
                        market_type=runtime_settings.exchange_default_type,
                        sandbox=runtime_settings.exchange_sandbox_enabled,
                    ),
                ) from exc
            finally:
                await engine.close()
                await exchange.close()
    finally:
        await locks.close()


@router.post("/tick", response_model=TradingTickOut)
async def tick(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)) -> TradingTickOut:
    user_settings = (await db.execute(select(UserSettings).where(UserSettings.user_id == user.id))).scalar_one()
    locks = RedisLockManager()
    try:
        async with locks.lock(TRADING_CYCLE_LOCK, ttl_seconds=55) as acquired:
            if not acquired:
                return TradingTickOut(checked=0, closed=0, updated=[])
            exchange = ExchangeClient.from_user_settings(user_settings)
            engine = TradingEngine(exchange)
            try:
                return await engine.manage_open_positions(db)
            finally:
                await engine.close()
                await exchange.close()
    finally:
        await locks.close()


@router.get("/status", response_model=SystemStatusOut)
async def status(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)) -> SystemStatusOut:
    settings = get_settings()
    paused, panic_reason = await _trading_control_status()
    user_settings = (await db.execute(select(UserSettings).where(UserSettings.user_id == user.id))).scalar_one()
    open_positions = (
        await db.execute(select(func.count()).select_from(Position).where(Position.status == "OPEN"))
    ).scalar_one()
    exposure = (
        await db.execute(
            select(func.coalesce(func.sum(Position.current_price * Position.volume), 0.0)).where(Position.status == "OPEN")
        )
    ).scalar_one()
    pnl = await PnlMetricsService().summary(db)
    gross_exposure = float(exposure)
    exchange_connected = True
    exchange_error = None
    balance = 0.0
    if gross_exposure > 0:
        exchange = ExchangeClient.from_user_settings(user_settings)
        try:
            balance = (await exchange.get_balance()).get("USDT", 0.0)
        except Exception as exc:
            logger.exception("Failed to fetch trading status exchange balance")
            exchange_connected = False
            exchange_error = exchange_error_message(
                exc,
                exchange=user_settings.exchange,
                market_type=settings.exchange_default_type,
                sandbox=settings.exchange_sandbox_enabled,
            )
        finally:
            await exchange.close()
    return SystemStatusOut(
        paper_trading=settings.paper_trading,
        market_data_mode=settings.market_data_mode,
        real_market_data=settings.uses_live_market_data,
        exchange=user_settings.exchange,
        exchange_connected=exchange_connected,
        exchange_error=exchange_error,
        exchange_market_type=settings.exchange_default_type,
        exchange_sandbox_enabled=settings.exchange_sandbox_enabled,
        telegram_enabled=bool(settings.telegram_bot_token),
        telegram_chat_count=len(settings.telegram_allowed_chat_ids),
        open_positions=int(open_positions),
        daily_pnl=pnl.pnl_day,
        panic_paused=paused,
        panic_reason=panic_reason,
        ai_committee_enabled=settings.ai_committee_enabled,
        ai_committee_min_consensus=settings.ai_committee_min_consensus,
        gross_exposure=gross_exposure,
        gross_exposure_percent=RiskManager().exposure_percent(gross_exposure, balance),
        max_gross_exposure_percent=settings.max_gross_exposure_percent,
        max_symbol_exposure_percent=settings.max_symbol_exposure_percent,
    )


@router.post("/panic", response_model=ActionMessage)
async def panic(_: User = Depends(current_user), reason: str = "manual") -> ActionMessage:
    control = TradingControlService()
    try:
        applied = await control.panic(reason)
    finally:
        await control.close()
    if not applied:
        return ActionMessage(ok=False, message="Redis unavailable; panic state could not be persisted. Trading remains fail-closed.")
    return ActionMessage(ok=True, message=f"Trading entry scans paused: {reason}")


@router.post("/resume", response_model=ActionMessage)
async def resume(_: User = Depends(current_user)) -> ActionMessage:
    control = TradingControlService()
    try:
        applied = await control.resume()
    finally:
        await control.close()
    if not applied:
        return ActionMessage(ok=False, message="Redis unavailable; trading remains fail-closed.")
    return ActionMessage(ok=True, message="Trading entry scans resumed")


@router.get("/guard", response_model=PerformanceGuardOut)
async def performance_guard(_: User = Depends(current_user), db: AsyncSession = Depends(get_db)) -> PerformanceGuardOut:
    report = await PerformanceGuardService().evaluate(db)
    return PerformanceGuardOut(**report.__dict__)


@router.get("/backtest/sample", response_model=BacktestOut)
async def sample_backtest(_: User = Depends(current_user)) -> BacktestOut:
    report = BacktestingService().summarize([12, -5, 18, 7, -9, 15, -4, 11, 3, -6])
    return BacktestOut(**report.__dict__)


@router.post("/backtest", response_model=BacktestOut)
async def run_backtest(
    symbol: str = "ETH/USDT",
    timeframe: str = "1h",
    limit: int = 500,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> BacktestOut:
    user_settings = (await db.execute(select(UserSettings).where(UserSettings.user_id == user.id))).scalar_one()
    history = HistoricalDataService(ExchangeClient.from_user_settings(user_settings))
    try:
        candles = await history.load(db, symbol=symbol, timeframe=timeframe, limit=min(limit, 1000))
        if len(candles) < 220:
            await history.ingest(db, symbol=symbol, timeframe=timeframe, limit=min(limit, 1000))
            candles = await history.load(db, symbol=symbol, timeframe=timeframe, limit=min(limit, 1000))
        report = BacktestingService().run(candles)
        return BacktestOut(**report.__dict__)
    finally:
        await history.exchange.close()


@router.post("/backtest/walk-forward", response_model=WalkForwardOut)
async def run_walk_forward_backtest(
    symbol: str = "ETH/USDT",
    timeframe: str = "1h",
    limit: int = 1000,
    train_size: int = 300,
    test_size: int = 120,
    step_size: int = 120,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> WalkForwardOut:
    user_settings = (await db.execute(select(UserSettings).where(UserSettings.user_id == user.id))).scalar_one()
    bounded_limit = max(500, min(limit, 3000))
    train_size = max(220, min(train_size, 1000))
    test_size = max(80, min(test_size, 500))
    step_size = max(40, min(step_size, test_size))
    history = HistoricalDataService(ExchangeClient.from_user_settings(user_settings))
    try:
        candles = await history.load(db, symbol=symbol, timeframe=timeframe, limit=bounded_limit)
        if len(candles) < train_size + test_size:
            await history.ingest(db, symbol=symbol, timeframe=timeframe, limit=bounded_limit)
            candles = await history.load(db, symbol=symbol, timeframe=timeframe, limit=bounded_limit)
        report = BacktestingService().walk_forward(
            candles,
            train_size=train_size,
            test_size=test_size,
            step_size=step_size,
        )
        return WalkForwardOut(**report.__dict__)
    finally:
        await history.exchange.close()


async def _trading_control_status() -> tuple[bool, str | None]:
    control = TradingControlService()
    try:
        return await control.is_paused()
    finally:
        await control.close()
