from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.entities import AgentDecision, RlModel, ShadowTrade


@dataclass(frozen=True)
class ShadowForwardReport:
    status: str
    reason: str
    closed_trades: int
    wins: int
    losses: int
    win_rate: float
    profit_factor: float
    total_pnl: float
    max_drawdown_percent: float


class ShadowTradingService:
    def __init__(self) -> None:
        self.settings = get_settings()

    async def process(
        self,
        db: AsyncSession,
        record: RlModel,
        decision: AgentDecision,
        market_price: float,
    ) -> ShadowTrade | None:
        if record.status != "SHADOW" or record.is_active or market_price <= 0:
            return None
        opened = (
            await db.execute(
                select(ShadowTrade)
                .where(ShadowTrade.model_id == record.id, ShadowTrade.status == "OPEN")
                .order_by(ShadowTrade.entered_at.desc())
                .limit(1)
            )
        ).scalars().first()
        if opened:
            opened.current_price = market_price
            opened.pnl = self._net_pnl(opened, market_price, include_exit_cost=True)
            exit_reason = self._exit_reason(opened, decision)
            if exit_reason:
                self._close(opened, market_price, exit_reason)
                report = await self.report(db, record.id, include=opened)
                record.metrics = {
                    **(record.metrics or {}),
                    "forward_status": report.status,
                    "forward": self._report_dict(report),
                }
            return opened
        if decision.action not in {"BUY", "SELL"} or decision.confidence < float(self.settings.shadow_trade_min_confidence):
            return None
        side = "LONG" if decision.action == "BUY" else "SHORT"
        slippage_rate = self._slippage_rate()
        entry_price = market_price * (1 + slippage_rate if side == "LONG" else 1 - slippage_rate)
        notional = max(float(self.settings.shadow_trade_notional), 1.0)
        volume = notional / entry_price
        stop_percent = max(float(self.settings.shadow_trade_stop_percent), 0.1) / 100
        take_percent = max(float(self.settings.shadow_trade_take_percent), stop_percent * 1.1) / 100
        entry_fee = notional * max(float(self.settings.paper_fee_rate), 0.0)
        trade = ShadowTrade(
            model_id=record.id,
            symbol=record.symbol,
            timeframe=record.timeframe,
            side=side,
            status="OPEN",
            entry_price=entry_price,
            current_price=entry_price,
            volume=volume,
            stop=entry_price * (1 - stop_percent if side == "LONG" else 1 + stop_percent),
            take=entry_price * (1 + take_percent if side == "LONG" else 1 - take_percent),
            fee=entry_fee,
            slippage=abs(entry_price - market_price) * volume,
            pnl=-entry_fee,
            confidence=decision.confidence,
            entry_context={
                "decision_id": decision.id,
                "model_id": record.id,
                "validation": record.metrics,
                "trading_authority": False,
                "virtual_notional": notional,
            },
        )
        db.add(trade)
        await db.flush()
        return trade

    async def report(
        self,
        db: AsyncSession,
        model_id: int,
        *,
        include: ShadowTrade | None = None,
    ) -> ShadowForwardReport:
        trades = list(
            (
                await db.execute(
                    select(ShadowTrade)
                    .where(ShadowTrade.model_id == model_id, ShadowTrade.status == "CLOSED")
                    .order_by(ShadowTrade.closed_at.asc())
                )
            ).scalars().all()
        )
        if include and include.status == "CLOSED" and all(item.id != include.id for item in trades):
            trades.append(include)
        pnls = [float(item.pnl or 0.0) for item in trades]
        wins = [value for value in pnls if value > 0]
        losses = [abs(value) for value in pnls if value < 0]
        gross_loss = sum(losses)
        profit_factor = sum(wins) / gross_loss if gross_loss > 0 else (99.0 if wins else 0.0)
        notional = max(float(self.settings.shadow_trade_notional), 1.0)
        equity = notional
        peak = equity
        max_drawdown = 0.0
        for pnl in pnls:
            equity += pnl
            peak = max(peak, equity)
            max_drawdown = max(max_drawdown, (peak - equity) / peak * 100 if peak > 0 else 100.0)
        closed = len(pnls)
        win_rate = len(wins) / closed * 100 if closed else 0.0
        reasons: list[str] = []
        if closed < int(self.settings.shadow_forward_min_trades):
            reasons.append(f"forward trades {closed}/{self.settings.shadow_forward_min_trades}")
        if profit_factor < float(self.settings.shadow_forward_min_profit_factor):
            reasons.append("forward profit factor below threshold")
        if win_rate < float(self.settings.shadow_forward_min_win_rate):
            reasons.append("forward win rate below threshold")
        if sum(pnls) < float(self.settings.shadow_forward_min_pnl):
            reasons.append("forward PnL below threshold")
        if max_drawdown > float(self.settings.shadow_forward_max_drawdown_percent):
            reasons.append("forward drawdown above threshold")
        status = "PENDING" if closed < int(self.settings.shadow_forward_min_trades) else "FAILED" if reasons else "PASSED"
        return ShadowForwardReport(
            status=status,
            reason="passed" if not reasons else "; ".join(reasons),
            closed_trades=closed,
            wins=len(wins),
            losses=len(losses),
            win_rate=round(win_rate, 2),
            profit_factor=round(min(profit_factor, 99.0), 4),
            total_pnl=round(sum(pnls), 4),
            max_drawdown_percent=round(max_drawdown, 4),
        )

    def _exit_reason(self, trade: ShadowTrade, decision: AgentDecision) -> str | None:
        if trade.side == "LONG":
            if trade.current_price <= trade.stop:
                return "STOP_LOSS"
            if trade.current_price >= trade.take:
                return "TAKE_PROFIT"
            if decision.action == "SELL":
                return "MODEL_FLIP"
        else:
            if trade.current_price >= trade.stop:
                return "STOP_LOSS"
            if trade.current_price <= trade.take:
                return "TAKE_PROFIT"
            if decision.action == "BUY":
                return "MODEL_FLIP"
        return None

    def _close(self, trade: ShadowTrade, market_price: float, reason: str) -> None:
        slippage_rate = self._slippage_rate()
        exit_price = market_price * (1 - slippage_rate if trade.side == "LONG" else 1 + slippage_rate)
        exit_fee = abs(exit_price * trade.volume) * max(float(self.settings.paper_fee_rate), 0.0)
        trade.current_price = exit_price
        trade.slippage = round(float(trade.slippage or 0.0) + abs(exit_price - market_price) * trade.volume, 8)
        trade.fee = round(float(trade.fee or 0.0) + exit_fee, 8)
        trade.pnl = self._gross_pnl(trade, exit_price) - trade.fee
        trade.status = "CLOSED"
        trade.exit_reason = reason
        trade.closed_at = datetime.now(timezone.utc)

    def _net_pnl(self, trade: ShadowTrade, price: float, *, include_exit_cost: bool) -> float:
        estimated_exit_fee = abs(price * trade.volume) * max(float(self.settings.paper_fee_rate), 0.0) if include_exit_cost else 0.0
        return round(self._gross_pnl(trade, price) - float(trade.fee or 0.0) - estimated_exit_fee, 4)

    def _gross_pnl(self, trade: ShadowTrade, price: float) -> float:
        multiplier = 1.0 if trade.side == "LONG" else -1.0
        return (price - trade.entry_price) * trade.volume * multiplier

    def _slippage_rate(self) -> float:
        bps = max(float(self.settings.paper_slippage_bps), 0.0) + max(
            float(self.settings.execution_market_impact_bps), 0.0
        )
        return bps / 10_000

    def _report_dict(self, report: ShadowForwardReport) -> dict[str, float | int | str]:
        return {
            "status": report.status,
            "reason": report.reason,
            "closed_trades": report.closed_trades,
            "wins": report.wins,
            "losses": report.losses,
            "win_rate": report.win_rate,
            "profit_factor": report.profit_factor,
            "total_pnl": report.total_pnl,
            "max_drawdown_percent": report.max_drawdown_percent,
        }
