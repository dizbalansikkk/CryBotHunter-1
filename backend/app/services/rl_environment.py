from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import gymnasium as gym
import numpy as np
import pandas as pd
from gymnasium import spaces


FEATURE_NAMES = [
    "return_1",
    "return_5",
    "ema_gap",
    "rsi",
    "atr_percent",
    "volume_zscore",
]


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
    return frame.replace([np.inf, -np.inf], np.nan).dropna().reset_index(drop=True)


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
        self.positive_returns = 0.0
        self.negative_returns = 0.0
        self.position_return = 0.0
        self.peak_position_return = 0.0
        self.adverse_steps = 0
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
        self.positive_returns = 0.0
        self.negative_returns = 0.0
        self.position_return = 0.0
        self.peak_position_return = 0.0
        self.adverse_steps = 0
        self.behavior_penalties_total = 0.0
        self.strategy_bonuses_total = 0.0
        self.turnover_total = 0.0
        self.execution_cost_total = 0.0
        return self._observation(), {}

    def step(self, action: int) -> tuple[np.ndarray, float, bool, bool, dict]:
        target_position = (0.0, 1.0, -1.0)[int(action)]
        turnover = abs(target_position - self.position)
        if turnover > 0:
            self.trades += 1
        row = self.frame.iloc[self.index]
        latency_risk = abs(float(row["return_1"])) * min(self.latency_ms / 60_000, 1.0) / 10
        liquidity_risk = max(-float(row["volume_zscore"]), 0.0) * self.market_impact_rate * 0.25
        dynamic_slippage = self.slippage_rate + self.market_impact_rate + latency_risk + liquidity_risk
        cost = turnover * (self.fee_rate + dynamic_slippage)
        replay_weight = max(min(float(row.get("replay_weight", 1.0)), 5.0), 1.0)
        current_close = max(float(self.frame.iloc[self.index]["close"]), 1e-12)
        next_close = max(float(self.frame.iloc[self.index + 1]["close"]), 1e-12)
        market_return = float(np.log(next_close / current_close))
        net_return = target_position * market_return - cost
        previous_position = self.position
        previous_position_return = self.position_return
        previous_peak_return = self.peak_position_return
        previous_adverse_steps = self.adverse_steps
        if turnover > 0:
            self.position_return = 0.0
            self.peak_position_return = 0.0
            self.adverse_steps = 0
        else:
            self.position_return += target_position * market_return
            self.peak_position_return = max(self.peak_position_return, self.position_return)
            self.adverse_steps = self.adverse_steps + 1 if target_position * market_return < 0 else 0
        previous_drawdown = max(1 - self.equity / self.peak_equity, 0.0)
        self.equity *= float(np.exp(net_return))
        self.peak_equity = max(self.peak_equity, self.equity)
        drawdown = max(1 - self.equity / self.peak_equity, 0.0)
        self.max_drawdown = max(self.max_drawdown, drawdown)
        self.positive_returns += max(net_return, 0.0)
        self.negative_returns += abs(min(net_return, 0.0))
        self.turnover_total += turnover
        self.execution_cost_total += cost
        self.position = target_position
        self.index += 1
        terminated = self.index >= len(self.frame) - 1
        financial_reward = net_return * 100
        if financial_reward < 0:
            financial_reward *= replay_weight
        behavior_reward = 0.0
        # Penalize giving back a meaningful unrealized gain and exiting only
        # after noise, rather than treating every losing close equally.
        if previous_position and target_position == 0 and previous_peak_return >= 0.005 and previous_position_return < previous_peak_return * 0.4:
            behavior_reward -= self.behavior_penalty * replay_weight
        trend_against_position = previous_position * float(row["return_5"]) < -0.05
        if previous_position and target_position == previous_position and previous_adverse_steps >= 3 and trend_against_position:
            behavior_reward -= self.behavior_penalty * 0.5 * replay_weight
        if previous_position and target_position == 0 and previous_adverse_steps >= 3 and trend_against_position:
            behavior_reward += self.strategy_adherence_bonus
        aligned = target_position != 0 and target_position * float(row["ema_gap"]) > 0
        strategy_reward = self.strategy_adherence_bonus if aligned else 0.0
        self.behavior_penalties_total += min(behavior_reward, 0.0)
        self.strategy_bonuses_total += max(behavior_reward, 0.0) + strategy_reward
        reward = (
            financial_reward
            - max(drawdown - previous_drawdown, 0.0) * self.drawdown_penalty * 100 * replay_weight
            + behavior_reward
            + strategy_reward
        )
        return self._observation(), float(reward), terminated, False, self.metrics()

    def metrics(self) -> dict[str, float | int]:
        profit_factor = self.positive_returns / self.negative_returns if self.negative_returns > 0 else (99.0 if self.positive_returns > 0 else 0.0)
        return {
            "return_percent": round((self.equity - 1) * 100, 4),
            "max_drawdown_percent": round(self.max_drawdown * 100, 4),
            "profit_factor": round(min(profit_factor, 99.0), 4),
            "trades": self.trades,
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
