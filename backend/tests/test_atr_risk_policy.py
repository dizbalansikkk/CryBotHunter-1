from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as Row
from unittest.mock import AsyncMock, Mock
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from app.core.config import get_settings
from app.db.base import Base
from app.models.entities import Trade, Order, DailyRiskState, EquitySnapshot
from app.services.daily_risk import DailyRiskService
from app.services.risk_manager import RiskManager, RiskSettings
from app.services.trading_engine import TradingEngine
from app.services.execution import ExecutionService
from app.services.exchange import PreparedOrder
from test_paper_exit_lifecycle import Ledger


@pytest.fixture
def ledger():
    database = create_engine("sqlite://")
    Base.metadata.create_all(database)
    with Session(database, expire_on_commit=False) as session:
        yield Ledger(session)
    database.dispose()


def config():
    return get_settings().model_copy(update={"atr_risk_policy_enabled": True,
        "experimental_exit_limits_enabled": True,  # Old env cannot override new policy.
        "paper_trading": True, "live_trading_enabled": False, "market_data_mode": "ccxt",
        "paper_starting_balance": 1000, "paper_fee_rate": .0004,
        "paper_slippage_bps": 2, "execution_market_impact_bps": 2,
        "min_entry_notional_usdt": 5, "futures_leverage": 3, "futures_max_leverage": 3,
        "telegram_trade_reports_enabled": False, "trading_day_timezone": "Europe/Kiev"})


def venue():
    async def prepare(symbol, amount, price):
        return PreparedOrder(amount=amount, fee_rate=.0004, min_amount=.00001, min_cost=5, metadata_available=True)
    return Row(is_derivatives_market=lambda: True, supports_native_protective_stops=lambda: False,
        prepare_order=prepare, prepare_derivatives_symbol=AsyncMock(return_value={"leverage": 3}),
        fetch_tickers=AsyncMock(return_value={"ETH/USDT": {"last": 100}}),
        create_order=AsyncMock(side_effect=AssertionError("No live requests permitted")))


@pytest.mark.parametrize("side,stop,take", [("LONG", 96, 110), ("SHORT", 104, 90)])
def test_strict_atr_geometry(side, stop, take):
    p = RiskManager().strict_atr_plan(100, 2, side)
    assert (p.stop_loss, p.take_profit, p.risk_per_unit) == (stop, take, 4)


@pytest.mark.parametrize("atr", [None, 0, -1, float("nan"), float("inf"), 1e-12])
def test_invalid_atr_has_no_percentage_fallback(atr):
    with pytest.raises(ValueError):
        RiskManager().strict_atr_plan(100, atr, "LONG")


def test_sizing_scales_caps_leverage_and_counts_costs():
    r = RiskManager()
    assert r.strict_position_size(1000, 100, 96, 3) == 2.5
    assert r.strict_position_size(2000, 100, 96, 3) == 5
    assert r.strict_position_size(500, 100, 96, 3) == 1.25
    assert r.strict_position_size(1000, 100, 100, 3) == 0
    assert r.strict_position_size(1000, 100, 99.999, 3) == 30
    assert r.strict_position_size(1000, 100, 96, 3, .1) * 4.1 <= 10


@pytest.mark.asyncio
async def test_daily_limit_latches_across_restart_recovery_and_resets_next_day(ledger):
    svc = DailyRiskService(config(), venue())
    now = datetime(2026, 10, 30, 21, 59, tzinfo=timezone.utc)
    assert (await svc.check(ledger, now)).allowed
    trade = Trade(symbol="ETH/USDT", side="LONG", entry_price=100, exit_price=99, profit=-25)
    ledger.add(trade)
    await ledger.commit()
    result = await svc.check(ledger, now)
    assert not result.allowed and result.starting_day_balance == 1000 and result.daily_pnl == -25
    trade.profit = 0  # Recovery cannot release today's latch.
    await ledger.commit()
    ledger.session.expunge_all()
    assert not (await DailyRiskService(config(), venue()).check(ledger, now)).allowed
    assert (await svc.check(ledger, now + timedelta(minutes=2))).allowed  # Kyiv midnight, not UTC midnight.
    assert len(ledger.session.scalars(select(DailyRiskState)).all()) == 2


@pytest.mark.asyncio
async def test_snapshot_baseline_is_fixed_and_not_reset_by_balance_change(ledger):
    now = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)
    ledger.add(EquitySnapshot(mode="PAPER", source="TEST", total_equity=1100, free_equity=1100,
        peak_equity=1100, captured_at=datetime(2026, 10, 29, 21, 59, tzinfo=timezone.utc)))
    await ledger.commit()
    decision = await DailyRiskService(config(), venue()).check(ledger, now)
    assert decision.starting_day_balance == 1100
    assert decision.daily_pnl == -100
    assert not decision.allowed


@pytest.mark.asyncio
@pytest.mark.parametrize("side", ["LONG", "SHORT"])
@pytest.mark.parametrize("exploration", [False, True])
async def test_real_paper_entry_uses_equity_atr_and_final_executor_approval(ledger, side, exploration):
    engine = TradingEngine()
    engine.settings = config()
    engine.exchange = engine.execution.exchange = venue()
    engine.execution.settings = engine.settings
    engine.learning.entry_context = Mock(return_value={})
    engine._paper_exploration_votes = Mock(return_value=(4, 1))
    settings = RiskSettings(balance=99999, risk_percent=9, daily_risk_percent=50,
        max_positions=3, min_rating=70, stop_loss_percent=9, take_profit_percent=99,
        trailing_stop_percent=.8, atr_stop_multiplier=9, risk_reward_ratio=9)
    coin = Row(symbol="ETH/USDT", price=100, atr=2, spread_bps=0, funding_rate=0)
    p = await engine._open_position(ledger, coin, "BUY" if side == "LONG" else "SELL",
        [], 99999, settings, paper_exploration=exploration)
    assert p is not None
    assert abs(p.entry_price - p.stop) == pytest.approx(4)
    assert abs(p.take - p.entry_price) == pytest.approx(10)
    assert 2 < p.volume < 2.5
    assert p.entry_context["risk_percent"] == 1
    assert p.entry_context["balance"] == 1000
    assert "exit_experiment" not in p.entry_context
    order = ledger.session.scalars(select(Order)).first()
    assert order.status == "FILLED"
    assert order.raw["risk_manager_approval"]["loss"] <= 10 + 1e-8
    assert order.raw["risk_manager_approval"]["profit"] > 0
    engine.exchange.create_order.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing", "atr", "geometry", "risk", "cost", "leverage", "day", "normalization", "live", "agent_alias"])
async def test_executor_veto_cannot_be_bypassed_by_signal_agents(ledger, failure):
    service = ExecutionService(venue())
    service.settings = config()
    amount = 100 if failure == "risk" else 2
    plan = {"atr": 2, "stop": 96.02, "take": 110.02, "slippage_bps": 2}
    if failure in {"missing", "agent_alias"}:
        plan = None
    elif failure == "live":
        service.settings = service.settings.model_copy(update={"paper_trading": False})
    elif failure == "normalization":
        service.exchange.prepare_order = AsyncMock(return_value=PreparedOrder(amount=100,
            fee_rate=.0004, min_amount=None, min_cost=None, metadata_available=True))
    elif failure == "atr":
        plan["atr"] = 0
    elif failure == "geometry":
        plan["take"] = 102
    elif failure in {"cost", "leverage"}:
        plan = {"atr": .01, "stop": 100, "take": 100.07, "slippage_bps": 2}
        amount = 31 if failure == "leverage" else 1
        if failure == "leverage":
            service.settings = service.settings.model_copy(update={"paper_fee_rate": 0,
                "paper_slippage_bps": 0, "execution_market_impact_bps": 0})
            plan = {"atr": .01, "stop": 99.98, "take": 100.05, "slippage_bps": 0}
            service.exchange.prepare_order = AsyncMock(return_value=PreparedOrder(amount=31,
                fee_rate=0, min_amount=None, min_cost=None, metadata_available=True))
    elif failure == "day":
        ledger.add(Trade(symbol="ETH/USDT", side="LONG", entry_price=100, exit_price=90, profit=-25))
        await ledger.commit()
    order = await service.execute_market(ledger, "ETH/USDT", "buy", amount, 100,
        "AGENT_SIGNAL" if failure == "agent_alias" else "ENTRY", risk_plan=plan)
    assert order.status == "FAILED"
    assert "veto" in order.raw.get("message", "").lower() or failure == "atr"
    service.exchange.create_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_daily_kill_does_not_block_protective_exit(ledger):
    service = ExecutionService(venue())
    service.settings = config()
    ledger.add(Trade(symbol="ETH/USDT", side="LONG", entry_price=100, exit_price=90, profit=-25))
    await ledger.commit()
    assert not (await DailyRiskService(service.settings, service.exchange).check(ledger)).allowed
    order = await service.execute_market(ledger, "ETH/USDT", "sell", 1, 95, "EXIT_STOP_LOSS")
    assert order.status == "FILLED"
    service.exchange.create_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_floating_loss_counts_and_missing_quote_fails_closed(ledger):
    from app.models.entities import Position
    ledger.add(Position(symbol="ETH/USDT", side="LONG", entry_price=100, current_price=100,
        volume=1, stop=70, take=175, status="OPEN", highest_price=100, lowest_price=100,
        trailing_stop_percent=1, entry_context={}))
    await ledger.commit()
    exchange = venue()
    exchange.fetch_tickers = AsyncMock(return_value={"ETH/USDT": {"last": 75}})
    assert not (await DailyRiskService(config(), exchange).check(ledger)).allowed
    exchange.fetch_tickers = AsyncMock(return_value={})
    with pytest.raises(ValueError, match="quote"):
        await DailyRiskService(config(), exchange).check(ledger)


def test_minimum_notional_never_increases_strict_volume():
    engine = TradingEngine()
    engine.settings = config()
    engine.exchange = venue()
    assert engine._calculate_position_size(1, 1, 100, 96, min_notional=5) == 0


@pytest.mark.asyncio
async def test_missing_midnight_baseline_with_carry_position_blocks_entries(ledger):
    from app.models.entities import Position
    now = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)
    ledger.add(Position(symbol="ETH/USDT", side="LONG", entry_price=100, current_price=100,
        volume=1, stop=70, take=175, status="OPEN", highest_price=100, lowest_price=100,
        trailing_stop_percent=1, entry_context={}, entered_at=now - timedelta(days=1)))
    await ledger.commit()
    with pytest.raises(ValueError, match="Starting-day equity"):
        await DailyRiskService(config(), venue()).check(ledger, now)
