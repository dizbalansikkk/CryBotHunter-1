from collections import Counter
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.entities import AgentDecision, Candle, LearningRule, LogEntry, Position, RlModel, Signal, StrategyOptimization
from app.schemas.dto import LearningMilestoneOut, LearningProgressOut, TradeBlockerOut
from app.services.performance_guard import PerformanceGuardService


class LearningProgressService:
    async def build(self, db: AsyncSession, *, now: datetime | None = None) -> LearningProgressOut:
        current_time = self._aware(now or datetime.now(timezone.utc))
        cutoff_24h = current_time - timedelta(hours=24)
        cutoff_7d = current_time - timedelta(days=7)
        settings = get_settings()

        positions = list((await db.execute(select(Position))).scalars().all())
        closed = [position for position in positions if position.status == "CLOSED"]
        opened = [position for position in positions if position.status == "OPEN"]
        exploration_closed = [position for position in closed if self._is_exploration(position)]
        exploration_opened = [position for position in opened if self._is_exploration(position)]

        closed_24h = sum(1 for position in closed if self._at_or_after(position.closed_at, cutoff_24h))
        closed_7d = sum(1 for position in closed if self._at_or_after(position.closed_at, cutoff_7d))
        exploration_closed_24h = sum(
            1 for position in exploration_closed if self._at_or_after(position.closed_at, cutoff_24h)
        )

        signal_rows = (
            await db.execute(
                select(Signal.signal, func.count(Signal.id))
                .where(Signal.created_at >= cutoff_24h)
                .group_by(Signal.signal)
            )
        ).all()
        signal_counts = {str(signal): int(count) for signal, count in signal_rows}
        last_signal_at = (
            await db.execute(select(func.max(Signal.created_at)))
        ).scalar_one_or_none()
        signals_24h = sum(signal_counts.values())
        directional_signals = signal_counts.get("BUY", 0) + signal_counts.get("SELL", 0)
        waits = signal_counts.get("WAIT", 0)

        agent_decisions_24h = int(
            (
                await db.execute(
                    select(func.count()).select_from(AgentDecision).where(AgentDecision.created_at >= cutoff_24h)
                )
            ).scalar_one()
        )
        last_agent_decision_at = (
            await db.execute(select(func.max(AgentDecision.created_at)))
        ).scalar_one_or_none()

        rules = list((await db.execute(select(LearningRule))).scalars().all())
        learning_observations = sum(int(rule.observations or 0) for rule in rules)
        last_learning_at = max((self._aware(rule.updated_at) for rule in rules if rule.updated_at), default=None)

        rl_models = list((await db.execute(select(RlModel))).scalars().all())
        active_rl_pairs = len({model.symbol for model in rl_models if model.is_active})
        optimized_pairs = int(
            (
                await db.execute(select(func.count(func.distinct(StrategyOptimization.symbol))))
            ).scalar_one()
        )

        symbols = settings.market_scan_symbols
        source = "ccxt" if settings.uses_live_market_data else "synthetic"
        candle_rows = (
            await db.execute(
                select(Candle.symbol, func.count(Candle.id))
                .where(
                    Candle.symbol.in_(symbols),
                    Candle.timeframe == "1h",
                    Candle.source == source,
                )
                .group_by(Candle.symbol)
            )
        ).all()
        candle_counts = {str(symbol): int(count) for symbol, count in candle_rows}
        candle_pairs_ready = sum(
            1 for symbol in symbols if candle_counts.get(symbol, 0) >= settings.pretrade_quality_min_candles
        )

        milestones = self.build_milestones(
            closed_trades=len(closed),
            learning_observations=learning_observations,
            active_rl_pairs=active_rl_pairs,
            candle_pairs_ready=candle_pairs_ready,
            candle_pairs_total=len(symbols),
            trade_target=settings.learning_progress_target_trades,
            observation_target=settings.learning_progress_target_observations,
        )
        overall_progress = round(sum(item.progress_percent for item in milestones) / max(len(milestones), 1), 2)
        incomplete = [item for item in milestones if not item.complete]
        next_milestone = min(incomplete, key=lambda item: item.progress_percent).key if incomplete else None
        stage = self.stage(
            closed_trades=len(closed),
            trade_target=settings.learning_progress_target_trades,
            candle_pairs_ready=candle_pairs_ready,
            candle_pairs_total=len(symbols),
            overall_progress=overall_progress,
        )
        guard = await PerformanceGuardService().evaluate(db)

        blocker_rows = (
            await db.execute(
                select(LogEntry.message)
                .where(LogEntry.created_at >= cutoff_24h, LogEntry.message.like("Skipped %"))
                .order_by(LogEntry.created_at.desc())
                .limit(2000)
            )
        ).scalars().all()
        blocker_counts = Counter(self.normalize_blocker(message) for message in blocker_rows)
        top_blockers = [
            TradeBlockerOut(reason=reason, count=count)
            for reason, count in blocker_counts.most_common(6)
        ]

        return LearningProgressOut(
            stage=stage,
            overall_progress_percent=overall_progress,
            next_milestone=next_milestone,
            closed_trades=len(closed),
            closed_24h=closed_24h,
            closed_7d=closed_7d,
            open_positions=len(opened),
            exploration_open_positions=len(exploration_opened),
            exploration_closed_trades=len(exploration_closed),
            exploration_closed_24h=exploration_closed_24h,
            signals_24h=signals_24h,
            directional_signals_24h=directional_signals,
            waits_24h=waits,
            agent_decisions_24h=agent_decisions_24h,
            learning_rules=len(rules),
            learning_observations=learning_observations,
            active_rl_pairs=active_rl_pairs,
            trained_rl_models=len(rl_models),
            optimized_pairs=optimized_pairs,
            candle_pairs_ready=candle_pairs_ready,
            candle_pairs_total=len(symbols),
            guard_allowed=guard.allowed,
            guard_recovery_mode=guard.recovery_mode,
            guard_reason=guard.reason,
            last_signal_at=last_signal_at,
            last_trade_closed_at=max((self._aware(position.closed_at) for position in closed if position.closed_at), default=None),
            last_learning_at=last_learning_at,
            last_agent_decision_at=last_agent_decision_at,
            milestones=milestones,
            top_blockers_24h=top_blockers,
        )

    def build_milestones(
        self,
        *,
        closed_trades: int,
        learning_observations: int,
        active_rl_pairs: int,
        candle_pairs_ready: int,
        candle_pairs_total: int,
        trade_target: int,
        observation_target: int,
    ) -> list[LearningMilestoneOut]:
        return [
            self._milestone("candle_coverage", candle_pairs_ready, candle_pairs_total),
            self._milestone("trade_lessons", closed_trades, trade_target),
            self._milestone("memory_observations", learning_observations, observation_target),
            self._milestone("active_rl_pairs", active_rl_pairs, candle_pairs_total),
        ]

    def stage(
        self,
        *,
        closed_trades: int,
        trade_target: int,
        candle_pairs_ready: int,
        candle_pairs_total: int,
        overall_progress: float,
    ) -> str:
        if candle_pairs_ready < candle_pairs_total or closed_trades < min(10, max(trade_target, 1)):
            return "COLLECTING"
        if closed_trades < max(trade_target, 1):
            return "CALIBRATING"
        if overall_progress < 100:
            return "LEARNING"
        return "MATURE"

    def normalize_blocker(self, message: str) -> str:
        reason = message.partition(":")[2].strip().lower()
        markers = (
            ("position already open", "POSITION_ALREADY_OPEN"),
            ("recovery position limit", "RECOVERY_POSITION_LIMIT"),
            ("performance guard", "PERFORMANCE_GUARD"),
            ("per-cycle entry limit", "PAPER_LANE_CYCLE_LIMIT"),
            ("paper exploration position limit", "PAPER_LANE_POSITION_LIMIT"),
            ("cooldown", "COOLDOWN"),
            ("maximum open positions", "MAX_POSITIONS"),
            ("signal score below", "LOW_SCORE"),
            ("pre-trade quality", "PRETRADE_QUALITY"),
            ("rl disagrees", "RL_DISAGREEMENT"),
            ("learning", "LEARNING_MEMORY"),
            ("market quality", "MARKET_QUALITY"),
            ("same-side", "DIRECTIONAL_EXPOSURE"),
            ("exposure limit", "EXPOSURE"),
        )
        return next((code for marker, code in markers if marker in reason), "OTHER")

    def _milestone(self, key: str, current: int, target: int) -> LearningMilestoneOut:
        safe_target = max(int(target), 1)
        progress = round(min(max(int(current), 0) / safe_target * 100, 100), 2)
        return LearningMilestoneOut(
            key=key,
            current=max(int(current), 0),
            target=safe_target,
            progress_percent=progress,
            complete=current >= safe_target,
        )

    def _is_exploration(self, position: Position) -> bool:
        return bool(isinstance(position.entry_context, dict) and position.entry_context.get("paper_exploration"))

    def _at_or_after(self, value: datetime | None, cutoff: datetime) -> bool:
        return bool(value and self._aware(value) >= cutoff)

    def _aware(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
