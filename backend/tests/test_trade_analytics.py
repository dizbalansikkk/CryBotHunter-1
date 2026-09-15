from datetime import datetime, timedelta, timezone

from app.models.entities import Position, PositionStatus
from app.services.trade_analytics import TradeAnalyticsService


def position(
    position_id: int,
    symbol: str,
    pnl: float,
    *,
    status: str = PositionStatus.CLOSED.value,
    minutes: int = 60,
    context: dict | None = None,
) -> Position:
    entered_at = datetime(2026, 7, 1, tzinfo=timezone.utc) + timedelta(hours=position_id)
    return Position(
        id=position_id,
        symbol=symbol,
        side="LONG",
        entry_price=100,
        current_price=105 if pnl >= 0 else 95,
        volume=1,
        stop=95,
        take=110,
        pnl=pnl,
        status=status,
        exit_reason="TAKE_PROFIT" if pnl > 0 else "STOP_LOSS",
        entered_at=entered_at,
        closed_at=entered_at + timedelta(minutes=minutes) if status == PositionStatus.CLOSED.value else None,
        entry_context=context or {},
    )


def test_trade_analytics_reports_lifetime_quality_and_symbol_breakdown():
    service = TradeAnalyticsService()
    positions = [
        position(1, "BTC/USDT", 10),
        position(2, "BTC/USDT", -5),
        position(3, "ETH/USDT", 5),
        position(4, "SOL/USDT", -2, status=PositionStatus.OPEN.value),
    ]

    summary = service.summarize_positions(positions)

    assert summary.closed_trades == 3
    assert summary.open_positions == 1
    assert summary.wins == 2
    assert summary.losses == 1
    assert summary.win_rate == 66.67
    assert summary.total_realized_pnl == 10
    assert summary.open_pnl == -2
    assert summary.net_pnl == 8
    assert summary.profit_factor == 3
    assert summary.expectancy == 3.3333
    assert summary.max_win_streak == 1
    assert summary.max_loss_streak == 1
    assert summary.by_symbol[0].symbol == "BTC/USDT"
    assert summary.by_symbol[0].trades == 2


def test_trade_history_explains_confidence_risk_and_entry_reason():
    context = {
        "notional": 1000,
        "committee_confidence": 0.84,
        "committee_consensus": 0.8,
        "signal_score": 91,
        "risk_percent": 0.5,
        "risk_reward_ratio": 2.2,
        "reasons": ["trend aligned", "momentum confirmed"],
        "decision_reason": "all gates passed",
    }

    summary = TradeAnalyticsService().summarize_positions([position(7, "BTC/USDT", 12, context=context)])
    item = summary.recent_trades[0]

    assert item.confidence == 0.84
    assert item.consensus_score == 0.8
    assert item.signal_score == 91
    assert item.risk_percent == 0.5
    assert item.return_percent == 1.2
    assert item.duration_minutes == 60
    assert item.entry_reasons == ["trend aligned", "momentum confirmed"]


def test_profit_factor_is_infinite_marker_when_there_are_only_wins():
    summary = TradeAnalyticsService().summarize_positions([position(1, "BTC/USDT", 3)])

    assert summary.profit_factor is None
