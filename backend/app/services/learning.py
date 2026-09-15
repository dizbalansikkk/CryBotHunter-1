from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import LearningRule, Position
from app.schemas.dto import LearningInsightOut, LearningInsightsOut, MarketCoin


@dataclass
class LearningAssessment:
    allowed: bool
    penalty: float
    reason: str
    risk_multiplier: float = 1.0


class LearningService:
    block_threshold = 2.5
    warn_threshold = 1.25
    max_penalty = 5.0
    min_observations_for_block = 2
    half_life_days = 14.0
    min_risk_multiplier = 0.35
    blocking_feature_keys = {"setup_signature", "momentum_profile", "risk_profile"}
    feature_weights = {
        "setup_signature": 1.6,
        "momentum_profile": 1.1,
        "risk_profile": 1.0,
        "trend_stack": 0.65,
        "regime": 0.55,
        "regime_score_bucket": 0.45,
        "rating_bucket": 0.45,
        "rsi_bucket": 0.4,
        "atr_bucket": 0.4,
        "macd_direction": 0.35,
        "exit_reason": 0.25,
    }

    def entry_context(self, coin: MarketCoin, signal: str, reasons: list[str]) -> dict[str, Any]:
        atr_percent = self._atr_percent(float(coin.atr), float(coin.price))
        context = {
            "symbol": coin.symbol,
            "side": "LONG" if signal == "BUY" else "SHORT",
            "signal": signal,
            "rating": coin.rating,
            "rsi": round(coin.rsi, 2),
            "rsi_bucket": self._rsi_bucket(coin.rsi),
            "atr_percent": round(atr_percent, 2),
            "atr_bucket": self._atr_bucket(atr_percent),
            "regime": coin.regime,
            "regime_score_bucket": self._score_bucket(coin.regime_score),
            "trend_stack": self._trend_stack(coin),
            "macd_direction": "positive" if coin.macd > 0 else "negative" if coin.macd < 0 else "flat",
            "rating_bucket": self._score_bucket(coin.rating),
            "market_context": coin.market_context,
            "reasons": reasons,
        }
        context["momentum_profile"] = self._profile(context, "trend_stack", "rsi_bucket", "macd_direction")
        context["risk_profile"] = self._profile(context, "atr_bucket", "regime_score_bucket", "rating_bucket")
        context["setup_signature"] = self._profile(
            context,
            "regime",
            "trend_stack",
            "rsi_bucket",
            "macd_direction",
            "rating_bucket",
        )
        return context

    async def assess_entry(self, db: AsyncSession, coin: MarketCoin, signal: str) -> LearningAssessment:
        side = "LONG" if signal == "BUY" else "SHORT"
        features = self._features_from_context(self.entry_context(coin, signal, []))
        total_penalty = 0.0
        matched: list[tuple[LearningRule, float, str]] = []
        for scope in ("GLOBAL", coin.symbol):
            rules = await self._rules_for(db, scope, side, features)
            for rule in rules:
                effective_penalty = self.effective_penalty(rule, scope)
                if effective_penalty > 0:
                    total_penalty += effective_penalty
                    label = f"{rule.feature_key}={rule.feature_value}:{effective_penalty:.2f}"
                    matched.append((rule, effective_penalty, label))
        matched.sort(key=lambda item: item[1], reverse=True)
        match_summary = ", ".join(label for _rule, _penalty, label in matched[:4])
        if total_penalty >= self.block_threshold and self._has_block_evidence(matched):
            return LearningAssessment(
                False,
                round(total_penalty, 2),
                f"learning guard blocked repeated losing setup ({match_summary})",
                0.0,
            )
        if total_penalty >= self.warn_threshold:
            risk_multiplier = self.risk_multiplier_for_penalty(total_penalty)
            return LearningAssessment(
                True,
                round(total_penalty, 2),
                f"learning guard reduced risk to {risk_multiplier:.2f}x after similar losses ({match_summary})",
                risk_multiplier,
            )
        if total_penalty > 0:
            return LearningAssessment(
                True,
                round(total_penalty, 2),
                f"learning guard noted weak risk memory ({match_summary})",
                1.0,
            )
        return LearningAssessment(True, round(total_penalty, 2), "learning guard passed")

    async def record_closed_position(self, db: AsyncSession, position: Position, profit: float, reason: str) -> None:
        context = position.entry_context or {}
        side = str(context.get("side") or position.side)
        features = self._features_from_context(context)
        if reason:
            features.append(("exit_reason", reason))
        for scope in ("GLOBAL", position.symbol):
            for feature_key, feature_value in features:
                await self._upsert_rule(db, scope, side, feature_key, feature_value, profit, reason)

    async def _rules_for(
        self,
        db: AsyncSession,
        scope: str,
        side: str,
        features: list[tuple[str, str]],
    ) -> list[LearningRule]:
        if not features:
            return []
        keys = {key for key, _ in features}
        values = {value for _, value in features}
        result = await db.execute(
            select(LearningRule).where(
                LearningRule.scope == scope,
                LearningRule.side == side,
                LearningRule.feature_key.in_(keys),
                LearningRule.feature_value.in_(values),
            )
        )
        return [
            rule
            for rule in result.scalars().all()
            if (rule.feature_key, rule.feature_value) in features
        ]

    async def _upsert_rule(
        self,
        db: AsyncSession,
        scope: str,
        side: str,
        feature_key: str,
        feature_value: str,
        profit: float,
        reason: str,
    ) -> None:
        loss = profit < 0
        penalty_delta = self._penalty_delta(profit)
        statement = insert(LearningRule).values(
            scope=scope,
            side=side,
            feature_key=feature_key,
            feature_value=feature_value,
            penalty=max(penalty_delta, 0),
            observations=1,
            wins=0 if loss else 1,
            losses=1 if loss else 0,
            total_profit=profit,
            last_reason=reason,
        )
        statement = statement.on_conflict_do_update(
            constraint="uq_learning_rule",
            set_={
                "penalty": func.greatest(0, func.least(self.max_penalty, LearningRule.penalty + penalty_delta)),
                "observations": LearningRule.observations + 1,
                "wins": LearningRule.wins + (0 if loss else 1),
                "losses": LearningRule.losses + (1 if loss else 0),
                "total_profit": LearningRule.total_profit + profit,
                "last_reason": reason,
            },
        )
        await db.execute(statement)

    def _penalty_delta(self, profit: float) -> float:
        if profit < 0:
            return min(1.0, 0.35 + abs(profit) / 25)
        return -min(0.5, 0.15 + profit / 50)

    def rule_confidence(self, observations: int, updated_at: datetime | None = None) -> float:
        if observations <= 0:
            return 0.0
        observation_confidence = min(1.0, observations / self.min_observations_for_block)
        return round(observation_confidence * self.recency_weight(updated_at), 4)

    def risk_level(self, penalty: float, observations: int, updated_at: datetime | None = None) -> str:
        effective = penalty * self.rule_confidence(observations, updated_at)
        if effective >= self.block_threshold:
            return "BLOCK"
        if effective >= self.warn_threshold:
            return "WARN"
        return "WATCH"

    def recency_weight(self, updated_at: datetime | None) -> float:
        if not updated_at:
            return 1.0
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        age_days = max((datetime.now(timezone.utc) - updated_at).total_seconds() / 86400, 0)
        return round(0.5 ** (age_days / self.half_life_days), 4)

    def _features_from_context(self, context: dict[str, Any]) -> list[tuple[str, str]]:
        feature_keys = [
            "regime",
            "regime_score_bucket",
            "rsi_bucket",
            "atr_bucket",
            "trend_stack",
            "macd_direction",
            "rating_bucket",
            "momentum_profile",
            "risk_profile",
            "setup_signature",
        ]
        return [(key, str(context[key])) for key in feature_keys if context.get(key) is not None]

    def effective_penalty(self, rule: LearningRule, scope: str) -> float:
        if rule.penalty <= 0:
            return 0.0
        confidence = self.rule_confidence(rule.observations, rule.updated_at)
        if confidence <= 0:
            return 0.0
        scope_weight = 1.0 if scope == "GLOBAL" else 1.2
        return round(
            rule.penalty
            * confidence
            * scope_weight
            * self.feature_weight(rule.feature_key)
            * self.outcome_weight(rule),
            4,
        )

    def feature_weight(self, feature_key: str) -> float:
        return self.feature_weights.get(feature_key, 0.4)

    def outcome_weight(self, rule: LearningRule) -> float:
        observations = max(int(rule.observations or 0), 0)
        if observations <= 0:
            return 0.0
        losses = max(int(rule.losses or 0), 0)
        wins = max(int(rule.wins or 0), 0)
        loss_rate = losses / observations
        if losses <= wins and float(rule.total_profit or 0) >= 0:
            return 0.25
        if loss_rate < 0.4:
            return 0.4
        return round(0.7 + min(loss_rate, 1.0) * 0.3, 4)

    def risk_multiplier_for_penalty(self, penalty: float) -> float:
        if penalty < self.warn_threshold:
            return 1.0
        span = max(self.block_threshold - self.warn_threshold, 0.01)
        severity = min(max((penalty - self.warn_threshold) / span, 0.0), 1.0)
        return round(max(self.min_risk_multiplier, 0.75 - severity * (0.75 - self.min_risk_multiplier)), 2)

    def build_insights(
        self,
        rules: list[LearningRule],
        learned_from_trades: int,
        limit: int = 12,
    ) -> LearningInsightsOut:
        insights = [self._insight(rule) for rule in rules if int(rule.observations or 0) > 0]
        priority = {"AVOID": 4, "CAUTION": 3, "PREFER": 2, "WATCH": 1}
        insights.sort(
            key=lambda item: (
                priority[item.impact],
                item.effective_penalty if item.impact in {"AVOID", "CAUTION"} else abs(item.total_profit),
                item.observations,
            ),
            reverse=True,
        )
        strong = [item for item in insights if item.confidence >= 0.75 and item.observations >= 2]
        return LearningInsightsOut(
            learned_from_trades=max(int(learned_from_trades), 0),
            rules_updated=len(rules),
            strong_patterns=len(strong),
            protective_patterns=sum(1 for item in strong if item.impact in {"AVOID", "CAUTION"}),
            favorable_patterns=sum(1 for item in strong if item.impact == "PREFER"),
            insights=insights[: max(int(limit), 0)],
        )

    def _insight(self, rule: LearningRule) -> LearningInsightOut:
        observations = max(int(rule.observations or 0), 0)
        wins = max(int(rule.wins or 0), 0)
        losses = max(int(rule.losses or 0), 0)
        win_rate = wins / observations * 100 if observations else 0.0
        confidence = self.rule_confidence(observations, rule.updated_at)
        risk_level = self.risk_level(rule.penalty, observations, rule.updated_at)
        effective_penalty = self.effective_penalty(rule, rule.scope)
        profitable = observations >= 2 and wins > losses and float(rule.total_profit or 0) > 0
        if risk_level == "BLOCK":
            impact = "AVOID"
            explanation = "Повторяющийся убыточный паттерн: новые совпадения блокируются."
        elif risk_level == "WARN" or effective_penalty >= self.warn_threshold:
            impact = "CAUTION"
            explanation = "Похожий вход исторически слабый: бот уменьшает риск."
        elif profitable:
            impact = "PREFER"
            explanation = "Паттерн чаще приносил прибыль и ослабляет старые штрафы для похожих входов."
        else:
            impact = "WATCH"
            explanation = "Данных пока мало: бот наблюдает, но не меняет решение автоматически."
        return LearningInsightOut(
            impact=impact,
            scope=rule.scope,
            side=rule.side,
            feature_key=rule.feature_key,
            feature_value=rule.feature_value,
            observations=observations,
            wins=wins,
            losses=losses,
            win_rate=round(win_rate, 2),
            total_profit=round(float(rule.total_profit or 0), 4),
            effective_penalty=round(effective_penalty, 4),
            confidence=confidence,
            risk_level=risk_level,
            explanation=explanation,
            last_reason=rule.last_reason,
        )

    def _has_block_evidence(self, matched: list[tuple[LearningRule, float, str]]) -> bool:
        for rule, _penalty, _label in matched:
            if rule.feature_key not in self.blocking_feature_keys:
                continue
            if rule.observations < self.min_observations_for_block:
                continue
            if rule.losses > rule.wins:
                return True
        return False

    def _atr_percent(self, atr: float, price: float) -> float:
        return atr / price * 100 if price > 0 else 0

    def _atr_bucket(self, value: float) -> str:
        if value < 0.75:
            return "quiet"
        if value <= 3.0:
            return "normal"
        if value <= 6.0:
            return "hot"
        return "extreme"

    def _rsi_bucket(self, value: float) -> str:
        if value < 30:
            return "oversold"
        if value < 45:
            return "bearish"
        if value <= 55:
            return "neutral"
        if value <= 70:
            return "bullish"
        return "overbought"

    def _score_bucket(self, value: float) -> str:
        if value < 45:
            return "weak"
        if value < 70:
            return "medium"
        if value < 85:
            return "strong"
        return "elite"

    def _trend_stack(self, coin: MarketCoin) -> str:
        if coin.ema20 > coin.ema50 > coin.ema200:
            return "bullish"
        if coin.ema20 < coin.ema50 < coin.ema200:
            return "bearish"
        return "mixed"

    def _profile(self, context: dict[str, Any], *keys: str) -> str:
        return "|".join(str(context.get(key, "unknown")) for key in keys)
