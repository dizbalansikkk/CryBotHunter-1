from datetime import datetime, timedelta, timezone

import pytest

import app.services.optimizer as optimizer_module
from app.models.entities import StrategyOptimization
from app.services.backtesting import BacktestReport
from app.services.optimizer import StrategyOptimizerService
from app.services.risk_manager import RiskSettings


def test_optimizer_score_rewards_profitable_stable_reports():
    service = StrategyOptimizerService()

    strong = service._score(
        total_profit=120,
        profit_factor=2.4,
        win_rate=64,
        max_drawdown=18,
        trades_count=24,
    )
    weak = service._score(
        total_profit=-35,
        profit_factor=0.7,
        win_rate=38,
        max_drawdown=42,
        trades_count=24,
    )

    assert strong > weak


def test_optimizer_score_rejects_empty_reports():
    assert StrategyOptimizerService()._score(0, 0, 0, 0, 0) == -9999


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


def optimization(**overrides):
    data = {
        "symbol": "BTC/USDT",
        "timeframe": "1h",
        "parameters": {
            "stop_loss_percent": 1.0,
            "take_profit_percent": 3.0,
            "trailing_stop_percent": 1.2,
        },
        "score": 88.0,
        "win_rate": 60.0,
        "profit_factor": 1.7,
        "max_drawdown": 10.0,
        "total_profit": 50.0,
        "trades_count": 8,
        "created_at": datetime.now(timezone.utc),
    }
    data.update(overrides)
    return StrategyOptimization(**data)


def test_optimizer_applies_parameters_to_risk_settings():
    service = StrategyOptimizerService()

    optimized, reason = service.apply_to_risk_settings(risk_settings(), optimization())

    assert optimized.stop_loss_percent == 1.0
    assert optimized.take_profit_percent == 3.0
    assert optimized.trailing_stop_percent == 1.2
    assert optimized.risk_reward_ratio == 3.0
    assert "optimizer applied" in reason


def test_optimizer_rejects_stale_optimization(monkeypatch):
    service = StrategyOptimizerService()
    monkeypatch.setattr(service.settings, "strategy_optimizer_max_age_days", 14)
    stale = optimization(created_at=datetime.now(timezone.utc) - timedelta(days=30))

    assert service._is_fresh(stale) is False


def test_optimizer_splits_train_and_validation(monkeypatch):
    service = StrategyOptimizerService()
    candles = list(range(500))
    monkeypatch.setattr(service.settings, "strategy_optimizer_validation_enabled", True)
    monkeypatch.setattr(service.settings, "strategy_optimizer_validation_min_candles", 220)
    monkeypatch.setattr(service.settings, "strategy_optimizer_validation_percent", 30.0)

    train, validation = service._split_train_validation(candles)

    assert len(train) == 280
    assert len(validation) == 220


def test_optimizer_requires_robust_validation(monkeypatch):
    service = StrategyOptimizerService()
    monkeypatch.setattr(service.settings, "strategy_optimizer_validation_enabled", True)
    monkeypatch.setattr(service.settings, "strategy_optimizer_require_validation_pass", True)

    unvalidated = optimization()
    validated = optimization(
        parameters={
            "stop_loss_percent": 1.0,
            "take_profit_percent": 3.0,
            "trailing_stop_percent": 1.2,
            "robustness": {"passed": True},
        }
    )

    assert service._passes_robustness(unvalidated) is False
    assert service._passes_robustness(validated) is True


def test_optimizer_robustness_rejects_bad_validation(monkeypatch):
    service = StrategyOptimizerService()
    monkeypatch.setattr(service.settings, "strategy_optimizer_validation_enabled", True)
    monkeypatch.setattr(service.settings, "strategy_optimizer_min_validation_trades", 2)
    train_report = BacktestReport(60, 1.8, 0, 8, 10, 4, trades_count=12, total_profit=80)
    validation_report = BacktestReport(20, 0.5, 0, 12, 3, 7, trades_count=2, total_profit=-10)

    robustness = service._robustness(train_report, validation_report)

    assert robustness["passed"] is False
    assert "validation profit below threshold" in robustness["reason"]


def test_optimizer_reason_includes_validation_metrics():
    service = StrategyOptimizerService()
    item = optimization(
        parameters={
            "stop_loss_percent": 1.0,
            "take_profit_percent": 3.0,
            "trailing_stop_percent": 1.2,
            "robustness": {
                "passed": True,
                "validation_profit_factor": 1.4,
                "validation_profit": 24.5,
            },
        }
    )

    _optimized, reason = service.apply_to_risk_settings(risk_settings(), item)

    assert "valPF=1.40" in reason
    assert "valPnL=24.50" in reason


@pytest.mark.asyncio
async def test_optimizer_moves_cpu_bound_candidate_search_off_event_loop(monkeypatch):
    service = StrategyOptimizerService()

    class History:
        async def load(self, *_args, **_kwargs):
            return [object()] * 220

        async def ingest(self, *_args, **_kwargs):
            raise AssertionError("dataset is already ready")

    class Db:
        committed = False

        def add(self, _item):
            raise AssertionError("empty candidate result should not add rows")

        async def commit(self):
            self.committed = True

    service.history = History()
    offloaded = []

    def candidate_results(candles, symbol, timeframe, top_n):
        assert len(candles) == 220
        assert (symbol, timeframe, top_n) == ("ETH/USDT", "1h", 5)
        return []

    async def fake_to_thread(function, *args):
        offloaded.append(function)
        return function(*args)

    monkeypatch.setattr(service, "_candidate_results", candidate_results)
    monkeypatch.setattr(optimizer_module.asyncio, "to_thread", fake_to_thread)
    db = Db()

    assert await service.optimize(db, "ETH/USDT", "1h", top_n=5) == []
    assert offloaded == [candidate_results]
    assert db.committed


@pytest.mark.asyncio
async def test_optimizer_refreshes_missing_or_stale_results(monkeypatch):
    service = StrategyOptimizerService()
    monkeypatch.setattr(service.settings, "strategy_optimizer_refresh_hours", 24)

    async def missing_latest(_db, _symbol, _timeframe):
        return None

    async def stale_latest(_db, _symbol, _timeframe):
        return optimization(created_at=datetime.now(timezone.utc) - timedelta(hours=25))

    monkeypatch.setattr(service, "latest_for", missing_latest)
    assert await service.needs_refresh(None, "BTC/USDT", "1h") is True

    monkeypatch.setattr(service, "latest_for", stale_latest)
    assert await service.needs_refresh(None, "BTC/USDT", "1h") is True


@pytest.mark.asyncio
async def test_optimizer_skips_fresh_results(monkeypatch):
    service = StrategyOptimizerService()
    monkeypatch.setattr(service.settings, "strategy_optimizer_refresh_hours", 24)

    async def fresh_latest(_db, _symbol, _timeframe):
        return optimization(created_at=datetime.now(timezone.utc) - timedelta(hours=2))

    monkeypatch.setattr(service, "latest_for", fresh_latest)

    assert await service.needs_refresh(None, "BTC/USDT", "1h") is False
