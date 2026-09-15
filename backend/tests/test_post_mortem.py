from datetime import datetime, timedelta, timezone

import pytest

from app.models.entities import Order, Position
from app.services.post_mortem import PostMortemService


class Exchange:
    pass


def losing_position(reason: str = "STOP_LOSS") -> Position:
    entered = datetime.now(timezone.utc) - timedelta(minutes=45)
    return Position(
        id=7,
        symbol="BTC/USDT",
        side="LONG",
        entry_price=100,
        current_price=96,
        volume=1,
        stop=96,
        take=108,
        initial_risk=4,
        highest_price=100.4,
        lowest_price=96,
        pnl=-4.1,
        status="CLOSED",
        exit_reason=reason,
        entered_at=entered,
        closed_at=datetime.now(timezone.utc),
        entry_context={"planned_risk": 4},
    )


def test_valid_stop_gets_discipline_credit_instead_of_behavior_penalty():
    service = PostMortemService(Exchange())
    position = losing_position()
    labels, followed = service.classify(
        position=position,
        reason="STOP_LOSS",
        entry_snapshot={},
        path_metrics={"mfe_r": 0.1, "adverse_first_10m_r": -0.2, "recovery_after_invalidation_r": 0.0},
        execution={"cost_to_planned_risk": 0.05},
    )
    components = service.reward_components(-1.0, labels, "STOP_LOSS", followed)

    assert labels == ["VALID_STOP"]
    assert followed is True
    assert components["risk_discipline"] == 0.35
    assert sum(components.values()) == -0.65


def test_post_mortem_labels_avoidable_entry_and_hold_errors():
    service = PostMortemService(Exchange())
    position = losing_position()
    labels, followed = service.classify(
        position=position,
        reason="STOP_LOSS",
        entry_snapshot={
            "order_book_available": True,
            "order_book_imbalance": -0.5,
            "tape_available": True,
            "trade_flow_imbalance": -0.4,
        },
        path_metrics={
            "mfe_r": 0.1,
            "pre_entry_directional_r": 1.2,
            "adverse_first_10m_r": -0.7,
            "recovery_after_invalidation_r": 0.1,
        },
        execution={"cost_to_planned_risk": 0.3},
    )

    assert followed is False
    assert "HELD_AFTER_EARLY_INVALIDATION" in labels
    assert "ENTRY_AGAINST_ORDER_FLOW" in labels
    assert "LATE_ENTRY_EXHAUSTION" in labels
    assert "EXECUTION_COST_DAMAGE" in labels


def test_execution_snapshot_measures_cost_against_planned_risk():
    service = PostMortemService(Exchange())
    order = Order(id=4, fee=0.2, slippage=0.1, filled_amount=2)

    snapshot = service.execution_snapshot(
        {"entry_execution": {"fee": 0.2, "slippage": 0.1, "volume": 2}},
        order,
        volume=2,
        planned_risk=2,
    )

    assert snapshot["total_cost"] == 0.8
    assert snapshot["cost_to_planned_risk"] == 0.4


@pytest.mark.asyncio
async def test_loss_analysis_persists_replay_record_and_enriches_learning_context(monkeypatch):
    class Result:
        def scalar_one_or_none(self):
            return None

    class Db:
        def __init__(self):
            self.added = []

        async def execute(self, _statement):
            return Result()

        def add(self, value):
            self.added.append(value)

        async def flush(self):
            self.added[-1].id = 41

    service = PostMortemService(Exchange())
    position = losing_position()
    order = Order(id=9, fee=0.1, slippage=0.01, filled_amount=1)

    async def snapshot(_symbol):
        return {"status": "READY", "order_book_available": True, "order_book_imbalance": 0.1}

    async def path(_position):
        return []

    monkeypatch.setattr(service.microstructure, "capture", snapshot)
    monkeypatch.setattr(service, "_market_path", path)
    db = Db()

    record = await service.analyze_loss(db, position, order, "STOP_LOSS")

    assert record is db.added[0]
    assert record.primary_label == "VALID_STOP"
    assert record.priority > 1
    assert position.entry_context["post_mortem"]["id"] == 41
    assert position.entry_context["post_mortem"]["strategy_followed"] is True
