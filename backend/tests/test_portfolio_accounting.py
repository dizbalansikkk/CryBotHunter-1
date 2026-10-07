from types import SimpleNamespace

import pytest

from app.models.entities import Position
from app.services.portfolio_accounting import PortfolioAccountingService


@pytest.mark.asyncio
async def test_paper_futures_reserves_margin_for_both_directions(monkeypatch):
    service = PortfolioAccountingService(SimpleNamespace(
        market_type="future", is_derivatives_market=lambda: True,
    ))
    monkeypatch.setattr(service.settings, "paper_trading", True)
    monkeypatch.setattr(service.settings, "paper_starting_balance", 1000)
    monkeypatch.setattr(service.settings, "futures_leverage", 2)
    monkeypatch.setattr(service.settings, "futures_max_leverage", 3)
    positions = [
        Position(symbol="ETH/USDT", side="LONG", current_price=100, volume=2, pnl=10, stop=95),
        Position(symbol="BNB/USDT", side="SHORT", current_price=100, volume=2, pnl=-5, stop=105),
    ]

    class Db:
        def __init__(self):
            self.statements = []
            self.results = iter([
                SimpleNamespace(scalar_one_or_none=lambda: None),
                SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: positions)),
                SimpleNamespace(scalar_one=lambda: 20),
                SimpleNamespace(scalar_one=lambda: 1100),
            ])

        async def execute(self, statement):
            self.statements.append(statement)
            return next(self.results)

        def add(self, item):
            self.snapshot = item

        async def flush(self):
            pass

    db = Db()
    snapshot = await service.capture(db)
    assert snapshot.total_equity == 1025
    assert snapshot.reserved_equity == 200
    assert snapshot.free_equity == 825
    assert snapshot.net_exposure == 0
    assert snapshot.open_stop_risk == 20
    assert snapshot.drawdown_percent == pytest.approx(6.8182)
    for statement in (db.statements[0], db.statements[-1]):
        assert "PAPER" in statement.compile().params.values()
