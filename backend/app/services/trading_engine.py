import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import logging
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.entities import AgentDecision, LogEntry, Order, OrderStatus, Position, Signal, Trade
from app.schemas.dto import AgentAnalysisOut, MarketCoin, PositionUpdateOut, StrategySignal, TradingDecision, TradingRunOut, TradingTickOut
from app.services.agents import AgentOrchestrator
from app.services.binance_events import BnbPriorityDecision, BinanceEventAssessment, BinanceEventPriorityService
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
        event_priority: BinanceEventPriorityService | None = None,
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
        self.event_priority = event_priority or BinanceEventPriorityService()
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
        cycle_id = uuid4().hex[:10]
        balance = (await self.exchange.get_balance()).get("USDT", settings.balance)
        settings = self._settings_with_balance(settings, balance)
        drawdown = await self._enforce_drawdown_limit(db, settings.balance)
        if drawdown.emergency:
            await db.commit()
            return TradingRunOut(scanned=0, opened=0, skipped=0, decisions=[])
        coins = await self.scanner.scan()
        if self.settings.binance_event_priority_enabled:
            bnb_event = await self.event_priority.assess()
        else:
            bnb_event = BinanceEventAssessment(
                status="NORMAL",
                event_score=0,
                checked_at=datetime.now(timezone.utc),
                reason="Binance event priority is disabled",
            )
        bnb_coin = next((coin for coin in coins if coin.symbol == "BNB/USDT"), None)
        provisional_bnb_priority = (
            self.event_priority.priority_decision(bnb_event, bnb_coin, 0)
            if bnb_coin is not None
            else None
        )
        if provisional_bnb_priority and provisional_bnb_priority.assessment.event_score > 0:
            coins = sorted(coins, key=lambda coin: coin.symbol == "BNB/USDT", reverse=True)
        guard = await self.guard.evaluate(db)
        learning_lane_enabled = self._paper_learning_lane_enabled()
        if not guard.allowed and not learning_lane_enabled:
            self._log_trading_event(
                db,
                "WARNING",
                "ENTRY_CYCLE_BLOCKED",
                f"Performance guard blocked entries: {guard.reason}",
                cycle_id=cycle_id,
                gate="PERFORMANCE_GUARD",
                reason=guard.reason,
                trades_checked=guard.trades_checked,
                win_rate=guard.win_rate,
                total_profit=guard.total_profit,
            )
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
        pending_symbols = await self._pending_order_symbols(db)
        side_counts = await self._open_side_counts(db)
        daily_pnl = (await self.pnl_metrics.summary(db)).pnl_day
        exposure = await self._portfolio_exposure(db)
        reserved_stop_risk = await self._open_stop_risk(db)
        decisions: list[TradingDecision] = []
        exploration_opened = 0

        ranked_coins = [(coin, self.strategy.evaluate(coin)) for coin in coins]
        bnb_priority: BnbPriorityDecision | None = None
        if bnb_coin is not None:
            bnb_signal = next(signal for coin, signal in ranked_coins if coin.symbol == "BNB/USDT")
            bnb_priority = self.event_priority.priority_decision(bnb_event, bnb_coin, bnb_signal.score)
        ranked_coins.sort(
            key=lambda item: self._opportunity_rank(
                item[0],
                item[1],
                bnb_priority if item[0].symbol == "BNB/USDT" else None,
            ),
            reverse=True,
        )
        event_context = bnb_priority.as_dict() if bnb_priority else bnb_event.as_dict()
        self._log_trading_event(
            db,
            "INFO" if bnb_event.status != "UNKNOWN" else "WARNING",
            "BNB_EVENT_EVALUATED",
            (
                f"BNB event status={event_context['status']} score={event_context['event_score']} "
                f"final={event_context.get('final_score', 'n/a')}"
            ),
            cycle_id=cycle_id,
            decision="PRIORITIZE" if bnb_priority and int(event_context["event_score"]) > 0 else "SKIP",
            **event_context,
        )

        for coin, original_signal in ranked_coins:
            signal, exploration = self._paper_exploration_signal(coin, original_signal)
            db_signal = Signal(symbol=coin.symbol, signal=signal.signal, score=signal.score)
            db.add(db_signal)
            trade_settings = self._guard_recovery_settings(settings, guard)
            optimizer_reason = ""
            committee: AgentAnalysisOut | None = None
            entry_snapshot: dict = {}
            candidate_stop_risk = 0.0

            if exploration:
                trade_settings = self._paper_exploration_settings(trade_settings)

            optimization = await self.optimizer.best_for(db, coin.symbol, timeframe)
            if optimization:
                trade_settings, optimizer_reason = self.optimizer.apply_to_risk_settings(trade_settings, optimization)

            if coin.symbol in open_symbols:
                accepted, reason = False, "position already open for symbol"
            elif coin.symbol in pending_symbols:
                accepted, reason = False, "unresolved exchange order for symbol"
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
            if accepted and not exploration:
                symbol_guard = await self.guard.evaluate_symbol(db, coin.symbol)
                if not symbol_guard.allowed:
                    accepted = False
                    reason = symbol_guard.reason
                elif symbol_guard.risk_multiplier < 1:
                    trade_settings = replace(
                        trade_settings,
                        risk_percent=round(trade_settings.risk_percent * symbol_guard.risk_multiplier, 4),
                    )
                    reason = f"{reason}; {symbol_guard.reason}"
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
                daily_risk_allowed, daily_risk_reason, candidate_stop_risk = self._daily_risk_gate(
                    coin,
                    signal.signal,
                    balance,
                    trade_settings,
                    daily_pnl,
                    reserved_stop_risk,
                )
                if not daily_risk_allowed:
                    accepted = False
                    reason = daily_risk_reason
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
                committee = await self._committee_gate(db, coin, signal.signal, cycle_id=cycle_id)
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
                    actual_notional = self.risk.position_notional(position.entry_price, position.volume)
                    exposure["gross"] += actual_notional
                    exposure["symbols"][coin.symbol] = exposure["symbols"].get(coin.symbol, 0.0) + actual_notional
                    reserved_stop_risk = round(reserved_stop_risk + self._position_stop_risk(position), 4)
                    entry_kind = "paper exploration" if exploration else "strategy"
                    entry_execution = (
                        position.entry_context.get("entry_execution", {})
                        if isinstance(position.entry_context, dict)
                        else {}
                    )
                    self._log_trading_event(
                        db,
                        "INFO",
                        "ENTRY_OPENED",
                        f"Opened {entry_kind} {signal.signal} position for {coin.symbol}",
                        cycle_id=cycle_id,
                        symbol=coin.symbol,
                        side=position.side,
                        lane="paper_exploration" if exploration else "strategy",
                        score=signal.score,
                        rating=coin.rating,
                        regime=coin.regime,
                        risk_percent=round(float(trade_settings.risk_percent), 4),
                        entry_price=position.entry_price,
                        requested_volume=entry_execution.get("requested_volume"),
                        filled_volume=position.volume,
                        execution_status=entry_execution.get("status"),
                        stop=position.stop,
                        take=position.take,
                        position_id=position.id,
                        reason=reason,
                        daily_pnl=round(daily_pnl, 4),
                        reserved_stop_risk=reserved_stop_risk,
                        candidate_stop_risk=round(candidate_stop_risk, 4),
                        daily_risk_limit=round(balance * trade_settings.daily_risk_percent / 100, 4),
                        **self._entry_log_context(coin, signal, entry_snapshot),
                    )
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
                    execution_reason = "position size is zero or execution was not filled"
                    if exploration:
                        self._record_paper_learning_decisions(
                            db,
                            coin,
                            signal,
                            allowed=False,
                            reason=execution_reason,
                        )
                    self._log_trading_event(
                        db,
                        "WARNING",
                        "ENTRY_REJECTED",
                        f"Skipped {coin.symbol}: {execution_reason}",
                        cycle_id=cycle_id,
                        symbol=coin.symbol,
                        signal=signal.signal,
                        score=signal.score,
                        lane="paper_exploration" if exploration else "strategy",
                        gate="EXECUTION",
                        risk_percent=round(float(trade_settings.risk_percent), 4),
                        reason=execution_reason,
                        daily_pnl=round(daily_pnl, 4),
                        reserved_stop_risk=reserved_stop_risk,
                        candidate_stop_risk=round(candidate_stop_risk, 4),
                        daily_risk_limit=round(balance * trade_settings.daily_risk_percent / 100, 4),
                        **self._entry_log_context(coin, signal, entry_snapshot),
                    )
                    decisions.append(
                        TradingDecision(symbol=coin.symbol, signal=signal.signal, score=signal.score, action="SKIPPED", reason="position size is zero")
                    )
            else:
                if exploration:
                    self._record_paper_learning_decisions(db, coin, signal, allowed=False, reason=reason)
                self._log_trading_event(
                    db,
                    "INFO",
                    "ENTRY_SKIPPED",
                    f"Skipped {coin.symbol}: {reason}",
                    cycle_id=cycle_id,
                    symbol=coin.symbol,
                    signal=signal.signal,
                    score=signal.score,
                    lane="paper_exploration" if exploration else "strategy",
                    gate=self._decision_gate(reason),
                    risk_percent=round(float(trade_settings.risk_percent), 4),
                    reason=reason,
                    daily_pnl=round(daily_pnl, 4),
                    reserved_stop_risk=reserved_stop_risk,
                    candidate_stop_risk=round(candidate_stop_risk, 4),
                    daily_risk_limit=round(balance * trade_settings.daily_risk_percent / 100, 4),
                    **self._entry_log_context(coin, signal, entry_snapshot),
                )
                decisions.append(
                    TradingDecision(symbol=coin.symbol, signal=signal.signal, score=signal.score, action="SKIPPED", reason=reason)
                )

        await db.commit()
        opened = sum(1 for item in decisions if item.action == "OPENED")
        return TradingRunOut(scanned=len(coins), opened=opened, skipped=len(decisions) - opened, decisions=decisions)

    async def manage_open_positions(self, db: AsyncSession) -> TradingTickOut:
        cycle_id = uuid4().hex[:10]
        positions = list(
            (
                await db.execute(select(Position).where(Position.status == "OPEN").order_by(Position.entered_at.asc()))
            ).scalars().all()
        )
        balance = self._safe_balance(
            (await self.exchange.get_balance()).get("USDT"),
            fallback=float(self.settings.paper_starting_balance),
        )
        if not positions:
            drawdown = await self._enforce_drawdown_limit(db, balance)
            if drawdown.emergency:
                await db.commit()
            return TradingTickOut(checked=0, closed=0, updated=[])

        position_symbols = list(dict.fromkeys(position.symbol for position in positions))
        prices: dict[str, float] = {}
        if self.settings.uses_live_market_data:
            try:
                tickers = await self.exchange.fetch_tickers(position_symbols)
            except Exception as exc:
                tickers = {}
                self._log_trading_event(
                    db,
                    "WARNING",
                    "POSITION_TICKER_SNAPSHOT_FAILED",
                    f"Position ticker snapshot failed: {type(exc).__name__}",
                    cycle_id=cycle_id,
                    symbols=position_symbols,
                    error_type=type(exc).__name__,
                )
            for position in positions:
                price = self._ticker_price(tickers.get(position.symbol, {}), position.side)
                if price <= 0:
                    continue
                prices[position.symbol] = price
        else:
            try:
                market = {coin.symbol: coin for coin in await self.scanner.scan(position_symbols)}
            except Exception as exc:
                market = {}
                self._log_trading_event(
                    db,
                    "WARNING",
                    "POSITION_MARKET_SNAPSHOT_FAILED",
                    f"Position market snapshot failed: {type(exc).__name__}",
                    cycle_id=cycle_id,
                    symbols=position_symbols,
                    error_type=type(exc).__name__,
                )
            prices = {
                symbol: self._finite_float(coin.price)
                for symbol, coin in market.items()
                if self._finite_float(coin.price) > 0
            }
        previous_prices: dict[int, float] = {}
        for position in positions:
            price = prices.get(position.symbol, 0.0)
            if price <= 0:
                self._log_trading_event(
                    db,
                    "ERROR",
                    "POSITION_PRICE_UNAVAILABLE",
                    f"Position {position.symbol} #{position.id} cannot be managed: price unavailable",
                    cycle_id=cycle_id,
                    symbol=position.symbol,
                    position_id=position.id,
                    side=position.side,
                    gate="PRICE_DATA",
                )
                continue
            previous_prices[position.id] = position.current_price
            position.current_price = price
            position.highest_price = max(position.highest_price or position.entry_price, price)
            position.lowest_price = min(position.lowest_price or position.entry_price, price)
            position.pnl = await self._position_total_pnl(db, position, price)
            await self._capture_position_microstructure(position)

        drawdown = await self._enforce_drawdown_limit(db, balance)
        emergency_close = drawdown.emergency
        pending_order_symbols = await self._pending_order_symbols(db)
        updates: list[PositionUpdateOut] = []

        for position in positions:
            price = prices.get(position.symbol, 0.0)
            if price <= 0:
                continue
            if await self._process_filled_protective_stop(db, position, price, cycle_id=cycle_id):
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
                continue
            if position.symbol in pending_order_symbols:
                self._log_trading_event(
                    db,
                    "WARNING",
                    "POSITION_MANAGEMENT_PAUSED",
                    f"Position management paused for {position.symbol} #{position.id}: pending exchange order",
                    cycle_id=cycle_id,
                    symbol=position.symbol,
                    position_id=position.id,
                    side=position.side,
                    gate="EXECUTION",
                )
            elif emergency_close:
                await self._close_position(
                    db,
                    position,
                    price,
                    "EMERGENCY_DRAWDOWN",
                    cycle_id=cycle_id,
                )
            else:
                stop_before_updates = position.stop
                first_partial_volume = await self._apply_partial_take_profit(db, position, price, cycle_id=cycle_id)
                if first_partial_volume:
                    self._log_trading_event(
                        db,
                        "INFO",
                        "POSITION_PARTIALLY_CLOSED",
                        f"Partially closed {position.symbol} #{position.id}: remaining={position.volume:.6f}",
                        cycle_id=cycle_id,
                        symbol=position.symbol,
                        position_id=position.id,
                        side=position.side,
                        current_price=price,
                        remaining_volume=position.volume,
                        pnl=position.pnl,
                    )
                # The stop is protected only after the exchange confirms TP1.
                # A price merely touching its trigger is not an execution event.
                if first_partial_volume and self._apply_breakeven(position):
                    entry_context = position.entry_context if isinstance(position.entry_context, dict) else {}
                    protection = entry_context.get("breakeven_protection", {})
                    self._log_trading_event(
                        db,
                        "INFO",
                        "BREAKEVEN_APPLIED",
                        f"Moved {position.symbol} #{position.id} stop to breakeven: stop={position.stop:.4f}",
                        cycle_id=cycle_id,
                        symbol=position.symbol,
                        position_id=position.id,
                        side=position.side,
                        entry_price=position.entry_price,
                        current_price=price,
                        stop=position.stop,
                        requested_offset_percent=protection.get("requested_offset_percent"),
                        effective_offset_percent=protection.get("effective_offset_percent"),
                    )
                    if self.settings.telegram_trade_reports_enabled:
                        await self.telegram.broadcast(
                            format_protection_update(position, "BREAKEVEN"),
                            photo=await self._position_card(position, event="PROTECTION"),
                            photo_filename=f"position-{position.id}-protection.jpg",
                            photo_caption="<b>Защита позиции обновлена</b>",
                            dedupe_key=f"position:{position.id}:breakeven",
                        )
                second_partial_volume = await self._apply_second_take_profit(
                    db,
                    position,
                    price,
                    cycle_id=cycle_id,
                )
                partial_closed_volume = first_partial_volume or second_partial_volume
                self._apply_trailing_stop(position)
                if first_partial_volume or second_partial_volume or position.stop != stop_before_updates:
                    await self._replace_protective_stop(
                        db,
                        position,
                        stop_price=position.stop,
                        stage="TP1_FILLED" if first_partial_volume else "POSITION_UPDATE",
                        cycle_id=cycle_id,
                    )
                position.pnl = await self._position_total_pnl(db, position, price)
                exit_reason = self._exit_reason(position)
                # A price gap can cross both the 1R partial target and the
                # final target in one snapshot. Do not send two exit orders
                # in that cycle; let the next fresh ticker decide whether the
                # remaining position should advance to a dynamic target.
                if exit_reason == "TAKE_PROFIT" and not partial_closed_volume:
                    take_outcome = await self._handle_dynamic_take_profit(
                        db,
                        position,
                        price,
                        cycle_id=cycle_id,
                    )
                    if take_outcome == "CLOSE":
                        await self._close_position(db, position, price, exit_reason, cycle_id=cycle_id)
                    elif take_outcome == "EXTENDED":
                        await self._replace_protective_stop(
                            db,
                            position,
                            stop_price=position.stop,
                            stage="DYNAMIC_TAKE",
                            cycle_id=cycle_id,
                        )
                elif exit_reason:
                    if self._defer_unconfirmed_breakeven_exit(position, exit_reason):
                        self._log_trading_event(
                            db,
                            "WARNING",
                            "BREAKEVEN_STOP_CONFIRMATION_PENDING",
                            f"Breakeven stop replacement is unconfirmed for {position.symbol} #{position.id}",
                            cycle_id=cycle_id,
                            symbol=position.symbol,
                            position_id=position.id,
                            exit_reason=exit_reason,
                            deadline=self._protection_state(position).get("deadline"),
                        )
                    else:
                        await self._close_position(db, position, price, exit_reason, cycle_id=cycle_id)
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

    def _log_trading_event(
        self,
        db: AsyncSession,
        level: str,
        event: str,
        message: str,
        **details: object,
    ) -> None:
        """Persist a readable trading log plus fields suitable for filtering and export."""
        context = {"event": event}
        context.update({key: value for key, value in details.items() if value is not None})
        db.add(LogEntry(level=level, message=message, context=context))

    def _decision_gate(self, reason: str) -> str:
        normalized = (reason or "").lower()
        rules = (
            ("symbol performance guard", "SYMBOL_GUARD"),
            ("symbol recovery probe", "SYMBOL_GUARD"),
            ("performance guard", "PERFORMANCE_GUARD"),
            ("daily risk reserve", "DAILY_RISK_BUDGET"),
            ("pre-trade quality", "PRETRADE_QUALITY"),
            ("micro gate", "MICROSTRUCTURE"),
            ("microstructure", "MICROSTRUCTURE"),
            ("unresolved exchange order", "EXECUTION"),
            ("market quality", "MARKET_QUALITY"),
            ("committee rejected", "COMMITTEE"),
            ("paper learning", "PAPER_LEARNING"),
            ("learning", "LEARNING_MEMORY"),
            ("rl_", "RL_GATE"),
            ("rl ", "RL_GATE"),
            ("cooldown", "COOLDOWN"),
            ("direction", "DIRECTIONAL_EXPOSURE"),
            ("exposure", "EXPOSURE"),
            ("maximum open positions", "MAX_POSITIONS"),
            ("position already open", "POSITION_ALREADY_OPEN"),
            ("volume", "VOLUME_CONFIRMATION"),
            ("extended", "PRICE_EXTENSION"),
            ("market regime", "MARKET_REGIME"),
            ("volatility", "VOLATILITY"),
            ("strategy wait", "STRATEGY_WAIT"),
            ("risk", "RISK_MANAGER"),
        )
        return next((gate for marker, gate in rules if marker in normalized), "ENTRY_RULES")

    def _entry_log_context(
        self,
        coin: MarketCoin,
        signal: StrategySignal,
        microstructure: dict | None,
    ) -> dict[str, object]:
        """Keep the inputs behind an entry decision alongside the readable reason."""
        result: dict[str, object] = {
            "signal_reasons": signal.reasons[:4],
            "market": {
                "price": round(float(coin.price), 8),
                "rating": int(coin.rating),
                "regime": coin.regime,
                "regime_score": int(coin.regime_score),
                "rsi": round(float(coin.rsi), 4),
                "atr": round(float(coin.atr), 8),
                "spread_bps": round(float(coin.spread_bps), 4),
                "price_change_percent": round(float(coin.price_change_percent), 4),
                "volume_24h": round(float(coin.volume_24h), 2),
                "volume_average_24h": round(float(coin.volume_average_24h), 2),
            },
        }
        if not isinstance(microstructure, dict) or not microstructure:
            return result
        fields = (
            "status",
            "available_sources",
            "latency_ms",
            "spread_bps",
            "order_book_imbalance",
            "trade_flow_imbalance",
            "price_change_10m_percent",
            "price_change_30m_percent",
            "realized_volatility_1m_percent",
            "errors",
        )
        result["microstructure"] = {
            field: microstructure[field]
            for field in fields
            if field in microstructure and microstructure[field] is not None
        }
        return result

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
        if entry_order.status not in {OrderStatus.FILLED.value, OrderStatus.PARTIAL.value} or not entry_order.average_price:
            return None
        entry_price = entry_order.average_price
        volume = entry_order.filled_amount or entry_order.requested_amount
        if volume <= 0:
            return None
        stop, take, initial_risk = self._exit_plan(entry_price, coin.atr, side, settings)
        entry_context = self.learning.entry_context(coin, signal, signal_reasons)
        entry_context["exit_plan"] = {
            "entry_atr": round(float(coin.atr or 0.0), 8),
            "atr_stop_multiplier": round(float(settings.atr_stop_multiplier), 4),
            "initial_risk": round(float(initial_risk), 8),
        }
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
            "requested_volume": round(float(entry_order.requested_amount or 0.0), 8),
            "volume": round(float(volume), 8),
            "average_price": round(float(entry_price), 8),
            "status": entry_order.status,
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
        entry_context["scale_out"] = self._initial_scale_out_plan(
            side=side,
            entry_price=entry_price,
            strategy_take=take,
            entry_volume=volume,
            entry_fee=entry_order.fee,
            tp1_percent=settings.partial_close_percent,
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
        await self._replace_protective_stop(
            db,
            position,
            stop_price=stop,
            stage="INITIAL",
            cycle_id=None,
        )
        return position

    def _opportunity_rank(
        self,
        coin: MarketCoin,
        signal: StrategySignal,
        bnb_priority: BnbPriorityDecision | None = None,
    ) -> tuple[int, int, int, int, int]:
        market_score = self.event_priority.market_score(coin)[0]
        event_score = bnb_priority.assessment.event_score if bnb_priority else 0
        final_score = bnb_priority.final_score if bnb_priority else int(signal.score) + market_score
        return (
            1 if signal.signal in {"BUY", "SELL"} else 0,
            final_score,
            int(signal.score),
            int(coin.regime_score),
            event_score,
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

    def _daily_risk_gate(
        self,
        coin: MarketCoin,
        signal: str,
        balance: float,
        settings: RiskSettings,
        daily_pnl: float,
        reserved_open_stop_risk: float,
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
        candidate_stop_risk = round(abs(coin.price - stop) * volume, 4)
        if candidate_stop_risk <= 0:
            return False, "candidate stop risk is zero", 0.0
        if not getattr(self.settings, "daily_risk_reserve_enabled", True):
            return True, "daily risk reserve disabled", candidate_stop_risk
        allowed, reason = self.risk.can_add_daily_risk_reserve(
            balance=balance,
            daily_risk_percent=settings.daily_risk_percent,
            daily_pnl=daily_pnl,
            reserved_open_stop_risk=reserved_open_stop_risk,
            candidate_stop_risk=candidate_stop_risk,
        )
        return allowed, reason, candidate_stop_risk

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

    async def _committee_gate(
        self,
        db: AsyncSession,
        coin: MarketCoin,
        signal: str,
        *,
        cycle_id: str | None = None,
    ) -> AgentAnalysisOut | None:
        if not self.settings.ai_committee_enabled or signal not in {"BUY", "SELL"}:
            return None
        analysis = await self.agents.analyze_coin(db, coin)
        self._log_trading_event(
            db,
            "INFO",
            "COMMITTEE_DECISION",
            (
                f"AI committee {coin.symbol}: final={analysis.final_action}, "
                f"consensus={analysis.consensus_score:.2f}, confidence={analysis.final_confidence:.2f}"
            ),
            cycle_id=cycle_id,
            symbol=coin.symbol,
            requested_signal=signal,
            final_action=analysis.final_action,
            approved=analysis.approved,
            consensus=round(float(analysis.consensus_score), 4),
            confidence=round(float(analysis.final_confidence), 4),
        )
        return analysis

    def _committee_allows_signal(self, analysis: AgentAnalysisOut, signal: str) -> bool:
        return (
            analysis.approved
            and analysis.final_action == signal
            and analysis.consensus_score >= self.settings.ai_committee_min_consensus
        )

    async def _apply_partial_take_profit(
        self,
        db: AsyncSession,
        position: Position,
        price: float,
        *,
        cycle_id: str | None = None,
    ) -> float:
        if position.partial_taken or not self._partial_take_profit_reached(position, price):
            return 0.0
        close_volume = self._partial_close_volume(position)
        if close_volume <= 0 or close_volume >= position.volume:
            return 0.0
        eligible = await self.execution.assess_exit_size(position.symbol, close_volume, price)
        if not eligible.allowed:
            self._cancel_scale_out_for_minimum(
                db,
                position,
                stage="TP1",
                requested_volume=close_volume,
                assessment=eligible,
                cycle_id=cycle_id,
            )
            return 0.0
        exit_order = await self.execution.execute_market(
            db,
            position.symbol,
            "sell" if position.side == "LONG" else "buy",
            close_volume,
            price,
            "PARTIAL_TAKE_PROFIT",
        )
        if (
            exit_order.status not in {OrderStatus.FILLED.value, OrderStatus.PARTIAL.value}
            or not exit_order.average_price
        ):
            self._log_trading_event(
                db,
                "ERROR",
                "PARTIAL_TAKE_PROFIT_FAILED",
                f"Failed partial take profit for {position.symbol} #{position.id}",
                cycle_id=cycle_id,
                symbol=position.symbol,
                position_id=position.id,
                side=position.side,
                requested_volume=close_volume,
                requested_price=price,
                order_status=exit_order.status,
                filled_volume=exit_order.filled_amount,
            )
            return 0.0
        closed_volume, partial_profit = await self._record_partial_exit(
            db,
            position,
            exit_price=exit_order.average_price,
            exit_fee=exit_order.fee,
            exit_volume=exit_order.filled_amount,
            mark_price=price,
        )
        if closed_volume <= 0:
            self._log_trading_event(
                db,
                "ERROR",
                "PARTIAL_TAKE_PROFIT_FAILED",
                f"Failed partial take profit for {position.symbol} #{position.id}: no executed volume",
                cycle_id=cycle_id,
                symbol=position.symbol,
                position_id=position.id,
                side=position.side,
                requested_volume=close_volume,
                requested_price=price,
                order_status=exit_order.status,
                filled_volume=exit_order.filled_amount,
            )
            return 0.0
        position.partial_taken = True
        self._mark_scale_out_taken(position, "tp1", exit_order.average_price, closed_volume)
        if self.settings.telegram_trade_reports_enabled:
            await self.telegram.broadcast(
                format_partial_take_profit(
                    position,
                    closed_volume=closed_volume,
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
        return closed_volume

    async def _apply_second_take_profit(
        self,
        db: AsyncSession,
        position: Position,
        price: float,
        *,
        cycle_id: str | None = None,
    ) -> float:
        """Execute TP2 only after a filled TP1 and a confirmed scale-out plan."""
        scale_out = self._scale_out_state(position)
        if (
            not position.partial_taken
            or scale_out.get("mode") != "CASCADE"
            or bool(scale_out.get("tp2_taken"))
            or not self._second_take_profit_reached(position, price)
        ):
            return 0.0
        close_volume = self._second_close_volume(position)
        if close_volume <= 0 or close_volume >= position.volume:
            return 0.0
        eligible = await self.execution.assess_exit_size(position.symbol, close_volume, price)
        if not eligible.allowed:
            self._cancel_scale_out_for_minimum(
                db,
                position,
                stage="TP2",
                requested_volume=close_volume,
                assessment=eligible,
                cycle_id=cycle_id,
            )
            return 0.0
        exit_order = await self.execution.execute_market(
            db,
            position.symbol,
            "sell" if position.side == "LONG" else "buy",
            close_volume,
            price,
            "PARTIAL_TAKE_PROFIT_2",
        )
        if exit_order.status not in {OrderStatus.FILLED.value, OrderStatus.PARTIAL.value} or not exit_order.average_price:
            self._log_trading_event(
                db,
                "ERROR",
                "SECOND_TAKE_PROFIT_FAILED",
                f"Failed TP2 for {position.symbol} #{position.id}",
                cycle_id=cycle_id,
                symbol=position.symbol,
                position_id=position.id,
                requested_volume=close_volume,
                requested_price=price,
                order_status=exit_order.status,
                filled_volume=exit_order.filled_amount,
            )
            return 0.0
        closed_volume, partial_profit = await self._record_partial_exit(
            db,
            position,
            exit_price=exit_order.average_price,
            exit_fee=exit_order.fee,
            exit_volume=exit_order.filled_amount,
            mark_price=price,
        )
        if closed_volume <= 0:
            return 0.0
        self._mark_scale_out_taken(position, "tp2", exit_order.average_price, closed_volume)
        self._log_trading_event(
            db,
            "INFO",
            "SECOND_TAKE_PROFIT_FILLED",
            f"TP2 filled for {position.symbol} #{position.id}",
            cycle_id=cycle_id,
            symbol=position.symbol,
            position_id=position.id,
            side=position.side,
            exit_price=exit_order.average_price,
            requested_volume=close_volume,
            filled_volume=closed_volume,
            remaining_volume=position.volume,
            partial_profit=partial_profit,
            pnl=position.pnl,
        )
        if self.settings.telegram_trade_reports_enabled:
            await self.telegram.broadcast(
                format_partial_take_profit(
                    position,
                    closed_volume=closed_volume,
                    exit_price=exit_order.average_price,
                    profit=partial_profit,
                ),
                photo=await self._position_card(position, event="PARTIAL", exit_price=exit_order.average_price),
                photo_filename=f"position-{position.id}-tp2.jpg",
                photo_caption="<b>Вторая фиксация прибыли</b>",
                dedupe_key=f"position:{position.id}:tp2",
            )
        return closed_volume

    def _partial_take_profit_reached(self, position: Position, price: float) -> bool:
        trigger = self._scale_out_price(position, "tp1_price", self._break_even_price(position))
        if position.side == "LONG":
            return price >= trigger
        return price <= trigger

    def _partial_close_volume(self, position: Position) -> float:
        close_percent = min(max(position.partial_close_percent or 0.0, 10.0), 30.0)
        return round(position.volume * close_percent / 100, 8)

    def _second_take_profit_reached(self, position: Position, price: float) -> bool:
        trigger = self._scale_out_price(position, "tp2_price", position.take)
        return price >= trigger if position.side == "LONG" else price <= trigger

    def _second_close_volume(self, position: Position) -> float:
        scale_out = self._scale_out_state(position)
        initial_volume = self._finite_float(scale_out.get("entry_volume"))
        percent = min(max(self._finite_float(scale_out.get("tp2_percent")), 10.0), 40.0)
        requested = initial_volume * percent / 100 if initial_volume > 0 else position.volume * percent / 100
        return round(min(requested, max(position.volume * 0.999999, 0.0)), 8)

    async def _handle_dynamic_take_profit(
        self,
        db: AsyncSession,
        position: Position,
        price: float,
        *,
        cycle_id: str | None = None,
    ) -> str:
        """Advance a winner one protected target at a time, or request its final close.

        The outcome is deliberately tri-state: a submitted but unresolved partial
        order must not be followed by a second full-close order in the same cycle.
        """
        if not getattr(self.settings, "dynamic_take_profit_enabled", True):
            return "CLOSE"
        state = self._dynamic_take_state(position)
        extensions = max(int(self._finite_float(state.get("extensions", 0))), 0)
        maximum = max(int(self._finite_float(getattr(self.settings, "dynamic_take_profit_max_extensions", 2))), 0)
        if extensions >= maximum:
            return "CLOSE"
        if self._scale_out_state(position).get("mode") == "MONOLITHIC":
            return "CLOSE"

        requested_volume = self._dynamic_take_close_volume(position)
        if requested_volume <= 0 or requested_volume >= position.volume:
            return "CLOSE"
        eligible = await self.execution.assess_exit_size(position.symbol, requested_volume, price)
        if not eligible.allowed:
            self._cancel_scale_out_for_minimum(
                db,
                position,
                stage="DYNAMIC_TP",
                requested_volume=requested_volume,
                assessment=eligible,
                cycle_id=cycle_id,
            )
            return "CLOSE"
        prior_take = position.take
        exit_order = await self.execution.execute_market(
            db,
            position.symbol,
            "sell" if position.side == "LONG" else "buy",
            requested_volume,
            price,
            "EXIT_DYNAMIC_TAKE_PROFIT",
        )
        if (
            exit_order.status not in {OrderStatus.FILLED.value, OrderStatus.PARTIAL.value}
            or not exit_order.average_price
        ):
            self._log_trading_event(
                db,
                "ERROR",
                "DYNAMIC_TAKE_PROFIT_FAILED",
                f"Dynamic take profit failed for {position.symbol} #{position.id}",
                cycle_id=cycle_id,
                symbol=position.symbol,
                position_id=position.id,
                side=position.side,
                requested_volume=requested_volume,
                requested_price=price,
                order_status=exit_order.status,
                filled_volume=exit_order.filled_amount,
            )
            return "PENDING"

        closed_volume, partial_profit = await self._record_partial_exit(
            db,
            position,
            exit_price=exit_order.average_price,
            exit_fee=exit_order.fee,
            exit_volume=exit_order.filled_amount,
            mark_price=price,
        )
        if closed_volume <= 0:
            self._log_trading_event(
                db,
                "ERROR",
                "DYNAMIC_TAKE_PROFIT_FAILED",
                f"Dynamic take profit failed for {position.symbol} #{position.id}: no executed volume",
                cycle_id=cycle_id,
                symbol=position.symbol,
                position_id=position.id,
                side=position.side,
                requested_volume=requested_volume,
                requested_price=price,
                order_status=exit_order.status,
                filled_volume=exit_order.filled_amount,
            )
            return "PENDING"

        next_stop, next_take, extension_distance = self._dynamic_take_levels(position, exit_order.average_price)
        position.stop = next_stop
        position.take = next_take
        context = dict(position.entry_context or {})
        context["dynamic_take_profit"] = {
            "extensions": extensions + 1,
            "prior_take": round(float(prior_take), 8),
            "last_exit_price": round(float(exit_order.average_price), 8),
            "next_take": round(float(next_take), 8),
            "locked_stop": round(float(next_stop), 8),
            "extension_distance": round(float(extension_distance), 8),
            "closed_volume": round(float(closed_volume), 8),
        }
        position.entry_context = context
        self._log_trading_event(
            db,
            "INFO",
            "DYNAMIC_TAKE_PROFIT_EXTENDED",
            (
                f"Dynamic take profit extended for {position.symbol} #{position.id}: "
                f"next={position.take:.8f}, stop={position.stop:.8f}"
            ),
            cycle_id=cycle_id,
            symbol=position.symbol,
            position_id=position.id,
            side=position.side,
            previous_take=prior_take,
            next_take=position.take,
            locked_stop=position.stop,
            extension_distance=extension_distance,
            requested_volume=requested_volume,
            filled_volume=closed_volume,
            remaining_volume=position.volume,
            exit_price=exit_order.average_price,
            exit_fee=exit_order.fee,
            partial_profit=partial_profit,
            pnl=position.pnl,
        )
        if self.settings.telegram_trade_reports_enabled:
            await self.telegram.broadcast(
                format_partial_take_profit(
                    position,
                    closed_volume=closed_volume,
                    exit_price=exit_order.average_price,
                    profit=partial_profit,
                    next_take=position.take,
                    locked_stop=position.stop,
                ),
                photo=await self._position_card(
                    position,
                    event="PARTIAL",
                    exit_price=exit_order.average_price,
                ),
                photo_filename=f"position-{position.id}-dynamic-take.jpg",
                photo_caption="<b>Динамическая фиксация прибыли</b>",
                dedupe_key=f"position:{position.id}:dynamic-take:{extensions + 1}",
            )
        return "EXTENDED"

    def _dynamic_take_state(self, position: Position) -> dict:
        context = position.entry_context if isinstance(position.entry_context, dict) else {}
        state = context.get("dynamic_take_profit")
        return dict(state) if isinstance(state, dict) else {}

    def _dynamic_take_close_volume(self, position: Position) -> float:
        close_percent = min(
            max(float(getattr(self.settings, "dynamic_take_profit_partial_close_percent", 35.0)), 1.0),
            90.0,
        )
        return round(position.volume * close_percent / 100, 8)

    def _dynamic_take_levels(self, position: Position, exit_price: float) -> tuple[float, float, float]:
        """Return a tighter stop and ATR-sized next target without widening risk."""
        distance = self._dynamic_take_extension_distance(position)
        if position.side == "LONG":
            return (
                round(max(position.stop, exit_price - distance), 8),
                round(exit_price + distance, 8),
                distance,
            )
        return (
            round(min(position.stop, exit_price + distance), 8),
            round(max(exit_price - distance, position.entry_price * 0.0001), 8),
            distance,
        )

    def _dynamic_take_extension_distance(self, position: Position) -> float:
        context = position.entry_context if isinstance(position.entry_context, dict) else {}
        exit_plan = context.get("exit_plan") if isinstance(context.get("exit_plan"), dict) else {}
        atr = self._finite_float(exit_plan.get("entry_atr"))
        if atr <= 0:
            atr_multiplier = self._finite_float(exit_plan.get("atr_stop_multiplier")) or 1.5
            atr_multiplier = max(atr_multiplier, 0.1)
            atr = max(self._finite_float(position.initial_risk), 0.0) / atr_multiplier
        multiplier = max(self._finite_float(getattr(self.settings, "dynamic_take_profit_extension_atr", 1.5)), 0.1)
        return round(max(atr * multiplier, position.entry_price * 0.0001), 8)

    async def _record_partial_exit(
        self,
        db: AsyncSession,
        position: Position,
        *,
        exit_price: float,
        exit_fee: float,
        exit_volume: float,
        mark_price: float,
    ) -> tuple[float, float]:
        """Record only the quantity confirmed by the exchange as exited."""
        closed_volume = self._executed_volume(exit_volume, position.volume)
        if closed_volume <= 0:
            return 0.0, 0.0
        partial_profit = self._profit_for_volume(position, exit_price, closed_volume) - exit_fee
        position.volume = round(max(position.volume - closed_volume, 0.0), 8)
        db.add(
            Trade(
                position_id=position.id,
                symbol=position.symbol,
                side=position.side,
                entry_price=position.entry_price,
                exit_price=exit_price,
                profit=round(partial_profit, 4),
            )
        )
        position.pnl = await self._position_total_pnl(db, position, mark_price)
        return closed_volume, round(partial_profit, 4)

    async def _close_position(
        self,
        db: AsyncSession,
        position: Position,
        exit_price: float,
        reason: str,
        *,
        cycle_id: str | None = None,
        exit_order: Order | None = None,
    ) -> None:
        requested_volume = position.volume
        if exit_order is None:
            await self._cancel_active_protective_stop(db, position, cycle_id=cycle_id)
            exit_order = await self.execution.execute_market(
                db,
                position.symbol,
                "sell" if position.side == "LONG" else "buy",
                requested_volume,
                exit_price,
                f"EXIT_{reason}",
            )
        if (
            exit_order.status not in {OrderStatus.FILLED.value, OrderStatus.PARTIAL.value}
            or not exit_order.average_price
        ):
            self._log_trading_event(
                db,
                "ERROR",
                "POSITION_CLOSE_FAILED",
                f"Failed to close {position.symbol} #{position.id}: {reason}",
                cycle_id=cycle_id,
                symbol=position.symbol,
                position_id=position.id,
                side=position.side,
                exit_reason=reason,
                requested_volume=requested_volume,
                requested_price=exit_price,
                order_status=exit_order.status,
                filled_volume=exit_order.filled_amount,
            )
            return
        closed_volume = self._executed_volume(exit_order.filled_amount, requested_volume)
        if closed_volume <= 0:
            self._log_trading_event(
                db,
                "ERROR",
                "POSITION_CLOSE_FAILED",
                f"Failed to close {position.symbol} #{position.id}: {reason}, no executed volume",
                cycle_id=cycle_id,
                symbol=position.symbol,
                position_id=position.id,
                side=position.side,
                exit_reason=reason,
                requested_volume=requested_volume,
                requested_price=exit_price,
                order_status=exit_order.status,
                filled_volume=exit_order.filled_amount,
            )
            return
        if closed_volume < requested_volume * 0.999999:
            closed_volume, partial_profit = await self._record_partial_exit(
                db,
                position,
                exit_price=exit_order.average_price,
                exit_fee=exit_order.fee,
                exit_volume=closed_volume,
                mark_price=exit_price,
            )
            self._log_trading_event(
                db,
                "WARNING",
                "POSITION_EXIT_PARTIALLY_FILLED",
                f"Partially closed {position.symbol} #{position.id}: {reason}, remaining={position.volume:.6f}",
                cycle_id=cycle_id,
                symbol=position.symbol,
                position_id=position.id,
                side=position.side,
                exit_reason=reason,
                requested_volume=requested_volume,
                filled_volume=closed_volume,
                remaining_volume=position.volume,
                exit_price=exit_order.average_price,
                exit_fee=exit_order.fee,
                partial_profit=partial_profit,
                pnl=position.pnl,
            )
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
        if reason in {"STOP_LOSS", "BREAKEVEN_STOP", "EMERGENCY_DRAWDOWN"}:
            self._record_stop_slippage_observation(
                db,
                position,
                expected_price=exit_price,
                actual_price=exit_order.average_price,
                exit_fee=exit_order.fee,
                exit_slippage=exit_order.slippage,
                reason=reason,
                cycle_id=cycle_id,
            )
        try:
            post_mortem = await self.post_mortem.analyze_loss(db, position, exit_order, reason)
            if post_mortem:
                self._log_trading_event(
                    db,
                    "WARNING",
                    "POST_MORTEM_CREATED",
                    (
                        f"Post-mortem {position.symbol} #{position.id}: "
                        f"label={post_mortem.primary_label}, reward={post_mortem.shaped_reward:+.2f}, "
                        f"priority={post_mortem.priority:.2f}"
                    ),
                    cycle_id=cycle_id,
                    symbol=position.symbol,
                    position_id=position.id,
                    exit_reason=reason,
                    primary_label=post_mortem.primary_label,
                    shaped_reward=round(float(post_mortem.shaped_reward), 4),
                    priority=round(float(post_mortem.priority), 4),
                )
        except Exception as exc:
            self._log_trading_event(
                db,
                "ERROR",
                "POST_MORTEM_FAILED",
                f"Post-mortem failed for {position.symbol} #{position.id}: {type(exc).__name__}",
                cycle_id=cycle_id,
                symbol=position.symbol,
                position_id=position.id,
                exit_reason=reason,
                error_type=type(exc).__name__,
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
            self._log_trading_event(
                db,
                "ERROR",
                "TRADE_MEMORY_FAILED",
                f"SQLite trade memory failed for {position.symbol} #{position.id}: {exc}",
                cycle_id=cycle_id,
                symbol=position.symbol,
                position_id=position.id,
                error_type=type(exc).__name__,
            )
        entry_context = position.entry_context if isinstance(position.entry_context, dict) else {}
        entry_execution = entry_context.get("entry_execution", {})
        self._log_trading_event(
            db,
            "INFO",
            "POSITION_CLOSED",
            f"Closed {position.symbol} #{position.id}: {reason}, pnl={position.pnl:.2f}",
            cycle_id=cycle_id,
            symbol=position.symbol,
            position_id=position.id,
            side=position.side,
            exit_reason=reason,
            pnl=position.pnl,
            entry_price=position.entry_price,
            exit_price=exit_order.average_price,
            volume=position.volume,
            entry_fee=entry_execution.get("fee") if isinstance(entry_execution, dict) else None,
            exit_fee=exit_order.fee,
            exit_slippage=exit_order.slippage,
        )
        self._log_trading_event(
            db,
            "INFO",
            "LEARNING_UPDATED",
            f"Learning updated from {position.symbol} #{position.id}: reason={reason}, pnl={position.pnl:.2f}",
            cycle_id=cycle_id,
            symbol=position.symbol,
            position_id=position.id,
            exit_reason=reason,
            pnl=position.pnl,
        )
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
        # TP1's confirmed fill is the only event that may arm this transition.
        # It prevents an unfilled/failed partial order from being treated as
        # profit that can protect the remaining position.
        if not position.partial_taken:
            return False
        requested_offset = max(float(position.breakeven_offset_percent or 0.0), 0.0)
        effective_offset = self._breakeven_offset_percent(position, requested_offset)
        price_bu = self._break_even_price(position)
        if price_bu <= 0:
            return False
        if position.side == "LONG":
            position.stop = max(position.stop, price_bu)
        else:
            position.stop = min(position.stop, price_bu)
        position.breakeven_applied = True
        self._record_breakeven_protection(position, requested_offset, effective_offset, price_bu)
        return True

    def _breakeven_offset_percent(self, position: Position, requested_offset: float) -> float:
        """Return a stop offset that covers known entry costs and expected exit friction."""
        entry_price = max(self._finite_float(position.entry_price), 0.0)
        volume = max(self._entry_volume(position), 0.0)
        if entry_price <= 0 or volume <= 0:
            return round(requested_offset, 4)

        context = position.entry_context if isinstance(position.entry_context, dict) else {}
        execution = context.get("entry_execution") if isinstance(context.get("entry_execution"), dict) else {}
        entry_fee = max(self._finite_float(execution.get("fee")), 0.0)
        return round(self._break_even_cost_rate(entry_price, volume, entry_fee) * 100, 4)

    def _record_breakeven_protection(
        self,
        position: Position,
        requested_offset: float,
        effective_offset: float,
        price_bu: float,
    ) -> None:
        context = dict(position.entry_context or {})
        context["breakeven_protection"] = {
            "requested_offset_percent": round(requested_offset, 4),
            "effective_offset_percent": round(effective_offset, 4),
            "price_bu": round(price_bu, 8),
            "trigger": "TP1_FILLED",
        }
        position.entry_context = context

    def _protection_state(self, position: Position) -> dict:
        context = position.entry_context if isinstance(position.entry_context, dict) else {}
        state = context.get("protective_stop")
        return dict(state) if isinstance(state, dict) else {}

    def _set_protection_state(self, position: Position, state: dict) -> None:
        context = dict(position.entry_context or {})
        context["protective_stop"] = state
        position.entry_context = context

    async def _active_protective_stop(self, db: AsyncSession, position: Position) -> Order | None:
        orders = (
            await db.execute(
                select(Order)
                .where(
                    Order.symbol == position.symbol,
                    Order.status.in_([OrderStatus.NEW.value, OrderStatus.PARTIAL.value, OrderStatus.FILLED.value]),
                )
                .order_by(Order.created_at.desc(), Order.id.desc())
            )
        ).scalars().all()
        for order in orders:
            raw = order.raw if isinstance(order.raw, dict) else {}
            if raw.get("order_role") != "PROTECTIVE_STOP":
                continue
            try:
                belongs_to_position = int(raw.get("position_id")) == int(position.id)
            except (TypeError, ValueError):
                belongs_to_position = False
            if belongs_to_position:
                return order
        return None

    async def _replace_protective_stop(
        self,
        db: AsyncSession,
        position: Position,
        *,
        stop_price: float,
        stage: str,
        cycle_id: str | None,
    ) -> bool:
        """Cancel the prior stop and acknowledge a new one for remaining size.

        The method is intentionally no-op-safe for paper and spot mode: the
        local price monitor remains authoritative there because a non-
        reduce-only spot stop could touch unrelated wallet inventory.
        """
        if not self.execution.supports_native_protective_stops():
            self._set_protection_state(
                position,
                {
                    "mode": "LOCAL_MONITOR",
                    "stage": stage,
                    "stop_price": round(float(stop_price), 8),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "reason": "native_reduce_only_stop_unavailable_for_configured_market",
                },
            )
            return False

        prior = await self._active_protective_stop(db, position)
        if prior and prior.status == OrderStatus.NEW.value:
            if not await self.execution.cancel_protective_stop(prior):
                self._arm_unconfirmed_protection(position, stage, stop_price, prior.id)
                self._log_trading_event(
                    db,
                    "ERROR",
                    "PROTECTIVE_STOP_CANCEL_FAILED",
                    f"Could not cancel previous protective stop for {position.symbol} #{position.id}",
                    cycle_id=cycle_id,
                    symbol=position.symbol,
                    position_id=position.id,
                    prior_order_id=prior.id,
                    stop_price=stop_price,
                )
                return False
            self._log_trading_event(
                db,
                "INFO",
                "PROTECTIVE_STOP_CANCELLED",
                f"Cancelled previous protective stop for {position.symbol} #{position.id}",
                cycle_id=cycle_id,
                symbol=position.symbol,
                position_id=position.id,
                prior_order_id=prior.id,
                stage=stage,
            )

        order = await self.execution.place_protective_stop(
            db,
            position_id=position.id,
            symbol=position.symbol,
            side="sell" if position.side == "LONG" else "buy",
            amount=position.volume,
            stop_price=stop_price,
            stage=stage,
        )
        if order.status == OrderStatus.NEW.value and order.exchange_order_id:
            self._set_protection_state(
                position,
                {
                    "mode": "NATIVE_ACTIVE",
                    "stage": stage,
                    "order_id": order.id,
                    "exchange_order_id": order.exchange_order_id,
                    "stop_price": round(float(stop_price), 8),
                    "remaining_volume": round(float(position.volume), 8),
                    "confirmed_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            self._log_trading_event(
                db,
                "INFO",
                "PROTECTIVE_STOP_CONFIRMED",
                f"Protective stop confirmed for {position.symbol} #{position.id}",
                cycle_id=cycle_id,
                symbol=position.symbol,
                position_id=position.id,
                order_id=order.id,
                exchange_order_id=order.exchange_order_id,
                stage=stage,
                stop_price=stop_price,
                remaining_volume=position.volume,
            )
            return True

        self._arm_unconfirmed_protection(position, stage, stop_price, order.id)
        self._log_trading_event(
            db,
            "ERROR",
            "PROTECTIVE_STOP_UNCONFIRMED",
            f"Protective stop was not acknowledged for {position.symbol} #{position.id}",
            cycle_id=cycle_id,
            symbol=position.symbol,
            position_id=position.id,
            order_id=order.id,
            stage=stage,
            stop_price=stop_price,
            error=(order.raw or {}).get("error") if isinstance(order.raw, dict) else None,
        )
        return False

    def _arm_unconfirmed_protection(
        self,
        position: Position,
        stage: str,
        stop_price: float,
        order_id: int | None,
    ) -> None:
        timeout_seconds = max(int(getattr(self.settings, "protective_stop_replace_timeout_seconds", 5)), 1)
        deadline = datetime.now(timezone.utc) + timedelta(seconds=timeout_seconds)
        self._set_protection_state(
            position,
            {
                "mode": "LOCAL_FALLBACK_PENDING",
                "stage": stage,
                "order_id": order_id,
                "stop_price": round(float(stop_price), 8),
                "deadline": deadline.isoformat(),
                "timeout_seconds": timeout_seconds,
            },
        )

    def _defer_unconfirmed_breakeven_exit(self, position: Position, reason: str) -> bool:
        if reason != "BREAKEVEN_STOP":
            return False
        state = self._protection_state(position)
        if state.get("mode") != "LOCAL_FALLBACK_PENDING":
            return False
        try:
            deadline = datetime.fromisoformat(str(state.get("deadline")))
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return False
        return datetime.now(timezone.utc) < deadline

    async def _process_filled_protective_stop(
        self,
        db: AsyncSession,
        position: Position,
        price: float,
        *,
        cycle_id: str | None,
    ) -> bool:
        """Use an exchange-confirmed stop fill; never submit a duplicate exit."""
        order = await self._active_protective_stop(db, position)
        if not order:
            return False
        raw = order.raw if isinstance(order.raw, dict) else {}
        if raw.get("position_fill_consumed"):
            return False
        if order.status == OrderStatus.NEW.value:
            await self.execution.refresh_order(order)
        if order.status not in {OrderStatus.FILLED.value, OrderStatus.PARTIAL.value} or not order.average_price:
            return False
        raw = dict(order.raw or {})
        raw["position_fill_consumed"] = True
        order.raw = raw
        await self._close_position(
            db,
            position,
            exit_price=price,
            reason="BREAKEVEN_STOP" if position.breakeven_applied else "STOP_LOSS",
            cycle_id=cycle_id,
            exit_order=order,
        )
        if position.status == "OPEN":
            # A terminal partial stop fill leaves a smaller position.  Replace
            # it with a fresh reduce-only stop for the residual volume.
            await self._replace_protective_stop(
                db,
                position,
                stop_price=position.stop,
                stage="STOP_PARTIAL_FILL",
                cycle_id=cycle_id,
            )
        return True

    async def _cancel_active_protective_stop(
        self,
        db: AsyncSession,
        position: Position,
        *,
        cycle_id: str | None,
    ) -> None:
        order = await self._active_protective_stop(db, position)
        if not order or order.status != OrderStatus.NEW.value:
            return
        if await self.execution.cancel_protective_stop(order):
            self._log_trading_event(
                db,
                "INFO",
                "PROTECTIVE_STOP_CANCELLED_FOR_EXIT",
                f"Cancelled protective stop before final exit for {position.symbol} #{position.id}",
                cycle_id=cycle_id,
                symbol=position.symbol,
                position_id=position.id,
                order_id=order.id,
            )
        else:
            # All native stops created by this engine are reduce-only.  A
            # failed cancellation therefore cannot reverse a final close, but
            # it must remain visible for reconciliation and audit.
            self._log_trading_event(
                db,
                "WARNING",
                "PROTECTIVE_STOP_CANCEL_BEFORE_EXIT_FAILED",
                f"Protective stop cancellation failed before final exit for {position.symbol} #{position.id}",
                cycle_id=cycle_id,
                symbol=position.symbol,
                position_id=position.id,
                order_id=order.id,
            )

    def _initial_scale_out_plan(
        self,
        *,
        side: str,
        entry_price: float,
        strategy_take: float,
        entry_volume: float,
        entry_fee: float,
        tp1_percent: float,
    ) -> dict:
        price_bu = self._break_even_price_from_values(side, entry_price, entry_volume, entry_fee)
        distance_percent = min(
            max(self._finite_float(getattr(self.settings, "scale_out_tp2_distance_percent", 1.25)), 1.0),
            1.5,
        )
        tp2 = entry_price * (1 + distance_percent / 100) if side == "LONG" else entry_price * (1 - distance_percent / 100)
        return {
            "mode": "CASCADE",
            "entry_volume": round(float(entry_volume), 8),
            "tp1_price": round(price_bu, 8),
            "tp1_percent": round(min(max(self._finite_float(tp1_percent), 10.0), 30.0), 4),
            "tp2_price": round(max(tp2, entry_price * 0.0001), 8),
            "tp2_percent": round(min(max(self._finite_float(getattr(self.settings, "scale_out_tp2_percent", 35.0)), 30.0), 40.0), 4),
            "tp3_price": round(float(strategy_take), 8),
            "price_basis": "TP1=Price_BU; TP2=fixed entry percentage because no explicit S/R detector is implemented; TP3=strategy target",
        }

    def _scale_out_state(self, position: Position) -> dict:
        context = position.entry_context if isinstance(position.entry_context, dict) else {}
        state = context.get("scale_out")
        return dict(state) if isinstance(state, dict) else {"mode": "CASCADE"}

    def _scale_out_price(self, position: Position, key: str, fallback: float) -> float:
        value = self._finite_float(self._scale_out_state(position).get(key))
        return value if value > 0 else fallback

    def _mark_scale_out_taken(self, position: Position, stage: str, price: float, volume: float) -> None:
        context = dict(position.entry_context or {})
        state = dict(context.get("scale_out") or {})
        state[f"{stage}_taken"] = True
        state[f"{stage}_fill_price"] = round(float(price), 8)
        state[f"{stage}_filled_volume"] = round(float(volume), 8)
        context["scale_out"] = state
        position.entry_context = context

    def _cancel_scale_out_for_minimum(
        self,
        db: AsyncSession,
        position: Position,
        *,
        stage: str,
        requested_volume: float,
        assessment: object,
        cycle_id: str | None,
    ) -> None:
        context = dict(position.entry_context or {})
        state = dict(context.get("scale_out") or {})
        state.update(
            {
                "mode": "MONOLITHIC",
                "cancelled_at_stage": stage,
                "minimum_notional": self._finite_float(getattr(assessment, "minimum_notional", 0.0)),
                "requested_volume": round(float(requested_volume), 8),
                "reason": str(getattr(assessment, "reason", "minimum exit size rejected")),
            }
        )
        context["scale_out"] = state
        position.entry_context = context
        self._log_trading_event(
            db,
            "WARNING",
            "SCALE_OUT_CANCELLED_MIN_NOTIONAL",
            f"Scale-out cancelled for {position.symbol} #{position.id}: {stage} below minimum; monolithic TP retained",
            cycle_id=cycle_id,
            symbol=position.symbol,
            position_id=position.id,
            stage=stage,
            requested_volume=requested_volume,
            requested_notional=self._finite_float(getattr(assessment, "notional", 0.0)),
            minimum_notional=self._finite_float(getattr(assessment, "minimum_notional", 0.0)),
            reason=str(getattr(assessment, "reason", "minimum exit size rejected")),
            fallback="MONOLITHIC_TP",
        )

    def _entry_volume(self, position: Position) -> float:
        context = position.entry_context if isinstance(position.entry_context, dict) else {}
        execution = context.get("entry_execution") if isinstance(context.get("entry_execution"), dict) else {}
        return self._finite_float(execution.get("volume")) or self._finite_float(position.volume)

    def _break_even_price(self, position: Position) -> float:
        context = position.entry_context if isinstance(position.entry_context, dict) else {}
        execution = context.get("entry_execution") if isinstance(context.get("entry_execution"), dict) else {}
        return self._break_even_price_from_values(
            position.side,
            position.entry_price,
            self._entry_volume(position),
            self._finite_float(execution.get("fee")),
        )

    def _break_even_price_from_values(self, side: str, entry_price: float, volume: float, entry_fee: float) -> float:
        entry = self._finite_float(entry_price)
        if entry <= 0:
            return 0.0
        rate = self._break_even_cost_rate(entry, volume, entry_fee)
        price = entry * (1 + rate) if side == "LONG" else entry * (1 - rate)
        return round(max(price, entry * 0.0001), 8)

    def _break_even_cost_rate(self, entry_price: float, volume: float, entry_fee: float) -> float:
        notional = entry_price * max(volume, 0.0)
        observed_entry_fee_rate = entry_fee / notional if notional > 0 else 0.0
        fallback_fee_rate = max(self._finite_float(getattr(self.settings, "paper_fee_rate", 0.0004)), 0.0)
        fee_entry = observed_entry_fee_rate if observed_entry_fee_rate > 0 else fallback_fee_rate
        fee_exit = fee_entry
        buffer_bps = max(self._finite_float(getattr(self.settings, "breakeven_slippage_buffer_bps", 2.0)), 0.0)
        return fee_entry + fee_exit + buffer_bps / 10_000

    def _exit_reason(self, position: Position) -> str | None:
        if position.side == "LONG":
            if position.current_price <= position.stop:
                return "BREAKEVEN_STOP" if position.breakeven_applied else "STOP_LOSS"
            if position.current_price >= position.take:
                return "TAKE_PROFIT"
        else:
            if position.current_price >= position.stop:
                return "BREAKEVEN_STOP" if position.breakeven_applied else "STOP_LOSS"
            if position.current_price <= position.take:
                return "TAKE_PROFIT"
        return None

    def _finite_float(self, value: object) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return 0.0
        return number if number == number and number not in {float("inf"), float("-inf")} else 0.0

    def _executed_volume(self, value: object, maximum: float) -> float:
        return round(min(max(self._finite_float(value), 0.0), max(self._finite_float(maximum), 0.0)), 8)

    def _ticker_price(self, ticker: object, side: str) -> float:
        if not isinstance(ticker, dict):
            return 0.0
        for key in ("last", "close"):
            price = self._finite_float(ticker.get(key))
            if price > 0:
                return price
        bid = self._finite_float(ticker.get("bid"))
        ask = self._finite_float(ticker.get("ask"))
        if side == "LONG" and bid > 0:
            return bid
        if side == "SHORT" and ask > 0:
            return ask
        if bid > 0 and ask > 0:
            return round((bid + ask) / 2, 8)
        return bid or ask

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

    def _record_stop_slippage_observation(
        self,
        db: AsyncSession,
        position: Position,
        *,
        expected_price: float,
        actual_price: float,
        exit_fee: float,
        exit_slippage: float,
        reason: str,
        cycle_id: str | None,
    ) -> None:
        """Persist an auditable stop-execution observation for replay/RL data.

        The value is observed, not an automatic model update: a single fill is
        insufficient evidence to change the slippage buffer safely.
        """
        expected = max(self._finite_float(expected_price), 0.0)
        actual = max(self._finite_float(actual_price), 0.0)
        reported_slippage = max(self._finite_float(exit_slippage), 0.0)
        price_deviation = abs(actual - expected)
        effective_slippage = max(reported_slippage, price_deviation)
        slippage_bps = effective_slippage / expected * 10_000 if expected > 0 else 0.0
        context = dict(position.entry_context or {})
        context["stop_execution"] = {
            "reason": reason,
            "expected_price": round(expected, 8),
            "actual_price": round(actual, 8),
            "reported_slippage": round(reported_slippage, 8),
            "effective_slippage": round(effective_slippage, 8),
            "slippage_bps": round(slippage_bps, 4),
            "exit_fee": round(max(self._finite_float(exit_fee), 0.0), 8),
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        position.entry_context = context
        self._log_trading_event(
            db,
            "WARNING",
            "STOP_SLIPPAGE_RECORDED",
            f"Recorded stop slippage for {position.symbol} #{position.id}: {slippage_bps:.2f} bps",
            cycle_id=cycle_id,
            symbol=position.symbol,
            position_id=position.id,
            side=position.side,
            exit_reason=reason,
            expected_price=expected,
            actual_price=actual,
            reported_slippage=reported_slippage,
            effective_slippage=effective_slippage,
            slippage_bps=slippage_bps,
            exit_fee=exit_fee,
            learning_event="STOP_EXECUTION_SLIPPAGE",
        )

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

    async def _pending_order_symbols(self, db: AsyncSession) -> set[str]:
        result = await db.execute(
            select(Order.symbol, Order.raw).where(Order.status == OrderStatus.NEW.value)
        )
        return {
            str(symbol)
            for symbol, raw in result.all()
            if not (isinstance(raw, dict) and raw.get("order_role") == "PROTECTIVE_STOP")
        }

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
            self._log_trading_event(
                db,
                "CRITICAL",
                "DRAWDOWN_EMERGENCY",
                message.replace("\n", " | "),
                gate="DRAWDOWN_LIMIT",
                drawdown_percent=round(float(assessment.drawdown_percent), 4),
                threshold_percent=round(float(assessment.threshold_percent), 4),
                current_equity=round(float(assessment.current_equity), 4),
                peak_equity=round(float(assessment.peak_equity), 4),
            )
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

    async def _open_stop_risk(self, db: AsyncSession) -> float:
        positions = list((await db.execute(select(Position).where(Position.status == "OPEN"))).scalars().all())
        return round(sum(self._position_stop_risk(position) for position in positions), 4)

    def _position_stop_risk(self, position: Position) -> float:
        price = self._finite_float(position.current_price)
        entry = self._finite_float(position.entry_price)
        stop = self._finite_float(position.stop)
        volume = max(self._finite_float(position.volume), 0.0)
        if price <= 0:
            price = entry
        if price <= 0 or stop <= 0 or volume <= 0:
            return 0.0
        distance_to_stop = price - stop if position.side == "LONG" else stop - price
        return round(max(distance_to_stop, 0.0) * volume, 4)
