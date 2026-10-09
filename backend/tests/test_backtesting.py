import pytest
from datetime import datetime, timedelta, timezone

from app.models.entities import Candle
from app.services.backtesting import BacktestingService
from app.schemas.dto import StrategySignal


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


def test_backtest_uses_trailing_quote_volume_and_real_price_change(monkeypatch):
    service = BacktestingService()
    history = candles(260)
    observed = []

    def observe(coin, **kwargs):
        observed.append(coin)
        return StrategySignal(symbol=coin.symbol, signal="WAIT", score=0, reasons=[])

    monkeypatch.setattr(service.strategy, "evaluate", observe)
    service.run(history)
    last = observed[-1]
    assert abs(last.volume_24h - sum(c.volume * c.close for c in history[-24:])) < 0.01
    assert abs(last.price_change_percent - (history[-1].close / history[-25].close - 1) * 100) < 0.0001
    assert last.open_interest == 0  # no fabricated historical derivatives data
    baseline = sum(sum(c.volume * c.close for c in history[i-23:i+1]) for i in range(91, 259)) / 168
    assert abs(last.volume_average_24h - baseline) < 0.01


def test_backtest_filters_opposite_direction(monkeypatch):
    service = BacktestingService()
    monkeypatch.setattr(service.strategy, "evaluate", lambda coin, **kw:
        StrategySignal(symbol=coin.symbol, signal="BUY", score=90, reasons=[]))
    assert service.run(candles(), direction="SELL").trades_count == 0
    assert service.run(candles(), direction="BUY").trades_count > 0


def test_walk_forward_fixed_entry_settings_are_not_reoptimized(monkeypatch):
    service = BacktestingService()
    calls = []
    def fake_run(history, **kwargs):
        calls.append(kwargs)
        return service.summarize([])
    monkeypatch.setattr(service, "run", fake_run)
    monkeypatch.setattr(service, "_best_parameters", lambda *_: (_ for _ in ()).throw(AssertionError("must not optimize")))
    parameters = {"stop_loss_percent": 2.0, "take_profit_percent": 4.0}
    service.walk_forward(candles(520), train_size=260, test_size=120,
        parameters=parameters, direction="SELL", learning_probe=True)
    assert calls and all(c["direction"] == "SELL" and c["learning_probe"] for c in calls)
    assert all(c["stop_loss_percent"] == 2.0 for c in calls)
    assert any(c.get("trade_start_index") == 220 for c in calls)


def test_walk_forward_includes_latest_candle_when_windows_do_not_divide_history():
    history = candles(539)
    result = BacktestingService().walk_forward(history, train_size=260, test_size=120,
        parameters={"stop_loss_percent": 1.5, "take_profit_percent": 3.0})
    assert result.windows[-1].test_end == history[-1].timestamp.isoformat()


def test_stop_gap_fills_at_open_and_intrabar_ties_are_conservative():
    service = BacktestingService()
    long = {"side": "LONG", "stop": 95.0, "take": 110.0}
    short = {"side": "SHORT", "stop": 105.0, "take": 90.0}
    assert service._bar_exit(long, {"open": 90, "low": 89, "high": 102}) == 90
    assert service._bar_exit(short, {"open": 110, "low": 98, "high": 111}) == 110
    assert service._bar_exit(long, {"open": 100, "low": 94, "high": 112}) == 95
    assert service._bar_exit(short, {"open": 100, "low": 88, "high": 106}) == 105
    assert service._bar_exit(long, {"open": 112, "low": 94, "high": 115}) == 110


@pytest.mark.parametrize("activation_r", [0.0, 2.0])
def test_trailing_stop_does_not_apply_retroactively(monkeypatch, activation_r):
    service = BacktestingService()
    history = candles(260)
    monkeypatch.setattr(service.scanner, "calculate_indicators", lambda frame: frame.assign(
        atr=2.0, rsi=55.0, ema20=100.0, ema50=99.0, ema200=98.0, macd=1.0))
    # Entry on bar 257 at 100; bar 258's low precedes its closing-based stop.
    for bar in history:
        bar.open = bar.close = 100.0
        bar.high, bar.low = 100.1, 99.9
    history[258].open, history[258].low, history[258].high, history[258].close = 100, 99, 106, 105
    history[259].open, history[259].low, history[259].high, history[259].close = 105, 103, 106, 104
    calls = []
    def signal(coin, **kw):
        return StrategySignal(symbol=coin.symbol, signal="BUY" if not calls else "WAIT", score=90, reasons=[])
    original = service._bar_exit
    def observe(position, row):
        calls.append((float(row["source_index"]), position["stop"]))
        return original(position, row)
    monkeypatch.setattr(service.strategy, "evaluate", signal)
    monkeypatch.setattr(service, "_bar_exit", observe)
    service.run(history, trade_start_index=257, stop_loss_percent=5, take_profit_percent=20,
                trailing_stop_percent=1, fee_rate=0, slippage_bps=0, trailing_activation_r=activation_r)
    assert len(calls) == 2
    assert calls[0][1] < 96
    if activation_r == 0:
        assert abs(calls[1][1] - 103.95) < 0.001
    else:
        assert calls[1][1] == calls[0][1]


def test_backtest_atr_exit_uses_shared_risk_plan(monkeypatch):
    from app.services.risk_manager import RiskManager
    service = BacktestingService()
    original = RiskManager.calculate_dynamic_exits
    observed = []
    def plan(self, **kwargs):
        result = original(self, **kwargs)
        observed.append((kwargs, result))
        return result
    monkeypatch.setattr(RiskManager, "calculate_dynamic_exits", plan)
    monkeypatch.setattr(service.strategy, "evaluate", lambda coin, **kw:
        StrategySignal(symbol=coin.symbol, signal="SELL", score=90, reasons=[]))
    service.run(candles(), atr_stop_multiplier=1.7, risk_reward_ratio=2.5)
    assert observed
    for args, result in observed:
        assert abs(result.risk_per_unit - args["atr"] * 1.7) < 1e-7
        assert abs((args["entry_price"] - result.take_profit) / result.risk_per_unit - 2.5) < 1e-6
