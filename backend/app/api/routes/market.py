from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user
from app.db.session import get_db
from app.models.entities import User, UserSettings
from app.core.config import get_settings
from app.schemas.dto import HistoryBatchIngestOut, HistoryIngestOut, HistoryReadinessOut, MarketCoin, MlPrediction, StrategySignal
from app.services.history import HistoricalDataService
from app.services.exchange import ExchangeClient
from app.services.market_scanner import MarketScanner
from app.services.ml import MlSignalService
from app.services.strategy import StrategyCore

router = APIRouter(prefix="/market", tags=["market"])


@router.get("/scan", response_model=list[MarketCoin])
async def scan_market(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)) -> list[MarketCoin]:
    user_settings = (await db.execute(select(UserSettings).where(UserSettings.user_id == user.id))).scalar_one()
    exchange = ExchangeClient.from_user_settings(user_settings)
    try:
        return await MarketScanner(exchange).scan()
    finally:
        await exchange.close()


@router.get("/signals", response_model=list[StrategySignal])
async def signals(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)) -> list[StrategySignal]:
    user_settings = (await db.execute(select(UserSettings).where(UserSettings.user_id == user.id))).scalar_one()
    exchange = ExchangeClient.from_user_settings(user_settings)
    try:
        coins = await MarketScanner(exchange).scan()
    finally:
        await exchange.close()
    strategy = StrategyCore()
    return [strategy.evaluate(coin) for coin in coins]


@router.get("/ml", response_model=list[MlPrediction])
async def ml_predictions(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)) -> list[MlPrediction]:
    user_settings = (await db.execute(select(UserSettings).where(UserSettings.user_id == user.id))).scalar_one()
    exchange = ExchangeClient.from_user_settings(user_settings)
    try:
        coins = await MarketScanner(exchange).scan()
    finally:
        await exchange.close()
    service = MlSignalService()
    return [service.predict(coin) for coin in coins]


@router.post("/history/ingest", response_model=HistoryIngestOut)
async def ingest_history(
    symbol: str = "ETH/USDT",
    timeframe: str = "1h",
    limit: int = 500,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> HistoryIngestOut:
    user_settings = (await db.execute(select(UserSettings).where(UserSettings.user_id == user.id))).scalar_one()
    exchange = ExchangeClient.from_user_settings(user_settings)
    try:
        inserted = await HistoricalDataService(exchange).ingest(
            db,
            symbol=symbol,
            timeframe=timeframe,
            limit=min(limit, 1000),
        )
        return HistoryIngestOut(symbol=symbol, timeframe=timeframe, inserted=inserted)
    finally:
        await exchange.close()


@router.post("/history/ingest/batch", response_model=HistoryBatchIngestOut)
async def ingest_history_batch(
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> HistoryBatchIngestOut:
    settings = get_settings()
    user_settings = (await db.execute(select(UserSettings).where(UserSettings.user_id == user.id))).scalar_one()
    exchange = ExchangeClient.from_user_settings(user_settings)
    try:
        inserted = await HistoricalDataService(exchange).ingest_many(
            db,
            symbols=settings.candle_ingest_symbols,
            timeframes=settings.candle_ingest_timeframes,
            limit=min(settings.candle_ingest_limit, 1000),
        )
        return HistoryBatchIngestOut(inserted=inserted)
    finally:
        await exchange.close()


@router.get("/history/readiness", response_model=list[HistoryReadinessOut])
async def history_readiness(
    _: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> list[HistoryReadinessOut]:
    settings = get_settings()
    rows = await HistoricalDataService().readiness(
        db,
        symbols=settings.candle_ingest_symbols,
        timeframes=settings.candle_ingest_timeframes,
        target=settings.candle_dataset_target,
    )
    return [HistoryReadinessOut(**row) for row in rows]
