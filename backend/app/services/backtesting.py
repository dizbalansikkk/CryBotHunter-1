from dataclasses import dataclass

import pandas as pd

from app.core.config import get_settings
from app.models.entities import Candle
from app.schemas.dto import MarketCoin
from app.services.market_scanner import MarketScanner
from app.services.strategy import StrategyCore


@dataclass
class BacktestReport:
    win_rate: float
    profit_factor: float
    sharpe_ratio: float
    max_drawdown: float
    average_profit: float
    average_loss: float
    trades_count: int = 0
    total_profit: float = 0.0


@dataclass
class WalkForwardWindow:
    index: int
    train_start: str
    train_end: str
    test_start: str
    test_end: str
    parameters: dict[str, float]
    train_profit: float
    test_profit: float
    test_win_rate: float
    test_profit_factor: float
    test_max_drawdown: float
    test_trades_count: int


@dataclass
class WalkForwardReport:
    windows: list[WalkForwardWindow]
    window_count: int
    profitable_windows: int
    total_profit: float
    average_window_profit: float
    average_win_rate: float
    average_profit_factor: float
    max_drawdown: float


class BacktestingService:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.scanner = MarketScanner()
        self.strategy = StrategyCore()

    def run(
        self,
        candles: list[Candle],
        risk_per_trade: float = 10.0,
        stop_loss_percent: float = 1.5,
        take_profit_percent: float = 3.0,
        trailing_stop_percent: float = 0.0,
        fee_rate: float | None = None,
        slippage_bps: float | None = None,
    ) -> BacktestReport:
        if len(candles) < 220:
            return self.summarize([])
        effective_fee_rate = self.settings.paper_fee_rate if fee_rate is None else fee_rate
        effective_slippage_bps = self.settings.paper_slippage_bps if slippage_bps is None else slippage_bps
        frame = pd.DataFrame(
            [
                {
                    "open": item.open,
                    "high": item.high,
                    "low": item.low,
                    "close": item.close,
                    "volume": item.volume,
                }
                for item in candles
            ]
        )
        frame = self.scanner.calculate_indicators(frame).dropna().reset_index(drop=True)
        frame["volume_average"] = frame["volume"].rolling(20, min_periods=1).mean()
        profits: list[float] = []
        position: dict | None = None
        for _, row in frame.iterrows():
            if position:
                exit_price = None
                if position["side"] == "LONG":
                    if trailing_stop_percent > 0:
                        trailed_stop = float(row["close"]) * (1 - trailing_stop_percent / 100)
                        position["stop"] = max(position["stop"], trailed_stop)
                    if row["low"] <= position["stop"]:
                        exit_price = position["stop"]
                    elif row["high"] >= position["take"]:
                        exit_price = position["take"]
                    if exit_price is not None:
                        exit_fill = self._apply_slippage(
                            exit_price,
                            "sell",
                            self._dynamic_slippage_bps(row, effective_slippage_bps),
                        )
                        profits.append(self._profit_after_costs(position, exit_fill, effective_fee_rate))
                        position = None
                else:
                    if trailing_stop_percent > 0:
                        trailed_stop = float(row["close"]) * (1 + trailing_stop_percent / 100)
                        position["stop"] = min(position["stop"], trailed_stop)
                    if row["high"] >= position["stop"]:
                        exit_price = position["stop"]
                    elif row["low"] <= position["take"]:
                        exit_price = position["take"]
                    if exit_price is not None:
                        exit_fill = self._apply_slippage(
                            exit_price,
                            "buy",
                            self._dynamic_slippage_bps(row, effective_slippage_bps),
                        )
                        profits.append(self._profit_after_costs(position, exit_fill, effective_fee_rate))
                        position = None
                if position:
                    continue

            average_volume = float(frame["volume"].rolling(20).mean().iloc[int(row.name)] or row["volume"])
            coin = MarketCoin(
                symbol=candles[0].symbol,
                price=float(row["close"]),
                volume_24h=float(row["volume"]),
                price_change_percent=0,
                atr=float(row["atr"]),
                rsi=float(row["rsi"]),
                ema20=float(row["ema20"]),
                ema50=float(row["ema50"]),
                ema200=float(row["ema200"]),
                macd=float(row["macd"]),
                funding_rate=0,
                open_interest=1_000_000_000,
                rating=85,
            )
            signal = self.strategy.evaluate(coin, average_volume=average_volume)
            if signal.signal in {"BUY", "SELL"}:
                entry = float(row["close"])
                entry_side = "buy" if signal.signal == "BUY" else "sell"
                entry_fill = self._apply_slippage(
                    entry,
                    entry_side,
                    self._dynamic_slippage_bps(row, effective_slippage_bps),
                )
                stop = entry_fill * (1 - stop_loss_percent / 100) if signal.signal == "BUY" else entry_fill * (1 + stop_loss_percent / 100)
                take = entry_fill * (1 + take_profit_percent / 100) if signal.signal == "BUY" else entry_fill * (1 - take_profit_percent / 100)
                volume = risk_per_trade / abs(entry_fill - stop)
                position = {
                    "side": "LONG" if signal.signal == "BUY" else "SHORT",
                    "entry": entry_fill,
                    "stop": stop,
                    "take": take,
                    "volume": volume,
                }
        return self.summarize(profits)

    def walk_forward(
        self,
        candles: list[Candle],
        train_size: int = 300,
        test_size: int = 120,
        step_size: int = 120,
    ) -> WalkForwardReport:
        if len(candles) < train_size + test_size:
            return self._walk_forward_summary([])

        windows: list[WalkForwardWindow] = []
        index = 0
        for start in range(0, len(candles) - train_size - test_size + 1, step_size):
            train = candles[start : start + train_size]
            test = candles[start + train_size : start + train_size + test_size]
            parameters, train_report = self._best_parameters(train)
            test_report = self.run(test, **parameters)
            windows.append(
                WalkForwardWindow(
                    index=index,
                    train_start=train[0].timestamp.isoformat(),
                    train_end=train[-1].timestamp.isoformat(),
                    test_start=test[0].timestamp.isoformat(),
                    test_end=test[-1].timestamp.isoformat(),
                    parameters=parameters,
                    train_profit=train_report.total_profit,
                    test_profit=test_report.total_profit,
                    test_win_rate=test_report.win_rate,
                    test_profit_factor=test_report.profit_factor,
                    test_max_drawdown=test_report.max_drawdown,
                    test_trades_count=test_report.trades_count,
                )
            )
            index += 1
        return self._walk_forward_summary(windows)

    def _best_parameters(self, candles: list[Candle]) -> tuple[dict[str, float], BacktestReport]:
        candidates: list[dict[str, float]] = []
        for stop in [1.0, 1.5, 2.0]:
            for take in [2.0, 3.0, 4.0]:
                for trailing in [0.0, 0.8, 1.2]:
                    candidates.append(
                        {
                            "risk_per_trade": 10.0,
                            "stop_loss_percent": stop,
                            "take_profit_percent": take,
                            "trailing_stop_percent": trailing,
                        }
                    )
        scored: list[tuple[float, dict[str, float], BacktestReport]] = []
        for parameters in candidates:
            report = self.run(candles, **parameters)
            score = report.total_profit + report.profit_factor * 10 + report.win_rate * 0.25 - report.max_drawdown
            scored.append((score, parameters, report))
        _score, parameters, report = max(scored, key=lambda item: item[0])
        return parameters, report

    def _apply_slippage(self, price: float, side: str, slippage_bps: float) -> float:
        direction = 1 if side.lower() == "buy" else -1
        return price * (1 + direction * slippage_bps / 10_000)

    def _dynamic_slippage_bps(self, row, base_slippage_bps: float) -> float:
        close = max(float(row.get("close") or 0.0), 1e-12)
        candle_range_bps = max((float(row.get("high") or close) - float(row.get("low") or close)) / close * 10_000, 0.0)
        intrabar_move_bps = abs(float(row.get("close") or close) - float(row.get("open") or close)) / close * 10_000
        latency_share = min(max(float(self.settings.execution_latency_ms), 0.0) / 3_600_000, 0.05)
        volume = max(float(row.get("volume") or 0.0), 1e-12)
        average_volume = max(float(row.get("volume_average") or volume), 1e-12)
        thin_liquidity_multiplier = min(max(average_volume / volume, 1.0), 4.0)
        spread_proxy = min(candle_range_bps * 0.02, 12.0)
        latency_cost = min(intrabar_move_bps * latency_share, 8.0)
        impact = float(self.settings.execution_market_impact_bps) * thin_liquidity_multiplier
        return round(max(float(base_slippage_bps), 0.0) + spread_proxy + latency_cost + impact, 4)

    def _profit_after_costs(self, position: dict, exit_price: float, fee_rate: float) -> float:
        if position["side"] == "LONG":
            gross_profit = (exit_price - position["entry"]) * position["volume"]
        else:
            gross_profit = (position["entry"] - exit_price) * position["volume"]
        entry_fee = abs(position["entry"] * position["volume"]) * fee_rate
        exit_fee = abs(exit_price * position["volume"]) * fee_rate
        return gross_profit - entry_fee - exit_fee

    def _walk_forward_summary(self, windows: list[WalkForwardWindow]) -> WalkForwardReport:
        if not windows:
            return WalkForwardReport([], 0, 0, 0, 0, 0, 0, 0)
        total_profit = round(sum(window.test_profit for window in windows), 2)
        return WalkForwardReport(
            windows=windows,
            window_count=len(windows),
            profitable_windows=sum(1 for window in windows if window.test_profit > 0),
            total_profit=total_profit,
            average_window_profit=round(total_profit / len(windows), 2),
            average_win_rate=round(sum(window.test_win_rate for window in windows) / len(windows), 2),
            average_profit_factor=round(sum(window.test_profit_factor for window in windows) / len(windows), 2),
            max_drawdown=round(max(window.test_max_drawdown for window in windows), 2),
        )

    def summarize(self, profits: list[float]) -> BacktestReport:
        wins = [value for value in profits if value > 0]
        losses = [abs(value) for value in profits if value < 0]
        gross_profit = sum(wins)
        gross_loss = sum(losses)
        win_rate = len(wins) / len(profits) * 100 if profits else 0
        equity = 0.0
        peak = 0.0
        max_drawdown = 0.0
        for profit in profits:
            equity += profit
            peak = max(peak, equity)
            max_drawdown = max(max_drawdown, peak - equity)
        return BacktestReport(
            win_rate=round(win_rate, 2),
            profit_factor=round(gross_profit / gross_loss, 2) if gross_loss else 0,
            sharpe_ratio=0.0,
            max_drawdown=round(max_drawdown, 2),
            average_profit=round(gross_profit / len(wins), 2) if wins else 0,
            average_loss=round(gross_loss / len(losses), 2) if losses else 0,
            trades_count=len(profits),
            total_profit=round(sum(profits), 2),
        )
