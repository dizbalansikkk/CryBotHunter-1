from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db.base import Base
from app.models.entities import Position, RlModel
from app.services.scalping_learning import (OrderBookReplay, ScalpFill, ScalpingLearningService,
    cash_preservation_reward, curriculum_report, scalp_walk_forward, profit_centric_reward)
from test_paper_exit_lifecycle import Ledger


def test_reward_requires_net_profit_that_clearly_covers_costs():
    settings = type("S", (), {"scalp_reward_holding_penalty_per_second": .001,
        "scalp_reward_loss_beta": 1, "scalp_reward_timeout_gamma": .5})()
    # Direction without net edge, even though gross is positive, is penalised.
    assert profit_centric_reward(gross_pnl=.24, fees=.1, slippage=.05, initial_risk=.2,
        drawdown=.03, holding_seconds=2, stop_or_loss=False, timeout_or_flat=False, settings=settings) < 0
    assert profit_centric_reward(gross_pnl=.60, fees=.1, slippage=.05, initial_risk=.2,
        drawdown=.03, holding_seconds=2, stop_or_loss=False, timeout_or_flat=False, settings=settings) > 0
    assert profit_centric_reward(gross_pnl=.60, fees=.1, slippage=.05, initial_risk=.2,
        drawdown=.5, holding_seconds=2, stop_or_loss=True, timeout_or_flat=False, settings=settings) < 0
    assert profit_centric_reward(gross_pnl=1, fees=.1, slippage=.05, initial_risk=.2,
        drawdown=0, holding_seconds=200, stop_or_loss=False, timeout_or_flat=True, settings=settings) == -.5


def test_replay_requires_queue_consumption_or_uses_adverse_taker_exit():
    replay = OrderBookReplay(maker_fee_rate=.0002, taker_fee_rate=.0004, slippage_bps=2)
    waiting = replay.maker_or_taker(side="BUY", limit_price=100, quantity=2, visible_queue=5,
        tape=[ScalpFill(1, 100, 6, "sell")], fallback_mark=101)
    assert not waiting.maker and waiting.reason == "TAKER_AFTER_UNFILLED_MAKER"
    assert waiting.exit_price == pytest.approx(101.0202)
    filled = replay.maker_or_taker(side="BUY", limit_price=100, quantity=2, visible_queue=5,
        tape=[ScalpFill(1, 100, 7, "sell")], fallback_mark=101)
    assert filled.maker and filled.exit_price == 100 and filled.slippage == 0


def test_cash_reward_only_applies_to_correct_flat_toxic_market_decision():
    assert cash_preservation_reward(is_flat=True, spread_bps=10, fee_bps=4, depth_usdt=100, required_depth_usdt=1000) == .01
    assert cash_preservation_reward(is_flat=False, spread_bps=10, fee_bps=4, depth_usdt=100, required_depth_usdt=1000) == 0
    assert cash_preservation_reward(is_flat=True, spread_bps=1, fee_bps=4, depth_usdt=2000, required_depth_usdt=1000) == 0


def test_curriculum_cannot_skip_phase_one_sharpe_gate():
    samples = [{"obi": .5, "flow": .5, "false_breakout": False, "net_pnl": value, "reward": value}
        for value in (-1, 1, -1, 1)]
    report = curriculum_report(samples, min_sharpe=1.5)
    assert not report["phases"]["PHASE_1_HIGH_OBI_IMPULSE"]["passed"]
    assert not report["passed"]


def test_walk_forward_requires_each_unseen_window_to_pass():
    good = [{"obi": .5, "flow": .5, "false_breakout": False, "net_pnl": .2, "reward": .2} for _ in range(40)]
    bad = [{"obi": .5, "flow": .5, "false_breakout": True, "net_pnl": -.2, "reward": -1} for _ in range(20)]
    report = scalp_walk_forward(good + bad, oos_size=20, min_sharpe=.1)
    assert len(report["folds"]) == 2 and not report["passed"]


@pytest.mark.asyncio
async def test_candidate_is_rejected_without_oos_net_edge_and_never_activates(monkeypatch):
    service = ScalpingLearningService()
    service.settings = type("S", (), {"scalp_training_enabled": True, "scalp_training_min_trades": 60,
        "scalp_training_min_oos_trades": 10, "scalp_training_min_sharpe": .1,
        "scalp_training_min_net_expectancy": 0, "scalp_reward_holding_penalty_per_second": .001,
        "scalp_reward_loss_beta": 1, "scalp_reward_timeout_gamma": .5})()
    database = create_engine("sqlite://")
    Base.metadata.create_all(database)
    try:
        with Session(database, expire_on_commit=False) as session:
            db = Ledger(session)
            for index in range(60):
                # Training 70% is profitable, final OOS 30% is negative.
                pnl = .2 if index < 40 else -.2
                now = datetime.now(timezone.utc) + timedelta(seconds=index)
                session.add(Position(symbol="ETH/USDT", side="LONG", status="CLOSED", entry_price=100,
                    current_price=100, volume=1, stop=99.8, take=100.3, initial_risk=.2, pnl=pnl,
                    entered_at=now-timedelta(seconds=30), closed_at=now, exit_reason="TAKE_PROFIT" if pnl > 0 else "STOP_LOSS",
                    entry_context={"scalping": {"obi": .6, "flow": .6, "drawdown": .1}, "planned_risk": .2,
                        "scalp_holding_seconds": 20, "entry_execution": {"fee": .01, "volume": 1}, "exit_fills": [],
                        "scalp_replay": {"tape": [{"timestamp": index, "price": 100, "amount": 1, "side": "sell"}]}}))
            await db.commit()
            records = await service.train_candidates(db)
            assert len(records) == 1 and records[0].status == "REJECTED" and not records[0].is_active
            assert session.scalars(select(RlModel).where(RlModel.is_active.is_(True))).all() == []
    finally:
        database.dispose()
