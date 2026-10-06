from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.entities import Order, OrderStatus
from app.services.exchange import ExchangeClient, PreparedOrder


class ExitSizeAssessment:
    """A pre-flight result for a partial/scale-out exit.

    A failed assessment is never sent to the exchange.  It lets the trading
    engine choose a documented monolithic exit instead of learning about a
    venue minimum from a rejected API order.
    """

    def __init__(
        self,
        *,
        allowed: bool,
        amount: float,
        notional: float,
        minimum_notional: float,
        reason: str,
        metadata_available: bool,
    ) -> None:
        self.allowed = allowed
        self.amount = amount
        self.notional = notional
        self.minimum_notional = minimum_notional
        self.reason = reason
        self.metadata_available = metadata_available


class ExecutionService:
    def __init__(self, exchange: ExchangeClient | None = None) -> None:
        self.settings = get_settings()
        self.exchange = exchange or ExchangeClient()

    async def execute_market(
        self,
        db: AsyncSession,
        symbol: str,
        side: str,
        amount: float,
        reference_price: float,
        reason: str,
    ) -> Order:
        order = Order(
            symbol=symbol,
            side=side,
            order_type="market",
            status=OrderStatus.NEW.value,
            requested_amount=amount,
            requested_price=reference_price,
            raw={"reason": reason},
        )
        db.add(order)
        await db.flush()
        try:
            reduce_only = reason.startswith("EXIT") or reason.startswith("PARTIAL_TAKE_PROFIT")
            derivatives_check = getattr(self.exchange, "is_derivatives_market", None)
            is_derivatives = (
                bool(derivatives_check()) if callable(derivatives_check)
                else self.settings.exchange_default_type in {"future", "futures", "swap"}
            )
            if not reduce_only and not is_derivatives and not self.settings.spot_secondary_enabled:
                raise RuntimeError("Secondary spot entries are disabled; futures are the primary trading market.")
            spot_short = side.lower() == "sell" and not reduce_only and not is_derivatives
            if spot_short and self.settings.block_spot_short_entries and (
                not self.settings.paper_trading or not self.settings.allow_paper_short_on_spot
            ):
                raise RuntimeError("Spot short entry is blocked; use a derivatives market or a LONG-only spot strategy.")
            prepare_derivatives = getattr(self.exchange, "prepare_derivatives_symbol", None)
            venue_setup = (
                await prepare_derivatives(symbol)
                if is_derivatives and callable(prepare_derivatives)
                else {
                    "market_type": getattr(self.exchange, "market_type", "spot"),
                    "margin_mode": None,
                    "leverage": 1,
                }
            )
            prepared_order = await self.exchange.prepare_order(symbol, amount, reference_price)
            order.requested_amount = prepared_order.amount
            order.raw = {
                **(order.raw or {}),
                "exchange_metadata": {
                    "fee_rate": prepared_order.fee_rate,
                    "min_amount": prepared_order.min_amount,
                    "min_cost": prepared_order.min_cost,
                    "available": prepared_order.metadata_available,
                },
                "venue_setup": venue_setup,
            }
            if self.settings.paper_trading:
                self._fill_paper(order, reference_price, prepared_order)
            else:
                if not reduce_only:
                    await self._assert_quote_balance(prepared_order.amount, reference_price)
                elif side.lower() == "sell" and not is_derivatives:
                    await self._assert_base_balance(symbol, prepared_order.amount)
                raw = await self.exchange.create_order(
                    symbol,
                    side,
                    prepared_order.amount,
                    "market",
                    client_order_id=self._client_order_id(order.id, reason),
                    reduce_only=reduce_only,
                )
                order.exchange_order_id = str(raw.get("id") or "")
                resolved_raw = await self._resolve_live_order(raw, symbol)
                self._apply_live_result(order, resolved_raw)
        except Exception as exc:
            order.status = OrderStatus.FAILED.value
            order.raw = {"reason": reason, "error": exc.__class__.__name__, "message": str(exc)}
        order.updated_at = datetime.now(timezone.utc)
        return order

    async def assess_exit_size(
        self,
        symbol: str,
        amount: float,
        reference_price: float,
    ) -> ExitSizeAssessment:
        """Check exchange precision/minimums before creating an exit order."""
        try:
            prepared_order = await self.exchange.prepare_order(symbol, amount, reference_price)
        except Exception as exc:
            return ExitSizeAssessment(
                allowed=False,
                amount=0.0,
                notional=0.0,
                minimum_notional=max(float(getattr(self.settings, "min_exit_notional_usdt", 5.0)), 0.0),
                reason=f"exchange minimum check failed: {type(exc).__name__}",
                metadata_available=False,
            )
        normalized_amount = max(float(prepared_order.amount or 0.0), 0.0)
        notional = abs(normalized_amount * max(float(reference_price or 0.0), 0.0))
        configured_minimum = max(float(getattr(self.settings, "min_exit_notional_usdt", 5.0)), 0.0)
        minimum_notional = max(float(prepared_order.min_cost or 0.0), configured_minimum)
        if normalized_amount <= 0:
            return ExitSizeAssessment(
                allowed=False,
                amount=normalized_amount,
                notional=notional,
                minimum_notional=minimum_notional,
                reason="normalized exit amount is zero",
                metadata_available=prepared_order.metadata_available,
            )
        if prepared_order.min_amount is not None and normalized_amount < prepared_order.min_amount:
            return ExitSizeAssessment(
                allowed=False,
                amount=normalized_amount,
                notional=notional,
                minimum_notional=minimum_notional,
                reason=f"amount {normalized_amount:.8f} below exchange minimum {prepared_order.min_amount:.8f}",
                metadata_available=prepared_order.metadata_available,
            )
        if notional < minimum_notional:
            return ExitSizeAssessment(
                allowed=False,
                amount=normalized_amount,
                notional=notional,
                minimum_notional=minimum_notional,
                reason=f"notional {notional:.8f} below required minimum {minimum_notional:.8f}",
                metadata_available=prepared_order.metadata_available,
            )
        return ExitSizeAssessment(
            allowed=True,
            amount=normalized_amount,
            notional=notional,
            minimum_notional=minimum_notional,
            reason="exit size accepted",
            metadata_available=prepared_order.metadata_available,
        )

    def supports_native_protective_stops(self) -> bool:
        check = getattr(self.exchange, "supports_native_protective_stops", None)
        return bool(check()) if callable(check) else False

    async def place_protective_stop(
        self,
        db: AsyncSession,
        *,
        position_id: int,
        symbol: str,
        side: str,
        amount: float,
        stop_price: float,
        stage: str,
    ) -> Order:
        """Persist and submit one exchange-side reduce-only protective stop.

        Unlike an entry/market exit, a successfully acknowledged conditional
        order is expected to remain ``NEW`` until it is triggered.  Its status
        is therefore deliberately not treated as an unconfirmed market fill.
        """
        order = Order(
            symbol=symbol,
            side=side,
            order_type="stop_market",
            status=OrderStatus.NEW.value,
            requested_amount=amount,
            requested_price=stop_price,
            raw={
                "reason": "PROTECTIVE_STOP",
                "order_role": "PROTECTIVE_STOP",
                "position_id": position_id,
                "protection_stage": stage,
                "stop_price": stop_price,
            },
        )
        db.add(order)
        await db.flush()
        try:
            if not self.supports_native_protective_stops():
                raise RuntimeError("Native protective stop is not safe for the configured exchange/market type.")
            prepared_order = await self.exchange.prepare_order(symbol, amount, stop_price)
            order.requested_amount = prepared_order.amount
            raw = await self.exchange.create_protective_stop_order(
                symbol,
                side,
                prepared_order.amount,
                stop_price,
                client_order_id=self._client_order_id(order.id, f"PROTECTIVE_{stage}"),
            )
            exchange_order_id = str(raw.get("id") or "")
            if not exchange_order_id:
                raise RuntimeError("Exchange did not acknowledge the protective stop with an order ID.")
            order.exchange_order_id = exchange_order_id
            order.status = OrderStatus.NEW.value
            order.raw = {
                **(order.raw or {}),
                "exchange_metadata": {
                    "fee_rate": prepared_order.fee_rate,
                    "min_amount": prepared_order.min_amount,
                    "min_cost": prepared_order.min_cost,
                    "available": prepared_order.metadata_available,
                },
                "exchange_response": raw,
                "acknowledged": True,
            }
        except Exception as exc:
            order.status = OrderStatus.FAILED.value
            order.raw = {
                **(order.raw or {}),
                "error": exc.__class__.__name__,
                "message": str(exc),
                "acknowledged": False,
            }
        order.updated_at = datetime.now(timezone.utc)
        return order

    async def cancel_protective_stop(self, order: Order) -> bool:
        if order.status != OrderStatus.NEW.value or not order.exchange_order_id:
            return order.status == OrderStatus.CANCELLED.value
        try:
            raw = await self.exchange.cancel_order(order.exchange_order_id, order.symbol)
        except Exception as exc:
            order.raw = {
                **(order.raw or {}),
                "cancel_error": exc.__class__.__name__,
                "cancel_message": str(exc),
            }
            order.updated_at = datetime.now(timezone.utc)
            return False
        order.status = OrderStatus.CANCELLED.value
        order.raw = {**(order.raw or {}), "cancel_response": raw, "cancelled_by_engine": True}
        order.updated_at = datetime.now(timezone.utc)
        return True

    async def refresh_order(self, order: Order) -> bool:
        """Refresh a persisted live order without inventing a fill."""
        if not order.exchange_order_id or order.exchange_order_id.startswith("paper-"):
            return False
        try:
            raw = await self.exchange.fetch_order(order.exchange_order_id, order.symbol)
        except Exception as exc:
            order.raw = {**(order.raw or {}), "refresh_error": exc.__class__.__name__}
            return False
        self._apply_live_result(order, raw)
        order.updated_at = datetime.now(timezone.utc)
        return True

    async def _resolve_live_order(self, raw: dict, symbol: str) -> dict:
        order_id = str(raw.get("id") or "")
        if not order_id:
            return raw
        try:
            return await self.exchange.fetch_order(order_id, symbol)
        except Exception as exc:
            return {**raw, "fill_resolution_error": type(exc).__name__}

    def _apply_live_result(self, order: Order, raw: dict) -> None:
        filled_amount = self._positive_float(raw.get("filled"))
        average_price = self._positive_float(raw.get("average"))
        requested_amount = max(float(order.requested_amount or 0.0), 0.0)
        status = str(raw.get("status") or "").strip().lower()
        is_terminal = status in {"closed", "filled"}
        is_complete = requested_amount > 0 and filled_amount >= requested_amount * 0.999999
        has_execution = filled_amount > 0 and average_price > 0
        if (is_terminal or is_complete) and has_execution:
            # A terminal exchange order can still have filled less than was
            # requested. Preserve that distinction so callers never close or
            # resize a position using the requested amount by mistake.
            order.status = OrderStatus.FILLED.value if is_complete else OrderStatus.PARTIAL.value
        elif status in {"canceled", "cancelled"}:
            order.status = OrderStatus.CANCELLED.value
        elif status in {"rejected", "expired", "failed"}:
            order.status = OrderStatus.FAILED.value
        else:
            # A market order without a confirmed fill remains pending. The
            # engine must not create a position at a made-up reference price.
            order.status = OrderStatus.NEW.value
        order.filled_amount = filled_amount
        order.average_price = average_price or None
        fee = raw.get("fee") if isinstance(raw.get("fee"), dict) else {}
        order.fee = max(self._positive_float(fee.get("cost")), 0.0)
        order.raw = {
            **(order.raw or {}),
            "exchange_response": raw,
            "fill_confirmed": order.status in {OrderStatus.FILLED.value, OrderStatus.PARTIAL.value},
            "fill_complete": order.status == OrderStatus.FILLED.value,
        }

    def _positive_float(self, value: object) -> float:
        try:
            result = float(value)
        except (TypeError, ValueError):
            return 0.0
        return result if result > 0 and result == result else 0.0

    def _fill_paper(self, order: Order, reference_price: float, prepared_order: PreparedOrder) -> None:
        slippage_direction = 1 if order.side.lower() == "buy" else -1
        slippage = reference_price * (self.settings.paper_slippage_bps / 10_000) * slippage_direction
        average_price = reference_price + slippage
        notional = abs(average_price * order.requested_amount)
        order.exchange_order_id = f"paper-{order.id}"
        order.status = OrderStatus.FILLED.value
        order.filled_amount = order.requested_amount
        order.average_price = round(average_price, 8)
        order.slippage = round(abs(slippage), 8)
        order.fee = round(notional * prepared_order.fee_rate, 8)
        order.raw = {
            **(order.raw or {}),
            "paper": True,
            "fee_rate": prepared_order.fee_rate,
            "slippage_bps": self.settings.paper_slippage_bps,
        }

    def _client_order_id(self, order_id: int, reason: str) -> str:
        safe_reason = "".join(char for char in reason.lower() if char.isalnum() or char == "_")[:24]
        return f"cbh-{order_id}-{safe_reason}"

    async def _assert_quote_balance(self, amount: float, reference_price: float) -> None:
        free_balance = await self.exchange.get_free_balance()
        free_usdt = float(free_balance.get("USDT") or 0.0)
        notional = abs(amount * reference_price)
        if notional > free_usdt * 0.98:
            raise RuntimeError(f"Insufficient free USDT balance for order notional: {notional:.4f} > {free_usdt:.4f}")

    async def _assert_base_balance(self, symbol: str, amount: float) -> None:
        free_balance = await self.exchange.get_free_balance()
        base = symbol.split("/", 1)[0]
        available = float(free_balance.get(base) or 0.0)
        if amount > available * 1.000001:
            raise RuntimeError(
                f"Spot exit cannot sell more {base} than the free bot-visible balance: {amount:.8f} > {available:.8f}"
            )
