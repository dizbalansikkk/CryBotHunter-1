from dataclasses import dataclass

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
    ) -> PreTradeQualityAssessment:
        if not self.settings.pretrade_quality_enabled:
            return PreTradeQualityAssessment(True, "pre-trade quality gate disabled", 0)

        candles = await self._recent_candles(db, symbol, timeframe, self.settings.pretrade_quality_min_candles + 240)
        used_timeframe = timeframe
        if len(candles) < self.settings.pretrade_quality_min_candles and timeframe != "1h":
            fallback_candles = await self._recent_candles(db, symbol, "1h", self.settings.pretrade_quality_min_candles + 240)
            if len(fallback_candles) > len(candles):
                candles = fallback_candles
                used_timeframe = "1h"
        if len(candles) < self.settings.pretrade_quality_min_candles:
            return PreTradeQualityAssessment(
                True,
                f"pre-trade quality warning: only {len(candles)} {used_timeframe} candles available, need {self.settings.pretrade_quality_min_candles}",
                len(candles),
                risk_multiplier=self.settings.pretrade_quality_min_risk_multiplier,
            )

        train_size = max(220, min(360, len(candles) // 2))
        test_size = max(80, min(160, len(candles) // 4))
        step_size = max(60, test_size)
        report = self.backtester.walk_forward(candles, train_size=train_size, test_size=test_size, step_size=step_size)
        return self._decision(report, len(candles), risk_settings, learning_probe=learning_probe)

    async def _recent_candles(self, db: AsyncSession, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        result = await db.execute(
            select(Candle)
            .where(Candle.symbol == symbol, Candle.timeframe == timeframe)
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
        if report.window_count <= 0:
            return PreTradeQualityAssessment(
                True,
                "pre-trade quality warning: not enough walk-forward windows",
                candles_checked,
                risk_multiplier=self.settings.pretrade_quality_min_risk_multiplier,
            )

        total_test_trades = sum(window.test_trades_count for window in report.windows)
        profitable_windows_percent = report.profitable_windows / report.window_count * 100
        if (
            learning_probe
            and total_test_trades < self.settings.pretrade_quality_min_trades
            and report.total_profit >= 0
        ):
            return PreTradeQualityAssessment(
                allowed=True,
                reason=(
                    "paper learning quality is inconclusive: strict strategy produced "
                    f"{total_test_trades} comparable trades; risk reduced to "
                    f"{self.settings.pretrade_quality_min_risk_multiplier:.2f}x"
                ),
                candles_checked=candles_checked,
                risk_multiplier=self.settings.pretrade_quality_min_risk_multiplier,
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
        if report.total_profit <= -risk_settings.risk_percent:
            hard_reasons.append(f"walk-forward total profit {report.total_profit:.2f} is below risk tolerance")

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
