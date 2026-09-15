import ast
import logging
from pathlib import Path

import ccxt
import pytest

from app.core.config import Settings
from app.safety_manager import SafetyCredentials, SafetyManager, ShutdownController, configure_stdout_logging


SAFETY_ENVIRONMENT = (
    "APP_PROCESS",
    "DEFAULT_EXCHANGE",
    "EXCHANGE_DEFAULT_TYPE",
    "PAPER_TRADING",
    "LIVE_TRADING",
    "LIVE_TRADING_ENABLED",
    "EXCHANGE_SANDBOX_ENABLED",
    "API_KEY",
    "API_SECRET",
    "EXCHANGE_API_KEY",
    "EXCHANGE_SECRET_KEY",
    "EXCHANGE_API_SECRET",
    "SAFETY_CHECK_ENABLED",
    "SAFETY_REQUIRE_API_CREDENTIALS",
    "SAFETY_VALIDATE_PRIVATE_API",
    "SAFETY_RETRY_ATTEMPTS",
    "SAFETY_RETRY_INITIAL_SECONDS",
    "SAFETY_RETRY_MAX_SECONDS",
)


class FakeExchange:
    def __init__(self, fail_time_attempts: int = 0, ticker: dict | None = None) -> None:
        self.fail_time_attempts = fail_time_attempts
        self.ticker = ticker or {"symbol": "BTC/USDT", "last": 65_000.0, "timestamp": 1_750_000_000_000}
        self.time_calls = 0
        self.balance_calls = 0
        self.close_calls = 0

    def fetch_time(self) -> int:
        self.time_calls += 1
        if self.time_calls <= self.fail_time_attempts:
            raise ccxt.NetworkError("temporary network failure")
        return 1_750_000_000_000

    def fetch_ticker(self, _symbol: str) -> dict:
        return self.ticker

    def fetch_balance(self) -> dict:
        self.balance_calls += 1
        return {"total": {"USDT": 100.0, "BTC": None}}

    def close(self) -> None:
        self.close_calls += 1


def clear_safety_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in SAFETY_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)


def test_stdout_logging_suppresses_http_client_urls() -> None:
    configure_stdout_logging()

    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


@pytest.mark.asyncio
async def test_paper_preflight_checks_public_real_market_without_credentials(monkeypatch):
    clear_safety_environment(monkeypatch)
    fake = FakeExchange()

    report = await SafetyManager(exchange_factory=lambda _config: fake).run()

    assert report.ok is True
    assert report.mode == "PAPER"
    assert report.last_price == 65_000.0
    assert report.private_api_checked is False
    assert fake.balance_calls == 0
    assert fake.close_calls == 1


@pytest.mark.asyncio
async def test_paper_preflight_does_not_attach_saved_credentials_to_public_client(monkeypatch):
    clear_safety_environment(monkeypatch)
    captured_params: dict = {}

    def build_exchange(params: dict) -> FakeExchange:
        captured_params.update(params)
        return FakeExchange()

    monkeypatch.setattr(ccxt, "binance", build_exchange)
    credentials = SafetyCredentials(
        api_key="saved-key",
        api_secret="saved-secret",
        passphrase="saved-passphrase",
    )

    report = await SafetyManager().run(credentials)

    assert report.mode == "PAPER"
    assert report.private_api_checked is False
    assert "apiKey" not in captured_params
    assert "secret" not in captured_params
    assert "password" not in captured_params


@pytest.mark.asyncio
async def test_live_preflight_exits_when_credentials_are_missing(monkeypatch):
    clear_safety_environment(monkeypatch)
    monkeypatch.setenv("PAPER_TRADING", "false")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")

    with pytest.raises(SystemExit) as exc_info:
        await SafetyManager(exchange_factory=lambda _config: FakeExchange()).run_or_exit()

    assert exc_info.value.code == 1


@pytest.mark.asyncio
async def test_live_preflight_validates_private_balance(monkeypatch):
    clear_safety_environment(monkeypatch)
    monkeypatch.setenv("PAPER_TRADING", "false")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    monkeypatch.setenv("API_KEY", "test-key")
    monkeypatch.setenv("API_SECRET", "test-secret")
    fake = FakeExchange()

    report = await SafetyManager(exchange_factory=lambda _config: fake).run()

    assert report.mode == "LIVE"
    assert report.private_api_checked is True
    assert fake.balance_calls == 1


@pytest.mark.asyncio
async def test_live_preflight_accepts_credentials_saved_in_user_settings(monkeypatch):
    clear_safety_environment(monkeypatch)
    monkeypatch.setenv("PAPER_TRADING", "false")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    fake = FakeExchange()
    credentials = SafetyCredentials(
        exchange="bybit",
        api_key="saved-key",
        api_secret="saved-secret",
        passphrase="saved-passphrase",
    )

    manager = SafetyManager(exchange_factory=lambda config: fake)
    report = await manager.run(credentials)
    config = manager.load_environment(credentials)

    assert report.exchange == "bybit"
    assert report.private_api_checked is True
    assert config.api_key is not None and config.api_key.get_secret_value() == "saved-key"
    assert config.api_secret is not None and config.api_secret.get_secret_value() == "saved-secret"
    assert config.passphrase is not None and config.passphrase.get_secret_value() == "saved-passphrase"


def test_live_preflight_accepts_exchange_api_secret_alias(monkeypatch):
    clear_safety_environment(monkeypatch)
    monkeypatch.setenv("PAPER_TRADING", "false")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    monkeypatch.setenv("EXCHANGE_API_KEY", "alias-key")
    monkeypatch.setenv("EXCHANGE_API_SECRET", "alias-secret")

    config = SafetyManager().load_environment()

    assert config.api_key is not None and config.api_key.get_secret_value() == "alias-key"
    assert config.api_secret is not None and config.api_secret.get_secret_value() == "alias-secret"


def test_runtime_settings_accept_preflight_credential_names(monkeypatch):
    monkeypatch.setenv("API_KEY", "fallback-key")
    monkeypatch.setenv("API_SECRET", "fallback-secret")

    settings = Settings(_env_file=None)

    assert settings.exchange_api_key == "fallback-key"
    assert settings.exchange_secret_key == "fallback-secret"


@pytest.mark.asyncio
async def test_network_failure_retries_with_backoff(monkeypatch):
    clear_safety_environment(monkeypatch)
    monkeypatch.setenv("SAFETY_RETRY_ATTEMPTS", "3")
    monkeypatch.setenv("SAFETY_RETRY_INITIAL_SECONDS", "0.25")
    fake = FakeExchange(fail_time_attempts=2)
    sleeps: list[float] = []

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    report = await SafetyManager(exchange_factory=lambda _config: fake, sleep=record_sleep).run()

    assert report.ok is True
    assert fake.time_calls == 3
    assert fake.close_calls == 3
    assert sleeps == [0.25, 0.5]


@pytest.mark.asyncio
async def test_paper_preflight_network_exhaustion_starts_degraded(monkeypatch, caplog):
    clear_safety_environment(monkeypatch)
    monkeypatch.setenv("SAFETY_RETRY_ATTEMPTS", "2")
    monkeypatch.setenv("SAFETY_RETRY_INITIAL_SECONDS", "0")
    fake = FakeExchange(fail_time_attempts=10)

    with caplog.at_level(logging.ERROR):
        report = await SafetyManager(exchange_factory=lambda _config: fake).run_or_exit()

    assert report.ok is False
    assert report.mode == "PAPER"
    assert fake.time_calls == 2
    assert "starting worker so runtime cycles can retry safely" in caplog.text


@pytest.mark.asyncio
async def test_live_preflight_network_exhaustion_still_exits(monkeypatch):
    clear_safety_environment(monkeypatch)
    monkeypatch.setenv("PAPER_TRADING", "false")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    monkeypatch.setenv("API_KEY", "test-key")
    monkeypatch.setenv("API_SECRET", "test-secret")
    monkeypatch.setenv("SAFETY_RETRY_ATTEMPTS", "1")
    fake = FakeExchange(fail_time_attempts=10)

    with pytest.raises(SystemExit) as exc_info:
        await SafetyManager(exchange_factory=lambda _config: fake).run_or_exit()

    assert exc_info.value.code == 1


@pytest.mark.asyncio
async def test_invalid_exchange_payload_stops_worker(monkeypatch):
    clear_safety_environment(monkeypatch)
    fake = FakeExchange(ticker={"symbol": "BTC/USDT", "last": 0})

    with pytest.raises(SystemExit) as exc_info:
        await SafetyManager(exchange_factory=lambda _config: fake).run_or_exit()

    assert exc_info.value.code == 1
    assert fake.close_calls == 1


@pytest.mark.asyncio
async def test_shutdown_controller_wakes_waiter():
    shutdown = ShutdownController()

    shutdown.request("test")

    assert shutdown.requested is True
    assert await shutdown.wait(30) is True


def test_rl_worker_does_not_import_stable_baselines_service_at_module_load():
    worker_path = Path(__file__).parents[1] / "app" / "rl_worker.py"
    module = ast.parse(worker_path.read_text(encoding="utf-8"))
    top_level_imports = [
        alias.name
        for node in module.body
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    ]

    assert "RlTrainingService" not in top_level_imports


def test_trader_worker_keeps_database_and_trading_engines_distinct():
    worker_path = Path(__file__).parents[1] / "app" / "trader_worker.py"
    module = ast.parse(worker_path.read_text(encoding="utf-8"))
    database_engine_imports = {
        alias.asname
        for node in module.body
        if isinstance(node, ast.ImportFrom) and node.module == "app.db.session"
        for alias in node.names
        if alias.name == "engine"
    }
    main = next(
        node
        for node in module.body
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == "main"
    )
    local_names = {
        node.id
        for node in ast.walk(main)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
    }

    assert database_engine_imports == {"database_engine"}
    assert "database_engine" not in local_names
    assert "trading_engine" in local_names


def test_stable_baselines_callback_stops_immediately_after_shutdown_flag():
    pytest.importorskip("stable_baselines3")
    from app.services.rl_training import ShutdownCallback

    shutdown = ShutdownController()
    callback = ShutdownCallback(lambda: shutdown.requested)

    shutdown.request("test")

    assert callback._on_step() is False
