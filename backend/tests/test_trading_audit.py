from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.services.trading_audit import INSUFFICIENT, TradingAuditService


UTC = timezone.utc


def position(identifier: int, closed_at: datetime, pnl: float, symbol: str = "ETH/USDT", **context):
    entered_at = closed_at - timedelta(minutes=40)
    return SimpleNamespace(
        id=identifier,
        symbol=symbol,
        side="LONG",
        pnl=pnl,
        status="CLOSED",
        entered_at=entered_at,
        closed_at=closed_at,
        entry_price=100.0,
        stop=98.0,
        volume=2.0,
        entry_context={"balance": 1000.0, "risk_percent": 1.0, "planned_risk": 4.0, "notional": 200.0, **context},
    )


def test_audit_returns_all_30_complete_calendar_days_and_daily_metrics():
    service = TradingAuditService()
    now = datetime(2026, 9, 30, 12, tzinfo=UTC)
    # The report ends at 2026-09-30 00:00 Simferopol (UTC+3).
    first = datetime(2026, 8, 31, 10, tzinfo=UTC)
    records = [
        position(1, first, 10.0),
        position(2, first + timedelta(hours=1), -4.0),
        position(3, first + timedelta(days=1), 2.0, "SOL/USDT"),
    ]
    report = service.build(records, [], records, SimpleNamespace(risk_percent=1, daily_risk_percent=3, max_positions=3, partial_take_profit_r=1, partial_close_percent=50, trailing_stop_percent=0.8, atr_stop_multiplier=1.5, stop_loss_percent=1.5), now=now)

    assert report["period"]["start"] == "2026-08-31 00"
    assert report["period"]["end_exclusive"] == "2026-09-30 00"
    assert len(report["daily_results"]) == 30
    assert report["daily_results"][0]["trades"] == 2
    assert report["daily_results"][0]["gross_profit"] == 10.0
    assert report["daily_results"][0]["gross_loss"] == 4.0
    assert report["daily_results"][0]["net_pnl"] == 6.0
    assert report["daily_results"][0]["max_drawdown"] == 4.0
    assert report["total"]["net_pnl"] == 8.0
    assert report["by_symbol"][0]["symbol"] == "ETH/USDT"


def test_audit_does_not_invent_missing_post_mortems_or_release_history():
    service = TradingAuditService()
    report = service.build([], [], [], None, now=datetime(2026, 9, 30, 12, tzinfo=UTC))

    assert report["before_after_changes"]["status"] == "MISSING_DEPLOYMENT_HISTORY"
    assert INSUFFICIENT in report["before_after_changes"]["message"]
    assert INSUFFICIENT in report["loss_causes"]["note"]
    assert report["total"]["trades"] == 0
