import pytest

from app.services.execution import ExecutionService
from app.services.exchange import ExchangeClient, PreparedOrder, OrderMinimumError


class Db:
    async def commit(self):
        pass
    async def execute(self, statement):
        from types import SimpleNamespace
        return SimpleNamespace(scalar_one_or_none=lambda: None)
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
async def test_exit_does_not_reconfigure_derivatives_margin_or_leverage(monkeypatch, paper):
    class ExitExchange(LowBalanceExchange):
        def is_derivatives_market(self):
            return True

        async def prepare_derivatives_symbol(self, symbol):
            raise AssertionError("closing existing inventory must not configure leverage")

        async def create_order(self, symbol, side, amount, order_type, **kwargs):
            assert kwargs["reduce_only"] is True
            return {"id": "exit", "status": "closed", "filled": amount, "average": 100}

    service = ExecutionService(ExitExchange())
    monkeypatch.setattr(service.settings, "paper_trading", paper)
    order = await service.execute_market(Db(), "ETH/USDT", "sell", 0.1, 100, "EXIT_STOP_LOSS")
    assert order.status == "FILLED"
    assert order.filled_amount == 0.1


@pytest.mark.asyncio
@pytest.mark.parametrize("paper,enabled,live,probe,expected", [
    (True, True, False, True, "FILLED"),
    (True, True, False, False, "FAILED"),
    (False, True, True, True, "FAILED"),
    (True, True, True, True, "FAILED"),
    (True, False, False, True, "FAILED"),
])
async def test_subminimum_learning_fill_never_increases_risk_or_reaches_live(monkeypatch, paper, enabled, live, probe, expected):
    class MinimumExchange(LowBalanceExchange):
        def is_derivatives_market(self):
            return True
        async def prepare_order(self, symbol, amount, reference_price):
            raise OrderMinimumError("notional below venue minimum 20 USDT")
    service = ExecutionService(MinimumExchange())
    for key, value in {"paper_trading": paper, "paper_exploration_enabled": enabled, "live_trading_enabled": live}.items():
        monkeypatch.setattr(service.settings, key, value)
    order = await service.execute_market(Db(), "LINK/USDT", "sell", 0.12, 12.5, "ENTRY", paper_learning=probe)
    assert order.status == expected
    if expected == "FILLED":
        assert order.filled_amount == 0.12
        assert order.raw["paper_learning_subminimum"]
        assert order.raw["venue_executable"] is False
        assert order.fee > 0
        assert order.average_price < 12.5


@pytest.mark.asyncio
async def test_learning_does_not_hide_metadata_failure(monkeypatch):
    class BrokenExchange(LowBalanceExchange):
        def is_derivatives_market(self):
            return True
        async def prepare_order(self, *args):
            raise RuntimeError("metadata unavailable")
    service = ExecutionService(BrokenExchange())
    monkeypatch.setattr(service.settings, "paper_trading", True)
    monkeypatch.setattr(service.settings, "paper_exploration_enabled", True)
    monkeypatch.setattr(service.settings, "live_trading_enabled", False)
    order = await service.execute_market(Db(), "LINK/USDT", "sell", 0.12, 12.5, "ENTRY", paper_learning=True)
    assert order.status == "FAILED"
    assert order.raw["message"] == "metadata unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("paper", [True, False])
async def test_dust_exit_is_simulated_exactly_but_live_minimum_is_preserved(monkeypatch, paper):
    class MinimumExchange(LowBalanceExchange):
        async def prepare_order(self, symbol, amount, reference_price):
            raise RuntimeError("below exchange minimum")

    service = ExecutionService(MinimumExchange())
    monkeypatch.setattr(service.settings, "paper_trading", paper)
    order = await service.execute_market(Db(), "ETH/USDT", "sell", 0.0018, 2500, "EXIT_STOP_LOSS")
    if paper:
        assert order.status == "FILLED"
        assert order.filled_amount == 0.0018
        assert order.fee > 0
        assert order.raw["paper_exit_exact_inventory"] is True
    else:
        assert order.status == "FAILED"
        assert "below exchange minimum" in order.raw["message"]


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


@pytest.mark.asyncio
async def test_entry_minimum_checked_after_exchange_rounding(monkeypatch):
    class RoundedExchange(LowBalanceExchange):
        def is_derivatives_market(self):
            return True
        async def prepare_order(self, *args):
            return PreparedOrder(amount=0.49, fee_rate=0.001, min_amount=None, min_cost=None, metadata_available=True)
    service = ExecutionService(RoundedExchange())
    monkeypatch.setattr(service.settings, "paper_trading", True)
    order = await service.execute_market(Db(), "LINK/USDT", "sell", 0.5002, 10, "ENTRY", min_notional=5)
    assert order.status == "FAILED"
    assert "configured minimum" in order.raw["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize('recover', [True, False])
async def test_timeout_recovers_by_client_id_without_resubmission(monkeypatch, recover):
    import ccxt
    class TimeoutExchange(LiveExchange):
        sent = 0
        looked_up = 0
        async def create_order(self, *args, **kwargs):
            self.sent += 1
            self.client_id = kwargs['client_order_id']
            raise ccxt.RequestTimeout('response lost')
        async def fetch_order_by_client_id(self, client_id, symbol):
            self.looked_up += 1
            assert client_id == self.client_id
            if not recover:
                raise ccxt.OrderNotFound('unknown yet')
            return await self.fetch_order('exchange-1', symbol)
    exchange = TimeoutExchange()
    service = ExecutionService(exchange)
    monkeypatch.setattr(service.settings, 'paper_trading', False)
    class PersistentDb(Db):
        committed = False
        async def commit(self): self.committed = True
    db = PersistentDb()
    order = await service.execute_market(db, 'BTC/USDT', 'buy', 1, 100, 'ENTRY')
    assert db.committed
    assert exchange.sent == exchange.looked_up == 1
    assert order.status == ('FILLED' if recover else 'NEW')
    assert order.raw['reconciliation_required'] is (not recover)


@pytest.mark.asyncio
async def test_unresolved_order_blocks_another_submission(monkeypatch):
    from types import SimpleNamespace
    class PendingDb(Db):
        async def execute(self, statement):
            return SimpleNamespace(scalar_one_or_none=lambda: 42)
    service = ExecutionService(LiveExchange())
    monkeypatch.setattr(service.settings, 'paper_trading', False)
    with pytest.raises(RuntimeError, match='42.*reconciliation'):
        await service.execute_market(PendingDb(), 'BTC/USDT', 'buy', 1, 100, 'ENTRY')


@pytest.mark.parametrize('status', ['open', 'canceled', 'expired'])
def test_partial_fill_is_not_lost_when_order_is_open_or_cancelled(status):
    from app.models.entities import Order
    service = ExecutionService()
    order = Order(requested_amount=1, raw={})
    service._apply_live_result(order, {'status': status, 'filled': .2, 'average': 101})
    assert order.status == 'PARTIAL'
    assert order.filled_amount == .2
    assert service._positive_float(float('inf')) == 0


@pytest.mark.asyncio
async def test_stop_timeout_keeps_persisted_unknown_intent(monkeypatch):
    import ccxt
    class Exchange(ProtectiveStopExchange):
        async def create_protective_stop_order(self, *args, **kwargs):
            raise ccxt.RequestTimeout('lost response')
    service = ExecutionService(Exchange())
    monkeypatch.setattr(service.settings, 'paper_trading', False)
    order = await service.place_protective_stop(Db(), position_id=3, symbol='ETH/USDT',
        side='sell', amount=1, stop_price=90, stage='INITIAL')
    assert order.status == 'NEW'
    assert order.raw['reconciliation_required'] is True
    assert order.raw['acknowledged'] is False
    assert order.raw['client_order_id']

@pytest.mark.asyncio
@pytest.mark.parametrize('status,filled', [('open',0), ('closed',1), ('canceled',.2)])
async def test_stop_cancel_requires_cancellation_and_preserves_racing_fill(status, filled):
    from app.models.entities import Order
    class Exchange:
        async def cancel_order(self, *args):
            return {'status':status, 'filled':filled, 'average':100 if filled else None}
    service=ExecutionService(Exchange())
    order=Order(status='NEW', exchange_order_id='venue1', requested_amount=1, symbol='ETH/USDT', raw={})
    assert await service.cancel_protective_stop(order) is False
    if filled:
        assert order.filled_amount == filled
        assert order.status in {'FILLED','PARTIAL'}
    else:
        assert order.status == 'NEW'
