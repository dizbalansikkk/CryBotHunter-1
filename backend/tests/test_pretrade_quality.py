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
        "test_trades_count": 4,
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


def test_pretrade_quality_reduces_risk_for_borderline_walk_forward():
    gate = PreTradeQualityGate()
    borderline_report = report(
        windows=[window(test_profit=2.0, test_win_rate=40.0, test_profit_factor=1.0, test_trades_count=4)],
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


def test_paper_learning_probe_can_collect_unknown_setup_at_minimum_risk():
    gate = PreTradeQualityGate()
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
