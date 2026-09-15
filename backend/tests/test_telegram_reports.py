from datetime import datetime, timedelta, timezone

from app.models.entities import Position
from app.schemas.dto import PositionUpdateOut, TradingDecision, TradingRunOut, TradingTickOut
from app.services.system_health import ComponentHealth, SystemHealthSnapshot, WorkerHealth
from app.services.telegram_daily import DailyPosition, DailyReportSnapshot
from app.services.telegram_reports import (
    format_cycle_report,
    format_daily_report,
    format_position_details,
    format_system_health,
    format_trade_closed,
    format_trade_opened,
    format_worker_heartbeat_event,
    split_telegram_message,
)


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
        breakeven_trigger_r=1.0,
        partial_take_profit_r=1.0,
        partial_close_percent=50.0,
        highest_price=105,
        lowest_price=99,
        status="OPEN",
        entry_context={
            "paper_exploration": True,
            "notional": 200.0,
            "planned_risk": 4.0,
            "planned_reward": 12.0,
            "risk_reward_ratio": 3.0,
            "risk_percent": 0.25,
            "stop_distance_percent": 2.0,
            "take_distance_percent": 6.0,
            "rating": 73,
            "rsi": 58.4,
            "atr_percent": 2.1,
            "regime": "TRENDING_UP",
            "trend_stack": "bullish",
            "macd_direction": "positive",
            "reasons": ["paper exploration from WAIT: bullish_votes=5, bearish_votes=2"],
        },
        entered_at=datetime.now(timezone.utc) - timedelta(minutes=35),
        closed_at=datetime.now(timezone.utc),
    )


def test_open_report_explains_test_entry_and_risk_plan():
    report = format_trade_opened(
        position(),
        score=73,
        reason="risk accepted; paper exploration from WAIT",
        paper_trading=True,
        exploration=True,
    )

    assert "ОТКРЫТА ТЕСТОВАЯ ПОЗИЦИЯ" in report
    assert "BTC/USDT" in report
    assert "<b>Риск-план</b>" in report
    assert "риск ≈ 4.00 USDT" in report
    assert "строгий сигнал был WAIT" in report
    assert "Дальше бот отслеживает" in report
    assert "Risk/Reward: <code>1:3.00</code>" in report
    assert "RSI 58.4" in report
    assert "частичная фиксация 50%" in report
    assert report.count("<b>") == report.count("</b>")
    assert report.count("<code>") == report.count("</code>")


def test_position_details_include_live_result_protection_and_entry_context():
    report = format_position_details(position())

    assert "PnL: +8.00 USDT · +4.00%" in report
    assert "100.0000 → 104.0000" in report
    assert "<b>Контекст входа</b>" in report
    assert "TRENDING_UP" in report


def test_close_report_explains_outcome_and_learning():
    report = format_trade_closed(position(), exit_price=104, reason="TAKE_PROFIT")

    assert "ПОЗИЦИЯ ЗАКРЫТА" in report
    assert "достигнут тейк-профит" in report
    assert "ИТОГ: +8.00 USDT" in report
    assert "+2.00R" in report
    assert "Лучшее движение: <code>+5.00%</code>" in report
    assert "Результат записан в память сделок" in report


def test_cycle_report_contains_every_decision_and_position_update():
    run = TradingRunOut(
        scanned=2,
        opened=1,
        skipped=1,
        decisions=[
            TradingDecision(symbol="BTC/USDT", signal="BUY", score=73, action="OPENED", reason="paper exploration from WAIT"),
            TradingDecision(symbol="ETH/USDT", signal="WAIT", score=61, action="SKIPPED", reason="strategy returned WAIT"),
        ],
    )
    tick = TradingTickOut(
        checked=1,
        closed=0,
        updated=[
            PositionUpdateOut(
                id=42,
                symbol="BTC/USDT",
                side="LONG",
                entry_price=100,
                previous_price=103,
                current_price=104,
                volume=2,
                pnl=8,
                status="OPEN",
                stop=98,
                take=106,
            )
        ],
    )

    report = format_cycle_report(run, tick, paper_trading=True)

    assert "BTC/USDT" in report
    assert "ETH/USDT" in report
    assert "строгий торговый сигнал ещё не сформирован" in report
    assert "PnL <b>+8.00 USDT</b>" in report


def test_long_telegram_report_is_split_without_data_loss():
    text = "\n".join(f"строка {index}: " + "x" * 60 for index in range(200))
    chunks = split_telegram_message(text, limit=500)

    assert len(chunks) > 1
    assert all(len(chunk) <= 500 for chunk in chunks)
    assert "строка 199" in chunks[-1]


def test_worker_heartbeat_alert_is_readable_and_escaped():
    report = format_worker_heartbeat_event(
        kind="STALE",
        worker_name="trader-worker",
        status="ERROR",
        age_seconds=205,
        detail={"error": "HTTP <timeout>"},
        stale_after_seconds=180,
    )

    assert "trader-worker" in report
    assert "3 мин 25 сек" in report
    assert "Порог тревоги" in report
    assert "HTTP &lt;timeout&gt;" in report
    assert report.count("<b>") == report.count("</b>")


def test_system_health_report_contains_dependencies_queue_and_workers():
    now = datetime(2026, 7, 21, 18, tzinfo=timezone.utc)
    snapshot = SystemHealthSnapshot(
        generated_at=now,
        database=ComponentHealth("PostgreSQL", True, 12, "запросы доступны"),
        redis=ComponentHealth("Redis", True, 4, "координация доступна"),
        exchange=ComponentHealth("Binance", False, 12000, "TimeoutError"),
        workers=(WorkerHealth("trader", "OK", 15, True),),
        pending_notifications=2,
        failed_notifications=1,
        trading_paused=False,
        pause_reason=None,
        paper_trading=True,
    )

    report = format_system_health(snapshot)

    assert "ТРЕБУЕТ ВНИМАНИЯ" in report
    assert "PostgreSQL" in report
    assert "Binance" in report
    assert "TimeoutError" in report
    assert "Ожидают отправки: <code>2</code>" in report
    assert "trader" in report
    assert report.count("<b>") == report.count("</b>")


def test_daily_report_is_detailed_and_readable():
    snapshot = DailyReportSnapshot(
        generated_at=datetime(2026, 7, 21, 18, tzinfo=timezone.utc),
        paper_trading=True,
        pnl_day=-3.25,
        pnl_week=8.5,
        total_pnl=42.0,
        open_pnl=-1.25,
        win_rate=55.5,
        trades_count=9,
        closed_today=2,
        positions=(DailyPosition("BTC/USDT", "LONG", -1.25, 118_000),),
        learning_rules=7,
        learning_observations=14,
        active_rl_models=1,
        healthy_workers=4,
        total_workers=5,
        unhealthy_workers=("optimizer",),
        pending_notifications=3,
        failed_notifications=0,
    )

    report = format_daily_report(snapshot)

    assert "ЕЖЕДНЕВНЫЙ ОТЧЁТ" in report
    assert "PnL за день: <b>-3.25 USDT</b>" in report
    assert "BTC/USDT" in report
    assert "Правил из сделок: <code>7</code>" in report
    assert "optimizer" in report
    assert "включает текущий плавающий результат" in report
    assert report.count("<b>") == report.count("</b>")
