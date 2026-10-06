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
    _NATIVE_PROTECTIVE_STOP_EXCHANGES = {"binance", "okx", "bybit"}
    _DERIVATIVE_MARKET_TYPES = {"future", "futures", "swap"}

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
        self._derivatives_client: ccxt.Exchange | None = None
        self._prepared_derivative_symbols: set[str] = set()

    @property
    def market_type(self) -> str:
        return str(self.settings.exchange_default_type or "spot").strip().lower()

    def is_derivatives_market(self) -> bool:
        return self.market_type in self._DERIVATIVE_MARKET_TYPES

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
            return {"USDT": float(self.settings.paper_starting_balance)}
        client = self._client(authenticated=True)
        balance = await asyncio.to_thread(client.fetch_balance)
        return {asset: float(amount) for asset, amount in balance.get("total", {}).items() if amount}

    async def get_free_balance(self) -> dict[str, float]:
        if self.settings.paper_trading or not self.settings.live_trading_enabled:
            return {"USDT": float(self.settings.paper_starting_balance)}
        client = self._client(authenticated=True)
        balance = await asyncio.to_thread(client.fetch_balance)
        return {asset: float(amount) for asset, amount in balance.get("free", {}).items() if amount}

    async def fetch_real_balance(self) -> dict[str, float]:
        client = self._client(authenticated=True)
        balance = await asyncio.to_thread(client.fetch_balance)
        return {asset: float(amount) for asset, amount in balance.get("total", {}).items() if amount}

    async def fetch_tickers(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        client = self._client(authenticated=False)

        def fetch() -> dict[str, dict[str, Any]]:
            client.load_markets()
            resolved = {symbol: self._market_symbol(client, symbol) for symbol in symbols}
            tickers = client.fetch_tickers(list(resolved.values()))
            return {
                original: tickers[venue_symbol]
                for original, venue_symbol in resolved.items()
                if venue_symbol in tickers
            }

        return await asyncio.to_thread(fetch)

    async def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str = "1h",
        limit: int = 250,
        since: int | None = None,
    ) -> list[list[float]]:
        client = self._client(authenticated=False)
        return await asyncio.to_thread(
            lambda: client.fetch_ohlcv(self._loaded_market_symbol(client, symbol), timeframe, since, limit)
        )

    async def fetch_order_book(self, symbol: str, limit: int = 20) -> dict[str, Any]:
        client = self._client(authenticated=False)
        return await asyncio.to_thread(
            lambda: client.fetch_order_book(self._loaded_market_symbol(client, symbol), max(int(limit), 5))
        )

    async def fetch_trades(self, symbol: str, limit: int = 100) -> list[dict[str, Any]]:
        client = self._client(authenticated=False)
        return await asyncio.to_thread(
            lambda: client.fetch_trades(self._loaded_market_symbol(client, symbol), None, max(int(limit), 10))
        )

    async def fetch_derivatives_context(self, symbol: str) -> dict[str, Any]:
        """Fetch public perpetual context without changing the execution market."""
        client = self._public_derivatives_client()

        def fetch() -> dict[str, Any]:
            client.load_markets()
            derivative_symbol = self._derivative_symbol(client, symbol)
            funding: dict[str, Any] = {}
            interest: dict[str, Any] = {}
            errors: list[str] = []
            try:
                if client.has.get("fetchFundingRate"):
                    funding = client.fetch_funding_rate(derivative_symbol) or {}
            except Exception as exc:
                errors.append(f"funding:{type(exc).__name__}")
            try:
                if client.has.get("fetchOpenInterest"):
                    interest = client.fetch_open_interest(derivative_symbol) or {}
            except Exception as exc:
                errors.append(f"open_interest:{type(exc).__name__}")
            long_short_ratio = None
            if self.exchange == "binance":
                try:
                    endpoint = getattr(client, "fapiDataGetGlobalLongShortAccountRatio", None)
                    if callable(endpoint):
                        market_id = str(client.market(derivative_symbol).get("id") or "")
                        rows = endpoint({"symbol": market_id, "period": "5m", "limit": 1}) or []
                        if rows:
                            long_short_ratio = self._optional_number(rows[-1].get("longShortRatio"))
                except Exception as exc:
                    errors.append(f"long_short_ratio:{type(exc).__name__}")
            funding_value = self._optional_number(funding.get("fundingRate"))
            interest_value = self._optional_number(
                interest.get("openInterestValue")
                or interest.get("openInterestAmount")
                or interest.get("openInterest")
            )
            return {
                "status": "READY" if funding_value is not None or interest_value is not None else "UNKNOWN",
                "symbol": derivative_symbol,
                "funding_rate": funding_value,
                "open_interest": interest_value,
                "long_short_ratio": long_short_ratio,
                "liquidation_notional": None,
                "errors": errors,
            }

        return await asyncio.to_thread(fetch)

    async def prepare_order(self, symbol: str, amount: float, reference_price: float) -> PreparedOrder:
        return await asyncio.to_thread(self._prepare_order_sync, symbol, amount, reference_price)

    async def prepare_derivatives_symbol(self, symbol: str) -> dict[str, Any]:
        if not self.is_derivatives_market():
            return {"market_type": self.market_type, "margin_mode": None, "leverage": 1}
        leverage = min(
            max(int(self.settings.futures_leverage), 1),
            max(int(self.settings.futures_max_leverage), 1),
        )
        margin_mode = str(self.settings.futures_margin_mode or "isolated").lower()
        if margin_mode not in {"isolated", "cross"}:
            raise RuntimeError(f"Unsupported futures margin mode: {margin_mode}")
        if self.settings.paper_trading or not self.settings.live_trading_enabled:
            return {"market_type": self.market_type, "margin_mode": margin_mode, "leverage": leverage}
        if symbol in self._prepared_derivative_symbols:
            return {"market_type": self.market_type, "margin_mode": margin_mode, "leverage": leverage}
        client = self._client(authenticated=True)

        def configure() -> None:
            venue_symbol = self._loaded_market_symbol(client, symbol)
            if client.has.get("setMarginMode"):
                try:
                    client.set_margin_mode(margin_mode, venue_symbol)
                except ccxt.ExchangeError as exc:
                    if "no need to change margin type" not in str(exc).lower():
                        raise
            elif margin_mode == "isolated":
                raise RuntimeError("Exchange cannot confirm isolated margin mode for this futures market.")
            if client.has.get("setLeverage"):
                client.set_leverage(leverage, venue_symbol)
            else:
                raise RuntimeError("Exchange cannot confirm leverage for this futures market.")

        await asyncio.to_thread(configure)
        self._prepared_derivative_symbols.add(symbol)
        return {"market_type": self.market_type, "margin_mode": margin_mode, "leverage": leverage}

    def _prepare_order_sync(self, symbol: str, amount: float, reference_price: float) -> PreparedOrder:
        client = self._client(authenticated=False)
        try:
            client.load_markets()
            venue_symbol = self._market_symbol(client, symbol)
            market = client.market(venue_symbol)
            normalized_amount = self._amount_to_precision(client, venue_symbol, amount)
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
        venue_symbol = await asyncio.to_thread(self._loaded_market_symbol, client, symbol)
        params = self._order_params(
            client_order_id=client_order_id,
            reduce_only=reduce_only and self.is_derivatives_market(),
        )
        return await asyncio.to_thread(client.create_order, venue_symbol, order_type, side, amount, None, params)

    def supports_native_protective_stops(self) -> bool:
        """Whether a reduce-only exchange stop can be used safely.

        A spot stop has no reliable reduce-only guarantee across the supported
        venues.  It could sell an unrelated wallet balance, so spot positions
        keep the engine's local stop monitor instead.  Binance, OKX and Bybit
        derivatives all accept CCXT's ``stopLossPrice`` and ``reduceOnly``
        parameters.
        """
        market_type = self.market_type
        return (
            not self.settings.paper_trading
            and bool(getattr(self.settings, "native_protective_stops_enabled", True))
            and self.exchange in self._NATIVE_PROTECTIVE_STOP_EXCHANGES
            and market_type in self._DERIVATIVE_MARKET_TYPES
        )

    async def create_protective_stop_order(
        self,
        symbol: str,
        side: str,
        amount: float,
        stop_price: float,
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a reduce-only stop-market close for a derivatives position.

        We intentionally use the unified CCXT ``create_order`` API rather
        than an exchange-specific endpoint.  On Binance, OKX and Bybit CCXT
        maps ``stopLossPrice`` plus a market order to that venue's conditional
        stop order.  The response ID is the acknowledgement we persist before
        relying on the exchange-side protection.
        """
        if not self.supports_native_protective_stops():
            raise RuntimeError("Native protective stop is unavailable for the configured exchange/market type.")
        if amount <= 0 or stop_price <= 0:
            raise RuntimeError("Protective stop amount and trigger price must be positive.")
        if not self.settings.live_trading_enabled:
            raise RuntimeError("Live trading is disabled. Set LIVE_TRADING_ENABLED=true only after paper validation.")
        self._assert_live_safety()
        client = self._client(authenticated=True)
        venue_symbol = await asyncio.to_thread(self._loaded_market_symbol, client, symbol)
        params = self._order_params(client_order_id=client_order_id, reduce_only=True)
        params["stopLossPrice"] = stop_price
        return await asyncio.to_thread(client.create_order, venue_symbol, "market", side, amount, None, params)

    async def cancel_order(self, order_id: str, symbol: str) -> dict[str, Any]:
        if self.settings.paper_trading:
            return {"id": order_id, "symbol": symbol, "status": "canceled"}
        if not self.settings.live_trading_enabled:
            raise RuntimeError("Live trading is disabled.")
        self._assert_live_safety()
        client = self._client(authenticated=True)
        venue_symbol = await asyncio.to_thread(self._loaded_market_symbol, client, symbol)
        return await asyncio.to_thread(client.cancel_order, order_id, venue_symbol)

    async def fetch_order(self, order_id: str, symbol: str) -> dict[str, Any]:
        if self.settings.paper_trading:
            return {"id": order_id, "symbol": symbol, "status": "closed"}
        if not self.settings.live_trading_enabled:
            raise RuntimeError("Live trading is disabled.")
        self._assert_live_safety()
        client = self._client(authenticated=True)
        venue_symbol = await asyncio.to_thread(self._loaded_market_symbol, client, symbol)
        return await asyncio.to_thread(client.fetch_order, order_id, venue_symbol)

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
        if self._derivatives_client is not None:
            clients.append(self._derivatives_client)
            self._derivatives_client = None
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

    def _public_derivatives_client(self) -> ccxt.Exchange:
        if self._derivatives_client is not None:
            return self._derivatives_client
        exchange_class = getattr(ccxt, self.exchange, None)
        if exchange_class is None:
            raise RuntimeError(f"Unsupported exchange: {self.exchange}")
        self._derivatives_client = exchange_class(
            {"enableRateLimit": True, "options": {"defaultType": "swap"}}
        )
        return self._derivatives_client

    def _derivative_symbol(self, client: ccxt.Exchange, symbol: str) -> str:
        if symbol in client.markets and bool(client.markets[symbol].get("contract")):
            return symbol
        base, quote = symbol.split("/", 1)
        for candidate in (f"{base}/{quote}:{quote}", symbol):
            market = client.markets.get(candidate)
            if market and bool(market.get("contract")):
                return candidate
        for candidate, market in client.markets.items():
            if market.get("base") == base and market.get("quote") == quote and market.get("contract"):
                return str(candidate)
        raise RuntimeError(f"No derivatives market found for {symbol}")

    def _loaded_market_symbol(self, client: ccxt.Exchange, symbol: str) -> str:
        client.load_markets()
        return self._market_symbol(client, symbol)

    def _market_symbol(self, client: ccxt.Exchange, symbol: str) -> str:
        return self._derivative_symbol(client, symbol) if self.is_derivatives_market() else symbol

    def _optional_number(self, value: Any) -> float | None:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed == parsed and parsed not in {float("inf"), float("-inf")} else None
