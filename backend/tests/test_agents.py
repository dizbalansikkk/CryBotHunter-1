from types import SimpleNamespace

import pytest

from app.schemas.dto import AgentDecisionOut, MarketCoin
from app.services.advanced_agents import (
    CalibrationDriftAgent,
    CrossTimeframeAgent,
    EventRiskAgent,
    ExecutionCostAgent,
    PortfolioCorrelationAgent,
)
from app.services.agent_competition import CompetitionProfile
from app.services.agents import (
    AgentOrchestrator,
    DataQualityAgent,
    EntryTimingAgent,
    LiquidityAgent,
    MarketAnalystAgent,
    MicrostructureAgent,
    RegimeAgent,
    RiskSupervisorAgent,
    TrendAgent,
    VolatilityAgent,
)


def coin(**overrides):
    data = {
        "symbol": "BTC/USDT",
        "price": 110,
        "volume_24h": 2_000_000_000,
        "volume_average_24h": 1_500_000_000,
        "price_change_percent": 4,
        "atr": 3,
        "rsi": 62,
        "ema20": 105,
        "ema50": 100,
        "ema200": 90,
        "macd": 10,
        "funding_rate": 0.01,
        "open_interest": 1_500_000_000,
        "bid": 109.95,
        "ask": 110.05,
        "spread_bps": 9.09,
        "rating": 88,
        "regime": "TRENDING_UP",
        "regime_score": 82,
        "regime_reason": "test trend",
    }
    data.update(overrides)
    return MarketCoin(**data)


def test_market_agent_returns_structured_decision():
    decision = MarketAnalystAgent().decide(coin())
    assert decision.agent_name == "MarketAnalystAgent"
    assert decision.symbol == "BTC/USDT"
    assert decision.action in {"BUY", "SELL", "WAIT"}
    assert 0 <= decision.confidence <= 1


def test_market_agent_defers_when_probability_model_strongly_disagrees(monkeypatch):
    monkeypatch.setattr(
        "app.services.agents.MlSignalService.predict",
        lambda _self, market: SimpleNamespace(
            symbol=market.symbol,
            long_probability=5,
            short_probability=95,
        ),
    )

    decision = MarketAnalystAgent().decide(coin(price=109, bid=108.95, ask=109.05))

    assert decision.action == "WAIT"
    assert decision.context["model_conflict"] is True
    assert decision.confidence <= 0.55


@pytest.mark.asyncio
async def test_risk_agent_blocks_low_confidence(monkeypatch):
    class Result:
        def scalar_one(self):
            return 0

    class Db:
        async def execute(self, *_args, **_kwargs):
            return Result()

    market = AgentDecisionOut(
        agent_name="MarketAnalystAgent",
        symbol="BTC/USDT",
        action="BUY",
        confidence=0.4,
        rationale="low confidence",
    )
    decision = await RiskSupervisorAgent().decide(Db(), market)
    assert decision.action == "BLOCK"


@pytest.mark.asyncio
async def test_risk_agent_exposes_real_size_reduction_multiplier():
    class Result:
        def __init__(self, value):
            self.value = value

        def scalar_one(self):
            return self.value

    class Db:
        def __init__(self):
            self.values = iter((5, 0))

        async def execute(self, *_args, **_kwargs):
            return Result(next(self.values))

    market = AgentDecisionOut(
        agent_name="TradeCommittee",
        symbol="BTC/USDT",
        action="BUY",
        confidence=0.9,
        rationale="approved",
    )

    decision = await RiskSupervisorAgent().decide(Db(), market)

    assert decision.action == "REDUCE_SIZE"
    assert decision.context["risk_multiplier"] == 0.5


def test_orchestrator_forces_wait_on_agent_disagreement():
    market = AgentDecisionOut(
        agent_name="MarketAnalystAgent",
        symbol="BTC/USDT",
        action="BUY",
        confidence=0.9,
        rationale="local buy",
    )
    llm = AgentDecisionOut(
        agent_name="LlmAdvisorAgent",
        symbol="BTC/USDT",
        action="SELL",
        confidence=0.9,
        rationale="llm sell",
    )
    combined = AgentOrchestrator()._combine(market, llm)
    assert combined.action == "WAIT"
    assert combined.confidence <= 0.55


def test_trade_committee_approves_strong_consensus():
    market = AgentDecisionOut(
        agent_name="MarketAnalystAgent",
        symbol="BTC/USDT",
        action="BUY",
        confidence=0.88,
        rationale="local buy",
    )
    committee = [
        RegimeAgent().decide(coin()),
        TrendAgent().decide(coin()),
        AgentDecisionOut(agent_name="MomentumAgent", symbol="BTC/USDT", action="BUY", confidence=0.8, rationale="momentum buy"),
        LiquidityAgent().decide(coin()),
        VolatilityAgent().decide(coin()),
    ]

    decision, consensus = AgentOrchestrator()._committee_consensus(market, None, committee)

    assert decision.action == "BUY"
    assert consensus >= 0.66


def test_regime_agent_blocks_bad_regime():
    decision = RegimeAgent().decide(coin(regime="HIGH_VOLATILITY", regime_score=25, regime_reason="too hot"))
    assert decision.action == "BLOCK"
    assert decision.confidence >= 0.9


def test_trade_committee_veto_blocks_thin_liquidity():
    market = AgentDecisionOut(
        agent_name="MarketAnalystAgent",
        symbol="BTC/USDT",
        action="BUY",
        confidence=0.88,
        rationale="local buy",
    )
    committee = [
        TrendAgent().decide(coin()),
        LiquidityAgent().decide(coin(volume_24h=10_000, open_interest=10_000)),
        VolatilityAgent().decide(coin()),
    ]

    decision, consensus = AgentOrchestrator()._committee_consensus(market, None, committee)

    assert decision.action == "WAIT"
    assert consensus == 0


def test_liquidity_agent_accepts_liquid_spot_market_without_open_interest():
    decision = LiquidityAgent().decide(coin(open_interest=0))

    assert decision.action == "ALLOW"
    assert "not available for spot" in decision.rationale


def test_shadow_challenger_cannot_overrule_current_champion():
    orchestrator = AgentOrchestrator()
    market = AgentDecisionOut(
        agent_name="MarketAnalystAgent", symbol="BTC/USDT", action="BUY", confidence=0.9, rationale="buy"
    )
    champion = AgentDecisionOut(
        agent_name="TrendAgent", symbol="BTC/USDT", action="BUY", confidence=0.8, rationale="buy"
    )
    challenger = AgentDecisionOut(
        agent_name="AdaptiveTrendAgent", symbol="BTC/USDT", action="SELL", confidence=0.99, rationale="sell"
    )
    profiles = {
        "TrendAgent": CompetitionProfile("TrendAgent", "TREND", "CHAMPION", rating=0.7),
        "AdaptiveTrendAgent": CompetitionProfile("AdaptiveTrendAgent", "TREND", "CHALLENGER", rating=0.9),
    }

    decision, consensus = orchestrator._committee_consensus(
        market,
        None,
        [
            orchestrator._competition_context(champion, profiles),
            orchestrator._competition_context(challenger, profiles),
        ],
    )

    assert decision.action == "BUY"
    assert consensus == 1
    assert challenger.context["vote_weight"] == 0


def test_agent_explanations_are_russian():
    decision = MarketAnalystAgent().decide(coin())
    assert "Структура рынка" in decision.rationale


def test_data_quality_agent_vetoes_inverted_order_book():
    decision = DataQualityAgent().decide(coin(bid=111, ask=110))

    assert decision.action == "BLOCK"
    assert "invalid_best_bid_ask" in decision.context["problems"]


def test_entry_timing_agent_vetoes_chasing_extended_move():
    decision = EntryTimingAgent().decide(coin(price=130, ema20=110, atr=2))

    assert decision.action == "BLOCK"
    assert decision.context["distance_from_ema20_atr"] > 1.5


def test_microstructure_agent_vetoes_two_strong_opposing_sources():
    decision = MicrostructureAgent().decide(
        coin(),
        "BUY",
        {
            "order_book_available": True,
            "order_book_imbalance": -0.4,
            "tape_available": True,
            "trade_flow_imbalance": -0.3,
            "momentum_available": True,
            "price_change_10m_percent": 0.1,
        },
    )

    assert decision.action == "BLOCK"
    assert decision.context["available_sources"] == 3


def test_committee_requires_two_independent_directional_families():
    orchestrator = AgentOrchestrator()
    market = AgentDecisionOut(
        agent_name="MarketAnalystAgent",
        symbol="BTC/USDT",
        action="BUY",
        confidence=0.9,
        rationale="buy",
        context={"vote_family": "TREND"},
    )
    duplicate = AgentDecisionOut(
        agent_name="TrendCloneAgent",
        symbol="BTC/USDT",
        action="BUY",
        confidence=0.95,
        rationale="same evidence",
        context={"vote_family": "TREND"},
    )

    decision, consensus = orchestrator._committee_consensus(market, None, [duplicate])

    assert consensus == 1
    assert decision.action == "WAIT"
    assert decision.context["agreeing_families"] == ["TREND"]


def test_portfolio_correlation_blocks_concentrated_same_direction_cluster():
    candidate_returns = {index: index / 10_000 for index in range(40)}
    series = {
        "BTC/USDT": candidate_returns,
        "ETH/USDT": candidate_returns,
        "SOL/USDT": candidate_returns,
    }
    positions = [
        SimpleNamespace(symbol="ETH/USDT", side="LONG", current_price=100, entry_price=90, volume=10),
        SimpleNamespace(symbol="SOL/USDT", side="LONG", current_price=50, entry_price=45, volume=20),
    ]

    decision = PortfolioCorrelationAgent().evaluate(coin(), "BUY", positions, series)

    assert decision.action == "BLOCK"
    assert decision.context["clustered_positions"] == 2


def test_execution_cost_agent_blocks_when_friction_consumes_expected_move():
    decision = ExecutionCostAgent().decide(
        coin(price=100, atr=0.2, spread_bps=70),
        {"spread_bps": 70, "bid_depth_quote": 10_000, "ask_depth_quote": 10_000},
        candidate_notional=2_000,
    )

    assert decision.action == "BLOCK"
    assert decision.context["total_cost_bps"] > 60


def test_cross_timeframe_agent_blocks_two_opposing_structures():
    falling = [[index, 0, 0, 0, 200 - index, 0] for index in range(70)]
    rising = [[index, 0, 0, 0, 100 + index, 0] for index in range(70)]

    blocked = CrossTimeframeAgent().decide(
        coin(),
        "BUY",
        {"15m": falling, "1h": falling, "4h": rising},
    )

    assert blocked.action == "BLOCK"
    assert blocked.context["opposing_timeframes"] == 2


def test_calibration_drift_blocks_sustained_recent_degradation():
    outcomes = [(0.9, 0.0)] * 10 + [(0.8, 1.0)] * 10

    decision = CalibrationDriftAgent().evaluate(coin(), outcomes)

    assert decision.action == "BLOCK"
    assert decision.context["recent_win_rate"] == 0
    assert decision.context["drift"] == 1


def test_event_risk_never_changes_risk_from_launchpool_alone():
    decision = EventRiskAgent().decide(
        coin(price_change_percent=2, volume_24h=1_600_000_000),
        {"status": "ACTIVE", "event_score": 15},
    )

    assert decision.action == "ALLOW"
    assert decision.context["risk_multiplier"] == 1


def test_event_risk_blocks_confirmed_market_shock_with_extreme_funding():
    decision = EventRiskAgent().decide(
        coin(funding_rate=0.1, price_change_percent=14),
        {"status": "NORMAL", "event_score": 0},
    )

    assert decision.action == "BLOCK"


def test_orchestrator_applies_strictest_advanced_agent_risk_reduction():
    risk = AgentDecisionOut(
        agent_name="RiskSupervisorAgent",
        symbol="BTC/USDT",
        action="ALLOW",
        confidence=0.9,
        rationale="allowed",
        context={"risk_multiplier": 1.0},
    )
    reducers = [
        AgentDecisionOut(
            agent_name="ExecutionCostAgent",
            symbol="BTC/USDT",
            action="REDUCE_SIZE",
            confidence=0.8,
            rationale="cost",
            context={"risk_multiplier": 0.65},
        ),
        AgentDecisionOut(
            agent_name="CalibrationDriftAgent",
            symbol="BTC/USDT",
            action="REDUCE_SIZE",
            confidence=0.75,
            rationale="drift",
            context={"risk_multiplier": 0.55},
        ),
    ]

    result = AgentOrchestrator()._apply_committee_risk_reduction(risk, reducers)

    assert result.action == "REDUCE_SIZE"
    assert result.context["risk_multiplier"] == 0.55
