from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import gymnasium as gym
import numpy as np
import pandas as pd
from gymnasium import spaces
from app.services.market_features import MICRO_FEATURES, join_market_features


FEATURE_NAMES = [
    "return_1",
    "return_5",
    "ema_gap",
    "rsi",
    "atr_percent",
    "volume_zscore",
]
FEATURE_NAMES += MICRO_FEATURES + [name + "_available" for name in MICRO_FEATURES]


def build_feature_frame(candles: Sequence[Any]) -> pd.DataFrame:
    rows = [
        {
            "timestamp": candle.timestamp,
            "open": float(candle.open),
            "high": float(candle.high),
            "low": float(candle.low),
            "close": float(candle.close),
            "volume": float(candle.volume),
        }
        for candle in candles
    ]
    frame = pd.DataFrame(rows).sort_values("timestamp").reset_index(drop=True)
    close = frame["close"].clip(lower=1e-12)
    returns = close.pct_change()
    ema20 = close.ewm(span=20, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = -delta.clip(upper=0).ewm(alpha=1 / 14, adjust=False).mean()
    rsi = 100 - (100 / (1 + gain / loss.replace(0, np.nan)))
    true_range = pd.concat(
        [
            frame["high"] - frame["low"],
            (frame["high"] - close.shift()).abs(),
            (frame["low"] - close.shift()).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = true_range.ewm(alpha=1 / 14, adjust=False).mean()
    volume_mean = frame["volume"].rolling(50).mean()
    volume_std = frame["volume"].rolling(50).std().replace(0, np.nan)

    frame["return_1"] = (returns * 10).clip(-2, 2)
    frame["return_5"] = (close.pct_change(5) * 5).clip(-2, 2)
    frame["ema_gap"] = ((ema20 / ema50) - 1).mul(20).clip(-2, 2)
    frame["rsi"] = ((rsi.fillna(50) - 50) / 25).clip(-2, 2)
    frame["atr_percent"] = ((atr / close) * 20).clip(0, 2)
    frame["volume_zscore"] = ((frame["volume"] - volume_mean) / volume_std).clip(-3, 3)
    frame = frame.replace([np.inf, -np.inf], np.nan).dropna().reset_index(drop=True)
    return join_market_features(frame, [])


class CryptoTradingEnv(gym.Env):
    metadata = {"render_modes": []}

    def __init__(
        self,
        frame: pd.DataFrame,
        fee_rate: float = 0.0004,
        slippage_bps: float = 2.0,
        drawdown_penalty: float = 0.15,
        latency_ms: int = 250,
        market_impact_bps: float = 1.5,
        behavior_penalty: float = 0.35,
        strategy_adherence_bonus: float = 0.03,
    ) -> None:
        super().__init__()
        if len(frame) < 100:
            raise ValueError("RL environment requires at least 100 prepared candles")
        self.frame = frame.reset_index(drop=True)
        self.fee_rate = max(float(fee_rate), 0.0)
        self.slippage_rate = max(float(slippage_bps), 0.0) / 10_000
        self.drawdown_penalty = max(float(drawdown_penalty), 0.0)
        self.latency_ms = max(int(latency_ms), 0)
        self.market_impact_rate = max(float(market_impact_bps), 0.0) / 10_000
        self.behavior_penalty = max(float(behavior_penalty), 0.0)
        self.strategy_adherence_bonus = max(float(strategy_adherence_bonus), 0.0)
        self.action_space = spaces.Discrete(3)  # 0 flat, 1 long, 2 short
        self.observation_space = spaces.Box(low=-5.0, high=5.0, shape=(len(FEATURE_NAMES) + 2,), dtype=np.float32)
        self.index = 0
        self.position = 0.0
        self.equity = 1.0
        self.peak_equity = 1.0
        self.max_drawdown = 0.0
        self.trades = 0
        self.closed_trades = 0
        self.positive_returns = 0.0
        self.negative_returns = 0.0
        self.position_return = 0.0
        self.peak_position_return = 0.0
        self.adverse_steps = 0
        self.trade_max_drawdown = 0.0
        self.losing_hours = 0.0
        self.behavior_penalties_total = 0.0
        self.strategy_bonuses_total = 0.0
        self.turnover_total = 0.0
        self.execution_cost_total = 0.0

    def reset(self, *, seed: int | None = None, options: dict | None = None) -> tuple[np.ndarray, dict]:
        super().reset(seed=seed)
        self.index = 0
        self.position = 0.0
        self.equity = 1.0
        self.peak_equity = 1.0
        self.max_drawdown = 0.0
        self.trades = 0
        self.closed_trades = 0
        self.positive_returns = 0.0
        self.negative_returns = 0.0
        self.position_return = 0.0
        self.peak_position_return = 0.0
        self.adverse_steps = 0
        self.trade_max_drawdown = 0.0
        self.losing_hours = 0.0
        self.behavior_penalties_total = 0.0
        self.strategy_bonuses_total = 0.0
        self.turnover_total = 0.0
        self.execution_cost_total = 0.0
        return self._observation(), {}

    def step(self, action: int) -> tuple[np.ndarray, float, bool, bool, dict]:
        target_position = (0.0, 1.0, -1.0)[int(action)]
        row = self.frame.iloc[self.index]
        next_row = self.frame.iloc[self.index + 1]
        hours = max((pd.Timestamp(next_row["timestamp"]) - pd.Timestamp(row["timestamp"])).total_seconds() / 3600, 0)
        latency_risk = abs(float(row["return_1"])) * min(self.latency_ms / 60_000, 1.0) / 10
        liquidity_risk = max(-float(row["volume_zscore"]), 0.0) * self.market_impact_rate * .25
        unit_cost = self.fee_rate + self.slippage_rate + self.market_impact_rate + latency_risk + liquidity_risk
        turnover = abs(target_position - self.position)
        cost = turnover * unit_cost
        reward = 0.0
        if turnover:
            self.trades += 1
            if self.position:
                self.position_return -= unit_cost
                reward += self._settle_trade()
            self.position_return = -unit_cost if target_position else 0.0
            self.peak_position_return = 0.0
            self.trade_max_drawdown = 0.0
            self.losing_hours = 0.0
        market_return = float(np.log(max(float(next_row["close"]), 1e-12) / max(float(row["close"]), 1e-12)))
        net_return = target_position * market_return - cost
        if target_position:
            self.position_return += target_position * market_return
            self.peak_position_return = max(self.peak_position_return, self.position_return)
            self.trade_max_drawdown = max(self.trade_max_drawdown,
                float(np.exp(self.peak_position_return) - np.exp(self.position_return)))
            if self.position_return < 0:
                before = max(self.losing_hours - 24, 0)
                self.losing_hours += hours
                penalty = .001 * (max(self.losing_hours - 24, 0) - before)
                reward -= penalty
                self.behavior_penalties_total -= penalty
        self.position = target_position
        self.index += 1
        terminated = self.index >= len(self.frame) - 1
        if terminated and self.position:
            # Liquidate the remaining position: no free exit at fold boundary.
            self.position_return -= unit_cost
            cost += unit_cost
            net_return -= unit_cost
            turnover += abs(self.position)
            reward += self._settle_trade()
            self.position = 0.0
        self.equity *= float(np.exp(net_return))
        self.peak_equity = max(self.peak_equity, self.equity)
        self.max_drawdown = max(self.max_drawdown, 1 - self.equity / self.peak_equity)
        self.turnover_total += turnover
        self.execution_cost_total += cost
        return self._observation(), float(reward), terminated, False, self.metrics()

    def _settle_trade(self):
        self.closed_trades += 1
        net_pnl = float(np.expm1(self.position_return))
        drawdown = max(self.trade_max_drawdown,
            float(np.exp(self.peak_position_return) - np.exp(self.position_return)))
        # Return units share a unit starting notional; costs are already in net_pnl.
        denominator = max(drawdown, 2 * (self.fee_rate + self.slippage_rate + self.market_impact_rate), .0001)
        self.positive_returns += max(net_pnl, 0)
        self.negative_returns += max(-net_pnl, 0)
        return float(np.clip(net_pnl / denominator, -10, 10))

    def metrics(self) -> dict[str, float | int]:
        profit_factor = self.positive_returns / self.negative_returns if self.negative_returns > 0 else (99.0 if self.positive_returns > 0 else 0.0)
        return {
            "return_percent": round((self.equity - 1) * 100, 4),
            "max_drawdown_percent": round(self.max_drawdown * 100, 4),
            "profit_factor": round(min(profit_factor, 99.0), 4),
            "trades": self.closed_trades,
            "turnover": round(self.turnover_total, 4),
            "execution_cost_percent": round(self.execution_cost_total * 100, 4),
            "behavior_penalty": round(self.behavior_penalties_total, 4),
            "strategy_bonus": round(self.strategy_bonuses_total, 4),
        }

    def _observation(self) -> np.ndarray:
        row = self.frame.iloc[min(self.index, len(self.frame) - 1)]
        drawdown = max(1 - self.equity / self.peak_equity, 0.0)
        values = [float(row[name]) for name in FEATURE_NAMES] + [self.position, drawdown]
        return np.clip(np.asarray(values, dtype=np.float32), -5.0, 5.0)


def latest_observation(frame: pd.DataFrame) -> np.ndarray:
    row = frame.iloc[-1]
    return np.clip(
        np.asarray([float(row[name]) for name in FEATURE_NAMES] + [0.0, 0.0], dtype=np.float32),
        -5.0,
        5.0,
    )
