from types import SimpleNamespace

import pytest

from app.schemas.dto import MarketCoin
from app.services.microstructure import EntryGatekeeper, MicrostructureService


class Exchange:
    async def fetch_order_book(self, _symbol, _limit):
        return {
            "bids": [[100, 5], [99.9, 2], [99.8, 1]],
            "asks": [[100.1, 1], [100.2, 1], [100.3, 1]],
        }

    async def fetch_trades(self, _symbol, _limit):
        return [
            {"side": "buy", "amount": 3, "price": 100},
            {"side": "sell", "amount": 1, "price": 100},
        ]

    async def fetch_ohlcv(self, _symbol, timeframe, limit):
        assert timeframe == "1m"
        return [[index, 100, 101, 99, 100 + index * 0.1, 10] for index in range(limit)]


def coin(regime: str = "TRENDING_UP") -> MarketCoin:
    return MarketCoin(
        symbol="BTC/USDT",
        price=100,
        volume_24h=1_000_000_000,
        price_change_percent=2,
        atr=2,
        rsi=60,
        ema20=103,
        ema50=100,
        ema200=90,
        macd=1,
        funding_rate=0,
        open_interest=0,
        rating=90,
        regime=regime,
        regime_score=85,
    )


@pytest.mark.asyncio
async def test_microstructure_combines_book_tape_and_short_momentum():
    service = MicrostructureService(Exchange())
    service.settings = SimpleNamespace(
        entry_microstructure_enabled=True,
        entry_microstructure_timeout_seconds=2,
        entry_microstructure_depth=20,
        entry_microstructure_trade_limit=100,
    )

    snapshot = await service.capture("BTC/USDT")

    assert snapshot["status"] == "READY"
    assert snapshot["available_sources"] == 3
    assert snapshot["order_book_imbalance"] > 0
    assert snapshot["trade_flow_imbalance"] == 0.5
    assert snapshot["price_change_10m_percent"] > 0


def test_gatekeeper_blocks_strong_flow_against_entry():
    gate = EntryGatekeeper()
    gate.settings = SimpleNamespace(
        entry_microstructure_fail_open_risk_multiplier=0.65,
        entry_microstructure_neutral_risk_multiplier=0.75,
        entry_microstructure_max_spread_bps=20,
        entry_microstructure_min_consensus=0.5,
    )
    snapshot = {
        "status": "READY",
        "spread_bps": 3,
        "order_book_available": True,
        "order_book_imbalance": -0.7,
        "tape_available": True,
        "trade_flow_imbalance": -0.5,
        "momentum_available": True,
        "price_change_10m_percent": -0.3,
    }

    assessment = gate.assess(coin(), "BUY", snapshot)

    assert assessment.allowed is False
    assert "opposing flow" in assessment.reason


def test_gatekeeper_never_allows_entry_against_strong_macro_regime():
    gate = EntryGatekeeper()

    assessment = gate.assess(coin("TRENDING_DOWN"), "BUY", {"status": "UNAVAILABLE"})

    assert assessment.allowed is False
    assert "macro gate" in assessment.reason
