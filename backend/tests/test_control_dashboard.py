from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as Row

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models.entities import LogEntry, LearningRule, StrategyRelease, RlModel
from app.services.control_dashboard import ControlDashboardService, position_economics
from app.services.log_retention import cleanup_technical_logs
from app.services.model_comparison import compare_trades


@pytest.fixture
def database():
    engine = create_engine("sqlite:///:memory:")
    for model in (LogEntry, LearningRule, StrategyRelease, RlModel):
        model.__table__.create(engine)
    with Session(engine) as session:
        class Bridge:
            async def execute(self, statement):
                return session.execute(statement)
        yield session, Bridge()
    engine.dispose()


def test_entry_amount_uses_initial_fill_not_remaining_volume():
    position = Row(id=1, symbol="ETH/USDT", side="LONG", status="OPEN", entered_at=None,
        closed_at=None, entry_price=100, volume=0.2, pnl=3,
        entry_context={"entry_execution": {"volume": 1, "fee": 0.04,
            "venue_setup": {"leverage": 5, "margin_mode": "isolated"}}, "exit_plan": {"initial_risk": 2}})
    result = position_economics(position)
    assert result["notional"] == 100
    assert result["estimated_margin"] == 20
    assert result["initial_stop_risk"] == 2
    position.entry_context = {}
    assert position_economics(position)["notional"] is None
    assert position_economics(position)["entry_fee"] is None


@pytest.mark.asyncio
async def test_latest_entry_per_symbol_and_window(database):
    session, bridge = database
    now = datetime.now(timezone.utc)
    for symbol, event, at in [("ETH/USDT", "ENTRY_SKIPPED", now),
        ("ETH/USDT", "ENTRY_OPENED", now), ("SOL/USDT", "ENTRY_REJECTED", now),
        ("OLD/USDT", "ENTRY_SKIPPED", now - timedelta(days=2))]:
        session.add(LogEntry(level="INFO", message="decision", created_at=at,
            context={"symbol": symbol, "event": event, "secret_unrelated": "not exposed"}))
    session.flush()
    result = await ControlDashboardService().entries(bridge)
    assert len(result["entries"]) == 2
    eth = next(r for r in result["entries"] if r["symbol"] == "ETH/USDT")
    assert eth["event"] == "ENTRY_OPENED"
    assert "secret_unrelated" not in eth


@pytest.mark.asyncio
async def test_cleanup_preserves_financial_audit_and_errors(database, monkeypatch):
    from app.services import log_retention
    monkeypatch.setattr(log_retention, "get_settings", lambda: Row(technical_log_cleanup_enabled=True, log_retention_days=90))
    session, bridge = database
    old = datetime.now(timezone.utc) - timedelta(days=100)
    session.add_all([
        LogEntry(level="INFO", message="Торговый цикл: summary", context={}, created_at=old),
        LogEntry(level="INFO", message="Торговый цикл: preserve audit", context={"event": "ENTRY_OPENED"}, created_at=old),
        LogEntry(level="ERROR", message="Торговый цикл: failure", context={}, created_at=old),
        LogEntry(level="INFO", message="Learning updated", context={}, created_at=old),
        LogEntry(level="INFO", message="Торговый цикл: recent", context={}, created_at=datetime.now(timezone.utc)),
    ])
    session.flush()
    assert await cleanup_technical_logs(bridge) == 1
    assert len(session.execute(select(LogEntry)).scalars().all()) == 4


@pytest.mark.asyncio
async def test_learning_uses_recorded_model_metrics_and_scope(database):
    session, bridge = database
    session.add(LearningRule(scope="GLOBAL", side="SHORT", feature_key="regime", feature_value="trend"))
    session.add(RlModel(symbol="ETH/USDT", timeframe="1h", metrics={"backtest_passed": True,
        "trades": 12, "forward": {"closed_trades": 3}, "unrelated": "hidden"}))
    session.flush()
    result = await ControlDashboardService().learning(bridge)
    assert result["rules"][0]["symbol"] == "GLOBAL"
    assert result["models"][0]["historical"] == {"backtest_passed": True, "trades": 12}
    assert result["models"][0]["forward"]["closed_trades"] == 3


def samples(pnl, count=24):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [Row(id=i, entered_at=start + timedelta(hours=12*i), closed_at=start + timedelta(hours=12*i+1),
                pnl=pnl, entry_price=10, volume=10, entry_context={"simulation_policy": {"version": 1, "fee_rate": .001}}) for i in range(count)]


def test_comparison_requires_shared_forward_period_and_real_improvement():
    assert compare_trades(samples(2), [])['status'] == "PENDING"
    assert compare_trades(samples(2, 3), samples(1, 3))['status'] == "PENDING"
    assert compare_trades(samples(2), samples(1))['status'] == "PASSED"
    assert compare_trades(samples(1), samples(2))['status'] == "FAILED"
    broken = samples(2)
    broken[0].pnl = float("nan")
    assert compare_trades(broken, samples(1))['status'] == "PENDING"
    # Raw dollar gains alone cannot win by using larger virtual positions.
    larger = samples(2)
    for row in larger:
        row.volume *= 2
    assert compare_trades(larger, samples(1))['status'] == "FAILED"


def test_comparison_rejects_missing_or_changed_execution_policy():
    candidate, incumbent = samples(2), samples(1)
    candidate[0].entry_context = {}
    assert compare_trades(candidate, incumbent)['status'] == 'PENDING'
    candidate = samples(2)
    candidate[0].entry_context['simulation_policy']['fee_rate'] = .002
    assert compare_trades(candidate, incumbent)['status'] == 'PENDING'
