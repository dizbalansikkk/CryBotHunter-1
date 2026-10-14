from types import SimpleNamespace
from dataclasses import replace

import pytest

from app.services.backtesting import WalkForwardReport, WalkForwardWindow
from app.services.pretrade_quality import PreTradeQualityGate
from app.services.risk_manager import RiskSettings


def window(**overrides):
    data = {
        "index": 0,
        "train_start": "2026-01-01T00:00:00+00:00",
        "train_end": "2026-01-10T00:00:00+00:00",
        "test_start": "2026-01-11T00:00:00+00:00",
        "test_end": "2026-01-20T00:00:00+00:00",
        "parameters": {"risk_per_trade": 10.0},
        "train_profit": 10.0,
        "test_profit": 5.0,
        "test_win_rate": 55.0,
        "test_profit_factor": 1.5,
        "test_max_drawdown": 2.0,
        "test_trades_count": 20,
    }
    data.update(overrides)
    return WalkForwardWindow(**data)


def report(**overrides):
    windows = overrides.pop("windows", [window()])
    data = {
        "windows": windows,
        "window_count": len(windows),
        "profitable_windows": sum(1 for item in windows if item.test_profit > 0),
        "total_profit": sum(item.test_profit for item in windows),
        "average_window_profit": 5.0,
        "average_win_rate": 55.0,
        "average_profit_factor": 1.5,
        "max_drawdown": 2.0,
    }
    data.update(overrides)
    return WalkForwardReport(**data)


def risk_settings():
    return RiskSettings(
        balance=1000.0,
        risk_percent=1.0,
        daily_risk_percent=3.0,
        max_positions=3,
        min_rating=80,
        stop_loss_percent=1.5,
        take_profit_percent=3.0,
        trailing_stop_percent=0.8,
    )


def test_pretrade_quality_blocks_weak_walk_forward():
    gate = PreTradeQualityGate()
    weak_report = report(
        windows=[window(test_profit=-8.0, test_win_rate=25.0, test_profit_factor=0.7, test_trades_count=4)],
        profitable_windows=0,
        total_profit=-8.0,
        average_win_rate=25.0,
        average_profit_factor=0.7,
    )

    decision = gate._decision(weak_report, candles_checked=500, risk_settings=risk_settings())

    assert decision.allowed is False
    assert "pre-trade quality blocked" in decision.reason
    assert decision.profitable_windows_percent == 0
    assert decision.risk_multiplier == 0.0


def test_quality_is_independent_of_candidate_risk_percent():
    gate = PreTradeQualityGate()
    history = report(windows=[window(test_profit=-0.5), window(index=1, test_profit=0.1)])
    decisions = [gate._decision(history, 500, replace(risk_settings(), risk_percent=r)) for r in (0.01, 1, 2)]
    assert all(not d.allowed for d in decisions)
    assert len({d.reason for d in decisions}) == 1
    assert "R (" in decisions[0].reason


def test_empty_windows_do_not_count_as_losing_evidence():
    gate = PreTradeQualityGate()
    history = report(windows=[window()] + [window(index=i, test_profit=0, test_trades_count=0,
        test_win_rate=0, test_profit_factor=0) for i in range(1, 10)],
        average_win_rate=5.5, average_profit_factor=0.15)
    decision = gate._decision(history, 2160, risk_settings())
    assert decision.allowed
    assert decision.profitable_windows_percent == 100
    assert decision.window_count == 1


def test_missing_windows_and_nonfinite_metrics_fail_closed():
    gate = PreTradeQualityGate()
    gate.settings = gate.settings.model_copy(update={"pretrade_quality_require_history": True})
    for history in (report(windows=[]), report(total_profit=float("nan"))):
        decision = gate._decision(history, 500, risk_settings())
        assert not decision.allowed
        assert decision.risk_multiplier == 0


def test_pretrade_quality_reduces_risk_for_borderline_walk_forward():
    gate = PreTradeQualityGate()
    borderline_report = report(
        windows=[window(test_profit=2.0, test_win_rate=40.0, test_profit_factor=1.0, test_trades_count=20)],
        total_profit=2.0,
        average_win_rate=40.0,
        average_profit_factor=1.0,
    )

    decision = gate._decision(borderline_report, candles_checked=500, risk_settings=risk_settings())

    assert decision.allowed is True
    assert "reduced risk" in decision.reason
    assert 0 < decision.risk_multiplier < 1


def test_pretrade_quality_allows_stable_walk_forward():
    gate = PreTradeQualityGate()
    strong_report = report(windows=[window(), window(index=1, test_profit=7.0, test_trades_count=4)], total_profit=12.0)

    decision = gate._decision(strong_report, candles_checked=500, risk_settings=risk_settings())

    assert decision.allowed is True
    assert decision.reason == "pre-trade quality passed"
    assert decision.profitable_windows_percent == 100.0
    assert decision.risk_multiplier == 1.0


def test_pretrade_quality_blocks_profitable_but_insufficient_sample():
    gate = PreTradeQualityGate()
    decision = gate._decision(
        report(windows=[window(test_trades_count=4)]),
        candles_checked=500,
        risk_settings=risk_settings(),
    )
    assert not decision.allowed
    assert "too few historical trades" in decision.reason


def test_paper_learning_probe_can_collect_unknown_setup_at_minimum_risk():
    gate = PreTradeQualityGate()
    gate.settings = gate.settings.model_copy(update={"paper_trading": True, "paper_exploration_enabled": True, "live_trading_enabled": False})
    unknown_report = report(
        windows=[window(test_profit=0.0, test_win_rate=0.0, test_profit_factor=0.0, test_trades_count=0)],
        profitable_windows=0,
        total_profit=0.0,
        average_win_rate=0.0,
        average_profit_factor=0.0,
    )

    decision = gate._decision(
        unknown_report,
        candles_checked=500,
        risk_settings=risk_settings(),
        learning_probe=True,
    )

    assert decision.allowed is True
    assert decision.risk_multiplier == gate.settings.pretrade_quality_min_risk_multiplier
    assert "inconclusive" in decision.reason


def test_paper_learning_probe_still_blocks_negative_sparse_history():
    gate = PreTradeQualityGate()
    gate.settings = gate.settings.model_copy(update={"paper_exploration_enabled": False})
    losing_report = report(
        windows=[window(test_profit=-2.0, test_win_rate=0.0, test_profit_factor=0.0, test_trades_count=1)],
        profitable_windows=0,
        total_profit=-2.0,
        average_win_rate=0.0,
        average_profit_factor=0.0,
    )

    decision = gate._decision(
        losing_report,
        candles_checked=500,
        risk_settings=risk_settings(),
        learning_probe=True,
    )

    assert decision.allowed is False
    assert decision.risk_multiplier == 0.0


@pytest.mark.parametrize("paper,exploration,live,probe,allowed", [
    (True, True, False, True, True),
    (True, True, False, False, False),
    (False, True, False, True, False),
    (True, False, False, True, False),
    (True, True, True, True, False),
    (False, True, True, True, False),
])
def test_negative_history_forward_learning_is_isolated_to_virtual_lane(paper, exploration, live, probe, allowed):
    gate = PreTradeQualityGate()
    gate.settings = gate.settings.model_copy(update={"paper_trading": paper,
        "paper_exploration_enabled": exploration, "live_trading_enabled": live})
    decision = gate._decision(report(windows=[window(test_profit=-20)]), 2160, risk_settings(), learning_probe=probe)
    assert decision.allowed is allowed
    assert decision.total_profit == -20
    if allowed:
        assert 0 < decision.risk_multiplier <= 0.35
        assert "paper learning only" in decision.reason
        assert "historical quality failed" in decision.reason
        assert "negative" in decision.reason
    else:
        assert decision.risk_multiplier == 0


@pytest.mark.parametrize("history", [report(windows=[]), report(total_profit=float("nan")), report(average_profit_factor=float("inf"))])
def test_virtual_exploration_does_not_override_missing_or_invalid_history(history):
    gate = PreTradeQualityGate()
    gate.settings = gate.settings.model_copy(update={"paper_trading": True,
        "paper_exploration_enabled": True, "live_trading_enabled": False, "pretrade_quality_require_history": True})
    decision = gate._decision(history, 500, risk_settings(), learning_probe=True)
    assert not decision.allowed
    assert decision.risk_multiplier == 0


@pytest.mark.asyncio
async def test_pretrade_quality_blocks_entry_until_required_history_is_available(monkeypatch):
    gate = PreTradeQualityGate()
    gate.settings = SimpleNamespace(
        pretrade_quality_enabled=True,
        pretrade_quality_min_candles=420,
        pretrade_quality_require_history=True,
    )

    async def no_history(*_args, **_kwargs):
        return []

    monkeypatch.setattr(gate, "_recent_candles", no_history)

    decision = await gate.assess(None, "BTC/USDT", "1h", risk_settings())

    assert decision.allowed is False
    assert decision.risk_multiplier == 0.0
    assert decision.reason.startswith("pre-trade quality blocked")


@pytest.mark.asyncio
async def test_quality_evaluates_same_direction_lane_and_risk_settings(monkeypatch):
    gate = PreTradeQualityGate()
    observed = {}
    async def history(db, symbol, timeframe, limit):
        observed["limit"] = limit
        return [object()] * 660
    def walk_forward(candles, **kwargs):
        observed.update(kwargs)
        return report(windows=[window(test_trades_count=24)])
    monkeypatch.setattr(gate, "_recent_candles", history)
    monkeypatch.setattr(gate.backtester, "walk_forward", walk_forward)
    await gate.assess(None, "DOT/USDT", "1h", risk_settings(), learning_probe=True, direction="SELL")
    assert observed["limit"] == 2160
    assert observed["direction"] == "SELL"
    assert observed["learning_probe"] is True
    assert observed["parameters"]["stop_loss_percent"] == risk_settings().stop_loss_percent

    assert observed["parameters"]["atr_stop_multiplier"] == risk_settings().atr_stop_multiplier
    assert observed["parameters"]["risk_reward_ratio"] == risk_settings().risk_reward_ratio
