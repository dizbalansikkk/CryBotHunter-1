import pytest

from app.schemas.dto import StrategySignal
from app.services.risk_manager import RiskManager, RiskSettings


def settings(**overrides):
    data = {
        "balance": 1000,
        "risk_percent": 1,
        "daily_risk_percent": 3,
        "max_positions": 2,
        "min_rating": 80,
        "stop_loss_percent": 1.5,
        "take_profit_percent": 3,
        "trailing_stop_percent": 0.8,
    }
    data.update(overrides)
    return RiskSettings(**data)


def test_risk_accepts_valid_signal():
    accepted, reason = RiskManager().can_open(
        StrategySignal(symbol="BTC/USDT", signal="BUY", score=90),
        settings(),
        open_positions_count=0,
        daily_pnl=0,
    )
    assert accepted is True
    assert reason == "risk accepted"


def test_risk_blocks_daily_loss_limit():
    accepted, reason = RiskManager().can_open(
        StrategySignal(symbol="BTC/USDT", signal="BUY", score=90),
        settings(),
        open_positions_count=0,
        daily_pnl=-30,
    )
    assert accepted is False
    assert reason == "daily loss limit reached"


def test_risk_blocks_oversized_trade_risk():
    accepted, reason = RiskManager().can_open(
        StrategySignal(symbol="BTC/USDT", signal="BUY", score=90),
        settings(risk_percent=3),
        open_positions_count=0,
        daily_pnl=0,
    )
    assert accepted is False
    assert reason == "risk per trade above safety limit"


def test_risk_blocks_weak_risk_reward():
    accepted, reason = RiskManager().can_open(
        StrategySignal(symbol="BTC/USDT", signal="BUY", score=90),
        settings(risk_reward_ratio=1.2),
        open_positions_count=0,
        daily_pnl=0,
    )
    assert accepted is False
    assert reason == "risk/reward ratio below safety limit"


def test_risk_blocks_nan_daily_pnl():
    accepted, reason = RiskManager().can_open(
        StrategySignal(symbol="BTC/USDT", signal="BUY", score=90),
        settings(),
        open_positions_count=0,
        daily_pnl=float("nan"),
    )

    assert accepted is False
    assert reason == "invalid risk inputs"


def test_daily_risk_reserve_blocks_combined_open_stop_risk_over_limit():
    accepted, reason = RiskManager().can_add_daily_risk_reserve(
        balance=1000,
        daily_risk_percent=3,
        daily_pnl=-5,
        reserved_open_stop_risk=15,
        candidate_stop_risk=12,
    )

    assert accepted is False
    assert "would exceed limit" in reason


def test_daily_risk_reserve_allows_entry_at_remaining_budget():
    accepted, reason = RiskManager().can_add_daily_risk_reserve(
        balance=1000,
        daily_risk_percent=3,
        daily_pnl=-5,
        reserved_open_stop_risk=15,
        candidate_stop_risk=10,
    )

    assert accepted is True
    assert reason == "daily risk reserve accepted"


def test_position_size_uses_loss_budget():
    size = RiskManager().position_size(balance=1000, risk_percent=1, entry_price=100, stop_price=98)
    assert size == 5


def test_exposure_percent_uses_balance_and_handles_zero_balance():
    manager = RiskManager()

    assert manager.exposure_percent(exposure=250, balance=1000) == 25
    assert manager.exposure_percent(exposure=250, balance=0) == 0


def test_exposure_guard_blocks_gross_limit():
    accepted, reason = RiskManager().can_add_exposure(
        balance=1000,
        current_gross_exposure=2900,
        current_symbol_exposure=0,
        candidate_notional=200,
        max_gross_exposure_percent=300,
        max_symbol_exposure_percent=100,
    )
    assert accepted is False
    assert reason == "gross exposure limit reached"


def test_exposure_guard_blocks_symbol_limit():
    accepted, reason = RiskManager().can_add_exposure(
        balance=1000,
        current_gross_exposure=500,
        current_symbol_exposure=950,
        candidate_notional=100,
        max_gross_exposure_percent=300,
        max_symbol_exposure_percent=100,
    )
    assert accepted is False
    assert reason == "symbol exposure limit reached"


def test_directional_exposure_reduces_risk_when_side_is_crowded():
    accepted, reason, multiplier = RiskManager().directional_exposure(
        side="LONG",
        side_counts={"LONG": 1, "SHORT": 0},
        max_same_side_positions=2,
        reduction_start=1,
        risk_multiplier=0.5,
    )

    assert accepted is True
    assert multiplier == 0.5
    assert reason == "long direction risk reduced to 0.50x"


def test_directional_exposure_blocks_when_side_limit_is_reached():
    accepted, reason, multiplier = RiskManager().directional_exposure(
        side="SHORT",
        side_counts={"LONG": 0, "SHORT": 2},
        max_same_side_positions=2,
        reduction_start=1,
        risk_multiplier=0.5,
    )

    assert accepted is False
    assert multiplier == 0.0
    assert reason == "short direction position limit reached"


def test_dynamic_exits_use_atr_and_risk_reward_for_both_sides():
    manager = RiskManager()

    long_plan = manager.calculate_dynamic_exits(100, atr=2, side="LONG", atr_multiplier=2, risk_reward_ratio=2)
    short_plan = manager.calculate_dynamic_exits(100, atr=2, side="SHORT", atr_multiplier=2, risk_reward_ratio=2)

    assert (long_plan.stop_loss, long_plan.take_profit, long_plan.risk_per_unit) == (96, 108, 4)
    assert (short_plan.stop_loss, short_plan.take_profit, short_plan.risk_per_unit) == (104, 92, 4)


def test_dynamic_exits_fall_back_to_percent_when_atr_is_nan():
    plan = RiskManager().calculate_dynamic_exits(
        100,
        atr=float("nan"),
        side="BUY",
        atr_multiplier=2,
        risk_reward_ratio=2,
        fallback_stop_percent=1.5,
    )

    assert plan.stop_loss == 98.5
    assert plan.take_profit == 103


def test_calculate_position_size_caps_notional_to_deposit_percent():
    size = RiskManager().calculate_position_size(
        balance=1000,
        risk_percent=2,
        entry_price=100,
        stop_price=99,
        max_position_percent=25,
    )

    assert size == 2.5


def test_position_size_returns_zero_for_none_or_nan_inputs():
    manager = RiskManager()

    assert manager.calculate_position_size(1000, 1, 100, float("nan")) == 0
    assert manager.calculate_position_size(1000, 1, 100, None) == 0


def test_drawdown_uses_equity_peak_and_triggers_emergency_at_five_percent():
    assessment = RiskManager().calculate_drawdown(
        starting_equity=1000,
        closed_pnls=[100, -20],
        open_pnl=-40,
        threshold_percent=5,
    )

    assert assessment.peak_equity == 1100
    assert assessment.current_equity == 1040
    assert assessment.drawdown_percent == 5.4545
    assert assessment.emergency is True


def test_drawdown_rejects_nan_instead_of_understating_portfolio_risk():
    with pytest.raises(ValueError, match="closed_pnls"):
        RiskManager().calculate_drawdown(1000, [float("nan")])
