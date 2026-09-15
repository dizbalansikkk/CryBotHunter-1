from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

pytest.importorskip("stable_baselines3")

import app.services.rl_training as rl_training_module
from app.models.entities import RlModel
from app.services.rl_training import RlTrainingService


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "age_hours", "expected"),
    [
        ("REJECTED", 1, False),
        ("REJECTED", 7, True),
        ("SHADOW", 1, False),
        ("SHADOW", 7, True),
        ("ACTIVE", 12, False),
        ("ACTIVE", 25, True),
    ],
)
async def test_model_refresh_uses_rejected_retry_cooldown(monkeypatch, status, age_hours, expected):
    service = RlTrainingService()
    service.settings = SimpleNamespace(rl_rejected_retry_hours=6, rl_refresh_hours=24)
    latest = SimpleNamespace(
        status=status,
        created_at=datetime.now(timezone.utc) - timedelta(hours=age_hours),
    )

    async def latest_for(_db, _symbol, _timeframe):
        return latest

    monkeypatch.setattr(service, "latest_for", latest_for)

    assert await service.needs_refresh(object(), "BTC/USDT", "1h") is expected


class FakeFrame:
    def __init__(self, length: int):
        self.length = length

    def __len__(self):
        return self.length

    @property
    def iloc(self):
        return self

    def __getitem__(self, item):
        if isinstance(item, slice):
            start = item.start or 0
            stop = self.length if item.stop is None else item.stop
            return FakeFrame(max(stop - start, 0))
        raise TypeError(item)

    def reset_index(self, *, drop: bool):
        assert drop
        return self


@pytest.mark.asyncio
async def test_training_moves_cpu_bound_candidate_search_off_event_loop(monkeypatch):
    service = RlTrainingService()
    service.settings = SimpleNamespace(
        uses_live_market_data=True,
        rl_training_limit=2000,
        rl_min_training_candles=2000,
        rl_validation_percent=25,
        rl_training_timesteps=20000,
        rl_min_validation_return_percent=0.0,
        rl_min_excess_return_percent=0.0,
        rl_min_profitable_seed_ratio=0.5,
        rl_min_validation_profit_factor=1.05,
        rl_min_validation_trades=5,
        rl_max_validation_drawdown_percent=15.0,
    )

    class History:
        async def load(self, *_args, **_kwargs):
            return [object()] * 2000

        async def ingest(self, *_args, **_kwargs):
            raise AssertionError("dataset is already ready")

    service.history = History()
    monkeypatch.setattr(rl_training_module, "build_feature_frame", lambda _candles: FakeFrame(2000))

    fake_model = object()

    def train_candidates(train_frame, validation_frame):
        assert len(train_frame) == 1500
        assert len(validation_frame) == 500
        return fake_model, {
            "return_percent": 1.0,
            "excess_return_percent": -1.0,
            "profitable_seed_ratio": 1.0,
            "max_drawdown_percent": 20.0,
            "profit_factor": 1.1,
            "trades": 10,
        }

    monkeypatch.setattr(service, "_train_candidates", train_candidates)
    monkeypatch.setattr(service, "_serialize", lambda model: b"artifact" if model is fake_model else b"")
    stored_decisions: list[str] = []

    def store_decision(_db, _record, _model, _frame, *, agent_name="rl_policy"):
        stored_decisions.append(agent_name)
        return None

    monkeypatch.setattr(service, "_store_decision", store_decision)
    offloaded: list[object] = []

    async def fake_to_thread(function, *args):
        offloaded.append(function)
        return function(*args)

    monkeypatch.setattr(rl_training_module.asyncio, "to_thread", fake_to_thread)

    class Db:
        def __init__(self):
            self.added: list[RlModel] = []

        def add(self, item):
            self.added.append(item)

        async def execute(self, _statement):
            return None

        async def flush(self):
            return None

        async def commit(self):
            return None

        async def refresh(self, _item):
            return None

    db = Db()
    record = await service.train_symbol(db, "ETH/USDT", "1h")

    assert offloaded == [train_candidates]
    assert record.status == "SHADOW"
    assert record.artifact == b"artifact"
    assert stored_decisions == ["rl_shadow"]


def test_promotion_requires_benchmark_edge_and_seed_stability():
    service = RlTrainingService()
    service.settings = SimpleNamespace(
        rl_min_validation_return_percent=0.0,
        rl_min_excess_return_percent=0.0,
        rl_min_profitable_seed_ratio=0.5,
        rl_min_validation_profit_factor=1.05,
        rl_min_validation_trades=5,
        rl_max_validation_drawdown_percent=15.0,
    )
    strong = {
        "return_percent": 4.0,
        "excess_return_percent": 1.5,
        "profitable_seed_ratio": 1.0,
        "profit_factor": 1.3,
        "trades": 12,
        "max_drawdown_percent": 8.0,
    }

    assert service._passes_promotion(strong)
    assert not service._passes_promotion({**strong, "excess_return_percent": -0.1})
    assert not service._passes_promotion({**strong, "profitable_seed_ratio": 0.0})
    assert "buy-and-hold" in service._promotion_reason({**strong, "excess_return_percent": -0.1})


@pytest.mark.asyncio
async def test_shadow_promotion_requires_both_backtest_and_forward_evidence(monkeypatch):
    service = RlTrainingService()
    service.settings = SimpleNamespace(shadow_forward_max_trial_days=7)
    record = RlModel(
        id=12,
        symbol="BTC/USDT",
        timeframe="1h",
        status="SHADOW",
        is_active=False,
        metrics={"backtest_passed": True},
        created_at=datetime.now(timezone.utc),
    )

    async def shadow_for(_db, _symbol, _timeframe):
        return record

    async def report(_db, _model_id):
        return SimpleNamespace(
            status="PASSED",
            reason="passed",
            closed_trades=7,
            wins=5,
            losses=2,
            win_rate=71.43,
            profit_factor=1.8,
            total_pnl=4.2,
            max_drawdown_percent=2.1,
        )

    class Db:
        async def execute(self, _statement):
            return None

        async def flush(self):
            return None

    monkeypatch.setattr(service, "shadow_for", shadow_for)
    monkeypatch.setattr(service.shadow_trading, "report", report)

    state = await service.evaluate_shadow_promotion(Db(), "BTC/USDT", "1h")

    assert state == "PROMOTED"
    assert record.status == "ACTIVE"
    assert record.is_active is True
    assert record.metrics["promoted_after_forward_test"] is True


@pytest.mark.asyncio
async def test_shadow_without_backtest_edge_never_promotes(monkeypatch):
    service = RlTrainingService()
    record = SimpleNamespace(metrics={"backtest_passed": False})

    async def shadow_for(_db, _symbol, _timeframe):
        return record

    monkeypatch.setattr(service, "shadow_for", shadow_for)

    assert await service.evaluate_shadow_promotion(object(), "BTC/USDT", "1h") == "NONE"


@pytest.mark.asyncio
async def test_excluded_symbol_cleanup_retires_models_and_closes_virtual_positions():
    service = RlTrainingService()

    class Result:
        def __init__(self, rowcount):
            self.rowcount = rowcount

    class Db:
        def __init__(self):
            self.results = [Result(2), Result(1)]
            self.flushes = 0

        async def execute(self, _statement):
            return self.results.pop(0)

        async def flush(self):
            self.flushes += 1

    db = Db()
    result = await service.retire_excluded_symbols(db, ["btc/usdt", "BTC/USDT"])

    assert result == {"models_retired": 2, "shadow_trades_closed": 1}
    assert db.flushes == 1
