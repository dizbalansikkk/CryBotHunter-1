from datetime import datetime, timedelta, timezone

from app.models.entities import Candle
from app.services.backtesting import BacktestingService


def candles(count: int = 260):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    items = []
    price = 100.0
    for index in range(count):
        close = price * (1 + (0.004 if index % 5 else -0.002))
        items.append(
            Candle(
                symbol="BTC/USDT",
                timeframe="1h",
                timestamp=start + timedelta(hours=index),
                open=price,
                high=max(price, close) * 1.01,
                low=min(price, close) * 0.99,
                close=close,
                volume=100_000 + index,
            )
        )
        price = close
    return items


def test_backtest_returns_report_for_candles():
    report = BacktestingService().run(candles())
    assert report.trades_count >= 0
    assert report.win_rate >= 0
    assert report.max_drawdown >= 0


def test_walk_forward_returns_window_summary():
    report = BacktestingService().walk_forward(candles(520), train_size=260, test_size=120, step_size=120)
    assert report.window_count >= 1
    assert report.profitable_windows >= 0
    assert len(report.windows) == report.window_count
    assert report.windows[0].parameters["risk_per_trade"] == 10.0


def test_walk_forward_warms_test_window_without_trading_the_training_slice(monkeypatch):
    service = BacktestingService()
    observed_calls: list[tuple[int, int]] = []
    original_run = service.run

    def observe_run(test_candles, *args, **kwargs):
        observed_calls.append((len(test_candles), kwargs.get("trade_start_index", 0)))
        return original_run(test_candles, *args, **kwargs)

    monkeypatch.setattr(service, "run", observe_run)

    service.walk_forward(candles(520), train_size=260, test_size=120, step_size=120)

    assert (340, 220) in observed_calls


def test_backtest_cost_model_reduces_trade_profit():
    service = BacktestingService()
    position = {"side": "LONG", "entry": 100.0, "volume": 2.0}

    clean_profit = service._profit_after_costs(position, exit_price=110.0, fee_rate=0.0)
    realistic_profit = service._profit_after_costs(position, exit_price=110.0, fee_rate=0.001)

    assert clean_profit == 20.0
    assert realistic_profit < clean_profit
    assert service._apply_slippage(100.0, "buy", 10.0) > 100.0
    assert service._apply_slippage(100.0, "sell", 10.0) < 100.0
