import asyncio
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, replace
import math

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.entities import Candle
from app.services.backtesting import BacktestingService, WalkForwardReport
from app.services.risk_manager import RiskSettings


@dataclass(frozen=True)
class PreTradeQualityAssessment:
    allowed: bool
    reason: str
    candles_checked: int
    risk_multiplier: float = 1.0
    window_count: int = 0
    profitable_windows_percent: float = 0.0
    average_win_rate: float = 0.0
    average_profit_factor: float = 0.0
    total_profit: float = 0.0


class PreTradeQualityGate:
    BACKTEST_RISK_USDT = 10.0

    def __init__(self, backtester: BacktestingService | None = None) -> None:
        self.settings = get_settings()
        self.backtester = backtester or BacktestingService()

    async def assess(
        self,
        db: AsyncSession,
        symbol: str,
        timeframe: str,
        risk_settings: RiskSettings,
        *,
        learning_probe: bool = False,
        direction: str | None = None,
    ) -> PreTradeQualityAssessment:
        if not self.settings.pretrade_quality_enabled:
            return PreTradeQualityAssessment(True, "pre-trade quality gate disabled", 0)

        candles = await self._recent_candles(db, symbol, timeframe, max(self.settings.pretrade_quality_min_candles + 240, 2160))
        used_timeframe = timeframe
        if len(candles) < self.settings.pretrade_quality_min_candles and timeframe != "1h":
            fallback_candles = await self._recent_candles(db, symbol, "1h", max(self.settings.pretrade_quality_min_candles + 240, 2160))
            if len(fallback_candles) > len(candles):
                candles = fallback_candles
                used_timeframe = "1h"
        if len(candles) < self.settings.pretrade_quality_min_candles:
            allowed = not self.settings.pretrade_quality_require_history
            state = "warning" if allowed else "blocked"
            return PreTradeQualityAssessment(
                allowed,
                (
                    f"pre-trade quality {state}: only {len(candles)} {used_timeframe} candles available, "
                    f"need {self.settings.pretrade_quality_min_candles}"
                ),
                len(candles),
                risk_multiplier=self.settings.pretrade_quality_min_risk_multiplier if allowed else 0.0,
            )

        train_size = max(220, min(360, len(candles) // 2))
        test_size = max(80, min(160, len(candles) // 4))
        step_size = max(60, test_size)
        # Validate the actual entry lane, side and exit settings, not an
        # independently optimized strict strategy mixing LONG and SHORT.
        report = await asyncio.to_thread(
            self.backtester.walk_forward, candles, train_size=train_size,
            test_size=test_size, step_size=step_size, direction=direction,
            learning_probe=learning_probe,
            parameters={"risk_per_trade": self.BACKTEST_RISK_USDT,
                "stop_loss_percent": risk_settings.stop_loss_percent,
                "take_profit_percent": risk_settings.take_profit_percent,
                "trailing_stop_percent": risk_settings.trailing_stop_percent,
                "atr_stop_multiplier": risk_settings.atr_stop_multiplier,
                "risk_reward_ratio": risk_settings.risk_reward_ratio,
                "trailing_activation_r": self.settings.trailing_activation_r},
        )
        return self._decision(report, len(candles), risk_settings, learning_probe=learning_probe)

    async def _recent_candles(self, db: AsyncSession, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        # Ingestion includes the developing candle; never evaluate it as final.
        units = {"m": 60, "h": 3600, "d": 86400}
        seconds = int(timeframe[:-1]) * units[timeframe[-1]]
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=seconds)
        result = await db.execute(
            select(Candle)
            .where(Candle.symbol == symbol, Candle.timeframe == timeframe, Candle.timestamp <= cutoff,
                   Candle.source == ("ccxt" if self.settings.uses_live_market_data else "synthetic"))
            .order_by(Candle.timestamp.desc())
            .limit(limit)
        )
        return list(reversed(result.scalars().all()))

    def _decision(
        self,
        report: WalkForwardReport,
        candles_checked: int,
        risk_settings: RiskSettings,
        *,
        learning_probe: bool = False,
    ) -> PreTradeQualityAssessment:
        # Empty windows carry no evidence about win rate or profitability.
        # Counting them as losses systematically rejects sparse directional lanes.
        active = [w for w in report.windows if w.test_trades_count > 0]
        if active and len(active) != len(report.windows):
            count = sum(w.test_trades_count for w in active)
            report = replace(report, windows=active, window_count=len(active),
                profitable_windows=sum(w.test_profit > 0 for w in active),
                average_win_rate=sum(w.test_win_rate * w.test_trades_count for w in active) / count,
                average_profit_factor=sum(w.test_profit_factor for w in active) / len(active))
        metrics = (report.total_profit, report.average_win_rate, report.average_profit_factor)
        if not all(math.isfinite(v) for v in metrics):
            return PreTradeQualityAssessment(False, "pre-trade quality blocked: invalid historical metrics", candles_checked, risk_multiplier=0.0)
        if report.window_count <= 0:
            return PreTradeQualityAssessment(
                not self.settings.pretrade_quality_require_history,
                "pre-trade quality blocked: not enough walk-forward windows" if self.settings.pretrade_quality_require_history else "pre-trade quality warning: not enough walk-forward windows",
                candles_checked,
                risk_multiplier=0.0 if self.settings.pretrade_quality_require_history else self.settings.pretrade_quality_min_risk_multiplier,
            )

        total_test_trades = sum(window.test_trades_count for window in report.windows)
        profitable_windows_percent = report.profitable_windows / report.window_count * 100
        # Virtual forward exploration must be explicitly enabled. A caller's
        # lane flag alone must never relax admission for real capital.
        paper_learning = (
            learning_probe
            and self.settings.paper_trading
            and self.settings.paper_exploration_enabled
            and not self.settings.live_trading_enabled
        )
        learning_risk = min(0.35, self.settings.pretrade_quality_min_risk_multiplier)
        if (
            paper_learning
            and total_test_trades < self.settings.pretrade_quality_min_trades
            and report.total_profit >= 0
        ):
            return PreTradeQualityAssessment(
                allowed=True,
                reason=(
                    "paper learning quality is inconclusive: comparable strategy produced "
                    f"{total_test_trades} comparable trades; risk reduced to "
                    f"{learning_risk:.2f}x"
                ),
                candles_checked=candles_checked,
                risk_multiplier=learning_risk,
                window_count=report.window_count,
                profitable_windows_percent=round(profitable_windows_percent, 2),
                average_win_rate=report.average_win_rate,
                average_profit_factor=report.average_profit_factor,
                total_profit=report.total_profit,
            )
        hard_reasons: list[str] = []
        soft_reasons: list[str] = []
        profit_factor_floor = self.settings.pretrade_quality_min_profit_factor * 0.75
        win_rate_floor = self.settings.pretrade_quality_min_win_rate * 0.75
        profitable_windows_floor = self.settings.pretrade_quality_min_profitable_windows_percent * 0.75
        if total_test_trades < self.settings.pretrade_quality_min_trades:
            hard_reasons.append(f"too few historical trades {total_test_trades} < {self.settings.pretrade_quality_min_trades}")
        self._collect_metric_reason(
            report.average_profit_factor,
            self.settings.pretrade_quality_min_profit_factor,
            profit_factor_floor,
            "profit factor",
            hard_reasons,
            soft_reasons,
        )
        self._collect_metric_reason(
            report.average_win_rate,
            self.settings.pretrade_quality_min_win_rate,
            win_rate_floor,
            "win rate",
            hard_reasons,
            soft_reasons,
            suffix="%",
        )
        if profitable_windows_percent < profitable_windows_floor:
            hard_reasons.append(
                f"profitable windows {profitable_windows_percent:.2f}% < {profitable_windows_floor:.2f}% hard floor"
            )
        elif profitable_windows_percent < self.settings.pretrade_quality_min_profitable_windows_percent:
            soft_reasons.append(
                f"profitable windows {profitable_windows_percent:.2f}% < "
                f"{self.settings.pretrade_quality_min_profitable_windows_percent:.2f}%"
            )
        # The simulation risks a fixed USDT amount. The candidate's risk_percent
        # is a sizing decision, not a comparable monetary loss threshold.
        net_r = report.total_profit / self.BACKTEST_RISK_USDT
        if net_r < 0:
            hard_reasons.append(f"walk-forward net result {net_r:.2f} R ({report.total_profit:.2f} USDT at {self.BACKTEST_RISK_USDT:.2f} USDT risk) is negative")

        allowed = not hard_reasons
        risk_multiplier = 0.0 if hard_reasons else self._risk_multiplier(
            report.average_profit_factor,
            report.average_win_rate,
            profitable_windows_percent,
            soft_reasons,
        )
        if hard_reasons:
            reason = f"pre-trade quality blocked: {'; '.join(hard_reasons)}"
        elif soft_reasons:
            reason = f"pre-trade quality reduced risk to {risk_multiplier:.2f}x: {'; '.join(soft_reasons)}"
        else:
            reason = "pre-trade quality passed"
        if hard_reasons and paper_learning:
            allowed = True
            risk_multiplier = learning_risk
            reason = (
                f"paper learning only: historical quality failed; virtual risk reduced to {learning_risk:.2f}x; "
                + "; ".join(hard_reasons)
            )
        return PreTradeQualityAssessment(
            allowed=allowed,
            reason=reason,
            candles_checked=candles_checked,
            risk_multiplier=risk_multiplier,
            window_count=report.window_count,
            profitable_windows_percent=round(profitable_windows_percent, 2),
            average_win_rate=report.average_win_rate,
            average_profit_factor=report.average_profit_factor,
            total_profit=report.total_profit,
        )

    def _collect_metric_reason(
        self,
        value: float,
        target: float,
        hard_floor: float,
        label: str,
        hard_reasons: list[str],
        soft_reasons: list[str],
        suffix: str = "",
    ) -> None:
        if value < hard_floor:
            hard_reasons.append(f"{label} {value:.2f}{suffix} < {hard_floor:.2f}{suffix} hard floor")
        elif value < target:
            soft_reasons.append(f"{label} {value:.2f}{suffix} < {target:.2f}{suffix}")

    def _risk_multiplier(
        self,
        profit_factor: float,
        win_rate: float,
        profitable_windows_percent: float,
        soft_reasons: list[str],
    ) -> float:
        if not soft_reasons:
            return 1.0
        ratios = [
            profit_factor / self.settings.pretrade_quality_min_profit_factor,
            win_rate / self.settings.pretrade_quality_min_win_rate,
            profitable_windows_percent / self.settings.pretrade_quality_min_profitable_windows_percent,
        ]
        multiplier = min(ratios)
        return round(max(self.settings.pretrade_quality_min_risk_multiplier, min(multiplier, 1.0)), 2)
