from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import LogEntry, Order, OrderStatus
from app.services.exchange import ExchangeClient


class OrderReconciliationService:
    def __init__(self, exchange: ExchangeClient | None = None) -> None:
        self.exchange = exchange or ExchangeClient()

    async def reconcile(self, db: AsyncSession, stale_minutes: int = 5) -> dict[str, int]:
        orders = (
            await db.execute(
                select(Order)
                .where(Order.status.in_([OrderStatus.NEW.value, OrderStatus.FILLED.value, OrderStatus.PARTIAL.value]))
                .order_by(Order.created_at.desc())
                .limit(200)
            )
        ).scalars().all()
        checked = 0
        updated = 0
        failed = 0
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=stale_minutes)

        for order in orders:
            checked += 1
            if (
                order.status == OrderStatus.NEW.value
                and order.created_at < cutoff
                and not order.exchange_order_id
            ):
                order.status = OrderStatus.FAILED.value
                order.raw = {**(order.raw or {}), "reconciliation": "stale_new_order"}
                db.add(
                    LogEntry(
                        level="ERROR",
                        message=f"Order #{order.id} marked failed during reconciliation",
                        context={
                            "event": "ORDER_RECONCILIATION_FAILED",
                            "order_id": order.id,
                            "symbol": order.symbol,
                            "reason": "stale_new_order",
                        },
                    )
                )
                failed += 1
                updated += 1
                continue

            if not order.exchange_order_id or order.exchange_order_id.startswith("paper-"):
                continue

            try:
                raw = await self.exchange.fetch_order(order.exchange_order_id, order.symbol)
            except Exception as exc:
                order.raw = {**(order.raw or {}), "reconciliation_error": exc.__class__.__name__}
                continue

            mapped = self._map_status(str(raw.get("status", "")).lower())
            filled_amount = self._positive_float(raw.get("filled"))
            average_price = self._positive_float(raw.get("average"))
            if mapped == OrderStatus.FILLED.value and (filled_amount <= 0 or average_price <= 0):
                order.raw = {
                    **(order.raw or {}),
                    "exchange_snapshot": raw,
                    "reconciliation": "filled_without_execution_details",
                }
                continue
            if (
                mapped == OrderStatus.FILLED.value
                and order.requested_amount > 0
                and filled_amount < order.requested_amount * 0.999999
            ):
                mapped = OrderStatus.PARTIAL.value
            if mapped == OrderStatus.PARTIAL.value and order.status != OrderStatus.PARTIAL.value:
                db.add(
                    LogEntry(
                        level="WARNING",
                        message=(
                            f"Order #{order.id} partially filled during reconciliation: "
                            f"filled={filled_amount:.8f}/{order.requested_amount:.8f}"
                        ),
                        context={
                            "event": "ORDER_PARTIALLY_FILLED",
                            "order_id": order.id,
                            "symbol": order.symbol,
                            "requested_volume": order.requested_amount,
                            "filled_volume": filled_amount,
                            "average_price": average_price,
                        },
                    )
                )
            if mapped and mapped != order.status:
                order.status = mapped
                updated += 1
            if raw.get("filled") is not None:
                order.filled_amount = filled_amount
            if raw.get("average") is not None:
                order.average_price = average_price or None
            order.raw = {**(order.raw or {}), "exchange_snapshot": raw}

        await db.commit()
        return {"checked": checked, "updated": updated, "failed": failed}

    def _map_status(self, status: str) -> str | None:
        if status in {"closed", "filled"}:
            return OrderStatus.FILLED.value
        if status in {"canceled", "cancelled"}:
            return OrderStatus.CANCELLED.value
        if status in {"rejected", "expired", "failed"}:
            return OrderStatus.FAILED.value
        if status in {"open", "new"}:
            return OrderStatus.NEW.value
        return None

    def _positive_float(self, value: object) -> float:
        try:
            result = float(value)
        except (TypeError, ValueError):
            return 0.0
        return result if result > 0 and result == result else 0.0
