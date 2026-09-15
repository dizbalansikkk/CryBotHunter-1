import asyncio
from itertools import product
from dataclasses import replace
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.entities import StrategyOptimization
from app.schemas.dto import StrategyOptimizationOut
from app.services.backtesting import BacktestingService
from app.services.history import HistoricalDataService
from app.services.risk_manager import RiskSettings


class StrategyOptimizerService:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.backtester = BacktestingService()
        self.history = HistoricalDataService()

    async def optimize(self, db: AsyncSession, symbol: str, timeframe: str = "1h", limit: int = 500, top_n: int = 5) -> list[StrategyOptimizationOut]:
        candles = await self.history.load(db, symbol=symbol, timeframe=timeframe, limit=limit)
        if len(candles) < 220:
            await self.history.ingest(db, symbol=symbol, timeframe=timeframe, limit=limit)
            candles = await self.history.load(db, symbol=symbol, timeframe=timeframe, limit=limit)

        top = await asyncio.to_thread(
            self._candidate_results,
            candles,
            symbol,
            timeframe,
            top_n,
        )
        for item in top:
            db.add(
                StrategyOptimization(
                    symbol=item.symbol,
                    timeframe=item.timeframe,
                    parameters=item.parameters,
                    score=item.score,
                    win_rate=item.win_rate,
                    profit_factor=item.profit_factor,
                    max_drawdown=item.max_drawdown,
                    total_profit=item.total_profit,
                    trades_count=item.trades_count,
                )
            )
        await db.commit()
        return top

    def _candidate_results(
        self,
        candles: list,
        symbol: str,
        timeframe: str,
        top_n: int,
    ) -> list[StrategyOptimizationOut]:
        train_candles, validation_candles = self._split_train_validation(candles)
        candidates: list[StrategyOptimizationOut] = []
        stop_values = [1.0, 1.5, 2.0]
        take_values = [2.0, 3.0, 4.0]
        risk_values = [7.5, 10.0, 12.5]
        trailing_values = [0.0, 0.8, 1.2]

        for stop_loss, take_profit, risk_per_trade, trailing_stop in product(stop_values, take_values, risk_values, trailing_values):
            parameters = {
                "stop_loss_percent": stop_loss,
                "take_profit_percent": take_profit,
                "trailing_stop_percent": trailing_stop,
                "risk_per_trade": risk_per_trade,
            }
            report = self.backtester.run(
                candles,
                risk_per_trade=risk_per_trade,
                stop_loss_percent=stop_loss,
                take_profit_percent=take_profit,
                trailing_stop_percent=trailing_stop,
            )
            train_report = self.backtester.run(train_candles, **parameters)
            validation_report = self.backtester.run(validation_candles, **parameters) if validation_candles else None
            robustness = self._robustness(train_report, validation_report)
            score = self._robust_score(report, validation_report, robustness)
            candidates.append(
                StrategyOptimizationOut(
                    symbol=symbol,
                    timeframe=timeframe,
                    parameters={**parameters, "robustness": robustness},
                    score=score,
                    win_rate=report.win_rate,
                    profit_factor=report.profit_factor,
                    max_drawdown=report.max_drawdown,
                    total_profit=report.total_profit,
                    trades_count=report.trades_count,
                )
            )

        return sorted(candidates, key=lambda item: item.score, reverse=True)[: max(int(top_n), 1)]

    async def recent(self, db: AsyncSession, limit: int = 20) -> list[StrategyOptimization]:
        result = await db.execute(select(StrategyOptimization).order_by(StrategyOptimization.created_at.desc()).limit(limit))
        return list(result.scalars().all())

    async def latest_for(self, db: AsyncSession, symbol: str, timeframe: str = "1h") -> StrategyOptimization | None:
        result = await db.execute(
            select(StrategyOptimization)
            .where(StrategyOptimization.symbol == symbol, StrategyOptimization.timeframe == timeframe)
            .order_by(StrategyOptimization.created_at.desc())
            .limit(1)
        )
        return result.scalars().first()

    async def needs_refresh(self, db: AsyncSession, symbol: str, timeframe: str = "1h") -> bool:
        latest = await self.latest_for(db, symbol, timeframe)
        if not latest or not latest.created_at:
            return True
        created_at = latest.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        age_hours = (datetime.now(timezone.utc) - created_at).total_seconds() / 3600
        return age_hours >= self.settings.strategy_optimizer_refresh_hours

    async def best_for(self, db: AsyncSession, symbol: str, timeframe: str = "1h") -> StrategyOptimization | None:
        if not self.settings.strategy_optimizer_apply_enabled:
            return None
        optimization = await self._best_for_timeframe(db, symbol, timeframe)
        if optimization:
            return optimization
        if timeframe != "1h":
            return await self._best_for_timeframe(db, symbol, "1h")
        return None

    async def _best_for_timeframe(self, db: AsyncSession, symbol: str, timeframe: str) -> StrategyOptimization | None:
        result = await db.execute(
            select(StrategyOptimization)
            .where(
                StrategyOptimization.symbol == symbol,
                StrategyOptimization.timeframe == timeframe,
                StrategyOptimization.profit_factor >= self.settings.strategy_optimizer_min_profit_factor,
                StrategyOptimization.trades_count >= self.settings.strategy_optimizer_min_trades,
            )
            .order_by(StrategyOptimization.score.desc(), StrategyOptimization.created_at.desc())
            .limit(10)
        )
        for optimization in result.scalars().all():
            if self._is_fresh(optimization):
                if not self._passes_robustness(optimization):
                    continue
                return optimization
        return None

    def apply_to_risk_settings(self, settings: RiskSettings, optimization: StrategyOptimization) -> tuple[RiskSettings, str]:
        parameters = optimization.parameters or {}
        stop_loss = self._positive_float(parameters.get("stop_loss_percent"), settings.stop_loss_percent)
        take_profit = self._positive_float(parameters.get("take_profit_percent"), settings.take_profit_percent)
        trailing_stop = self._bounded_float(parameters.get("trailing_stop_percent"), settings.trailing_stop_percent, minimum=0.0, maximum=20.0)
        risk_reward_ratio = max(settings.min_risk_reward_ratio, round(take_profit / stop_loss, 4)) if stop_loss > 0 else settings.risk_reward_ratio
        optimized = replace(
            settings,
            stop_loss_percent=stop_loss,
            take_profit_percent=take_profit,
            trailing_stop_percent=trailing_stop,
            risk_reward_ratio=risk_reward_ratio,
        )
        reason = (
            f"optimizer applied: SL={stop_loss:.2f}%, TP={take_profit:.2f}%, "
            f"trail={trailing_stop:.2f}%, PF={optimization.profit_factor:.2f}, score={optimization.score:.2f}"
        )
        robustness = parameters.get("robustness")
        if isinstance(robustness, dict):
            validation_profit_factor = self._optional_float(robustness.get("validation_profit_factor"))
            validation_profit = self._optional_float(robustness.get("validation_profit"))
            if validation_profit_factor is not None and validation_profit is not None:
                reason = f"{reason}, valPF={validation_profit_factor:.2f}, valPnL={validation_profit:.2f}"
        return optimized, reason

    def _score(self, total_profit: float, profit_factor: float, win_rate: float, max_drawdown: float, trades_count: int) -> float:
        if trades_count == 0:
            return -9999
        return round(total_profit + profit_factor * 20 + win_rate * 0.5 - max_drawdown * 1.2 + min(trades_count, 30), 4)

    def _robust_score(self, report, validation_report, robustness: dict) -> float:
        base_score = self._score(report.total_profit, report.profit_factor, report.win_rate, report.max_drawdown, report.trades_count)
        if not self.settings.strategy_optimizer_validation_enabled:
            return base_score
        if validation_report is None:
            return round(base_score - 1000, 4)
        validation_score = self._score(
            validation_report.total_profit,
            validation_report.profit_factor,
            validation_report.win_rate,
            validation_report.max_drawdown,
            validation_report.trades_count,
        )
        consistency = self._optional_float(robustness.get("consistency")) or 0.0
        consistency_bonus = max(min(consistency * 25, 25), -25)
        failed_penalty = 500 if not robustness.get("passed") else 0
        return round(base_score * 0.35 + validation_score * 0.65 + consistency_bonus - failed_penalty, 4)

    def _split_train_validation(self, candles: list) -> tuple[list, list]:
        if not self.settings.strategy_optimizer_validation_enabled:
            return candles, []
        min_candles = max(1, self.settings.strategy_optimizer_validation_min_candles)
        if len(candles) < min_candles * 2:
            return candles, []
        percent = min(max(self.settings.strategy_optimizer_validation_percent, 5.0), 50.0)
        validation_size = max(min_candles, int(len(candles) * percent / 100))
        if len(candles) - validation_size < min_candles:
            validation_size = len(candles) - min_candles
        if validation_size < min_candles:
            return candles, []
        return candles[:-validation_size], candles[-validation_size:]

    def _robustness(self, train_report, validation_report) -> dict:
        if not self.settings.strategy_optimizer_validation_enabled:
            return {"passed": True, "reason": "validation disabled"}
        if validation_report is None:
            return {
                "passed": False,
                "reason": "not enough candles for validation split",
                "validation_profit": 0.0,
                "validation_profit_factor": 0.0,
                "validation_win_rate": 0.0,
                "validation_trades": 0,
                "validation_max_drawdown": 0.0,
                "train_profit": round(train_report.total_profit, 2),
                "overfit_ratio": 0.0,
                "consistency": 0.0,
            }

        reasons: list[str] = []
        if validation_report.trades_count < self.settings.strategy_optimizer_min_validation_trades:
            reasons.append("too few validation trades")
        if validation_report.total_profit < self.settings.strategy_optimizer_min_validation_profit:
            reasons.append("validation profit below threshold")
        if validation_report.profit_factor < self.settings.strategy_optimizer_min_validation_profit_factor:
            reasons.append("validation profit factor below threshold")
        if validation_report.win_rate < self.settings.strategy_optimizer_min_validation_win_rate:
            reasons.append("validation win rate below threshold")

        overfit_ratio = 0.0
        consistency = 0.0
        if train_report.total_profit > 0:
            consistency = validation_report.total_profit / train_report.total_profit
            if validation_report.total_profit > 0:
                overfit_ratio = train_report.total_profit / max(validation_report.total_profit, 1.0)
                if overfit_ratio > self.settings.strategy_optimizer_max_overfit_ratio:
                    reasons.append("validation profit too weak vs train")
            else:
                overfit_ratio = 999.0

        passed = not reasons
        return {
            "passed": passed,
            "reason": "passed" if passed else "; ".join(reasons),
            "validation_profit": round(validation_report.total_profit, 2),
            "validation_profit_factor": round(validation_report.profit_factor, 2),
            "validation_win_rate": round(validation_report.win_rate, 2),
            "validation_trades": validation_report.trades_count,
            "validation_max_drawdown": round(validation_report.max_drawdown, 2),
            "train_profit": round(train_report.total_profit, 2),
            "overfit_ratio": round(overfit_ratio, 2),
            "consistency": round(consistency, 4),
        }

    def _passes_robustness(self, optimization: StrategyOptimization) -> bool:
        if not self.settings.strategy_optimizer_validation_enabled or not self.settings.strategy_optimizer_require_validation_pass:
            return True
        parameters = optimization.parameters or {}
        robustness = parameters.get("robustness")
        if not isinstance(robustness, dict):
            return False
        return bool(robustness.get("passed"))

    def _is_fresh(self, optimization: StrategyOptimization) -> bool:
        if self.settings.strategy_optimizer_max_age_days <= 0 or not optimization.created_at:
            return True
        created_at = optimization.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        age_days = (datetime.now(timezone.utc) - created_at).total_seconds() / 86400
        return age_days <= self.settings.strategy_optimizer_max_age_days

    def _positive_float(self, value: object, default: float) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return default
        return parsed if parsed > 0 else default

    def _bounded_float(self, value: object, default: float, minimum: float, maximum: float) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return default
        return min(max(parsed, minimum), maximum)

    def _optional_float(self, value: object) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
