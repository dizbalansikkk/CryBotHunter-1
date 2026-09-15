from types import SimpleNamespace

import pytest

from app.models.entities import AgentDecision, RlModel, ShadowTrade
from app.services.shadow_trading import ShadowTradingService


class Result:
    def __init__(self, rows):
        self.rows = rows

    def scalars(self):
        return self

    def first(self):
        return self.rows[0] if self.rows else None

    def all(self):
        return self.rows


class Db:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.added = []

    async def execute(self, _statement):
        return Result(self.rows)

    def add(self, item):
        self.added.append(item)

    async def flush(self):
        for index, item in enumerate(self.added, 1):
            item.id = item.id or index


def settings():
    return SimpleNamespace(
        shadow_trade_min_confidence=0.55,
        shadow_trade_notional=100,
        shadow_trade_stop_percent=1.5,
        shadow_trade_take_percent=3,
        paper_fee_rate=0.0004,
        paper_slippage_bps=2,
        execution_market_impact_bps=1.5,
        shadow_forward_min_trades=2,
        shadow_forward_min_profit_factor=1.1,
        shadow_forward_min_win_rate=40,
        shadow_forward_min_pnl=0,
        shadow_forward_max_drawdown_percent=8,
    )


@pytest.mark.asyncio
async def test_shadow_decision_opens_virtual_trade_without_exchange_order():
    service = ShadowTradingService()
    service.settings = settings()
    db = Db()
    record = RlModel(id=11, symbol="BTC/USDT", timeframe="1h", status="SHADOW", is_active=False, metrics={})
    decision = AgentDecision(id=21, action="BUY", confidence=0.8)

    trade = await service.process(db, record, decision, 100)

    assert trade is not None
    assert trade.status == "OPEN"
    assert trade.model_id == 11
    assert trade.entry_context["trading_authority"] is False
    assert trade.pnl < 0  # entry fee is charged immediately


@pytest.mark.asyncio
async def test_forward_report_requires_profit_and_stability_before_pass():
    service = ShadowTradingService()
    service.settings = settings()
    trades = [
        ShadowTrade(id=1, model_id=11, status="CLOSED", pnl=2.0),
        ShadowTrade(id=2, model_id=11, status="CLOSED", pnl=1.0),
    ]

    report = await service.report(Db(trades), 11)

    assert report.status == "PASSED"
    assert report.closed_trades == 2
    assert report.win_rate == 100
    assert report.total_pnl == 3


def test_shadow_trade_closes_on_model_flip_with_realistic_costs():
    service = ShadowTradingService()
    service.settings = settings()
    trade = ShadowTrade(
        side="LONG",
        entry_price=100,
        current_price=102,
        volume=1,
        stop=98,
        take=105,
        fee=0.04,
        slippage=0.02,
        status="OPEN",
    )
    decision = AgentDecision(action="SELL", confidence=0.8)

    assert service._exit_reason(trade, decision) == "MODEL_FLIP"
    service._close(trade, 102, "MODEL_FLIP")

    assert trade.status == "CLOSED"
    assert trade.pnl < 2
    assert trade.fee > 0.04
