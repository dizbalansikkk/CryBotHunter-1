import pytest
from app.services.exit_comparison import replay_exit
from app.services.risk_manager import RiskManager


def replay(bars, **changes):
    args = dict(bars=bars, entry_time_ms=0, end_time_ms=900000, entry=100,
                volume=1, side="LONG", initial_risk=2, target=104, entry_fee=.1,
                exit_fee_rate=.001, slippage_bps=2)
    return replay_exit(**{**args, **changes})


def test_ambiguous_bar_uses_stop_first_and_charges_both_fees():
    result = replay([[0, 100, 105, 97, 102]])
    price = 98 * .9998
    assert result["reason"] == "STOP_LOSS"
    assert result["net_pnl"] == pytest.approx(price - 100 - .1 - price * .001)
    assert result["ambiguous_bars"] == 1


def test_gap_through_stop_is_not_filled_at_unavailable_stop_price():
    result = replay([[0, 96, 97, 95, 96]])
    assert result["fill"] == pytest.approx(96 * .9998)


def test_trail_uses_completed_bar_extreme_and_does_not_change_fixed_branch():
    bars = [[0, 100, 103, 99, 102.5], [300000, 102.5, 103, 101, 102]]
    fixed = replay(bars)
    trail = replay(bars, trailing_percent=.8)
    assert fixed["status"] == "OPEN_AT_HORIZON"
    assert trail["reason"] == "TRAILING_STOP"
    assert trail["exit_bar_ms"] == 300000


def test_short_stop_and_incomplete_or_missing_candles():
    bars = [[0, 100, 103, 99, 102]]
    result = replay(bars, side="SHORT", target=96)
    assert result["fill"] == pytest.approx(102 * 1.0002)
    assert replay(bars, entry_time_ms=1)["status"] == "INSUFFICIENT_DATA"
    assert replay([[0, 100, 101, 99, 100], [600000, 100, 101, 99, 100]])["status"] == "DATA_GAP"


def test_risk_size_accounts_for_costs_and_minimum_never_overrides_hard_cap():
    risk = RiskManager()
    gross = risk.calculate_position_size(1000, 1, 100, 98)
    net = risk.calculate_position_size(1000, 1, 100, 98, cost_per_unit=.2)
    assert net < gross
    assert net * 2.2 <= 10
    # Minimum fits price-only risk but breaches total risk including costs.
    assert risk.calculate_position_size(100, 1, 100, 98, min_notional=50,
        max_risk_percent=1, cost_per_unit=.2) == 0


@pytest.mark.parametrize("cost", [-1, float("nan"), float("inf")])
def test_invalid_cost_estimate_blocks_sizing(cost):
    assert RiskManager().calculate_position_size(1000, 1, 100, 98, cost_per_unit=cost) == 0
