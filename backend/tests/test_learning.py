from datetime import datetime, timedelta, timezone

from app.schemas.dto import MarketCoin
from app.models.entities import LearningRule
from app.services.learning import LearningService


def coin(**overrides):
    data = {
        "symbol": "BTC/USDT",
        "price": 100,
        "volume_24h": 2_000_000_000,
        "price_change_percent": 4,
        "atr": 2,
        "rsi": 62,
        "ema20": 106,
        "ema50": 100,
        "ema200": 90,
        "macd": 10,
        "funding_rate": 0.01,
        "open_interest": 1_500_000_000,
        "rating": 88,
        "regime": "TRENDING_UP",
        "regime_score": 82,
        "regime_reason": "trend confirmed",
    }
    data.update(overrides)
    return MarketCoin(**data)


def test_learning_entry_context_buckets_market_features():
    context = LearningService().entry_context(coin(), "BUY", ["ok"])

    assert context["side"] == "LONG"
    assert context["rsi_bucket"] == "bullish"
    assert context["atr_bucket"] == "normal"
    assert context["trend_stack"] == "bullish"
    assert context["rating_bucket"] == "elite"
    assert context["momentum_profile"] == "bullish|bullish|positive"
    assert context["risk_profile"] == "normal|strong|elite"
    assert context["setup_signature"] == "TRENDING_UP|bullish|bullish|positive|elite"


def test_learning_penalty_delta_penalizes_losses_and_forgives_wins():
    service = LearningService()

    assert service._penalty_delta(-10) > 0
    assert service._penalty_delta(10) < 0


def test_learning_service_has_bounded_penalty_policy():
    service = LearningService()

    assert service.max_penalty == 5.0
    assert service.block_threshold > service.warn_threshold


def test_learning_confidence_requires_multiple_observations():
    service = LearningService()

    assert service.rule_confidence(0) == 0
    assert service.rule_confidence(1) == 0.5
    assert service.rule_confidence(2) == 1.0


def test_learning_risk_level_uses_effective_penalty():
    service = LearningService()

    assert service.risk_level(3.0, 1) == "WARN"
    assert service.risk_level(3.0, 2) == "BLOCK"


def test_learning_confidence_decays_with_age():
    service = LearningService()
    old_timestamp = datetime.now(timezone.utc) - timedelta(days=service.half_life_days)

    assert 0.49 <= service.recency_weight(old_timestamp) <= 0.51
    assert 0.49 <= service.rule_confidence(2, old_timestamp) <= 0.51


def test_learning_weights_full_setup_more_than_single_bucket():
    service = LearningService()
    base = learning_rule("rsi_bucket", penalty=1.0)
    setup = learning_rule("setup_signature", penalty=1.0)

    assert service.effective_penalty(setup, "GLOBAL") > service.effective_penalty(base, "GLOBAL")


def test_learning_risk_multiplier_scales_between_warn_and_block():
    service = LearningService()

    assert service.risk_multiplier_for_penalty(service.warn_threshold - 0.01) == 1.0
    assert service.min_risk_multiplier <= service.risk_multiplier_for_penalty(service.block_threshold) < 1.0
    assert service.risk_multiplier_for_penalty(service.warn_threshold) > service.risk_multiplier_for_penalty(service.block_threshold)


def test_learning_block_requires_repeated_specific_losing_setup():
    service = LearningService()
    broad_rule = learning_rule("rsi_bucket", observations=5, losses=5)
    first_setup_loss = learning_rule("setup_signature", observations=1, losses=1)
    repeated_setup_loss = learning_rule("setup_signature", observations=2, losses=2)

    assert not service._has_block_evidence([(broad_rule, 3.0, "broad")])
    assert not service._has_block_evidence([(first_setup_loss, 3.0, "first")])
    assert service._has_block_evidence([(repeated_setup_loss, 3.0, "repeated")])


def test_learning_insights_explain_protective_and_favorable_patterns():
    service = LearningService()
    losing = learning_rule("setup_signature", penalty=3.0, observations=4, wins=0, losses=4)
    winning = learning_rule("trend_stack", penalty=0.0, observations=4, wins=3, losses=1)
    winning.total_profit = 18

    result = service.build_insights([losing, winning], learned_from_trades=4)

    assert result.learned_from_trades == 4
    assert result.strong_patterns == 2
    assert result.protective_patterns == 1
    assert result.favorable_patterns == 1
    assert result.insights[0].impact == "AVOID"
    assert any(item.impact == "PREFER" for item in result.insights)


def learning_rule(
    feature_key: str,
    penalty: float = 1.0,
    observations: int = 2,
    wins: int = 0,
    losses: int = 2,
) -> LearningRule:
    return LearningRule(
        scope="GLOBAL",
        side="LONG",
        feature_key=feature_key,
        feature_value="test",
        penalty=penalty,
        observations=observations,
        wins=wins,
        losses=losses,
        total_profit=-10.0,
    )
