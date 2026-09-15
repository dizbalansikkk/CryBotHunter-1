from datetime import datetime, timedelta, timezone
from io import BytesIO

from PIL import Image

from app.models.entities import Position
from app.schemas.dto import TradingDecision, TradingRunOut, TradingTickOut
from app.services.telegram_cards import (
    CARD_SIZE,
    render_cycle_card,
    render_daily_report_card,
    render_position_card,
)
from app.services.telegram_daily import DailyPosition, DailyReportSnapshot


def position() -> Position:
    return Position(
        id=42,
        symbol="BTC/USDT",
        side="LONG",
        entry_price=100,
        current_price=104,
        volume=2,
        stop=98,
        take=106,
        initial_risk=2,
        trailing_stop_percent=0.8,
        pnl=8,
        status="OPEN",
        entry_context={
            "paper_exploration": True,
            "notional": 200.0,
            "risk_reward_ratio": 3.0,
        },
        entered_at=datetime.now(timezone.utc) - timedelta(minutes=35),
    )


def assert_valid_card(payload: bytes) -> None:
    assert len(payload) > 30_000
    with Image.open(BytesIO(payload)) as image:
        assert image.format == "JPEG"
        assert image.size == CARD_SIZE
        assert image.mode == "RGB"


def test_position_card_is_rendered_as_telegram_ready_jpeg():
    payload = render_position_card(position(), event="OPENED", score=73)

    assert_valid_card(payload)


def test_position_card_renders_real_candlestick_chart():
    candles = []
    for index in range(48):
        open_price = 99.0 + index * 0.1
        close_price = open_price + (0.35 if index % 3 else -0.2)
        candles.append(
            [
                1_700_000_000_000 + index * 3_600_000,
                open_price,
                max(open_price, close_price) + 0.25,
                min(open_price, close_price) - 0.2,
                close_price,
                1000 + index,
            ]
        )

    plain = render_position_card(position(), event="OPENED", score=73)
    chart = render_position_card(position(), event="OPENED", score=73, candles=candles)

    assert_valid_card(chart)
    assert chart != plain


def test_cycle_card_is_rendered_as_telegram_ready_jpeg():
    run = TradingRunOut(
        scanned=5,
        opened=1,
        skipped=4,
        decisions=[
            TradingDecision(
                symbol="BTC/USDT",
                signal="BUY",
                score=73,
                action="OPENED",
                reason="risk accepted",
            )
        ],
    )
    tick = TradingTickOut(checked=3, closed=0, updated=[])

    payload = render_cycle_card(run, tick, paper_trading=True)

    assert_valid_card(payload)


def test_daily_report_card_is_rendered_as_telegram_ready_jpeg():
    snapshot = DailyReportSnapshot(
        generated_at=datetime(2026, 7, 21, 18, tzinfo=timezone.utc),
        paper_trading=True,
        pnl_day=12.5,
        pnl_week=31.25,
        total_pnl=105.75,
        open_pnl=4.5,
        win_rate=62.5,
        trades_count=16,
        closed_today=2,
        positions=(DailyPosition("BTC/USDT", "LONG", 4.5, 118_000),),
        learning_rules=8,
        learning_observations=16,
        active_rl_models=2,
        healthy_workers=5,
        total_workers=5,
        unhealthy_workers=(),
        pending_notifications=0,
        failed_notifications=0,
    )

    payload = render_daily_report_card(snapshot)

    assert_valid_card(payload)
