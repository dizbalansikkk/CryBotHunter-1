import asyncio
import re
from dataclasses import dataclass
from typing import Any

import ccxt

from app.core.config import get_settings
from app.core.security import decrypt_secret
from app.models.entities import UserSettings


def exchange_error_message(exc: Exception, exchange: str, market_type: str, sandbox: bool) -> str:
    mode = "sandbox" if sandbox else "real"
    suffix = f"Exchange={exchange}, market={market_type}, mode={mode}."
    detail = _safe_error_detail(exc)
    if isinstance(exc, RuntimeError):
        return f"{exc} {suffix}"
    if isinstance(exc, ccxt.AuthenticationError):
        return (
            "Биржа не приняла API key/secret. Проверь ключи, IP whitelist, права ключа "
            f"и совпадение sandbox/live режима. {suffix}{detail}"
        )
    if isinstance(exc, ccxt.PermissionDenied):
        return f"У API ключа не хватает прав для запроса баланса. Проверь права в кабинете биржи. {suffix}{detail}"
    if isinstance(exc, ccxt.NetworkError):
        return f"Биржа сейчас недоступна по сети или запрос заблокирован. {suffix}{detail}"
    if isinstance(exc, ccxt.BaseError):
        return f"Биржа ответила ошибкой. {suffix}{detail}"
    return f"Проверка биржи не удалась. {suffix}{detail}"


def _safe_error_detail(exc: Exception) -> str:
    detail = str(exc).replace("\n", " ").strip()
    if not detail:
        return f" Детали: {type(exc).__name__}."
    detail = re.sub(r"(?i)(signature=)[^&\s]+", r"\1***", detail)
    detail = re.sub(r"(?i)(timestamp=)[^&\s]+", r"\1***", detail)
    detail = re.sub(r"(?i)(recvWindow=)[^&\s]+", r"\1***", detail)
    detail = re.sub(r"(?i)(X-MBX-APIKEY['\"]?\s*[:=]\s*['\"]?)[^,'\"\s}]+", r"\1***", detail)
    return f" Детали: {type(exc).__name__}: {detail[:320]}"


@dataclass(frozen=True)
class PreparedOrder:
    amount: float
    fee_rate: float
    min_amount: float | None
    min_cost: float | None
    metadata_available: bool


class ExchangeClient:
    def __init__(
        self,
        exchange: str | None = None,
        api_key: str | None = None,
        secret_key: str | None = None,
        passphrase: str | None = None,
    ) -> None:
        self.settings = get_settings()
        self.exchange = exchange or self.settings.default_exchange
        self.api_key = api_key
        self.secret_key = secret_key
        self.passphrase = passphrase
        # Reuse one CCXT instance per authentication mode. Creating a new
        # Binance instance for every ticker/candle call reloads all market
        # metadata, consumes a large amount of request weight, and retains
        # sessions/market caches until shutdown.
        self._clients: dict[bool, ccxt.Exchange] = {}

    @classmethod
    def from_user_settings(cls, settings: UserSettings) -> "ExchangeClient":
        return cls(
            exchange=settings.exchange,
            api_key=decrypt_secret(settings.api_key_encrypted),
            secret_key=decrypt_secret(settings.secret_key_encrypted),
            passphrase=decrypt_secret(settings.passphrase_encrypted),
        )

    async def get_balance(self) -> dict[str, float]:
        if self.settings.paper_trading or not self.settings.live_trading_enabled:
            return {"USDT": 1000.0}
        client = self._client(authenticated=True)
        balance = await asyncio.to_thread(client.fetch_balance)
        return {asset: float(amount) for asset, amount in balance.get("total", {}).items() if amount}

    async def get_free_balance(self) -> dict[str, float]:
        if self.settings.paper_trading or not self.settings.live_trading_enabled:
            return {"USDT": 1000.0}
        client = self._client(authenticated=True)
        balance = await asyncio.to_thread(client.fetch_balance)
        return {asset: float(amount) for asset, amount in balance.get("free", {}).items() if amount}

    async def fetch_real_balance(self) -> dict[str, float]:
        client = self._client(authenticated=True)
        balance = await asyncio.to_thread(client.fetch_balance)
        return {asset: float(amount) for asset, amount in balance.get("total", {}).items() if amount}

    async def fetch_tickers(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        client = self._client(authenticated=False)
        tickers = await asyncio.to_thread(client.fetch_tickers, symbols)
        return {symbol: tickers[symbol] for symbol in symbols if symbol in tickers}

    async def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str = "1h",
        limit: int = 250,
        since: int | None = None,
    ) -> list[list[float]]:
        client = self._client(authenticated=False)
        return await asyncio.to_thread(client.fetch_ohlcv, symbol, timeframe, since, limit)

    async def fetch_order_book(self, symbol: str, limit: int = 20) -> dict[str, Any]:
        client = self._client(authenticated=False)
        return await asyncio.to_thread(client.fetch_order_book, symbol, max(int(limit), 5))

    async def fetch_trades(self, symbol: str, limit: int = 100) -> list[dict[str, Any]]:
        client = self._client(authenticated=False)
        return await asyncio.to_thread(client.fetch_trades, symbol, None, max(int(limit), 10))

    async def prepare_order(self, symbol: str, amount: float, reference_price: float) -> PreparedOrder:
        return await asyncio.to_thread(self._prepare_order_sync, symbol, amount, reference_price)

    def _prepare_order_sync(self, symbol: str, amount: float, reference_price: float) -> PreparedOrder:
        client = self._client(authenticated=False)
        try:
            client.load_markets()
            market = client.market(symbol)
            normalized_amount = self._amount_to_precision(client, symbol, amount)
            min_amount = self._nested_float(market, "limits", "amount", "min")
            min_cost = self._nested_float(market, "limits", "cost", "min")
            fee_rate = self._float_or_default(market.get("taker"), self.settings.paper_fee_rate)
            self._validate_minimums(symbol, normalized_amount, reference_price, min_amount, min_cost)
            return PreparedOrder(
                amount=normalized_amount,
                fee_rate=fee_rate,
                min_amount=min_amount,
                min_cost=min_cost,
                metadata_available=True,
            )
        except (ccxt.BaseError, AttributeError, KeyError):
            normalized_amount = round(float(amount), 8)
            return PreparedOrder(
                amount=normalized_amount,
                fee_rate=self.settings.paper_fee_rate,
                min_amount=None,
                min_cost=None,
                metadata_available=False,
            )

    async def create_order(
        self,
        symbol: str,
        side: str,
        amount: float,
        order_type: str = "market",
        client_order_id: str | None = None,
        reduce_only: bool = False,
    ) -> dict[str, str | float]:
        if self.settings.paper_trading:
            return {"id": f"paper-{symbol}-{side}", "symbol": symbol, "side": side, "amount": amount, "type": order_type}
        if not self.settings.live_trading_enabled:
            raise RuntimeError("Live trading is disabled. Set LIVE_TRADING_ENABLED=true only after paper validation.")
        self._assert_live_safety()
        client = self._client(authenticated=True)
        params = self._order_params(client_order_id=client_order_id, reduce_only=reduce_only)
        return await asyncio.to_thread(client.create_order, symbol, order_type, side, amount, None, params)

    async def fetch_order(self, order_id: str, symbol: str) -> dict[str, Any]:
        if self.settings.paper_trading:
            return {"id": order_id, "symbol": symbol, "status": "closed"}
        if not self.settings.live_trading_enabled:
            raise RuntimeError("Live trading is disabled.")
        self._assert_live_safety()
        client = self._client(authenticated=True)
        return await asyncio.to_thread(client.fetch_order, order_id, symbol)

    def _client(self, authenticated: bool) -> ccxt.Exchange:
        cached = self._clients.get(authenticated)
        if cached is not None:
            return cached
        exchange_class = getattr(ccxt, self.exchange, None)
        if exchange_class is None:
            raise RuntimeError(f"Unsupported exchange: {self.exchange}")
        market_type = self.settings.exchange_default_type
        options: dict[str, Any] = {"defaultType": market_type}
        # CCXT otherwise loads Binance spot, USDT futures and coin futures
        # exchangeInfo together. Spot workers must never spend request weight
        # on fapi/dapi metadata they do not use.
        if self.exchange == "binance" and market_type == "spot":
            options["fetchMarkets"] = ["spot"]
        params: dict[str, Any] = {
            "enableRateLimit": True,
            "options": options,
        }
        if authenticated:
            api_key = self.api_key or self.settings.exchange_api_key
            secret_key = self.secret_key or self.settings.exchange_secret_key
            passphrase = self.passphrase or self.settings.exchange_passphrase
            if not api_key or not secret_key:
                raise RuntimeError("Exchange API credentials are not configured")
            params["apiKey"] = api_key
            params["secret"] = secret_key
            if passphrase:
                params["password"] = passphrase
        client = exchange_class(params)
        if authenticated and self.settings.exchange_sandbox_enabled and hasattr(client, "set_sandbox_mode"):
            client.set_sandbox_mode(True)
        self._clients[authenticated] = client
        return client

    async def close(self) -> None:
        clients, self._clients = list(self._clients.values()), {}
        for client in clients:
            close = getattr(client, "close", None)
            if not callable(close):
                continue
            try:
                await asyncio.to_thread(close)
            except Exception:
                # Shutdown must continue even if an exchange transport is already gone.
                continue

    def _assert_live_safety(self) -> None:
        if self.settings.exchange_sandbox_enabled:
            return
        if self.settings.allow_live_trading_without_sandbox:
            return
        raise RuntimeError("Live exchange execution without sandbox is blocked by safety policy.")

    def _order_params(self, client_order_id: str | None = None, reduce_only: bool = False) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if client_order_id:
            params["clientOrderId"] = client_order_id
            if self.exchange == "bybit":
                params["orderLinkId"] = client_order_id
        if reduce_only:
            params["reduceOnly"] = True
        return params

    def _amount_to_precision(self, client: ccxt.Exchange, symbol: str, amount: float) -> float:
        try:
            normalized = float(client.amount_to_precision(symbol, amount))
        except (ccxt.BaseError, ValueError, TypeError):
            normalized = round(float(amount), 8)
        if normalized <= 0:
            raise RuntimeError(f"Order amount for {symbol} is too small after exchange precision rounding.")
        return normalized

    def _validate_minimums(
        self,
        symbol: str,
        amount: float,
        reference_price: float,
        min_amount: float | None,
        min_cost: float | None,
    ) -> None:
        if min_amount is not None and amount < min_amount:
            raise RuntimeError(f"Order amount for {symbol} is below exchange minimum: {amount} < {min_amount}.")
        notional = abs(amount * reference_price)
        if min_cost is not None and notional < min_cost:
            raise RuntimeError(f"Order notional for {symbol} is below exchange minimum: {notional:.8f} < {min_cost}.")

    def _nested_float(self, payload: dict[str, Any], *keys: str) -> float | None:
        value: Any = payload
        for key in keys:
            if not isinstance(value, dict):
                return None
            value = value.get(key)
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _float_or_default(self, value: Any, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default
