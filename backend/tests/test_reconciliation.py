from app.services.reconciliation import OrderReconciliationService


def test_reconciliation_maps_exchange_statuses():
    service = OrderReconciliationService()
    assert service._map_status("closed") == "FILLED"
    assert service._map_status("canceled") == "CANCELLED"
    assert service._map_status("rejected") == "FAILED"
    assert service._map_status("open") == "NEW"
    assert service._map_status("mystery") is None


import pytest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from app.models.entities import Order

@pytest.mark.asyncio
async def test_unknown_submission_is_not_marked_failed_after_timeout():
    class Exchange:
        async def fetch_order_by_client_id(self, client_id, symbol):
            assert client_id == 'stable-id'
            return {'id': 'venue-7', 'status': 'closed', 'filled': 1, 'average': 100}
    order = Order(id=7, symbol='ETH/USDT', status='NEW', requested_amount=1,
        created_at=datetime.now(timezone.utc)-timedelta(hours=1),
        raw={'client_order_id': 'stable-id', 'reconciliation_required': True})
    class Db:
        async def execute(self, query):
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [order]))
        async def commit(self): pass
        def add(self, obj): pass
    result = await OrderReconciliationService(Exchange()).reconcile(Db())
    assert result['failed'] == 0
    assert order.status == 'NEW'
    assert order.exchange_order_id == 'venue-7'
    assert order.raw['reconciliation_required'] is True
    assert order.raw['reconciliation'] == 'inventory_review_required'
