import math
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.entities import AgentDecision, Position
from app.schemas.dto import AgentAnalysisOut, AgentDecisionOut, MarketCoin
from app.services.advanced_agents import (
    CalibrationDriftAgent,
    CrossTimeframeAgent,
    EventRiskAgent,
    ExecutionCostAgent,
    PortfolioCorrelationAgent,
)
from app.services.agent_competition import AgentCompetitionService, CompetitionProfile
from app.services.derivatives_context import DerivativesContextService
from app.services.llm import LlmAdvisorProvider
from app.services.market_scanner import MarketScanner
from app.services.ml import MlSignalService
from app.services.strategy import StrategyCore


class MarketAnalystAgent:
    name = "MarketAnalystAgent"

    def decide(self, coin: MarketCoin) -> AgentDecisionOut:
        signal = StrategyCore().evaluate(coin)
        ml = MlSignalService().predict(coin)
        trend = "восходящая" if coin.ema50 > coin.ema200 else "нисходящая"
        action = signal.signal
        strategy_confidence = max(min(signal.score / 100, 1.0), 0.0)
        supporting_probability = (
            ml.long_probability if action == "BUY" else ml.short_probability if action == "SELL" else 50
        )
        opposing_probability = (
            ml.short_probability if action == "BUY" else ml.long_probability if action == "SELL" else 50
        )
        confidence = 0.75 * strategy_confidence + 0.25 * supporting_probability / 100
        model_conflict = action in {"BUY", "SELL"} and opposing_probability >= supporting_probability + 15
        if model_conflict:
            action = "WAIT"
            confidence = min(confidence, 0.55)
        rationale = (
            f"Структура рынка {trend}: рейтинг={coin.rating}, RSI={coin.rsi:.2f}; "
            f"ML оценивает вероятность роста в {ml.long_probability}%, снижения — в {ml.short_probability}%."
        )
        if model_conflict:
            rationale += " Вероятностная оценка существенно противоречит стратегии, поэтому вход отложен."
        if action == "WAIT":
            confidence = min(confidence, 0.65)
        return AgentDecisionOut(
            agent_name=self.name,
            symbol=coin.symbol,
            action=action,
            confidence=round(confidence, 2),
            rationale=rationale,
            context={
                "vote_family": "CORE_STRATEGY",
                "rating": coin.rating,
                "rsi": round(coin.rsi, 2),
                "ema50": round(coin.ema50, 4),
                "ema200": round(coin.ema200, 4),
                "long_probability": ml.long_probability,
                "short_probability": ml.short_probability,
                "model_conflict": model_conflict,
            },
        )


class RiskSupervisorAgent:
    name = "RiskSupervisorAgent"

    async def decide(self, db: AsyncSession, market_decision: AgentDecisionOut, min_confidence: float = 0.72) -> AgentDecisionOut:
        open_positions = (
            await db.execute(select(func.count()).select_from(Position).where(Position.status == "OPEN"))
        ).scalar_one()
        duplicate = (
            await db.execute(
                select(func.count()).select_from(Position).where(
                    Position.status == "OPEN",
                    Position.symbol == market_decision.symbol,
                )
            )
        ).scalar_one()

        action = "ALLOW"
        rationale = "Все проверки риска пройдены: комитет может передать сигнал на исполнение."
        confidence = market_decision.confidence
        if market_decision.action == "WAIT":
            action = "BLOCK"
            rationale = "Рыночный комитет не выбрал направление, поэтому открывать позицию нельзя."
        elif market_decision.confidence < min_confidence:
            action = "BLOCK"
            rationale = f"Уверенность {market_decision.confidence:.0%} ниже обязательного порога {min_confidence:.0%}."
        elif duplicate:
            action = "BLOCK"
            rationale = "По этой торговой паре уже есть открытая позиция; дублирование риска запрещено."
        elif open_positions >= 5:
            action = "REDUCE_SIZE"
            rationale = "В портфеле уже много открытых позиций, поэтому размер нового риска необходимо уменьшить."
            confidence = min(confidence, 0.6)

        return AgentDecisionOut(
            agent_name=self.name,
            symbol=market_decision.symbol,
            action=action,
            confidence=round(confidence, 2),
            rationale=rationale,
            context={
                "open_positions": int(open_positions),
                "duplicate_positions": int(duplicate),
                "risk_multiplier": 0.5 if action == "REDUCE_SIZE" else 1.0 if action == "ALLOW" else 0.0,
            },
        )


class LlmAdvisorAgent:
    name = "LlmAdvisorAgent"

    def __init__(self) -> None:
        self.provider = LlmAdvisorProvider()

    async def decide(self, coin: MarketCoin, market_decision: AgentDecisionOut) -> AgentDecisionOut | None:
        advice = await self.provider.advise(coin, market_decision.context)
        if not advice:
            return None
        action = advice.action
        confidence = advice.confidence
        rationale = advice.rationale
        if market_decision.action in {"BUY", "SELL"} and action not in {market_decision.action, "WAIT"}:
            action = "WAIT"
            confidence = min(confidence, 0.55)
            rationale = f"LLM не согласился с локальным сигналом; безопасное действие — ждать. Объяснение LLM: {advice.rationale}"
        return AgentDecisionOut(
            agent_name=self.name,
            symbol=coin.symbol,
            action=action,
            confidence=round(confidence, 2),
            rationale=rationale,
            context={"invalid_if": advice.invalid_if, "provider": "openai"},
        )


class TrendAgent:
    name = "TrendAgent"

    def decide(self, coin: MarketCoin) -> AgentDecisionOut:
        bullish = coin.ema20 > coin.ema50 > coin.ema200 and coin.price > coin.ema50
        bearish = coin.ema20 < coin.ema50 < coin.ema200 and coin.price < coin.ema50
        spread = abs(coin.ema50 - coin.ema200) / max(coin.price, 1)
        confidence = min(0.95, 0.55 + spread * 8 + max(coin.rating - 70, 0) / 100)
        action = "BUY" if bullish else "SELL" if bearish else "WAIT"
        rationale = (
            f"Порядок EMA дал действие {action}: цена={coin.price:.4f}, "
            f"EMA20={coin.ema20:.4f}, EMA50={coin.ema50:.4f}, EMA200={coin.ema200:.4f}."
        )
        if action == "WAIT":
            confidence = 0.52
        return AgentDecisionOut(
            agent_name=self.name,
            symbol=coin.symbol,
            action=action,
            confidence=round(confidence, 2),
            rationale=rationale,
            context={"vote_family": "TREND", "ema20": coin.ema20, "ema50": coin.ema50, "ema200": coin.ema200, "rating": coin.rating},
        )


class MomentumAgent:
    name = "MomentumAgent"

    def decide(self, coin: MarketCoin) -> AgentDecisionOut:
        long_zone = 55 <= coin.rsi <= 75 and coin.macd > 0
        short_zone = 25 <= coin.rsi <= 45 and coin.macd < 0
        action = "BUY" if long_zone else "SELL" if short_zone else "WAIT"
        confidence = 0.7
        if action == "BUY":
            confidence += min((coin.rsi - 55) / 100, 0.15)
        elif action == "SELL":
            confidence += min((45 - coin.rsi) / 100, 0.15)
        else:
            confidence = 0.5
        return AgentDecisionOut(
            agent_name=self.name,
            symbol=coin.symbol,
            action=action,
            confidence=round(min(confidence, 0.9), 2),
            rationale=f"Импульс: RSI={coin.rsi:.2f}, MACD={coin.macd:.4f}; итоговое действие — {action}.",
            context={"vote_family": "MOMENTUM", "rsi": round(coin.rsi, 2), "macd": round(coin.macd, 4)},
        )


class LiquidityAgent:
    name = "LiquidityAgent"

    def decide(self, coin: MarketCoin) -> AgentDecisionOut:
        open_interest_available = coin.open_interest > 0
        open_interest_liquid = not open_interest_available or coin.open_interest >= 100_000_000
        liquid = coin.volume_24h >= 100_000_000 and open_interest_liquid
        action = "ALLOW" if liquid else "BLOCK"
        confidence = 0.9 if liquid else 0.8
        open_interest_text = (
            f"{coin.open_interest:.0f}"
            if open_interest_available
            else "недоступен для спотового рынка (технический статус: not available for spot)"
        )
        rationale = (
            f"Объём за 24 часа={coin.volume_24h:.0f}, открытый интерес={open_interest_text}; "
            f"ликвидность {'достаточная' if liquid else 'слишком низкая для безопасного входа'}."
        )
        return AgentDecisionOut(
            agent_name=self.name,
            symbol=coin.symbol,
            action=action,
            confidence=confidence,
            rationale=rationale,
            context={"gate_kind": "SAFETY", "volume_24h": coin.volume_24h, "open_interest": coin.open_interest},
        )


class VolatilityAgent:
    name = "VolatilityAgent"

    def decide(self, coin: MarketCoin) -> AgentDecisionOut:
        atr_percent = coin.atr / max(coin.price, 1) * 100
        funding_risk = abs(coin.funding_rate) > float(get_settings().extreme_funding_rate_abs)
        too_hot = atr_percent > 8 or funding_risk
        action = "BLOCK" if too_hot else "ALLOW"
        confidence = 0.85 if too_hot else 0.75
        rationale = f"ATR составляет {atr_percent:.2f}% цены, funding={coin.funding_rate:.4f}; решение по волатильности — {action}."
        return AgentDecisionOut(
            agent_name=self.name,
            symbol=coin.symbol,
            action=action,
            confidence=confidence,
            rationale=rationale,
            context={"gate_kind": "SAFETY", "atr_percent": round(atr_percent, 2), "funding_rate": coin.funding_rate},
        )


class RegimeAgent:
    name = "RegimeAgent"

    def decide(self, coin: MarketCoin) -> AgentDecisionOut:
        if coin.regime == "TRENDING_UP":
            action = "BUY"
            confidence = max(0.65, coin.regime_score / 100)
        elif coin.regime == "TRENDING_DOWN":
            action = "SELL"
            confidence = max(0.65, coin.regime_score / 100)
        elif coin.regime in {"LOW_LIQUIDITY", "HIGH_VOLATILITY"}:
            action = "BLOCK"
            confidence = 0.9
        else:
            action = "WAIT"
            confidence = max(0.45, coin.regime_score / 100)
        return AgentDecisionOut(
            agent_name=self.name,
            symbol=coin.symbol,
            action=action,
            confidence=round(min(confidence, 0.95), 2),
            rationale=f"Режим рынка={coin.regime}; объяснение классификатора: {coin.regime_reason or 'дополнительная причина не указана'}.",
            context={"vote_family": "REGIME", "regime": coin.regime, "regime_score": coin.regime_score},
        )


class AdaptiveTrendAgent:
    """Теневой кандидат: быстрее реагирует на EMA20 и подтверждение режима."""

    name = "AdaptiveTrendAgent"

    def decide(self, coin: MarketCoin) -> AgentDecisionOut:
        bullish = coin.price > coin.ema20 > coin.ema50 and coin.regime != "TRENDING_DOWN"
        bearish = coin.price < coin.ema20 < coin.ema50 and coin.regime != "TRENDING_UP"
        action = "BUY" if bullish else "SELL" if bearish else "WAIT"
        distance = abs(coin.price - coin.ema20) / max(coin.price, 1)
        confidence = 0.5 if action == "WAIT" else min(0.92, 0.62 + distance * 8 + coin.regime_score / 500)
        return AgentDecisionOut(
            agent_name=self.name,
            symbol=coin.symbol,
            action=action,
            confidence=round(confidence, 2),
            rationale=(
                f"Адаптивный тренд: цена={coin.price:.4f}, EMA20={coin.ema20:.4f}, "
                f"EMA50={coin.ema50:.4f}, режим={coin.regime}; решение — {action}."
            ),
            context={"vote_family": "TREND", "price": coin.price, "ema20": coin.ema20, "ema50": coin.ema50, "regime": coin.regime},
        )


class BreakoutAgent:
    """Теневой кандидат: ищет подтверждённое ускорение цены и объёма."""

    name = "BreakoutAgent"

    def decide(self, coin: MarketCoin) -> AgentDecisionOut:
        volume_ratio = coin.volume_24h / coin.volume_average_24h if coin.volume_average_24h > 0 else 1.0
        bullish = coin.price_change_percent >= 1.5 and coin.macd > 0 and coin.rsi >= 52 and volume_ratio >= 1
        bearish = coin.price_change_percent <= -1.5 and coin.macd < 0 and coin.rsi <= 48 and volume_ratio >= 1
        action = "BUY" if bullish else "SELL" if bearish else "WAIT"
        strength = min(abs(coin.price_change_percent) / 10, 0.2)
        confidence = 0.48 if action == "WAIT" else min(0.9, 0.62 + strength + min(max(volume_ratio - 1, 0) / 5, 0.08))
        return AgentDecisionOut(
            agent_name=self.name,
            symbol=coin.symbol,
            action=action,
            confidence=round(confidence, 2),
            rationale=(
                f"Проверка пробоя: изменение цены={coin.price_change_percent:+.2f}%, RSI={coin.rsi:.2f}, "
                f"MACD={coin.macd:.4f}, объём к среднему={volume_ratio:.2f}; решение — {action}."
            ),
            context={"vote_family": "MOMENTUM", "price_change_percent": coin.price_change_percent, "volume_ratio": round(volume_ratio, 3), "rsi": coin.rsi, "macd": coin.macd},
        )


class DataQualityAgent:
    """Hard veto for malformed or internally inconsistent market snapshots."""

    name = "DataQualityAgent"

    def decide(self, coin: MarketCoin) -> AgentDecisionOut:
        numeric = {
            "price": coin.price,
            "volume_24h": coin.volume_24h,
            "atr": coin.atr,
            "rsi": coin.rsi,
            "ema20": coin.ema20,
            "ema50": coin.ema50,
            "ema200": coin.ema200,
            "macd": coin.macd,
            "spread_bps": coin.spread_bps,
        }
        problems = [name for name, value in numeric.items() if not math.isfinite(float(value))]
        if coin.price <= 0 or coin.atr < 0 or coin.volume_24h < 0:
            problems.append("non_positive_market_value")
        if not 0 <= coin.rsi <= 100:
            problems.append("rsi_out_of_range")
        if min(coin.ema20, coin.ema50, coin.ema200) <= 0:
            problems.append("invalid_ema")
        if coin.spread_bps < 0:
            problems.append("negative_spread")
        if coin.bid is not None and coin.ask is not None and (coin.bid <= 0 or coin.ask <= 0 or coin.ask < coin.bid):
            problems.append("invalid_best_bid_ask")
        action = "BLOCK" if problems else "ALLOW"
        return AgentDecisionOut(
            agent_name=self.name,
            symbol=coin.symbol,
            action=action,
            confidence=0.99 if problems else 0.95,
            rationale=(
                f"Качество рыночных данных не прошло проверку: {', '.join(sorted(set(problems)))}."
                if problems
                else "Цена, индикаторы, объём и bid/ask внутренне согласованы; данные пригодны для решения."
            ),
            context={"gate_kind": "SAFETY", "problems": sorted(set(problems))},
        )


class VenueSafetyAgent:
    """Prevents an entry signal from violating the configured venue semantics."""

    name = "VenueSafetyAgent"

    def __init__(self) -> None:
        self.settings = get_settings()

    def decide(self, coin: MarketCoin, signal: str) -> AgentDecisionOut:
        market_type = str(self.settings.exchange_default_type or "spot").lower()
        derivatives = market_type in {"future", "futures", "swap"}
        blocked = (
            signal == "SELL"
            and not derivatives
            and self.settings.block_spot_short_entries
            and (not self.settings.paper_trading or not self.settings.allow_paper_short_on_spot)
        )
        return AgentDecisionOut(
            agent_name=self.name,
            symbol=coin.symbol,
            action="BLOCK" if blocked else "ALLOW",
            confidence=0.99,
            rationale=(
                "SHORT запрещён в Spot-контуре: бот не имеет права продавать несвязанное содержимое кошелька."
                if blocked
                else f"Направление {signal} совместимо с рынком {market_type}."
            ),
            context={
                "gate_kind": "SAFETY",
                "market_type": market_type,
                "signal": signal,
                "spot_short_blocked": blocked,
            },
        )


class EntryTimingAgent:
    """Prevents chasing an extended move or entering without volume confirmation."""

    name = "EntryTimingAgent"

    def __init__(self) -> None:
        self.settings = get_settings()

    def decide(self, coin: MarketCoin) -> AgentDecisionOut:
        atr_floor = max(float(coin.atr), float(coin.price) * 0.0001)
        distance_atr = abs(float(coin.price) - float(coin.ema20)) / atr_floor
        volume_ratio = (
            float(coin.volume_24h) / float(coin.volume_average_24h)
            if coin.volume_average_24h > 0
            else 0.0
        )
        reasons: list[str] = []
        if coin.volume_average_24h <= 0:
            reasons.append("нет надёжной средней базы объёма")
        elif volume_ratio < float(self.settings.strategy_min_volume_ratio):
            reasons.append(
                f"объём {volume_ratio:.2f}x ниже подтверждающего порога {self.settings.strategy_min_volume_ratio:.2f}x"
            )
        if distance_atr > float(self.settings.strategy_max_entry_distance_atr):
            reasons.append(
                f"цена удалена от EMA20 на {distance_atr:.2f} ATR при лимите {self.settings.strategy_max_entry_distance_atr:.2f}"
            )
        if coin.spread_bps > float(self.settings.market_quality_max_spread_bps) * 2:
            reasons.append(f"спред {coin.spread_bps:.2f} bps неприемлем для входа")
        action = "BLOCK" if reasons else "ALLOW"
        return AgentDecisionOut(
            agent_name=self.name,
            symbol=coin.symbol,
            action=action,
            confidence=0.94 if reasons else 0.82,
            rationale=(
                f"Тайминг входа отклонён: {'; '.join(reasons)}."
                if reasons
                else f"Тайминг подтверждён: объём={volume_ratio:.2f}x среднего, удаление от EMA20={distance_atr:.2f} ATR."
            ),
            context={
                "gate_kind": "SAFETY",
                "volume_ratio": round(volume_ratio, 4),
                "distance_from_ema20_atr": round(distance_atr, 4),
                "reasons": reasons,
            },
        )


class MicrostructureAgent:
    """Makes the already captured order-book/tape evidence visible to the committee."""

    name = "MicrostructureAgent"

    def decide(self, coin: MarketCoin, signal: str, snapshot: dict | None) -> AgentDecisionOut:
        snapshot = snapshot if isinstance(snapshot, dict) else {}
        if signal not in {"BUY", "SELL"}:
            return AgentDecisionOut(
                agent_name=self.name,
                symbol=coin.symbol,
                action="ALLOW",
                confidence=0.5,
                rationale="Microstructure has no directional signal to validate",
                context={"gate_kind": "SAFETY", "evidence": []},
            )
        direction = 1.0 if signal == "BUY" else -1.0
        evidence: list[tuple[str, float]] = []
        if snapshot.get("order_book_available"):
            evidence.append(("book", direction * float(snapshot.get("order_book_imbalance") or 0.0)))
        if snapshot.get("tape_available"):
            evidence.append(("tape", direction * float(snapshot.get("trade_flow_imbalance") or 0.0)))
        if snapshot.get("momentum_available"):
            change = direction * float(snapshot.get("price_change_10m_percent") or 0.0)
            evidence.append(("momentum", 0.08 if change > 0 else -0.08 if change < 0 else 0.0))
        strong_opposition = sum(value <= -0.15 for _, value in evidence)
        action = "BLOCK" if strong_opposition >= 2 else "ALLOW"
        details = ", ".join(f"{name}={value:+.2f}" for name, value in evidence) or "нет новых данных"
        return AgentDecisionOut(
            agent_name=self.name,
            symbol=coin.symbol,
            action=action,
            confidence=0.92 if action == "BLOCK" else min(0.9, 0.55 + len(evidence) * 0.1),
            rationale=(
                f"Микроструктура против входа {signal}: {details}."
                if action == "BLOCK"
                else f"Микроструктура не содержит двойного сильного встречного подтверждения: {details}."
            ),
            context={
                "gate_kind": "SAFETY",
                "signal": signal,
                "available_sources": len(evidence),
                "directional_evidence": dict(evidence),
            },
        )


class AgentOrchestrator:
    def __init__(self) -> None:
        self.scanner = MarketScanner()
        self.derivatives_context = DerivativesContextService(self.scanner.exchange)
        self.market_agent = MarketAnalystAgent()
        self.llm_agent = LlmAdvisorAgent()
        self.risk_agent = RiskSupervisorAgent()
        self.settings = get_settings()
        self.committee_agents = [
            DataQualityAgent(),
            RegimeAgent(),
            TrendAgent(),
            MomentumAgent(),
            LiquidityAgent(),
            VolatilityAgent(),
            EntryTimingAgent(),
        ]
        self.challenger_agents = [AdaptiveTrendAgent(), BreakoutAgent()]
        self.competition = AgentCompetitionService()
        self.portfolio_agent = PortfolioCorrelationAgent()
        self.execution_cost_agent = ExecutionCostAgent()
        self.cross_timeframe_agent = CrossTimeframeAgent()
        self.calibration_agent = CalibrationDriftAgent()
        self.event_risk_agent = EventRiskAgent()

    async def analyze(self, db: AsyncSession, symbol: str) -> AgentAnalysisOut:
        coins = await self.derivatives_context.enrich(db, await self.scanner.scan([symbol]))
        return await self.analyze_coin(db, coins[0])

    async def analyze_coin(
        self,
        db: AsyncSession,
        coin: MarketCoin,
        microstructure: dict | None = None,
        *,
        timeframe: str = "1h",
        timeframe_candles: dict[str, list[Any]] | None = None,
        candidate_notional: float = 0.0,
        event_context: dict[str, Any] | None = None,
    ) -> AgentAnalysisOut:
        market = self.market_agent.decide(coin)
        llm = await self.llm_agent.decide(coin, market)
        profiles = await self.competition.profiles(db)
        committee = [
            self._competition_context(agent.decide(coin), profiles)
            for agent in [*self.committee_agents, *self.challenger_agents]
        ]
        committee.append(VenueSafetyAgent().decide(coin, market.action))
        committee.append(MicrostructureAgent().decide(coin, market.action, microstructure))
        if self.settings.advanced_agents_enabled:
            committee.extend(
                [
                    await self.portfolio_agent.decide(db, coin, market.action, timeframe),
                    self.execution_cost_agent.decide(coin, microstructure, candidate_notional),
                    self.cross_timeframe_agent.decide(coin, market.action, timeframe_candles),
                    await self.calibration_agent.decide(db, coin),
                    self.event_risk_agent.decide(coin, event_context),
                ]
            )
        candidate, consensus_score = self._committee_consensus(market, llm, committee)
        risk = await self.risk_agent.decide(db, candidate)
        risk = self._apply_committee_risk_reduction(risk, committee)
        approved = candidate.action in {"BUY", "SELL"} and risk.action in {"ALLOW", "REDUCE_SIZE"}
        final_action = candidate.action if approved else "BLOCK" if risk.action == "BLOCK" else "WAIT"
        final_confidence = min(candidate.confidence, risk.confidence)
        await self._persist(db, market)
        if llm:
            await self._persist(db, llm)
        for decision in committee:
            await self._persist(db, decision)
        await self._persist(db, candidate)
        await self._persist(db, risk)
        await db.commit()
        return AgentAnalysisOut(
            symbol=coin.symbol,
            market=market,
            llm=llm,
            risk=risk,
            committee=committee,
            consensus_score=round(consensus_score, 2),
            final_action=final_action,
            final_confidence=round(final_confidence, 2),
            approved=approved,
        )

    def _apply_committee_risk_reduction(
        self,
        risk: AgentDecisionOut,
        committee: list[AgentDecisionOut],
    ) -> AgentDecisionOut:
        if risk.action == "BLOCK":
            return risk
        reducers = [
            decision
            for decision in committee
            if decision.action == "REDUCE_SIZE"
            and 0 < float(decision.context.get("risk_multiplier") or 0.0) < 1
        ]
        if not reducers:
            return risk
        committee_multiplier = min(float(item.context["risk_multiplier"]) for item in reducers)
        supervisor_multiplier = float(risk.context.get("risk_multiplier") or 1.0)
        effective_multiplier = min(supervisor_multiplier, committee_multiplier)
        context = dict(risk.context)
        context.update(
            {
                "risk_multiplier": round(effective_multiplier, 4),
                "risk_reduction_agents": [item.agent_name for item in reducers],
            }
        )
        return risk.model_copy(
            update={
                "action": "REDUCE_SIZE",
                "confidence": min(float(risk.confidence), min(float(item.confidence) for item in reducers)),
                "rationale": (
                    f"{risk.rationale} Дополнительное ограничение риска до {effective_multiplier:.2f}x: "
                    f"{', '.join(item.agent_name for item in reducers)}."
                ),
                "context": context,
            }
        )

    def _committee_consensus(
        self,
        market: AgentDecisionOut,
        llm: AgentDecisionOut | None,
        committee: list[AgentDecisionOut],
    ) -> tuple[AgentDecisionOut, float]:
        if any(decision.action == "BLOCK" for decision in committee):
            blockers = [decision.agent_name for decision in committee if decision.action == "BLOCK"]
            return (
                AgentDecisionOut(
                    agent_name="TradeCommittee",
                    symbol=market.symbol,
                    action="WAIT",
                    confidence=0.45,
                    rationale=f"Защитное вето комитета наложили: {', '.join(blockers)}. Вход отменён независимо от направленных голосов.",
                    context={"blockers": blockers},
                ),
                0,
            )

        candidate = self._combine(market, llm)
        if candidate.action not in {"BUY", "SELL"}:
            return candidate, 0

        directional_votes = [
            decision for decision in [market, *committee]
            if decision.action in {"BUY", "SELL"}
            and decision.context.get("competition_status") in {None, "CHAMPION"}
        ]
        agreeing = [decision for decision in directional_votes if decision.action == candidate.action]
        opposing = [decision for decision in directional_votes if decision.action != candidate.action]
        vote_mass = lambda decision: (
            float(decision.context.get("vote_weight", 1.0))
            * max(min(float(decision.confidence), 1.0), 0.05)
        )
        total_weight = sum(vote_mass(decision) for decision in directional_votes)
        agreeing_weight = sum(vote_mass(decision) for decision in agreeing)
        opposing_weight = sum(vote_mass(decision) for decision in opposing)
        consensus_score = agreeing_weight / max(total_weight, 1e-9)
        avg_confidence = sum(
            decision.confidence * vote_mass(decision) for decision in agreeing
        ) / max(agreeing_weight, 1e-9)
        agreeing_families = {
            str(decision.context.get("vote_family") or decision.context.get("competition_role") or decision.agent_name)
            for decision in agreeing
        }
        strong_opposing = [
            decision.agent_name
            for decision in opposing
            if decision.confidence >= 0.78 and vote_mass(decision) >= 0.55
        ]
        min_consensus = max(min(float(self.settings.ai_committee_min_consensus), 1.0), 0.5)

        if consensus_score < min_consensus or len(agreeing_families) < 2 or strong_opposing:
            return (
                AgentDecisionOut(
                    agent_name="TradeCommittee",
                    symbol=market.symbol,
                    action="WAIT",
                    confidence=round(min(avg_confidence, 0.58), 2),
                    rationale=(
                        f"Согласие недостаточно: {consensus_score:.0%} калиброванного веса за {candidate.action}, "
                        f"независимых семейств={len(agreeing_families)}, сильных встречных голосов={len(strong_opposing)}."
                    ),
                    context={
                        "candidate_action": candidate.action,
                        "agreeing_agents": [decision.agent_name for decision in agreeing],
                        "opposing_agents": [decision.agent_name for decision in opposing],
                        "strong_opposing_agents": strong_opposing,
                        "agreeing_families": sorted(agreeing_families),
                        "support_weight": round(agreeing_weight, 4),
                        "opposition_weight": round(opposing_weight, 4),
                        "required_consensus": round(min_consensus, 4),
                    },
                ),
                consensus_score,
            )

        return (
            AgentDecisionOut(
                agent_name="TradeCommittee",
                symbol=market.symbol,
                action=candidate.action,
                confidence=round(min(avg_confidence, candidate.confidence, 0.95), 2),
                rationale=(
                    f"Комитет одобрил {candidate.action}: калиброванное согласие={consensus_score:.0%}, "
                    f"независимых семейств подтверждения={len(agreeing_families)}."
                ),
                context={
                    "agreeing_agents": [decision.agent_name for decision in agreeing],
                    "consensus_score": round(consensus_score, 2),
                    "agreeing_families": sorted(agreeing_families),
                    "support_weight": round(agreeing_weight, 4),
                    "opposition_weight": round(opposing_weight, 4),
                },
            ),
            consensus_score,
        )

    def _combine(self, market: AgentDecisionOut, llm: AgentDecisionOut | None) -> AgentDecisionOut:
        if not llm:
            return market
        if llm.action == "WAIT":
            return AgentDecisionOut(
                agent_name="AgentOrchestrator",
                symbol=market.symbol,
                action="WAIT",
                confidence=min(market.confidence, llm.confidence),
                rationale="LLM-советник запросил ожидание; оркестратор не разрешает вход без согласования.",
                context={"market_action": market.action, "llm_action": llm.action},
            )
        if market.action == llm.action:
            return AgentDecisionOut(
                agent_name="AgentOrchestrator",
                symbol=market.symbol,
                action=market.action,
                confidence=min(0.99, (market.confidence + llm.confidence) / 2),
                rationale="Рыночный аналитик и LLM-советник выбрали одинаковое направление.",
                context={"market_confidence": market.confidence, "llm_confidence": llm.confidence},
            )
        return AgentDecisionOut(
            agent_name="AgentOrchestrator",
            symbol=market.symbol,
            action="WAIT",
            confidence=min(market.confidence, llm.confidence, 0.55),
            rationale="Агенты выбрали разные направления; безопасное итоговое действие — ожидание.",
            context={"market_action": market.action, "llm_action": llm.action},
        )

    async def _persist(self, db: AsyncSession, decision: AgentDecisionOut) -> None:
        db.add(
            AgentDecision(
                agent_name=decision.agent_name,
                symbol=decision.symbol,
                action=decision.action,
                confidence=decision.confidence,
                rationale=decision.rationale,
                context=decision.context,
            )
        )

    def _competition_context(
        self,
        decision: AgentDecisionOut,
        profiles: dict[str, CompetitionProfile],
    ) -> AgentDecisionOut:
        profile = profiles.get(decision.agent_name)
        if not profile:
            return decision
        decision.context.update(
            {
                "competition_role": profile.role,
                "competition_status": profile.status,
                "performance_rating": profile.rating,
                "performance_observations": profile.observations,
                "vote_weight": profile.vote_weight if profile.status == "CHAMPION" else 0.0,
                "competition_explanation": (
                    "Основной аналитик влияет на решение комитета."
                    if profile.status == "CHAMPION"
                    else "Теневой претендент обучается на том же рынке, но пока не влияет на сделку."
                ),
            }
        )
        return decision
