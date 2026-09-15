from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user
from app.db.session import get_db
from app.models.entities import Order, User, UserSettings
from app.schemas.dto import OrderOut
from app.services.exchange import ExchangeClient
from app.services.reconciliation import OrderReconciliationService

router = APIRouter(prefix="/orders", tags=["orders"])


@router.get("", response_model=list[OrderOut])
async def list_orders(_: User = Depends(current_user), db: AsyncSession = Depends(get_db), limit: int = 50) -> list[Order]:
    result = await db.execute(select(Order).order_by(Order.created_at.desc()).limit(min(limit, 200)))
    return list(result.scalars().all())


@router.post("/reconcile")
async def reconcile_orders(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)) -> dict[str, int]:
    user_settings = (await db.execute(select(UserSettings).where(UserSettings.user_id == user.id))).scalar_one()
    exchange = ExchangeClient.from_user_settings(user_settings)
    try:
        return await OrderReconciliationService(exchange).reconcile(db)
    finally:
        await exchange.close()
