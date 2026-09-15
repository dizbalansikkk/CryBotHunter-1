import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.entities import AgentDecision, LogEntry, OrderStatus, Position, Signal, Trade
from app.schemas.dto import AgentAnalysisOut, MarketCoin, PositionUpdateOut, StrategySignal, TradingDecision, TradingRunOut, TradingTickOut
from app.services.agents import AgentOrchestrator
from app.services.context_manager import ContextManager
from app.services.control import TradingControlService
from app.services.cooldown import LossCooldownGuard
from app.services.exchange import ExchangeClient
from app.services.execution import ExecutionService
from app.services.learning import LearningService
from app.services.market_quality import MarketQualityGate
from app.services.market_scanner import MarketScanner
from app.services.microstructure import EntryGatekeeper, MicrostructureService
from app.services.optimizer import StrategyOptimizerService
from app.services.performance_guard import PerformanceGuardReport, PerformanceGuardService
from app.services.pnl import PnlMetricsService
from app.services.pretrade_quality import PreTradeQualityGate
from app.services.post_mortem import PostMortemService
from app.services.risk_manager import DrawdownAssessment, RiskManager, RiskSettings
from app.services.rl_gate import RlDecisionGate
from app.services.strategy import StrategyCore
from app.services.telegram_bot import TelegramNotifier
from app.services.telegram_reports import (
    format_partial_take_profit,
    format_protection_update,
    format_trade_closed,
    format_trade_opened,
)
from app.services.telegram_cards import safe_render_position_card


logger = logging.getLogger(__name__)


class TradingEngine:
    def __init__(
        self,
        exchange: ExchangeClient | None = None,
        control: TradingControlService | None = None,
    ) -> None:
        self.exchange = exchange or ExchangeClient()
        self.scanner = MarketScanner(self.exchange)
        self.market_quality = MarketQualityGate()
        self.microstructure = MicrostructureService(self.exchange)
        self.entry_gatekeeper = EntryGatekeeper()
        self.strategy = StrategyCore()
        self.optimizer = StrategyOptimizerService()
        self.risk = RiskManager()
        self.rl_gate = RlDecisionGate()
        self.execution = ExecutionService(self.exchange)
        self.guard = PerformanceGuardService()
        self.cooldown_guard = LossCooldownGuard()
        self.pnl_metrics = PnlMetricsService()
        self.quality_gate = PreTradeQualityGate()
        self.agents = AgentOrchestrator()
        self.learning = LearningService()
        self.post_mortem = PostMortemService(self.exchange)
        self.telegram = TelegramNotifier()
        self.context = ContextManager()
        self.control = control or TradingControlService()
        self._owns_control = control is None
        self.settings = get_settings()

    async def close(self) -> None:
        if self._owns_control:
            await self.control.close()

    async def _position_card(
        self,
        position: Position,
        *,
        event: str,
        score: int | None = None,
        exit_price: float | None = None,
        exit_reason: str | None = None,
    ) -> bytes | None:
        candles: list[list[float]] | None = None
        try:
            candles = await self.exchange.fetch_ohlcv(position.symbol, timeframe="1h", limit=48)
        except Exception as exc:
            logger.warning(
                "Telegram candle chart data unavailable symbol=%s error=%s",
                position.symbol,
                type(exc).__name__,
            )
        return safe_render_position_card(
            position,
            event=event,
            score=score,
            exit_price=exit_price,
            exit_reason=exit_reason,
            candles=candles,
        )

    async def run_once(self, db: AsyncSession, settings: RiskSettings, timeframe: str = "1h") -> TradingRunOut:
        balance = (await self.exchange.get_balance()).get("USDT", settings.balance)
        settings = self._settings_with_balance(settings, balance)
        drawdown = await self._enforce_drawdown_limit(db, settings.balance)
        if drawdown.emergency:
            await db.commit()
            return TradingRunOut(scanned=0, opened=0, skipped=0, decisions=[])
        coins = await self.scanner.scan()
        guard = await self.guard.evaluate(db)
        learning_lane_enabled = self._paper_learning_lane_enabled()
        if not guard.allowed and not learning_lane_enabled:
            db.add(LogEntry(level="WARNING", message=f"Performance guard blocked entries: {guard.reason}"))
            await db.commit()
            return TradingRunOut(
                scanned=len(coins),
                opened=0,
                skipped=len(coins),
                decisions=[
                    TradingDecision(symbol=coin.symbol, signal="WAIT", score=coin.rating, action="SKIPPED", reason=f"performance guard: {guard.reason}")
                    for coin in coins
                ],
            )
        open_count = await self._open_positions_count(db)
        strategy_open_count, exploration_open_count = await self._open_position_counts_by_lane(db)
        open_symbols = await self._open_symbols(db)
        side_counts = await self._open_side_counts(db)
        daily_pnl = (await self.pnl_metrics.summary(db)).pnl_day
        exposure = await self._portfolio_exposure(db)
        decisions: list[TradingDecision] = []
        exploration_opened = 0

        ranked_coins = [(coin, self.strategy.evaluate(coin)) for coin in coins]
        ranked_coins.sort(key=lambda item: self._opportunity_rank(item[0], item[1]), reverse=True)

        for coin, original_signal in ranked_coins:
            signal, exploration = self._paper_exploration_signal(coin, original_signal)
            db_signal = Signal(symbol=coin.symbol, signal=signal.signal, score=signal.score)
            db.add(db_signal)
            trade_settings = self._guard_recovery_settings(settings, guard)
            optimizer_reason = ""
            committee: AgentAnalysisOut | None = None
            entry_snapshot: dict = {}

            if exploration:
                trade_settings = self._paper_exploration_settings(trade_settings)

            optimization = await self.optimizer.best_for(db, coin.symbol, timeframe)
            if optimization:
                trade_settings, optimizer_reason = self.optimizer.apply_to_risk_settings(trade_settings, optimization)

            if coin.symbol in open_symbols:
                accepted, reason = False, "position already open for symbol"
            elif not exploration and not guard.allowed:
                accepted, reason = False, f"performance guard: {guard.reason}"
            elif not exploration and self._guard_recovery_position_limit_reached(guard, strategy_open_count):
                accepted, reason = False, "performance guard recovery position limit reached"
            elif exploration and exploration_opened >= max(int(self.settings.paper_exploration_max_per_cycle), 1):
                accepted, reason = False, "paper learning per-cycle entry limit reached"
            elif exploration and exploration_open_count >= self._paper_exploration_position_limit(guard):
                accepted, reason = False, "paper exploration position limit reached"
            elif exploration and not await self._paper_exploration_cooldown_elapsed(db, coin.symbol):
                accepted, reason = False, "paper exploration cooldown is active"
            else:
                accepted, reason = self.risk.can_open(signal, trade_settings, open_count, daily_pnl)
                if not accepted and signal.signal == "WAIT":
                    reason = self._strategy_wait_reason(coin, signal)
                if accepted and optimizer_reason:
                    reason = f"{reason}; {optimizer_reason}"
                if accepted and exploration:
                    reason = f"{reason}; paper exploration from WAIT"
                if accepted and exploration and not guard.allowed:
                    reason = f"{reason}; paper learning lane during guard cooldown: {guard.reason}"
                elif accepted and guard.recovery_mode:
                    reason = f"{reason}; {guard.reason}"
            if accepted:
                cooldown = await self.cooldown_guard.assess(db, coin.symbol)
                if not cooldown.allowed:
                    accepted = False
                    reason = cooldown.reason
            if accepted:
                learning = await self.learning.assess_entry(db, coin, signal.signal)
                if not learning.allowed:
                    accepted = False
                    reason = learning.reason
                elif learning.risk_multiplier < 1:
                    trade_settings = replace(trade_settings, risk_percent=round(trade_settings.risk_percent * learning.risk_multiplier, 4))
                    reason = f"{reason}; {learning.reason}"
                elif learning.penalty > 0:
                    reason = f"{reason}; {learning.reason}"
            if accepted:
                market_quality = self.market_quality.assess(coin)
                if not market_quality.allowed:
                    accepted = False
                    reason = market_quality.reason
                elif market_quality.risk_multiplier < 1:
                    trade_settings = replace(trade_settings, risk_percent=round(trade_settings.risk_percent * market_quality.risk_multiplier, 4))
                    reason = f"{reason}; {market_quality.reason}"
                elif exploration:
                    reason = f"{reason}; {market_quality.reason}"
            if accepted:
                quality = await self.quality_gate.assess(
                    db,
                    coin.symbol,
                    timeframe,
                    trade_settings,
                    learning_probe=exploration,
                )
                if not quality.allowed:
                    accepted = False
                    reason = quality.reason
                elif quality.risk_multiplier < 1:
                    trade_settings = replace(trade_settings, risk_percent=round(trade_settings.risk_percent * quality.risk_multiplier, 4))
                    reason = f"{reason}; {quality.reason}"
                elif "warning" in quality.reason:
                    reason = f"{reason}; {quality.reason}"
                elif exploration:
                    reason = f"{reason}; {quality.reason}"
            if accepted:
                rl_assessment = await self.rl_gate.assess(db, coin.symbol, signal.signal)
                if not rl_assessment.allowed:
                    accepted = False
                    reason = rl_assessment.reason
                elif rl_assessment.risk_multiplier < 1:
                    trade_settings = replace(
                        trade_settings,
                        risk_percent=round(trade_settings.risk_percent * rl_assessment.risk_multiplier, 4),
                    )
                    reason = f"{reason}; {rl_assessment.reason}"
                elif "agrees" in rl_assessment.reason:
                    reason = f"{reason}; {rl_assessment.reason}"
                elif exploration:
                    reason = f"{reason}; {rl_assessment.reason}"
            if accepted:
                entry_snapshot = await self.microstructure.capture(coin.symbol)
                entry_gate = self.entry_gatekeeper.assess(coin, signal.signal, entry_snapshot)
                if not entry_gate.allowed:
                    accepted = False
                    reason = entry_gate.reason
                elif entry_gate.risk_multiplier < 1:
                    trade_settings = replace(
                        trade_settings,
                        risk_percent=round(trade_settings.risk_percent * entry_gate.risk_multiplier, 4),
                    )
                    reason = f"{reason}; {entry_gate.reason}"
                else:
                    reason = f"{reason}; {entry_gate.reason}"
            if accepted:
                side = "LONG" if signal.signal == "BUY" else "SHORT"
                direction_allowed, direction_reason, direction_multiplier = self.risk.directional_exposure(
                    side=side,
                    side_counts=side_counts,
                    max_same_side_positions=self._same_side_position_limit(exploration),
                    reduction_start=self.settings.directional_risk_reduction_start,
                    risk_multiplier=self.settings.directional_risk_multiplier,
                )
                if not direction_allowed:
                    accepted = False
                    reason = direction_reason
                elif direction_multiplier < 1:
                    trade_settings = replace(trade_settings, risk_percent=round(trade_settings.risk_percent * direction_multiplier, 4))
                    reason = f"{reason}; {direction_reason}"
            if accepted:
                exposure_allowed, exposure_reason, candidate_notional = self._exposure_gate(
                    coin,
                    signal.signal,
                    balance,
                    trade_settings,
                    exposure,
                )
                accepted = exposure_allowed
                reason = f"{reason}; {exposure_reason}" if exposure_allowed else exposure_reason
            else:
                candidate_notional = 0.0
            if accepted and not exploration:
                committee = await self._committee_gate(db, coin, signal.signal)
                if committee and not self._committee_allows_signal(committee, signal.signal):
                    accepted = False
                    reason = (
                        f"committee rejected: final={committee.final_action}, "
                        f"consensus={committee.consensus_score:.2f}, confidence={committee.final_confidence:.2f}"
                    )
            if accepted:
                position = await self._open_position(
                    db,
                    coin,
                    signal.signal,
                    signal.reasons,
                    balance,
                    trade_settings,
                    signal_score=signal.score,
                    decision_reason=reason,
                    paper_exploration=exploration,
                    committee=committee,
                    microstructure=entry_snapshot,
                )
                if position:
                    open_count += 1
                    if exploration:
                        exploration_open_count += 1
                        exploration_opened += 1
                    else:
                        strategy_open_count += 1
                    open_symbols.add(coin.symbol)
                    side_counts[position.side] = side_counts.get(position.side, 0) + 1
                    exposure["gross"] += candidate_notional
                    exposure["symbols"][coin.symbol] = exposure["symbols"].get(coin.symbol, 0.0) + candidate_notional
                    entry_kind = "paper exploration" if exploration else "strategy"
                    db.add(LogEntry(level="INFO", message=f"Opened {entry_kind} {signal.signal} position for {coin.symbol}"))
                    if self.settings.telegram_trade_reports_enabled:
                        await self.telegram.broadcast(
                            format_trade_opened(
                                position,
                                score=signal.score,
                                reason=reason,
                                paper_trading=self.settings.paper_trading,
                                exploration=exploration,
                            ),
                            photo=await self._position_card(position, event="OPENED", score=signal.score),
                            photo_filename=f"position-{position.id}-opened.jpg",
                            photo_caption="<b>Визуальная карточка входа</b>",
                            dedupe_key=f"position:{position.id}:opened",
                        )
                    decisions.append(
                        TradingDecision(symbol=coin.symbol, signal=signal.signal, score=signal.score, action="OPENED", reason=reason)
                    )
                    if exploration:
                        self._record_paper_learning_decisions(db, coin, signal, allowed=True, reason=reason)
                else:
                    if exploration:
                        self._record_paper_learning_decisions(
                            db,
                            coin,
                            signal,
                            allowed=False,
                            reason="position size is zero or execution was not filled",
                        )
                    decisions.append(
                        TradingDecision(symbol=coin.symbol, signal=signal.signal, score=signal.score, action="SKIPPED", reason="position size is zero")
                    )
            else:
                if exploration:
                    self._record_paper_learning_decisions(db, coin, signal, allowed=False, reason=reason)
                db.add(LogEntry(level="INFO", message=f"Skipped {coin.symbol}: {reason}"))
                decisions.append(
                    TradingDecision(symbol=coin.symbol, signal=signal.signal, score=signal.score, action="SKIPPED", reason=reason)
                )

        await db.commit()
        opened = sum(1 for item in decisions if item.action == "OPENED")
        return TradingRunOut(scanned=len(coins), opened=opened, skipped=len(decisions) - opened, decisions=decisions)

    async def manage_open_positions(self, db: AsyncSession) -> TradingTickOut:
        positions = list(
            (
                await db.execute(select(Position).where(Position.status == "OPEN").order_by(Position.entered_at.asc()))
            ).scalars().all()
        )
        balance = self._safe_balance((await self.exchange.get_balance()).get("USDT"), fallback=1000.0)
        if not positions:
            drawdown = await self._enforce_drawdown_limit(db, balance)
            if drawdown.emergency:
                await db.commit()
            return TradingTickOut(checked=0, closed=0, updated=[])

        market = {coin.symbol: coin for coin in await self.scanner.scan([position.symbol for position in positions])}
        previous_prices: dict[int, float] = {}
        for position in positions:
            coin = market.get(position.symbol)
            if not coin:
                continue
            previous_prices[position.id] = position.current_price
            position.current_price = coin.price
            position.highest_price = max(position.highest_price or position.entry_price, coin.price)
            position.lowest_price = min(position.lowest_price or position.entry_price, coin.price)
            position.pnl = await self._position_total_pnl(db, position, coin.price)
            await self._capture_position_microstructure(position)

        drawdown = await self._enforce_drawdown_limit(db, balance)
        emergency_close = drawdown.emergency
        updates: list[PositionUpdateOut] = []

        for position in positions:
            coin = market.get(position.symbol)
            if not coin:
                continue
            if emergency_close:
                await self._close_position(db, position, coin.price, "EMERGENCY_DRAWDOWN")
            else:
                if await self._apply_partial_take_profit(db, position, coin.price):
                    db.add(LogEntry(level="INFO", message=f"Partially closed {position.symbol} #{position.id}: remaining={position.volume:.6f}"))
                if self._apply_breakeven(position):
                    db.add(LogEntry(level="INFO", message=f"Moved {position.symbol} #{position.id} stop to breakeven: stop={position.stop:.4f}"))
                    if self.settings.telegram_trade_reports_enabled:
                        await self.telegram.broadcast(
                            format_protection_update(position, "BREAKEVEN"),
                            photo=await self._position_card(position, event="PROTECTION"),
                            photo_filename=f"position-{position.id}-protection.jpg",
                            photo_caption="<b>Защита позиции обновлена</b>",
                            dedupe_key=f"position:{position.id}:breakeven",
                        )
                self._apply_trailing_stop(position)
                position.pnl = await self._position_total_pnl(db, position, coin.price)
                exit_reason = self._exit_reason(position)
                if exit_reason:
                    await self._close_position(db, position, coin.price, exit_reason)
            updates.append(
                PositionUpdateOut(
                    id=position.id,
                    symbol=position.symbol,
                    side=position.side,
                    entry_price=position.entry_price,
                    previous_price=previous_prices.get(position.id, position.current_price),
                    current_price=position.current_price,
                    volume=position.volume,
                    pnl=position.pnl,
                    status=position.status,
                    exit_reason=position.exit_reason,
                    stop=position.stop,
                    take=position.take,
                )
            )

        await db.commit()
        closed = sum(1 for item in updates if item.status == "CLOSED")
        return TradingTickOut(checked=len(positions), closed=closed, updated=updates)

    def _paper_exploration_signal(
        self,
        coin: MarketCoin,
        signal: StrategySignal,
    ) -> tuple[StrategySignal, bool]:
        if (
            not self.settings.paper_trading
            or not self.settings.paper_exploration_enabled
            or signal.signal != "WAIT"
            or signal.score < self.settings.paper_exploration_min_score
        ):
            return signal, False

        hard_blocks = ("blocked by market regime", "volatility too low", "volatility too high")
        if any(marker in reason for marker in hard_blocks for reason in signal.reasons):
            return signal, False

        bullish_votes, bearish_votes = self._paper_exploration_votes(coin)
        strongest_votes = max(bullish_votes, bearish_votes)
        vote_margin = abs(bullish_votes - bearish_votes)
        if (
            strongest_votes < max(int(self.settings.paper_exploration_min_directional_votes), 1)
            or vote_margin < max(int(self.settings.paper_exploration_min_vote_margin), 1)
        ):
            return signal, False
        direction = "BUY" if bullish_votes > bearish_votes else "SELL"
        reasons = [
            (
                "paper exploration from WAIT: "
                f"bullish_votes={bullish_votes}, bearish_votes={bearish_votes}, margin={vote_margin}"
            ),
            *signal.reasons[:3],
        ]
        return StrategySignal(symbol=signal.symbol, signal=direction, score=signal.score, reasons=reasons), True

    def _paper_learning_lane_enabled(self) -> bool:
        return bool(self.settings.paper_trading and self.settings.paper_exploration_enabled)

    def _paper_exploration_settings(self, settings: RiskSettings) -> RiskSettings:
        return replace(
            settings,
            risk_percent=min(
                float(settings.risk_percent),
                max(float(self.settings.paper_exploration_risk_percent), 0.01),
                max(float(self.settings.paper_exploration_max_risk_percent), 0.01),
            ),
            min_rating=min(
                int(settings.min_rating),
                max(int(self.settings.paper_exploration_min_score), 0),
            ),
        )

    def _paper_exploration_votes(self, coin: MarketCoin) -> tuple[int, int]:
        bullish_votes = sum(
            (
                coin.regime in {"TRENDING_UP", "UNKNOWN"},
                coin.ema20 > coin.ema50,
                coin.ema50 > coin.ema200,
                coin.price > coin.ema20,
                coin.rsi >= 50,
                coin.macd > 0,
                coin.price_change_percent >= 0,
            )
        )
        bearish_votes = sum(
            (
                coin.regime in {"TRENDING_DOWN", "UNKNOWN"},
                coin.ema20 < coin.ema50,
                coin.ema50 < coin.ema200,
                coin.price < coin.ema20,
                coin.rsi < 50,
                coin.macd < 0,
                coin.price_change_percent < 0,
            )
        )
        return bullish_votes, bearish_votes

    def _strategy_wait_reason(self, coin: MarketCoin, signal: StrategySignal) -> str:
        bullish_votes, bearish_votes = self._paper_exploration_votes(coin)
        missing = "; ".join(signal.reasons[:2]) if signal.reasons else "no directional confirmation"
        return (
            f"strategy WAIT score={signal.score}, rating={coin.rating}, "
            f"bullish_votes={bullish_votes}, bearish_votes={bearish_votes}: {missing}"
        )

    def _record_paper_learning_decisions(
        self,
        db: AsyncSession,
        coin: MarketCoin,
        signal: StrategySignal,
        *,
        allowed: bool,
        reason: str,
    ) -> None:
        bullish_votes, bearish_votes = self._paper_exploration_votes(coin)
        context = {
            "paper_only": True,
            "source_signal": "WAIT",
            "signal_score": int(signal.score),
            "bullish_votes": bullish_votes,
            "bearish_votes": bearish_votes,
            "vote_margin": abs(bullish_votes - bearish_votes),
        }
        db.add(
            AgentDecision(
                agent_name="PaperLearningScout",
                symbol=coin.symbol,
                action=signal.signal,
                confidence=round(max(min(signal.score / 100, 1.0), 0.0), 4),
                rationale=signal.reasons[0] if signal.reasons else "paper learning candidate",
                context=context,
            )
        )
        db.add(
            AgentDecision(
                agent_name="PaperLearningRiskGate",
                symbol=coin.symbol,
                action="ALLOW" if allowed else "BLOCK",
                confidence=1.0,
                rationale=reason,
                context=context,
            )
        )

    async def _paper_exploration_cooldown_elapsed(self, db: AsyncSession, symbol: str) -> bool:
        cooldown_minutes = max(int(self.settings.paper_exploration_cooldown_minutes), 0)
        if cooldown_minutes == 0:
            return True
        entered_at = (
            await db.execute(
                select(Position.entered_at)
                .where(Position.symbol == symbol)
                .order_by(Position.entered_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if not entered_at:
            return True
        if entered_at.tzinfo is None:
            entered_at = entered_at.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - entered_at >= timedelta(minutes=cooldown_minutes)

    async def _open_position(
        self,
        db: AsyncSession,
        coin: MarketCoin,
        signal: str,
        signal_reasons: list[str],
        balance: float,
        settings: RiskSettings,
        signal_score: int = 0,
        decision_reason: str = "",
        paper_exploration: bool = False,
        committee: AgentAnalysisOut | None = None,
        microstructure: dict | None = None,
    ) -> Position | None:
        side = "LONG" if signal == "BUY" else "SHORT"
        stop, take, initial_risk = self._exit_plan(coin.price, coin.atr, side, settings)
        volume = self.risk.calculate_position_size(
            balance,
            settings.risk_percent,
            coin.price,
            stop,
            max_position_percent=settings.max_position_size_percent,
        )
        if volume <= 0:
            return None
        entry_order = await self.execution.execute_market(
            db,
            coin.symbol,
            "buy" if side == "LONG" else "sell",
            volume,
            coin.price,
            "ENTRY",
        )
        if entry_order.status != OrderStatus.FILLED.value or not entry_order.average_price:
            return None
        entry_price = entry_order.average_price
        volume = entry_order.filled_amount or entry_order.requested_amount
        stop, take, initial_risk = self._exit_plan(entry_price, coin.atr, side, settings)
        entry_context = self.learning.entry_context(coin, signal, signal_reasons)
        entry_context["paper_exploration"] = paper_exploration
        entry_context["decision_reason"] = decision_reason
        entry_context["signal_score"] = int(signal_score)
        entry_context["entry_confidence"] = round(max(min(signal_score / 100, 1.0), 0.0), 4)
        entry_context["microstructure"] = microstructure or {}
        entry_context["position_microstructure"] = [
            {
                **(microstructure or {}),
                "phase": "ENTRY",
                "price": round(float(entry_price), 8),
                "pnl": round(float(-entry_order.fee), 4),
            }
        ] if microstructure else []
        entry_context["entry_execution"] = {
            "order_id": entry_order.id,
            "fee": round(float(entry_order.fee or 0.0), 8),
            "slippage": round(float(entry_order.slippage or 0.0), 8),
            "volume": round(float(volume), 8),
            "average_price": round(float(entry_price), 8),
        }
        if paper_exploration:
            bullish_votes, bearish_votes = self._paper_exploration_votes(coin)
            entry_context.update(
                {
                    "learning_lane": "paper_exploration",
                    "bullish_votes": bullish_votes,
                    "bearish_votes": bearish_votes,
                    "vote_margin": abs(bullish_votes - bearish_votes),
                }
            )
        if committee:
            agent_votes = [committee.market, *committee.committee, committee.risk]
            if committee.llm:
                agent_votes.append(committee.llm)
            entry_context.update(
                {
                    "committee_consensus": round(float(committee.consensus_score), 4),
                    "committee_confidence": round(float(committee.final_confidence), 4),
                    "committee_action": committee.final_action,
                    "committee_approved": bool(committee.approved),
                    "agent_votes": [
                        {
                            "agent": vote.agent_name,
                            "action": vote.action,
                            "confidence": round(float(vote.confidence), 4),
                            "rationale": vote.rationale,
                        }
                        for vote in agent_votes
                    ],
                }
            )
        notional = entry_price * volume
        planned_risk = initial_risk * volume
        planned_reward = abs(take - entry_price) * volume
        entry_context.update(
            {
                "balance": round(float(balance), 2),
                "risk_percent": round(float(settings.risk_percent), 4),
                "notional": round(notional, 2),
                "planned_risk": round(planned_risk, 4),
                "planned_reward": round(planned_reward, 4),
                "risk_reward_ratio": round(planned_reward / planned_risk, 2) if planned_risk > 0 else 0.0,
                "stop_distance_percent": round(initial_risk / entry_price * 100, 4),
                "take_distance_percent": round(abs(take - entry_price) / entry_price * 100, 4),
            }
        )
        position = Position(
            symbol=coin.symbol,
            side=side,
            entry_price=entry_price,
            current_price=entry_price,
            volume=volume,
            stop=stop,
            take=take,
            initial_risk=initial_risk,
            breakeven_applied=False,
            breakeven_trigger_r=settings.breakeven_trigger_r,
            breakeven_offset_percent=settings.breakeven_offset_percent,
            partial_take_profit_r=settings.partial_take_profit_r,
            partial_close_percent=settings.partial_close_percent,
            partial_taken=False,
            trailing_stop_percent=settings.trailing_stop_percent,
            highest_price=entry_price,
            lowest_price=entry_price,
            entry_context=entry_context,
        )
        db.add(position)
        await db.flush()
        db.add(Trade(position_id=position.id, symbol=coin.symbol, side=side, entry_price=entry_price, exit_price=None, profit=-entry_order.fee))
        return position

    def _opportunity_rank(self, coin: MarketCoin, signal: StrategySignal) -> tuple[int, int, int, int]:
        return (
            1 if signal.signal in {"BUY", "SELL"} else 0,
            int(signal.score),
            int(coin.regime_score),
            int(coin.rating),
        )

    async def _capture_position_microstructure(self, position: Position) -> None:
        if not self.settings.post_mortem_enabled:
            return
        context = dict(position.entry_context or {})
        snapshots = list(context.get("position_microstructure") or [])
        interval = timedelta(minutes=max(int(self.settings.post_mortem_snapshot_interval_minutes), 1))
        if snapshots:
            observed_at = snapshots[-1].get("observed_at") if isinstance(snapshots[-1], dict) else None
            try:
                last_observed = datetime.fromisoformat(str(observed_at))
                if last_observed.tzinfo is None:
                    last_observed = last_observed.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) - last_observed < interval:
                    return
            except (TypeError, ValueError):
                pass
        snapshot = await self.microstructure.capture(position.symbol)
        snapshot.update(
            {
                "phase": "POSITION",
                "price": round(float(position.current_price), 8),
                "pnl": round(float(position.pnl or 0.0), 4),
            }
        )
        snapshots.append(snapshot)
        max_snapshots = max(int(self.settings.post_mortem_max_position_snapshots), 3)
        context["position_microstructure"] = snapshots[-max_snapshots:]
        position.entry_context = context

    def _same_side_position_limit(self, paper_exploration: bool) -> int:
        configured = max(int(self.settings.max_same_side_positions), 1)
        if paper_exploration and self.settings.paper_trading:
            return max(configured, max(int(self.settings.paper_exploration_max_positions), 1))
        return configured

    def _guard_recovery_settings(
        self,
        settings: RiskSettings,
        guard: PerformanceGuardReport,
    ) -> RiskSettings:
        if not guard.recovery_mode:
            return settings
        return replace(
            settings,
            risk_percent=round(float(settings.risk_percent) * float(guard.risk_multiplier), 4),
        )

    def _guard_recovery_position_limit_reached(
        self,
        guard: PerformanceGuardReport,
        open_count: int,
    ) -> bool:
        return guard.recovery_mode and open_count >= max(int(self.settings.guard_recovery_max_positions), 1)

    def _paper_exploration_position_limit(self, guard: PerformanceGuardReport) -> int:
        configured = max(int(self.settings.paper_exploration_max_positions), 1)
        if guard.recovery_mode or not guard.allowed:
            return min(configured, max(int(self.settings.paper_exploration_recovery_slots), 1))
        return configured

    def _exit_plan(self, entry_price: float, atr: float, side: str, settings: RiskSettings) -> tuple[float, float, float]:
        plan = self.risk.calculate_dynamic_exits(
            entry_price=entry_price,
            atr=atr,
            side=side,
            atr_multiplier=settings.atr_stop_multiplier,
            risk_reward_ratio=settings.risk_reward_ratio,
            fallback_stop_percent=settings.stop_loss_percent,
        )
        return plan.stop_loss, plan.take_profit, plan.risk_per_unit

    def _exposure_gate(
        self,
        coin: MarketCoin,
        signal: str,
        balance: float,
        settings: RiskSettings,
        exposure: dict,
    ) -> tuple[bool, str, float]:
        side = "LONG" if signal == "BUY" else "SHORT"
        try:
            stop, _take, _initial_risk = self._exit_plan(coin.price, coin.atr, side, settings)
        except ValueError as exc:
            return False, f"invalid dynamic risk inputs: {exc}", 0.0
        volume = self.risk.calculate_position_size(
            balance,
            settings.risk_percent,
            coin.price,
            stop,
            max_position_percent=settings.max_position_size_percent,
        )
        candidate_notional = self.risk.position_notional(coin.price, volume)
        accepted, reason = self.risk.can_add_exposure(
            balance=balance,
            current_gross_exposure=exposure["gross"],
            current_symbol_exposure=exposure["symbols"].get(coin.symbol, 0.0),
            candidate_notional=candidate_notional,
            max_gross_exposure_percent=self.settings.max_gross_exposure_percent,
            max_symbol_exposure_percent=self.settings.max_symbol_exposure_percent,
        )
        return accepted, reason, candidate_notional

    async def _committee_gate(self, db: AsyncSession, coin: MarketCoin, signal: str) -> AgentAnalysisOut | None:
        if not self.settings.ai_committee_enabled or signal not in {"BUY", "SELL"}:
            return None
        analysis = await self.agents.analyze_coin(db, coin)
        db.add(
            LogEntry(
                level="INFO",
                message=(
                    f"AI committee {coin.symbol}: final={analysis.final_action}, "
                    f"consensus={analysis.consensus_score:.2f}, confidence={analysis.final_confidence:.2f}"
                ),
            )
        )
        return analysis

    def _committee_allows_signal(self, analysis: AgentAnalysisOut, signal: str) -> bool:
        return (
            analysis.approved
            and analysis.final_action == signal
            and analysis.consensus_score >= self.settings.ai_committee_min_consensus
        )

    async def _apply_partial_take_profit(self, db: AsyncSession, position: Position, price: float) -> bool:
        if position.partial_taken or not self._partial_take_profit_reached(position, price):
            return False
        close_volume = self._partial_close_volume(position)
        if close_volume <= 0 or close_volume >= position.volume:
            return False
        exit_order = await self.execution.execute_market(
            db,
            position.symbol,
            "sell" if position.side == "LONG" else "buy",
            close_volume,
            price,
            "PARTIAL_TAKE_PROFIT",
        )
        if exit_order.status != OrderStatus.FILLED.value or not exit_order.average_price:
            db.add(LogEntry(level="ERROR", message=f"Failed partial take profit for {position.symbol} #{position.id}"))
            return False
        partial_profit = self._profit_for_volume(position, exit_order.average_price, close_volume) - exit_order.fee
        position.volume = round(position.volume - close_volume, 8)
        position.partial_taken = True
        db.add(
            Trade(
                position_id=position.id,
                symbol=position.symbol,
                side=position.side,
                entry_price=position.entry_price,
                exit_price=exit_order.average_price,
                profit=round(partial_profit, 4),
            )
        )
        position.pnl = await self._position_total_pnl(db, position, price)
        if self.settings.telegram_trade_reports_enabled:
            await self.telegram.broadcast(
                format_partial_take_profit(
                    position,
                    closed_volume=close_volume,
                    exit_price=exit_order.average_price,
                    profit=partial_profit,
                ),
                photo=await self._position_card(
                    position,
                    event="PARTIAL",
                    exit_price=exit_order.average_price,
                ),
                photo_filename=f"position-{position.id}-partial.jpg",
                photo_caption="<b>Частичная фиксация прибыли</b>",
                dedupe_key=f"position:{position.id}:partial",
            )
        return True

    def _partial_take_profit_reached(self, position: Position, price: float) -> bool:
        initial_risk = position.initial_risk or abs(position.entry_price - position.stop)
        if initial_risk <= 0:
            return False
        trigger = initial_risk * (position.partial_take_profit_r or 1.0)
        if position.side == "LONG":
            return price >= position.entry_price + trigger
        return price <= position.entry_price - trigger

    def _partial_close_volume(self, position: Position) -> float:
        close_percent = min(max(position.partial_close_percent or 0.0, 0.0), 90.0)
        return round(position.volume * close_percent / 100, 8)

    async def _close_position(self, db: AsyncSession, position: Position, exit_price: float, reason: str) -> None:
        exit_order = await self.execution.execute_market(
            db,
            position.symbol,
            "sell" if position.side == "LONG" else "buy",
            position.volume,
            exit_price,
            f"EXIT_{reason}",
        )
        if exit_order.status != OrderStatus.FILLED.value or not exit_order.average_price:
            db.add(LogEntry(level="ERROR", message=f"Failed to close {position.symbol} #{position.id}: {reason}"))
            return
        position.status = "CLOSED"
        position.exit_reason = reason
        position.closed_at = datetime.now(timezone.utc)
        position.current_price = exit_order.average_price
        trade = (
            await db.execute(
                select(Trade)
                .where(Trade.position_id == position.id, Trade.exit_price.is_(None))
                .order_by(Trade.created_at.desc())
            )
        ).scalars().first()
        if not trade:
            trade = (
                await db.execute(
                    select(Trade)
                    .where(Trade.symbol == position.symbol, Trade.exit_price.is_(None))
                    .order_by(Trade.created_at.desc())
            )
        ).scalars().first()
        previous_realized = await self._position_profit_sum(db, position.id, exclude_trade_id=trade.id if trade else None)
        final_trade_profit = self._final_trade_profit(
            existing_trade_profit=float(trade.profit or 0.0) if trade else 0.0,
            position=position,
            exit_price=exit_order.average_price,
            exit_fee=exit_order.fee,
        )
        if trade:
            trade.exit_price = exit_order.average_price
            trade.profit = final_trade_profit
        else:
            db.add(
                Trade(
                    position_id=position.id,
                    symbol=position.symbol,
                    side=position.side,
                    entry_price=position.entry_price,
                    exit_price=exit_order.average_price,
                    profit=final_trade_profit,
                )
            )
        position.pnl = self._total_closed_profit(previous_realized, final_trade_profit)
        try:
            post_mortem = await self.post_mortem.analyze_loss(db, position, exit_order, reason)
            if post_mortem:
                db.add(
                    LogEntry(
                        level="WARNING",
                        message=(
                            f"Post-mortem {position.symbol} #{position.id}: "
                            f"label={post_mortem.primary_label}, reward={post_mortem.shaped_reward:+.2f}, "
                            f"priority={post_mortem.priority:.2f}"
                        ),
                    )
                )
        except Exception as exc:
            db.add(
                LogEntry(
                    level="ERROR",
                    message=f"Post-mortem failed for {position.symbol} #{position.id}: {type(exc).__name__}",
                )
            )
        await self.learning.record_closed_position(db, position, position.pnl, reason)
        try:
            await self.context.remember_trade(
                symbol=position.symbol,
                side=position.side,
                entry_price=position.entry_price,
                exit_price=exit_order.average_price,
                pnl=position.pnl,
                exit_reason=reason,
                timestamp=position.closed_at,
            )
        except (OSError, ValueError, sqlite3.Error) as exc:
            db.add(LogEntry(level="ERROR", message=f"SQLite trade memory failed for {position.symbol} #{position.id}: {exc}"))
        db.add(LogEntry(level="INFO", message=f"Closed {position.symbol} #{position.id}: {reason}, pnl={position.pnl:.2f}"))
        db.add(LogEntry(level="INFO", message=f"Learning updated from {position.symbol} #{position.id}: reason={reason}, pnl={position.pnl:.2f}"))
        if self.settings.telegram_trade_reports_enabled:
            await self.telegram.broadcast(
                format_trade_closed(position, exit_price=exit_order.average_price, reason=reason),
                photo=await self._position_card(
                    position,
                    event="CLOSED",
                    exit_price=exit_order.average_price,
                    exit_reason=reason,
                ),
                photo_filename=f"position-{position.id}-closed.jpg",
                photo_caption="<b>Финальная карточка сделки</b>",
                dedupe_key=f"position:{position.id}:closed",
            )

    def _apply_trailing_stop(self, position: Position) -> None:
        if position.trailing_stop_percent <= 0:
            return
        if position.side == "LONG":
            trailing_stop = position.highest_price * (1 - position.trailing_stop_percent / 100)
            position.stop = max(position.stop, trailing_stop)
        else:
            trailing_stop = position.lowest_price * (1 + position.trailing_stop_percent / 100)
            position.stop = min(position.stop, trailing_stop)

    def _apply_breakeven(self, position: Position) -> bool:
        if position.breakeven_applied:
            return False
        initial_risk = position.initial_risk or abs(position.entry_price - position.stop)
        if initial_risk <= 0:
            return False
        trigger = initial_risk * (position.breakeven_trigger_r or 1.0)
        offset = position.entry_price * (position.breakeven_offset_percent or 0.0) / 100
        if position.side == "LONG" and position.current_price >= position.entry_price + trigger:
            position.stop = max(position.stop, position.entry_price + offset)
            position.breakeven_applied = True
            return True
        if position.side == "SHORT" and position.current_price <= position.entry_price - trigger:
            position.stop = min(position.stop, position.entry_price - offset)
            position.breakeven_applied = True
            return True
        return False

    def _exit_reason(self, position: Position) -> str | None:
        if position.side == "LONG":
            if position.current_price <= position.stop:
                return "STOP_LOSS"
            if position.current_price >= position.take:
                return "TAKE_PROFIT"
        else:
            if position.current_price >= position.stop:
                return "STOP_LOSS"
            if position.current_price <= position.take:
                return "TAKE_PROFIT"
        return None

    def _pnl(self, position: Position, price: float) -> float:
        multiplier = 1 if position.side == "LONG" else -1
        return round((price - position.entry_price) * position.volume * multiplier, 4)

    def _profit_for_volume(self, position: Position, price: float, volume: float) -> float:
        multiplier = 1 if position.side == "LONG" else -1
        return round((price - position.entry_price) * volume * multiplier, 4)

    def _final_trade_profit(self, existing_trade_profit: float, position: Position, exit_price: float, exit_fee: float) -> float:
        remaining_profit = self._profit_for_volume(position, exit_price, position.volume)
        return round(existing_trade_profit + remaining_profit - exit_fee, 4)

    def _total_closed_profit(self, previous_realized: float, final_trade_profit: float) -> float:
        return round(previous_realized + final_trade_profit, 4)

    async def _position_profit_sum(self, db: AsyncSession, position_id: int | None, exclude_trade_id: int | None = None) -> float:
        if position_id is None:
            return 0.0
        query = select(func.coalesce(func.sum(Trade.profit), 0.0)).where(Trade.position_id == position_id)
        if exclude_trade_id is not None:
            query = query.where(Trade.id != exclude_trade_id)
        result = await db.execute(query)
        return float(result.scalar_one())

    async def _position_total_pnl(self, db: AsyncSession, position: Position, price: float) -> float:
        realized_profit = await self._position_profit_sum(db, position.id)
        unrealized_profit = self._pnl(position, price)
        return round(realized_profit + unrealized_profit, 4)

    async def _open_positions_count(self, db: AsyncSession) -> int:
        result = await db.execute(select(func.count()).select_from(Position).where(Position.status == "OPEN"))
        return int(result.scalar_one())

    async def _open_position_counts_by_lane(self, db: AsyncSession) -> tuple[int, int]:
        result = await db.execute(select(Position.entry_context).where(Position.status == "OPEN"))
        strategy_count = 0
        exploration_count = 0
        for entry_context in result.scalars().all():
            if isinstance(entry_context, dict) and entry_context.get("paper_exploration"):
                exploration_count += 1
            else:
                strategy_count += 1
        return strategy_count, exploration_count

    async def _open_symbols(self, db: AsyncSession) -> set[str]:
        result = await db.execute(select(Position.symbol).where(Position.status == "OPEN"))
        return set(result.scalars().all())

    async def _open_side_counts(self, db: AsyncSession) -> dict[str, int]:
        result = await db.execute(
            select(Position.side, func.count(Position.id))
            .where(Position.status == "OPEN")
            .group_by(Position.side)
        )
        counts = {"LONG": 0, "SHORT": 0}
        for side, count in result.all():
            counts[str(side)] = int(count)
        return counts

    async def _daily_pnl(self, db: AsyncSession) -> float:
        return (await self.pnl_metrics.summary(db)).pnl_day

    async def _enforce_drawdown_limit(self, db: AsyncSession, balance: float) -> DrawdownAssessment:
        threshold = max(float(self.settings.max_drawdown_percent), 0.01)
        closed_pnls = list(
            (
                await db.execute(
                    select(Position.pnl)
                    .where(Position.status == "CLOSED")
                    .order_by(Position.closed_at.asc(), Position.id.asc())
                )
            ).scalars().all()
        )
        open_pnl = float(
            (
                await db.execute(
                    select(func.coalesce(func.sum(Position.pnl), 0.0)).where(Position.status == "OPEN")
                )
            ).scalar_one()
        )
        try:
            assessment = self.risk.calculate_drawdown(
                starting_equity=balance,
                closed_pnls=closed_pnls,
                open_pnl=open_pnl,
                threshold_percent=threshold,
            )
        except (TypeError, ValueError):
            assessment = DrawdownAssessment(
                starting_equity=max(balance, 0.0),
                peak_equity=max(balance, 0.0),
                current_equity=0.0,
                drawdown_percent=100.0,
                threshold_percent=threshold,
                emergency=True,
            )

        if not assessment.emergency:
            return assessment

        reason = f"risk_drawdown:{assessment.drawdown_percent:.2f}%>={assessment.threshold_percent:.2f}%"
        paused, previous_reason = await self.control.is_paused()
        first_activation = not paused or not (previous_reason or "").startswith("risk_drawdown:")
        await self.control.panic(reason)
        if first_activation:
            message = (
                "CRITICAL: portfolio drawdown limit reached\n"
                f"Drawdown: {assessment.drawdown_percent:.2f}%\n"
                f"Limit: {assessment.threshold_percent:.2f}%\n"
                f"Equity: {assessment.current_equity:.2f}\n"
                "Mode: ONLY CLOSE. New entries are blocked."
            )
            db.add(LogEntry(level="CRITICAL", message=message.replace("\n", " | ")))
            await self.telegram.broadcast(message)
        return assessment

    def _settings_with_balance(self, settings: RiskSettings, balance: float) -> RiskSettings:
        return replace(
            settings,
            balance=self._safe_balance(balance, fallback=settings.balance),
            max_position_size_percent=min(
                max(float(self.settings.max_position_size_percent), 0.01),
                100.0,
            ),
        )

    def _safe_balance(self, balance: float | None, fallback: float) -> float:
        try:
            value = float(balance) if balance is not None else float(fallback)
        except (TypeError, ValueError):
            value = float(fallback)
        if value <= 0 or value != value or value in {float("inf"), float("-inf")}:
            value = float(fallback)
        return max(value, 0.01)

    async def _portfolio_exposure(self, db: AsyncSession) -> dict:
        result = await db.execute(select(Position).where(Position.status == "OPEN"))
        symbols: dict[str, float] = {}
        gross = 0.0
        for position in result.scalars().all():
            notional = self.risk.position_notional(position.current_price, position.volume)
            gross += notional
            symbols[position.symbol] = symbols.get(position.symbol, 0.0) + notional
        return {"gross": round(gross, 4), "symbols": symbols}
