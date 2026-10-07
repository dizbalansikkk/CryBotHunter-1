import json
from datetime import datetime, timezone

import httpx
import pytest

from app.schemas.dto import MarketCoin, StrategySignal
from app.services.binance_events import BinanceEventAssessment, BinanceEventPriorityService
from app.services.risk_manager import RiskManager, RiskSettings
from app.services.trading_engine import TradingEngine


def coin(**overrides) -> MarketCoin:
    values = {
        "symbol": "BNB/USDT",
        "price": 600,
        "volume_24h": 180_000_000,
        "volume_average_24h": 100_000_000,
        "price_change_percent": 6,
        "atr": 30,
        "rsi": 58,
        "ema20": 590,
        "ema50": 580,
        "ema200": 550,
        "macd": 2,
        "funding_rate": 0,
        "open_interest": 0,
        "bid": 599.9,
        "ask": 600.1,
        "spread_bps": 3.33,
        "rating": 74,
        "regime": "TRENDING",
        "regime_score": 80,
    }
    values.update(overrides)
    return MarketCoin(**values)


def announcement_client(title: str, text: str, *, fail: bool = False) -> httpx.AsyncClient:
    async def handler(request: httpx.Request) -> httpx.Response:
        if fail:
            return httpx.Response(503, request=request)
        if request.url.path.endswith("/article/list/query"):
            page = int(request.url.params.get("pageNo", "1"))
            articles = [] if page > 1 else [{
                "code": "event-code",
                "title": title,
                "releaseDate": int(datetime(2026, 10, 4, tzinfo=timezone.utc).timestamp() * 1000),
            }]
            return httpx.Response(
                200,
                request=request,
                json={"code": "000000", "data": {"catalogs": [{"articles": articles}] }},
            )
        body = json.dumps({"node": "root", "child": [{"node": "text", "text": text}]})
        return httpx.Response(200, request=request, json={"code": "000000", "data": {"body": body}})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_active_launchpool_is_confirmed_only_when_bnb_and_dates_are_explicit():
    client = announcement_client(
        "Introducing TEST on Binance Launchpool! Farm TEST by Locking BNB and USDC",
        "Users can lock BNB. Farming Period: 2026-10-05 00:00 (UTC) to 2026-10-06 23:59 (UTC)",
    )
    try:
        result = await BinanceEventPriorityService(client).assess(datetime(2026, 10, 5, 12, tzinfo=timezone.utc))
    finally:
        await client.aclose()

    assert result.status == "ACTIVE"
    assert result.event_type == "LAUNCHPOOL"
    assert result.bnb_required is True
    assert result.event_score == 15
    assert result.time_to_end_seconds == 35 * 3600 + 59 * 60


@pytest.mark.asyncio
async def test_unrelated_launchpool_does_not_raise_bnb_priority():
    client = announcement_client(
        "Introducing TEST on Binance Launchpool! Farm TEST by Locking USDC",
        "Users can lock USDC. Farming Period: 2026-10-05 00:00 (UTC) to 2026-10-06 23:59 (UTC)",
    )
    try:
        result = await BinanceEventPriorityService(client).assess(datetime(2026, 10, 5, 12, tzinfo=timezone.utc))
    finally:
        await client.aclose()

    assert result.status == "NORMAL"
    assert result.event_score == 0


@pytest.mark.asyncio
async def test_unavailable_official_data_fails_closed_to_unknown():
    client = announcement_client("unused", "unused", fail=True)
    try:
        result = await BinanceEventPriorityService(client).assess(datetime(2026, 10, 5, 12, tzinfo=timezone.utc))
    finally:
        await client.aclose()

    assert result.status == "UNKNOWN"
    assert result.event_score == 0


def test_high_impact_requires_multiple_primary_market_confirmations():
    service = BinanceEventPriorityService()
    assessment = BinanceEventAssessment(
        status="ACTIVE",
        event_type="LAUNCHPOOL",
        bnb_required=True,
        event_score=15,
    )

    result = service.priority_decision(assessment, coin(), strategy_score=68)

    assert result.assessment.status == "ACTIVE_HIGH_IMPACT"
    assert result.assessment.event_score == 20
    assert result.final_score == result.strategy_score + result.market_score + 20


def test_event_bonus_changes_rank_but_never_strategy_gate_or_direction():
    service = BinanceEventPriorityService()
    assessment = BinanceEventAssessment(
        status="ACTIVE_HIGH_IMPACT",
        event_type="LAUNCHPOOL",
        bnb_required=True,
        event_score=20,
    )
    bnb = coin()
    weak = StrategySignal(symbol="BNB/USDT", signal="BUY", score=35, reasons=[])
    priority = service.priority_decision(assessment, bnb, weak.score)
    engine = TradingEngine(event_priority=service)

    accepted, reason = RiskManager().can_open(
        weak,
        RiskSettings(
            balance=1000,
            risk_percent=1,
            daily_risk_percent=5,
            max_positions=3,
            min_rating=60,
            stop_loss_percent=2,
            take_profit_percent=4,
            trailing_stop_percent=1,
        ),
        open_positions_count=0,
        daily_pnl=0,
    )

    assert priority.final_score > weak.score
    assert engine._opportunity_rank(bnb, weak, priority)[1] == engine._expected_net_r(bnb, weak)
    assert engine._opportunity_rank(bnb, weak, priority)[2] == priority.final_score
    assert accepted is False
    assert reason == "signal score below minimum rating"
    assert weak.signal == "BUY"
