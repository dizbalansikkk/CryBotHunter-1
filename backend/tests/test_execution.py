import pytest

from app.services.execution import ExecutionService
from app.services.exchange import ExchangeClient, PreparedOrder


class Db:
    def add(self, _obj):
        return None

    async def flush(self):
        return None


@pytest.mark.asyncio
async def test_paper_execution_fills_order_with_fee_and_slippage():
    order = await ExecutionService().execute_market(
        Db(),
        symbol="BTC/USDT",
        side="buy",
        amount=2,
        reference_price=100,
        reason="TEST",
    )
    assert order.status == "FILLED"
    assert order.average_price > 100
    assert order.fee > 0
    assert order.slippage > 0


class LowBalanceExchange:
    async def prepare_order(self, symbol, amount, reference_price):
        return PreparedOrder(amount=amount, fee_rate=0.001, min_amount=None, min_cost=None, metadata_available=False)

    async def get_free_balance(self):
        return {"USDT": 50.0}

    async def create_order(self, *args, **kwargs):
        raise AssertionError("create_order should not be called when balance is insufficient")


@pytest.mark.asyncio
@pytest.mark.parametrize("paper", [True, False])
@pytest.mark.parametrize("side", ["buy", "sell"])
async def test_disabled_secondary_spot_blocks_entries(monkeypatch, paper, side):
    service = ExecutionService(LowBalanceExchange())
    monkeypatch.setattr(service.settings, "exchange_default_type", "spot")
    monkeypatch.setattr(service.settings, "spot_secondary_enabled", False)
    monkeypatch.setattr(service.settings, "paper_trading", paper)
    order = await service.execute_market(
        Db(), "ETH/USDT", side, 1, 100, "ENTRY"
    )
    assert order.status == "FAILED"
    assert "Secondary spot entries are disabled" in order.raw["message"]


@pytest.mark.asyncio
async def test_disabled_secondary_spot_still_allows_paper_exit(monkeypatch):
    service = ExecutionService(LowBalanceExchange())
    monkeypatch.setattr(service.settings, "exchange_default_type", "spot")
    monkeypatch.setattr(service.settings, "spot_secondary_enabled", False)
    monkeypatch.setattr(service.settings, "paper_trading", True)
    order = await service.execute_market(
        Db(), "ETH/USDT", "sell", 1, 100, "EXIT_STOP_LOSS"
    )
    assert order.status == "FILLED"


@pytest.mark.asyncio
async def test_live_execution_blocks_entry_when_free_balance_is_too_low(monkeypatch):
    service = ExecutionService(LowBalanceExchange())
    monkeypatch.setattr(service.settings, "paper_trading", False)

    order = await service.execute_market(
        Db(),
        symbol="BTC/USDT",
        side="buy",
        amount=1,
        reference_price=100,
        reason="ENTRY",
    )

    assert order.status == "FAILED"
    assert order.raw["error"] == "RuntimeError"
    assert "Insufficient free USDT balance" in order.raw["message"]


class LiveExchange:
    def __init__(self, resolved_status="closed", filled=1, average=101):
        self.resolved_status = resolved_status
        self.filled = filled
        self.average = average

    async def prepare_order(self, symbol, amount, reference_price):
        return PreparedOrder(amount=amount, fee_rate=0.001, min_amount=None, min_cost=None, metadata_available=True)

    async def get_free_balance(self):
        return {"USDT": 1_000.0}

    async def create_order(self, *args, **kwargs):
        return {"id": "exchange-1", "status": "open", "filled": 0, "average": None}

    async def fetch_order(self, _order_id, _symbol):
        return {
            "id": "exchange-1",
            "status": self.resolved_status,
            "filled": self.filled,
            "average": self.average,
            "fee": {"cost": 0.101},
        }


@pytest.mark.asyncio
async def test_live_execution_waits_for_confirmed_exchange_fill(monkeypatch):
    service = ExecutionService(LiveExchange())
    monkeypatch.setattr(service.settings, "paper_trading", False)

    order = await service.execute_market(
        Db(), symbol="BTC/USDT", side="buy", amount=1, reference_price=100, reason="ENTRY"
    )

    assert order.status == "FILLED"
    assert order.filled_amount == 1
    assert order.average_price == 101
    assert order.raw["fill_confirmed"] is True


@pytest.mark.asyncio
async def test_live_execution_does_not_assume_an_unresolved_order_was_filled(monkeypatch):
    service = ExecutionService(LiveExchange(resolved_status="open", filled=0, average=None))
    monkeypatch.setattr(service.settings, "paper_trading", False)

    order = await service.execute_market(
        Db(), symbol="BTC/USDT", side="buy", amount=1, reference_price=100, reason="ENTRY"
    )

    assert order.status == "NEW"
    assert order.filled_amount == 0
    assert order.average_price is None
    assert order.raw["fill_confirmed"] is False


@pytest.mark.asyncio
async def test_live_execution_marks_terminal_partial_fill_without_claiming_full_execution(monkeypatch):
    service = ExecutionService(LiveExchange(resolved_status="closed", filled=0.4, average=101))
    monkeypatch.setattr(service.settings, "paper_trading", False)

    order = await service.execute_market(
        Db(), symbol="BTC/USDT", side="sell", amount=1, reference_price=100, reason="EXIT_STOP_LOSS"
    )

    assert order.status == "PARTIAL"
    assert order.filled_amount == 0.4
    assert order.raw["fill_confirmed"] is True
    assert order.raw["fill_complete"] is False


class MinimumExchange:
    async def prepare_order(self, _symbol, amount, _reference_price):
        return PreparedOrder(amount=amount, fee_rate=0.001, min_amount=0.01, min_cost=10.0, metadata_available=True)


@pytest.mark.asyncio
async def test_exit_size_check_rejects_scale_out_below_exchange_notional(monkeypatch):
    service = ExecutionService(MinimumExchange())
    monkeypatch.setattr(service.settings, "min_exit_notional_usdt", 5.0)

    assessment = await service.assess_exit_size("BTC/USDT", amount=0.02, reference_price=100)

    assert assessment.allowed is False
    assert assessment.notional == 2.0
    assert assessment.minimum_notional == 10.0
    assert "below required minimum" in assessment.reason


class ProtectiveStopExchange:
    def __init__(self):
        self.cancelled: list[tuple[str, str]] = []

    def supports_native_protective_stops(self):
        return True

    async def prepare_order(self, _symbol, amount, _reference_price):
        return PreparedOrder(amount=amount, fee_rate=0.001, min_amount=0.01, min_cost=5.0, metadata_available=True)

    async def create_protective_stop_order(self, symbol, side, amount, stop_price, client_order_id=None):
        return {
            "id": "native-stop-1",
            "symbol": symbol,
            "side": side,
            "amount": amount,
            "stop_price": stop_price,
            "client_order_id": client_order_id,
            "status": "open",
        }

    async def cancel_order(self, order_id, symbol):
        self.cancelled.append((order_id, symbol))
        return {"id": order_id, "status": "canceled"}


@pytest.mark.asyncio
async def test_native_protective_stop_is_persisted_as_active_then_cancelled(monkeypatch):
    exchange = ProtectiveStopExchange()
    service = ExecutionService(exchange)
    monkeypatch.setattr(service.settings, "paper_trading", False)
    db = Db()

    order = await service.place_protective_stop(
        db,
        position_id=77,
        symbol="BTC/USDT",
        side="sell",
        amount=1,
        stop_price=98,
        stage="TP1_FILLED",
    )

    assert order.status == "NEW"
    assert order.exchange_order_id == "native-stop-1"
    assert order.raw["order_role"] == "PROTECTIVE_STOP"
    assert order.raw["position_id"] == 77
    assert order.raw["acknowledged"] is True

    assert await service.cancel_protective_stop(order) is True
    assert order.status == "CANCELLED"
    assert exchange.cancelled == [("native-stop-1", "BTC/USDT")]


@pytest.mark.parametrize("exchange", ["binance", "okx", "bybit"])
def test_native_protective_stops_are_limited_to_supported_derivatives(monkeypatch, exchange):
    client = ExchangeClient(exchange=exchange)
    monkeypatch.setattr(client.settings, "paper_trading", False)
    monkeypatch.setattr(client.settings, "exchange_default_type", "swap")
    monkeypatch.setattr(client.settings, "native_protective_stops_enabled", True)

    assert client.supports_native_protective_stops() is True

    monkeypatch.setattr(client.settings, "exchange_default_type", "spot")
    assert client.supports_native_protective_stops() is False
