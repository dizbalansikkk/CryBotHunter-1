from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.services.history import HistoricalDataService
from app.services.market_scanner import MarketScanner
from app.services.strategy import StrategyCore


class PagedExchange:
    def __init__(self) -> None:
        self.calls: list[tuple[int | None, int]] = []

    async def fetch_ohlcv(self, _symbol, timeframe="1h", limit=250, since=None):
        self.calls.append((since, limit))
        start = since or 1_000_000
        interval = 3_600_000 if timeframe == "1h" else 60_000
        return [[start + index * interval, 1, 2, 0.5, 1.5, 100] for index in range(limit)]


def test_paper_market_data_value_is_real_feed_compatibility_alias():
    assert Settings(_env_file=None, market_data_mode="paper").uses_live_market_data is True
    assert Settings(_env_file=None, market_data_mode="ccxt").uses_live_market_data is True
    assert Settings(_env_file=None, market_data_mode="synthetic").uses_live_market_data is False


def test_rl_worker_trains_by_default(monkeypatch):
    monkeypatch.delenv("RL_TRAINER_ENABLED", raising=False)

    assert Settings(_env_file=None).rl_trainer_enabled is True


def test_default_market_universe_excludes_bitcoin_and_expands_altcoin_coverage():
    settings = Settings(_env_file=None)

    assert len(settings.market_scan_symbols) == 16
    assert "BTC/USDT" not in settings.market_scan_symbols
    assert {"ETH/USDT", "LINK/USDT", "TRX/USDT", "AAVE/USDT", "UNI/USDT", "NEAR/USDT"}.issubset(
        settings.market_scan_symbols
    )
    assert settings.rl_symbols == settings.market_scan_symbols
    assert settings.rl_training_max_per_cycle == 1


def test_exclusion_overrides_legacy_railway_symbol_lists():
    settings = Settings(
        _env_file=None,
        MARKET_SCAN_SYMBOLS="BTC/USDT,eth/usdt,SOL/USDT",
        CANDLE_INGEST_SYMBOLS="BTC/USDT,ETH/USDT",
        RL_SYMBOLS="BTC/USDT,SOL/USDT",
        TRADING_EXCLUDED_SYMBOLS="btc/usdt",
    )

    assert settings.market_scan_symbols == ["ETH/USDT", "SOL/USDT"]
    assert settings.candle_ingest_symbols == ["ETH/USDT"]
    assert settings.rl_symbols == ["SOL/USDT"]
    assert settings.is_symbol_excluded("btc/usdt") is True


def test_strong_spot_setup_can_reach_tradeable_rating_without_open_interest():
    scanner = MarketScanner()
    coin = scanner._coin_from_row(
        {
            "symbol": "BTC/USDT",
            "price": 110.0,
            "volume_24h": 3_000_000_000.0,
            "price_change_percent": 4.0,
            "atr": 3.0,
            "rsi": 62.0,
            "ema20": 105.0,
            "ema50": 100.0,
            "ema200": 90.0,
            "macd": 10.0,
            "funding_rate": 0.0,
            "open_interest": 0.0,
            "bid": 109.99,
            "ask": 110.01,
            "spread_bps": 1.82,
        }
    )

    assert coin.rating > 80
    assert coin.regime == "TRENDING_UP"
    assert StrategyCore().evaluate(coin).signal == "BUY"


def test_adaptive_liquidity_rating_keeps_clean_altcoin_setup_tradeable():
    scanner = MarketScanner()
    coin = scanner._coin_from_row(
        {
            "symbol": "AAVE/USDT",
            "price": 110.0,
            "volume_24h": 20_000_000.0,
            "price_change_percent": 2.0,
            "atr": 2.5,
            "rsi": 62.0,
            "ema20": 105.0,
            "ema50": 100.0,
            "ema200": 95.0,
            "macd": 1.0,
            "funding_rate": 0.0,
            "open_interest": 0.0,
            "bid": 109.97,
            "ask": 110.03,
            "spread_bps": 5.45,
        }
    )

    assert coin.rating > 80
    assert StrategyCore().evaluate(coin).signal == "BUY"


@pytest.mark.asyncio
async def test_history_fetches_multiple_real_market_pages():
    exchange = PagedExchange()
    service = HistoricalDataService(exchange)

    candles = await service._fetch_ccxt_pages("BTC/USDT", "1h", 2_500)

    assert len(candles) == 2_500
    assert [limit for _, limit in exchange.calls] == [1_000, 1_000, 500]
    assert candles == sorted(candles, key=lambda item: item[0])


@pytest.mark.asyncio
async def test_real_scanner_does_not_hide_exchange_failure(monkeypatch):
    class FailingExchange:
        async def fetch_tickers(self, _symbols):
            raise RuntimeError("exchange unavailable")

    monkeypatch.setattr("app.services.market_scanner.get_settings", lambda: SimpleNamespace(uses_live_market_data=True))

    with pytest.raises(RuntimeError, match="exchange unavailable"):
        await MarketScanner(FailingExchange()).scan(["BTC/USDT"])


@pytest.mark.asyncio
async def test_scanner_never_requests_excluded_symbol(monkeypatch):
    class Exchange:
        def __init__(self):
            self.calls = 0

        async def fetch_tickers(self, _symbols):
            self.calls += 1
            return {}

    exchange = Exchange()
    settings = Settings(_env_file=None)
    monkeypatch.setattr("app.services.market_scanner.get_settings", lambda: settings)

    assert await MarketScanner(exchange).scan(["btc/usdt"]) == []
    assert exchange.calls == 0
