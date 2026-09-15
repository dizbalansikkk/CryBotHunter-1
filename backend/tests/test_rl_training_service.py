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
            "max_drawdown_percent": 20.0,
            "profit_factor": 1.1,
            "trades": 10,
        }

    monkeypatch.setattr(service, "_train_candidates", train_candidates)
    monkeypatch.setattr(service, "_serialize", lambda model: b"artifact" if model is fake_model else b"")
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

        async def flush(self):
            return None

        async def commit(self):
            return None

        async def refresh(self, _item):
            return None

    db = Db()
    record = await service.train_symbol(db, "ETH/USDT", "1h")

    assert offloaded == [train_candidates]
    assert record.status == "REJECTED"
    assert record.artifact == b"artifact"
