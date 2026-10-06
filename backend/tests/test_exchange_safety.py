import pytest

from app.api.routes.dashboard import paper_equity
from app.services.exchange import ExchangeClient
from app.services.execution import ExecutionService


class FakeMarketClient:
    markets = {
        "BTC/USDT:USDT": {
            "contract": True, "linear": True, "quote": "USDT",
            "base": "BTC", "settle": "USDT",
        }
    }

    def load_markets(self):
        return None

    def market(self, symbol):
        return {
            "symbol": symbol,
            "taker": 0.001,
            "limits": {"amount": {"min": 0.01}, "cost": {"min": 5.0}},
        }

    def amount_to_precision(self, symbol, amount):
        return "0.012"


class FakeExchange:
    instances = []

    def __init__(self, params):
        self.params = params
        self.closed = False
        self.__class__.instances.append(self)

    def close(self):
        self.closed = True


def test_exchange_blocks_live_without_sandbox_by_default(monkeypatch):
    client = ExchangeClient()
    monkeypatch.setattr(client.settings, "exchange_sandbox_enabled", False)
    monkeypatch.setattr(client.settings, "allow_live_trading_without_sandbox", False)

    with pytest.raises(RuntimeError):
        client._assert_live_safety()


def test_exchange_allows_live_with_sandbox(monkeypatch):
    client = ExchangeClient()
    monkeypatch.setattr(client.settings, "exchange_sandbox_enabled", True)
    monkeypatch.setattr(client.settings, "allow_live_trading_without_sandbox", False)

    client._assert_live_safety()


@pytest.mark.asyncio
async def test_paper_balance_uses_configured_starting_capital(monkeypatch):
    client = ExchangeClient()
    monkeypatch.setattr(client.settings, "paper_trading", True)
    monkeypatch.setattr(client.settings, "paper_starting_balance", 638.0)

    assert await client.get_balance() == {"USDT": 638.0}
    assert await client.get_free_balance() == {"USDT": 638.0}


def test_paper_equity_tracks_profit_and_loss_from_638_usdt():
    assert paper_equity(638.0, 12.5) == (650.5, 12.5, 1.9592)
    assert paper_equity(638.0, -20.0) == (618.0, -20.0, -3.1348)


def test_exchange_order_params_include_client_id_and_reduce_only():
    client = ExchangeClient(exchange="bybit")
    params = client._order_params(client_order_id="cbh-1-exit", reduce_only=True)

    assert params["clientOrderId"] == "cbh-1-exit"
    assert params["orderLinkId"] == "cbh-1-exit"
    assert params["reduceOnly"] is True


def test_execution_client_order_id_is_stable_and_safe():
    client_order_id = ExecutionService()._client_order_id(42, "EXIT_TAKE_PROFIT")

    assert client_order_id == "cbh-42-exit_take_profit"


def test_prepare_order_uses_exchange_precision_and_fee(monkeypatch):
    client = ExchangeClient()
    monkeypatch.setattr(client, "_client", lambda authenticated: FakeMarketClient())

    prepared = client._prepare_order_sync("BTC/USDT", 0.012345, 50_000)

    assert prepared.amount == 0.012
    assert prepared.fee_rate == 0.001
    assert prepared.min_amount == 0.01
    assert prepared.min_cost == 5.0
    assert prepared.metadata_available is True


def test_prepare_order_blocks_below_exchange_minimum(monkeypatch):
    client = ExchangeClient()
    monkeypatch.setattr(client, "_client", lambda authenticated: FakeMarketClient())

    with pytest.raises(RuntimeError, match="below exchange minimum"):
        client._prepare_order_sync("BTC/USDT", 0.012345, 100)


def test_binance_spot_client_is_reused_and_does_not_load_futures_markets(monkeypatch):
    FakeExchange.instances = []
    monkeypatch.setattr("app.services.exchange.ccxt.binance", FakeExchange)
    client = ExchangeClient(exchange="binance")
    monkeypatch.setattr(client.settings, "exchange_default_type", "spot")

    first = client._client(authenticated=False)
    second = client._client(authenticated=False)

    assert first is second
    assert len(FakeExchange.instances) == 1
    assert first.params["options"]["defaultType"] == "spot"
    assert first.params["options"]["fetchMarkets"] == ["spot"]
