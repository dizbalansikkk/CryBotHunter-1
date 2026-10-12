import pytest

from app.services.course_research import (
    ResearchBar, ResearchParameters, course_contexts, evaluate_sweeps,
)


def bars(rows):
    return [ResearchBar(i * 3600000, *row) for i, row in enumerate(rows)]


def sample():
    return bars([
        (100, 102, 98, 100), (100, 105, 99, 102),
        (102, 103, 100, 101), (101, 106, 100, 104),
        (104, 107, 95, 100), (100, 103, 97, 101),
        (101, 102, 98, 100),
    ])


def test_pivot_requires_closed_right_neighbor_and_sweep_is_consumed():
    contexts = course_contexts(sample(), 3600000)
    assert contexts[1].swing_high is None
    assert contexts[2].swing_high.index == 1
    assert contexts[2].swing_high.confirmed_at == 2
    assert contexts[3].upper_sweep
    assert not contexts[4].upper_sweep


def test_all_contexts_are_prefix_invariant():
    data = sample()
    complete = course_contexts(data, 3600000)
    for end in range(1, len(data) + 1):
        assert course_contexts(data[:end], 3600000) == complete[:end]


def test_equal_highs_are_not_strict_swings():
    data = bars([(100, 105, 99, 100)] * 4)
    assert all(c.swing_high is None for c in course_contexts(data, 3600000))


def test_fvg_is_known_only_on_third_close():
    data = bars([(100, 101, 99, 100), (100, 105, 100, 104), (104, 106, 103, 105)])
    result = course_contexts(data, 3600000)
    assert result[1].fvg is None
    assert result[2].fvg == {"side": "LONG", "low": 101, "high": 103, "confirmed_at": 2}


def test_next_open_execution_and_stop_first_ambiguity():
    result = evaluate_sweeps(sample(), 3600000, start=0, end=7,
        parameters=ResearchParameters(fee_rate=0, slippage_bps=0,
                                      stop_buffer_bps=0, max_holding_bars=2))
    trade = result["trades"][0]
    assert trade["signal_index"] == 3
    assert trade["entry_index"] == 4
    assert trade["entry"] == 104
    assert trade["stop"] == 106
    assert trade["take"] == 100
    assert trade["reason"] == "STOP"
    assert trade["net_r"] == -1
    assert result["promotion_allowed"] is False


def test_costs_reduce_result_and_slice_does_not_cross_end():
    result = evaluate_sweeps(sample(), 3600000, start=0, end=7,
        parameters=ResearchParameters(max_holding_bars=2))
    assert result["trades"][0]["net_r"] < -1
    assert result["trades"][0]["fees_r"] > 0
    short = evaluate_sweeps(sample(), 3600000, start=0, end=5,
        parameters=ResearchParameters(max_holding_bars=2))
    assert short["trades_count"] == 0


def test_missing_bar_and_nonfinite_price_fail_closed():
    data = sample()
    with pytest.raises(ValueError, match="missing"):
        course_contexts(data[:2] + data[3:], 3600000)
    with pytest.raises(ValueError, match="invalid price"):
        course_contexts([ResearchBar(0, 1, float("nan"), 1, 1)], 3600000)


@pytest.mark.parametrize("parameters", [
    ResearchParameters(fee_rate=-1), ResearchParameters(slippage_bps=float("nan")),
    ResearchParameters(reward_r=0), ResearchParameters(max_holding_bars=0),
])
def test_invalid_simulation_parameters(parameters):
    with pytest.raises(ValueError):
        evaluate_sweeps(sample(), 3600000, start=0, end=7, parameters=parameters)
