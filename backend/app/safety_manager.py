from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import ccxt
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, model_validator

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SafetyCredentials:
    exchange: str | None = None
    api_key: str | None = field(default=None, repr=False)
    api_secret: str | None = field(default=None, repr=False)
    passphrase: str | None = field(default=None, repr=False)


class SafetyConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    process: str = "unknown"
    exchange: str = "binance"
    market_type: str = "spot"
    check_symbol: str = "ETH/USDT"
    paper_trading: bool = True
    live_trading_enabled: bool = False
    sandbox_enabled: bool = True
    api_key: SecretStr | None = None
    api_secret: SecretStr | None = None
    passphrase: SecretStr | None = None
    require_api_credentials: bool = False
    validate_private_api: bool = False
    retry_attempts: int = Field(default=5, ge=1, le=20)
    retry_initial_seconds: float = Field(default=2.0, ge=0, le=60)
    retry_max_seconds: float = Field(default=30.0, ge=0, le=300)

    @model_validator(mode="after")
    def validate_mode_and_credentials(self) -> "SafetyConfiguration":
        if self.paper_trading and self.live_trading_enabled:
            raise ValueError("PAPER_TRADING and LIVE_TRADING cannot both be enabled")
        if not self.paper_trading and not self.live_trading_enabled:
            raise ValueError("either PAPER_TRADING or LIVE_TRADING must be enabled")
        has_key = self.api_key is not None and bool(self.api_key.get_secret_value())
        has_secret = self.api_secret is not None and bool(self.api_secret.get_secret_value())
        if has_key != has_secret:
            raise ValueError("API_KEY and API_SECRET must be configured together")
        if self.require_api_credentials and not (has_key and has_secret):
            raise ValueError("API_KEY and API_SECRET are required for this process")
        return self


class ExchangeTimePayload(BaseModel):
    server_time: int = Field(gt=0)


class ExchangeTickerPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    symbol: str = Field(min_length=3)
    last: float = Field(gt=0)
    timestamp: int | None = Field(default=None, gt=0)


class ExchangeBalancePayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    total: dict[str, float | None]


class SafetyReport(BaseModel):
    ok: bool
    exchange: str
    process: str
    mode: str
    server_time: int | None = None
    symbol: str | None = None
    last_price: float | None = None
    private_api_checked: bool = False


class SafetyCheckError(RuntimeError):
    pass


class SafetyNetworkError(SafetyCheckError):
    """A transient exchange connectivity failure after all retries."""


class ShutdownController:
    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._signal_flag = threading.Event()
        self._installed = False
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def requested(self) -> bool:
        return self._signal_flag.is_set()

    def install(self) -> None:
        if self._installed:
            return
        self._loop = asyncio.get_running_loop()
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, self._handle_signal)
        self._installed = True
        logger.info("Graceful shutdown handlers installed for SIGTERM and SIGINT")

    def request(self, reason: str = "shutdown requested") -> None:
        self._signal_flag.set()
        self._complete_request(reason)

    def _handle_signal(self, signum: int, _frame: Any) -> None:
        self._signal_flag.set()
        reason = signal.Signals(signum).name
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._complete_request, reason)

    def _complete_request(self, reason: str) -> None:
        if self._event.is_set():
            return
        logger.warning("Graceful shutdown requested: %s", reason)
        self._event.set()

    async def wait(self, timeout: float) -> bool:
        if self.requested:
            return True
        try:
            await asyncio.wait_for(self._event.wait(), timeout=max(timeout, 0.0))
        except TimeoutError:
            return False
        return True


ExchangeFactory = Callable[[SafetyConfiguration], Any]
SleepCallable = Callable[[float], Awaitable[None]]


class SafetyManager:
    def __init__(
        self,
        exchange_factory: ExchangeFactory | None = None,
        sleep: SleepCallable = asyncio.sleep,
    ) -> None:
        self.exchange_factory = exchange_factory or self._default_exchange_factory
        self.sleep = sleep

    async def run(self, credentials: SafetyCredentials | None = None) -> SafetyReport:
        config = self.load_environment(credentials)
        mode = "PAPER" if config.paper_trading else "LIVE"
        logger.info(
            "Pre-flight starting process=%s exchange=%s market=%s mode=%s",
            config.process,
            config.exchange,
            config.market_type,
            mode,
        )
        if not config.enabled:
            logger.warning("Pre-flight disabled by SAFETY_CHECK_ENABLED=false")
            return SafetyReport(ok=True, exchange=config.exchange, process=config.process, mode=mode)
        if not config.paper_trading:
            logger.warning("LIVE TRADING MODE IS ENABLED: real funds may be used")
        elif not config.require_api_credentials:
            logger.info("Paper/RL pre-flight uses public Binance endpoints; private credentials are not required")

        report = await self._probe_with_retry(config)
        logger.info(
            "Pre-flight passed exchange=%s symbol=%s price=%s private_api=%s",
            report.exchange,
            report.symbol,
            report.last_price,
            report.private_api_checked,
        )
        return report

    async def run_or_exit(self, credentials: SafetyCredentials | None = None) -> SafetyReport:
        config: SafetyConfiguration | None = None
        try:
            config = self.load_environment(credentials)
            return await self.run(credentials)
        except SafetyNetworkError as exc:
            if config is not None and config.paper_trading and not config.validate_private_api:
                logger.error(
                    "Paper pre-flight degraded: %s; starting worker so runtime cycles can retry safely",
                    self._safe_error(exc),
                )
                return SafetyReport(
                    ok=False,
                    exchange=config.exchange,
                    process=config.process,
                    mode="PAPER",
                )
            logger.critical("Pre-flight failed: %s", self._safe_error(exc))
            sys.exit(1)
        except (SafetyCheckError, ValidationError, ValueError) as exc:
            logger.critical("Pre-flight failed: %s", self._safe_error(exc))
            sys.exit(1)

    def load_environment(self, credentials: SafetyCredentials | None = None) -> SafetyConfiguration:
        live = _env_bool("LIVE_TRADING", _env_bool("LIVE_TRADING_ENABLED", False))
        require_credentials = _env_bool("SAFETY_REQUIRE_API_CREDENTIALS", live)
        stored = credentials or SafetyCredentials()
        api_key = _normalized_optional(stored.api_key) or _first_env("API_KEY", "EXCHANGE_API_KEY")
        api_secret = _normalized_optional(stored.api_secret) or _first_env(
            "API_SECRET", "EXCHANGE_SECRET_KEY", "EXCHANGE_API_SECRET"
        )
        passphrase = _normalized_optional(stored.passphrase) or _first_env(
            "API_PASSPHRASE", "EXCHANGE_PASSPHRASE"
        )
        exchange = _normalized_optional(stored.exchange) or _env_text("DEFAULT_EXCHANGE", "binance")
        return SafetyConfiguration(
            enabled=_env_bool("SAFETY_CHECK_ENABLED", True),
            process=_env_text("APP_PROCESS", "unknown"),
            exchange=exchange.lower(),
            market_type=_env_text("EXCHANGE_DEFAULT_TYPE", "spot").lower(),
            check_symbol=_env_text("SAFETY_CHECK_SYMBOL", "ETH/USDT").upper(),
            paper_trading=_env_bool("PAPER_TRADING", True),
            live_trading_enabled=live,
            sandbox_enabled=_env_bool("EXCHANGE_SANDBOX_ENABLED", True),
            api_key=SecretStr(api_key) if api_key else None,
            api_secret=SecretStr(api_secret) if api_secret else None,
            passphrase=SecretStr(passphrase) if passphrase else None,
            require_api_credentials=require_credentials,
            validate_private_api=_env_bool("SAFETY_VALIDATE_PRIVATE_API", require_credentials),
            retry_attempts=_env_int("SAFETY_RETRY_ATTEMPTS", 5),
            retry_initial_seconds=_env_float("SAFETY_RETRY_INITIAL_SECONDS", 2.0),
            retry_max_seconds=_env_float("SAFETY_RETRY_MAX_SECONDS", 30.0),
        )

    async def _probe_with_retry(self, config: SafetyConfiguration) -> SafetyReport:
        delay = config.retry_initial_seconds
        for attempt in range(1, config.retry_attempts + 1):
            try:
                return await asyncio.to_thread(self._probe_sync, config)
            except (ccxt.NetworkError, TimeoutError, ConnectionError) as exc:
                if attempt >= config.retry_attempts:
                    raise SafetyNetworkError(
                        f"exchange network check exhausted {config.retry_attempts} attempts: {type(exc).__name__}"
                    ) from exc
                logger.warning(
                    "Pre-flight network attempt %s/%s failed (%s); retrying in %.1fs",
                    attempt,
                    config.retry_attempts,
                    type(exc).__name__,
                    delay,
                )
                await self.sleep(delay)
                delay = min(max(delay * 2, 0.1), config.retry_max_seconds)
            except ValidationError as exc:
                raise SafetyCheckError(f"exchange returned invalid data: {exc.errors(include_url=False)}") from exc
            except ccxt.AuthenticationError as exc:
                raise SafetyCheckError("exchange rejected API credentials") from exc
            except ccxt.PermissionDenied as exc:
                raise SafetyCheckError("API credentials do not have permission for the safety request") from exc
            except ccxt.BaseError as exc:
                raise SafetyCheckError(f"exchange rejected pre-flight request: {type(exc).__name__}") from exc
            except SafetyCheckError:
                raise
            except Exception as exc:
                raise SafetyCheckError(f"unexpected pre-flight error: {type(exc).__name__}") from exc
        raise SafetyCheckError("pre-flight retry loop ended unexpectedly")

    def _probe_sync(self, config: SafetyConfiguration) -> SafetyReport:
        client = self.exchange_factory(config)
        try:
            time_payload = ExchangeTimePayload(server_time=client.fetch_time())
            ticker = ExchangeTickerPayload.model_validate(client.fetch_ticker(config.check_symbol))
            private_checked = False
            if config.validate_private_api:
                ExchangeBalancePayload.model_validate(client.fetch_balance())
                private_checked = True
            return SafetyReport(
                ok=True,
                exchange=config.exchange,
                process=config.process,
                mode="PAPER" if config.paper_trading else "LIVE",
                server_time=time_payload.server_time,
                symbol=ticker.symbol,
                last_price=ticker.last,
                private_api_checked=private_checked,
            )
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    logger.exception("Failed to close pre-flight exchange session")

    def _default_exchange_factory(self, config: SafetyConfiguration) -> ccxt.Exchange:
        exchange_class = getattr(ccxt, config.exchange, None)
        if exchange_class is None:
            raise SafetyCheckError(f"unsupported exchange: {config.exchange}")
        params: dict[str, Any] = {
            "enableRateLimit": True,
            "timeout": 20_000,
            "options": {"defaultType": config.market_type},
        }
        # Public paper/RL probes must stay credential-free even when the user has
        # exchange keys saved for a future live session. Some exchanges validate
        # supplied keys on otherwise public endpoints, which can make a safe paper
        # worker fail before it starts.
        if config.validate_private_api and config.api_key and config.api_secret:
            params["apiKey"] = config.api_key.get_secret_value()
            params["secret"] = config.api_secret.get_secret_value()
            if config.passphrase:
                params["password"] = config.passphrase.get_secret_value()
        client = exchange_class(params)
        if config.validate_private_api and config.sandbox_enabled and hasattr(client, "set_sandbox_mode"):
            client.set_sandbox_mode(True)
        return client

    def _safe_error(self, exc: Exception) -> str:
        return str(exc).replace("\n", " ")[:800]


def configure_stdout_logging() -> None:
    logging.basicConfig(
        level=_env_text("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    # httpx includes the complete request URL in INFO records. Telegram puts the
    # bot token in that URL, so keep transport-level records out of production
    # logs while preserving our own success/failure messages.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def _env_text(name: str, default: str) -> str:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().strip('"').strip("'") or default


def _first_env(*names: str) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value is not None:
            normalized = value.strip().strip('"').strip("'")
            if normalized:
                return normalized
    return None


def _normalized_optional(value: str | None) -> str | None:
    if value is None:
        return None
    return value.strip().strip('"').strip("'") or None


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().strip('"').strip("'").lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def _env_int(name: str, default: int) -> int:
    return int(_env_text(name, str(default)))


def _env_float(name: str, default: float) -> float:
    return float(_env_text(name, str(default)))


async def main() -> None:
    configure_stdout_logging()
    shutdown = ShutdownController()
    shutdown.install()
    await SafetyManager().run_or_exit()


if __name__ == "__main__":
    asyncio.run(main())
