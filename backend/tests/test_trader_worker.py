from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

from app.trader_worker import _cycle_blocker, _cycle_metrics, _cycle_summary, _report_due


def test_cycle_summary_makes_automatic_trade_attempt_visible():
    decisions = [
        SimpleNamespace(
            symbol="BTC/USDT",
            signal="BUY",
            action="OPENED",
            score=91,
            reason="risk accepted",
        ),
        SimpleNamespace(
            symbol="ETH/USDT",
            signal="WAIT",
            action="SKIPPED",
            score=72,
            reason="strategy returned WAIT",
        ),
    ]

    summary = _cycle_summary(scanned=2, opened=1, skipped=1, decisions=decisions, closed=1)

    assert "Auto-trade cycle scanned=2 opened=1 skipped=1 closed=1" in summary
    assert "learning_updates=1" in summary
    assert "BTC/USDT=BUY/OPENED(91)" in summary
    assert "directional=1" in summary
    assert "top_opportunities" in summary


def test_cycle_metrics_expose_opportunity_flow_and_top_blocker(monkeypatch):
    monkeypatch.setattr(
        "app.trader_worker.get_settings",
        lambda: type("Settings", (), {"paper_exploration_min_score": 60})(),
    )
    decisions = [
        SimpleNamespace(
            symbol="ETH/USDT",
            signal="WAIT",
            action="SKIPPED",
            score=64,
            reason="strategy WAIT score=64",
        ),
        SimpleNamespace(
            symbol="SOL/USDT",
            signal="SELL",
            action="SKIPPED",
            score=82,
            reason="micro gate blocked",
        ),
    ]

    assert _cycle_metrics(decisions) == {
        "directional_candidates": 1,
        "strong_wait_candidates": 1,
        "top_blocker": "STRATEGY_WAIT",
    }


def test_cycle_blocker_explains_position_capacity_in_production_logs():
    assert _cycle_blocker("position already open for symbol") == "POSITION_ALREADY_OPEN"
    assert _cycle_blocker("maximum open positions reached") == "MAX_POSITIONS"
    assert (
        _cycle_blocker("performance guard recovery position limit reached")
        == "RECOVERY_POSITION_LIMIT"
    )


def test_report_due_respects_interval():
    assert _report_due(None, 15)
    assert not _report_due(datetime.now(timezone.utc) - timedelta(minutes=5), 15)
    assert _report_due(datetime.now(timezone.utc) - timedelta(minutes=20), 15)
