import csv
import io
import json
from datetime import datetime, timezone
from zipfile import ZipFile

from app.models.entities import LogEntry, Order, Position, Trade, TradePostMortem
from app.services.trade_export import TradeAuditExportService


NOW = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)


def test_trade_audit_archive_contains_full_position_decision_and_execution_history():
    position = Position(
        id=17,
        symbol="ETH/USDT",
        side="LONG",
        entry_price=100,
        current_price=108,
        volume=2,
        stop=96,
        take=110,
        pnl=15,
        status="CLOSED",
        exit_reason="TAKE_PROFIT",
        entered_at=NOW,
        closed_at=NOW,
        entry_context={
            "signal": "BUY",
            "signal_score": 91,
            "decision_reason": "all gates passed",
            "agent_votes": [{"agent": "risk", "action": "ALLOW", "confidence": 0.9}],
            "entry_execution": {"fee": 0.1, "slippage": 0.02},
        },
    )
    trade = Trade(id=4, position_id=17, symbol="ETH/USDT", side="LONG", entry_price=100, exit_price=108, profit=15, created_at=NOW)
    order = Order(
        id=5,
        exchange_order_id="paper-5",
        symbol="ETH/USDT",
        side="buy",
        order_type="ENTRY",
        status="FILLED",
        requested_amount=2,
        filled_amount=2,
        requested_price=100,
        average_price=100,
        fee=0.1,
        slippage=0.02,
        created_at=NOW,
        updated_at=NOW,
    )
    post_mortem = TradePostMortem(
        id=6,
        position_id=17,
        symbol="ETH/USDT",
        side="LONG",
        pnl=15,
        planned_risk=8,
        result_r=1.875,
        shaped_reward=1.5,
        priority=1,
        primary_label="DISCIPLINED_WIN",
        behavior_labels=[],
        strategy_followed=True,
        market_snapshot={},
        execution_snapshot={},
        reward_components={},
        lessons=["keep setup"],
        entered_at=NOW,
        closed_at=NOW,
        replay_count=0,
        created_at=NOW,
    )
    event = LogEntry(id=7, level="INFO", message="Closed ETH/USDT #17: TAKE_PROFIT, pnl=15.00", created_at=NOW)

    payload = TradeAuditExportService().build_archive(
        positions=[position],
        trades=[trade],
        orders=[order],
        post_mortems=[post_mortem],
        events=[event],
        generated_at=NOW,
    )

    with ZipFile(io.BytesIO(payload)) as archive:
        assert set(archive.namelist()) == {
            "README.txt",
            "orders.csv",
            "post-mortems.csv",
            "summary.json",
            "trade-events.csv",
            "trade-fills.csv",
            "trade-journal.csv",
        }
        journal = list(csv.DictReader(io.StringIO(archive.read("trade-journal.csv").decode("utf-8-sig"))))
        summary = json.loads(archive.read("summary.json"))

    assert journal[0]["position_id"] == "17"
    assert journal[0]["decision_reason"] == "all gates passed"
    assert "ALLOW" in journal[0]["agent_votes"]
    assert journal[0]["post_mortem_label"] == "DISCIPLINED_WIN"
    assert summary["positions"] == 1
    assert summary["closed_positions"] == 1
    assert summary["orders"] == 1


def test_trade_audit_archive_is_valid_when_history_is_empty():
    payload = TradeAuditExportService().build_archive(
        positions=[],
        trades=[],
        orders=[],
        post_mortems=[],
        events=[],
        generated_at=NOW,
    )

    with ZipFile(io.BytesIO(payload)) as archive:
        assert json.loads(archive.read("summary.json"))["positions"] == 0
        assert archive.read("trade-journal.csv") == b""
