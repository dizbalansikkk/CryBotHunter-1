from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import Position, PositionStatus
from app.schemas.dto import SymbolPerformanceOut, TradeAnalyticsOut, TradeHistoryItemOut


class TradeAnalyticsService:
    async def summary(self, db: AsyncSession, recent_limit: int = 20) -> TradeAnalyticsOut:
        positions = (
            await db.execute(select(Position).order_by(Position.entered_at.asc(), Position.id.asc()))
        ).scalars().all()
        return self.summarize_positions(list(positions), recent_limit=recent_limit)

    def summarize_positions(self, positions: list[Position], recent_limit: int = 20) -> TradeAnalyticsOut:
        closed = [position for position in positions if position.status == PositionStatus.CLOSED.value]
        opened = [position for position in positions if position.status == PositionStatus.OPEN.value]
        pnls = [float(position.pnl or 0.0) for position in closed]
        wins = [pnl for pnl in pnls if pnl > 0]
        losses = [pnl for pnl in pnls if pnl < 0]
        breakeven = len(pnls) - len(wins) - len(losses)
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        realized = sum(pnls)
        open_pnl = sum(float(position.pnl or 0.0) for position in opened)
        max_win_streak, max_loss_streak = self._streaks(pnls)

        grouped: dict[str, list[Position]] = defaultdict(list)
        for position in closed:
            grouped[position.symbol].append(position)

        by_symbol = [self._symbol_summary(symbol, rows) for symbol, rows in grouped.items()]
        by_symbol.sort(key=lambda item: (item.trades, item.total_pnl), reverse=True)
        recent = sorted(
            closed,
            key=lambda position: (self._aware(position.closed_at), int(position.id or 0)),
            reverse=True,
        )[: max(int(recent_limit), 0)]

        return TradeAnalyticsOut(
            closed_trades=len(closed),
            open_positions=len(opened),
            wins=len(wins),
            losses=len(losses),
            breakeven=breakeven,
            win_rate=round(len(wins) / len(closed) * 100, 2) if closed else 0.0,
            total_realized_pnl=round(realized, 4),
            open_pnl=round(open_pnl, 4),
            net_pnl=round(realized + open_pnl, 4),
            gross_profit=round(gross_profit, 4),
            gross_loss=round(gross_loss, 4),
            profit_factor=self._profit_factor(gross_profit, gross_loss),
            expectancy=round(realized / len(closed), 4) if closed else 0.0,
            average_win=round(gross_profit / len(wins), 4) if wins else 0.0,
            average_loss=round(sum(losses) / len(losses), 4) if losses else 0.0,
            best_trade=round(max(pnls), 4) if pnls else 0.0,
            worst_trade=round(min(pnls), 4) if pnls else 0.0,
            max_win_streak=max_win_streak,
            max_loss_streak=max_loss_streak,
            by_symbol=by_symbol,
            recent_trades=[self._history_item(position) for position in recent],
        )

    def _symbol_summary(self, symbol: str, positions: list[Position]) -> SymbolPerformanceOut:
        pnls = [float(position.pnl or 0.0) for position in positions]
        wins = [pnl for pnl in pnls if pnl > 0]
        losses = [pnl for pnl in pnls if pnl < 0]
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        return SymbolPerformanceOut(
            symbol=symbol,
            trades=len(pnls),
            wins=len(wins),
            losses=len(losses),
            win_rate=round(len(wins) / len(pnls) * 100, 2) if pnls else 0.0,
            total_pnl=round(sum(pnls), 4),
            average_pnl=round(sum(pnls) / len(pnls), 4) if pnls else 0.0,
            profit_factor=self._profit_factor(gross_profit, gross_loss),
            expectancy=round(sum(pnls) / len(pnls), 4) if pnls else 0.0,
        )

    def _history_item(self, position: Position) -> TradeHistoryItemOut:
        context: dict[str, Any] = position.entry_context or {}
        pnl = float(position.pnl or 0.0)
        notional = self._optional_float(context.get("notional")) or abs(float(position.entry_price) * float(position.volume))
        entered_at = position.entered_at
        closed_at = position.closed_at
        duration_minutes = None
        if entered_at and closed_at:
            duration_minutes = max(int((self._aware(closed_at) - self._aware(entered_at)).total_seconds() // 60), 0)
        return TradeHistoryItemOut(
            id=int(position.id or 0),
            symbol=position.symbol,
            side=position.side,
            entered_at=entered_at,
            closed_at=closed_at,
            entry_price=float(position.entry_price),
            exit_price=float(position.current_price),
            pnl=round(pnl, 4),
            return_percent=round(pnl / notional * 100, 4) if notional > 0 else 0.0,
            result="WIN" if pnl > 0 else "LOSS" if pnl < 0 else "BREAKEVEN",
            exit_reason=position.exit_reason,
            duration_minutes=duration_minutes,
            confidence=self._optional_float(context.get("committee_confidence") or context.get("entry_confidence")),
            consensus_score=self._optional_float(context.get("committee_consensus")),
            signal_score=self._optional_int(context.get("signal_score")),
            risk_percent=self._optional_float(context.get("risk_percent")),
            risk_reward_ratio=self._optional_float(context.get("risk_reward_ratio")),
            paper_exploration=bool(context.get("paper_exploration", False)),
            entry_reasons=[str(reason) for reason in context.get("reasons", [])][:6],
            decision_reason=str(context.get("decision_reason") or ""),
        )

    def _streaks(self, pnls: list[float]) -> tuple[int, int]:
        win_streak = loss_streak = max_wins = max_losses = 0
        for pnl in pnls:
            if pnl > 0:
                win_streak += 1
                loss_streak = 0
            elif pnl < 0:
                loss_streak += 1
                win_streak = 0
            else:
                win_streak = loss_streak = 0
            max_wins = max(max_wins, win_streak)
            max_losses = max(max_losses, loss_streak)
        return max_wins, max_losses

    def _profit_factor(self, gross_profit: float, gross_loss: float) -> float | None:
        if gross_loss <= 0:
            return None if gross_profit > 0 else 0.0
        return round(gross_profit / gross_loss, 4)

    def _aware(self, value: datetime | None) -> datetime:
        if value is None:
            return datetime.min.replace(tzinfo=timezone.utc)
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    def _optional_float(self, value: object) -> float | None:
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def _optional_int(self, value: object) -> int | None:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None
