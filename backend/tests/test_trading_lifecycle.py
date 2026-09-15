from types import SimpleNamespace

import pytest

from app.models.entities import Position
from app.schemas.dto import AgentAnalysisOut, AgentDecisionOut, MarketCoin, StrategySignal
from app.services.performance_guard import PerformanceGuardReport
from app.services.risk_manager import RiskSettings
from app.services.trading_engine import TradingEngine


def analysis(action: str, approved: bool = True, consensus_score: float = 0.8) -> AgentAnalysisOut:
    decision = AgentDecisionOut(
        agent_name="TradeCommittee",
        symbol="BTC/USDT",
        action=action,
        confidence=0.8,
        rationale="test",
    )
    risk = AgentDecisionOut(
        agent_name="RiskSupervisorAgent",
        symbol="BTC/USDT",
        action="ALLOW" if approved else "BLOCK",
        confidence=0.8,
        rationale="test",
    )
    return AgentAnalysisOut(
        symbol="BTC/USDT",
        market=decision,
        risk=risk,
        committee=[],
        consensus_score=consensus_score,
        final_action=action,
        final_confidence=0.8,
        approved=approved,
    )


def coin() -> MarketCoin:
    return MarketCoin(
        symbol="BTC/USDT",
        price=100,
        volume_24h=1_000_000_000,
        price_change_percent=2,
        atr=2,
        rsi=62,
        ema20=105,
        ema50=100,
        ema200=90,
        macd=1,
        funding_rate=0.01,
        open_interest=1_000_000_000,
        rating=90,
    )


def risk_settings() -> RiskSettings:
    return RiskSettings(
        balance=1000,
        risk_percent=1,
        daily_risk_percent=3,
        max_positions=3,
        min_rating=80,
        stop_loss_percent=1,
        take_profit_percent=3,
        trailing_stop_percent=0.8,
        atr_stop_multiplier=1.5,
        risk_reward_ratio=2,
        breakeven_trigger_r=1,
        breakeven_offset_percent=0.05,
        partial_take_profit_r=1,
        partial_close_percent=50,
    )


def test_long_position_pnl_and_take_profit_exit():
    position = Position(
        symbol="BTC/USDT",
        side="LONG",
        entry_price=100,
        current_price=106,
        volume=2,
        stop=98,
        take=105,
        trailing_stop_percent=0.8,
        highest_price=106,
        lowest_price=100,
    )
    engine = TradingEngine()
    assert engine._pnl(position, 106) == 12
    assert engine._exit_reason(position) == "TAKE_PROFIT"


def test_short_position_pnl_and_stop_loss_exit():
    position = Position(
        symbol="ETH/USDT",
        side="SHORT",
        entry_price=100,
        current_price=103,
        volume=3,
        stop=102,
        take=95,
        trailing_stop_percent=0.8,
        highest_price=103,
        lowest_price=99,
    )
    engine = TradingEngine()
    assert engine._pnl(position, 103) == -9
    assert engine._exit_reason(position) == "STOP_LOSS"


def test_trailing_stop_moves_only_in_favorable_direction():
    position = Position(
        symbol="SOL/USDT",
        side="LONG",
        entry_price=100,
        current_price=110,
        volume=1,
        stop=98,
        take=120,
        trailing_stop_percent=2,
        highest_price=110,
        lowest_price=100,
    )
    TradingEngine()._apply_trailing_stop(position)
    assert position.stop == 107.8


def test_committee_gate_allows_matching_high_consensus_signal():
    assert TradingEngine()._committee_allows_signal(analysis("BUY"), "BUY")


def test_committee_gate_rejects_mismatch_or_low_consensus():
    engine = TradingEngine()
    assert not engine._committee_allows_signal(analysis("SELL"), "BUY")
    assert not engine._committee_allows_signal(analysis("BUY", consensus_score=0.4), "BUY")


def test_opportunity_ranking_prefers_tradeable_high_confidence_signal():
    engine = TradingEngine()
    buy = StrategySignal(symbol="BTC/USDT", signal="BUY", score=82, reasons=[])
    wait = StrategySignal(symbol="BTC/USDT", signal="WAIT", score=99, reasons=[])

    assert engine._opportunity_rank(coin(), buy) > engine._opportunity_rank(coin(), wait)


def test_exposure_gate_rejects_overloaded_portfolio():
    engine = TradingEngine()
    accepted, reason, candidate = engine._exposure_gate(
        coin(),
        "BUY",
        balance=1000,
        settings=risk_settings(),
        exposure={"gross": 2950, "symbols": {}},
    )
    assert accepted is False
    assert reason == "gross exposure limit reached"
    assert candidate > 0


def test_exit_plan_uses_atr_risk_when_larger_than_percent_stop():
    stop, take, initial_risk = TradingEngine()._exit_plan(
        entry_price=100,
        atr=3,
        side="LONG",
        settings=risk_settings(),
    )

    assert initial_risk == 4.5
    assert stop == 95.5
    assert take == 109


def test_breakeven_moves_long_stop_after_trigger():
    position = Position(
        symbol="BTC/USDT",
        side="LONG",
        entry_price=100,
        current_price=105,
        volume=1,
        stop=95,
        take=110,
        initial_risk=4,
        breakeven_applied=False,
        breakeven_trigger_r=1,
        breakeven_offset_percent=0.05,
        partial_take_profit_r=1,
        partial_close_percent=50,
        partial_taken=False,
        trailing_stop_percent=0.8,
        highest_price=105,
        lowest_price=100,
    )

    applied = TradingEngine()._apply_breakeven(position)

    assert applied is True
    assert position.breakeven_applied is True
    assert position.stop == 100.05


def test_partial_take_profit_reaches_trigger_and_sizes_close():
    position = Position(
        symbol="BTC/USDT",
        side="LONG",
        entry_price=100,
        current_price=104,
        volume=2,
        stop=96,
        take=110,
        initial_risk=4,
        partial_take_profit_r=1,
        partial_close_percent=50,
        partial_taken=False,
        trailing_stop_percent=0.8,
        highest_price=104,
        lowest_price=100,
    )
    engine = TradingEngine()

    assert engine._partial_take_profit_reached(position, 104)
    assert engine._partial_close_volume(position) == 1
    assert engine._profit_for_volume(position, 104, 1) == 4


def test_total_closed_profit_includes_partial_profit_and_entry_fee():
    position = Position(
        symbol="BTC/USDT",
        side="LONG",
        entry_price=100,
        current_price=106,
        volume=1,
        stop=96,
        take=110,
        initial_risk=4,
        partial_take_profit_r=1,
        partial_close_percent=50,
        partial_taken=True,
        trailing_stop_percent=0.8,
        highest_price=106,
        lowest_price=100,
    )
    engine = TradingEngine()

    final_trade_profit = engine._final_trade_profit(
        existing_trade_profit=-0.08,
        position=position,
        exit_price=106,
        exit_fee=0.06,
    )
    total_profit = engine._total_closed_profit(previous_realized=3.95, final_trade_profit=final_trade_profit)

    assert final_trade_profit == 5.86
    assert total_profit == 9.81


def test_trading_engine_rebases_risk_settings_to_actual_balance():
    engine = TradingEngine()

    updated = engine._settings_with_balance(risk_settings(), 2500)

    assert updated.balance == 2500


def test_performance_guard_recovery_reduces_risk_and_caps_open_positions():
    engine = TradingEngine()
    engine.settings = SimpleNamespace(guard_recovery_max_positions=1)
    guard = PerformanceGuardReport(
        allowed=True,
        reason="recovery probe",
        trades_checked=5,
        win_rate=20,
        loss_streak=3,
        total_profit=-10,
        recovery_mode=True,
        risk_multiplier=0.25,
    )

    updated = engine._guard_recovery_settings(risk_settings(), guard)

    assert updated.risk_percent == 0.25
    assert engine._guard_recovery_position_limit_reached(guard, open_count=0) is False
    assert engine._guard_recovery_position_limit_reached(guard, open_count=1) is True


def test_paper_exploration_converts_strong_wait_to_small_test_direction():
    engine = TradingEngine()
    engine.settings = SimpleNamespace(
        paper_trading=True,
        paper_exploration_enabled=True,
        paper_exploration_min_score=65,
        paper_exploration_min_directional_votes=5,
        paper_exploration_min_vote_margin=2,
    )
    wait = StrategySignal(
        symbol="BTC/USDT",
        signal="WAIT",
        score=73,
        reasons=["long missing: volume above average"],
    )

    signal, exploration = engine._paper_exploration_signal(coin(), wait)

    assert exploration is True
    assert signal.signal == "BUY"
    assert signal.score == 73
    assert "paper exploration from WAIT" in signal.reasons[0]


def test_paper_exploration_never_overrides_hard_market_block_or_live_mode():
    engine = TradingEngine()
    engine.settings = SimpleNamespace(
        paper_trading=True,
        paper_exploration_enabled=True,
        paper_exploration_min_score=65,
        paper_exploration_min_directional_votes=5,
        paper_exploration_min_vote_margin=2,
    )
    wait = StrategySignal(
        symbol="BTC/USDT",
        signal="WAIT",
        score=80,
        reasons=["volatility too high for safe entry: ATR 9.00%"],
    )

    signal, exploration = engine._paper_exploration_signal(coin(), wait)
    assert signal.signal == "WAIT"
    assert exploration is False

    engine.settings.paper_trading = False
    safe_wait = wait.model_copy(update={"reasons": ["long missing: MACD positive"]})
    signal, exploration = engine._paper_exploration_signal(coin(), safe_wait)
    assert signal.signal == "WAIT"
    assert exploration is False


def test_paper_exploration_requires_decisive_indicator_vote():
    engine = TradingEngine()
    engine.settings = SimpleNamespace(
        paper_trading=True,
        paper_exploration_enabled=True,
        paper_exploration_min_score=65,
        paper_exploration_min_directional_votes=5,
        paper_exploration_min_vote_margin=2,
    )
    mixed_coin = coin().model_copy(
        update={
            "regime": "RANGING",
            "ema20": 101,
            "ema50": 100,
            "ema200": 102,
            "price": 100,
            "rsi": 50,
            "macd": -1,
            "price_change_percent": 1,
        }
    )
    wait = StrategySignal(symbol="BTC/USDT", signal="WAIT", score=75, reasons=["mixed setup"])

    signal, exploration = engine._paper_exploration_signal(mixed_coin, wait)

    assert signal.signal == "WAIT"
    assert exploration is False


def test_strategy_wait_reason_exposes_score_votes_and_missing_rules():
    engine = TradingEngine()
    wait = StrategySignal(
        symbol="ETH/USDT",
        signal="WAIT",
        score=62,
        reasons=["long missing: MACD positive", "long missing: price above EMA20"],
    )

    test_coin = coin().model_copy(update={"symbol": "ETH/USDT", "rating": 71})
    reason = engine._strategy_wait_reason(test_coin, wait)

    assert "score=62" in reason
    assert "rating=71" in reason
    assert "bullish_votes=" in reason
    assert "long missing: MACD positive" in reason

def test_paper_exploration_uses_its_own_same_side_limit_only_in_paper_mode():
    engine = TradingEngine()
    engine.settings = SimpleNamespace(
        paper_trading=True,
        max_same_side_positions=2,
        paper_exploration_max_positions=5,
    )

    assert engine._same_side_position_limit(paper_exploration=True) == 5
    assert engine._same_side_position_limit(paper_exploration=False) == 2

    engine.settings.paper_trading = False
    assert engine._same_side_position_limit(paper_exploration=True) == 2


def test_paper_exploration_has_independent_recovery_slots():
    engine = TradingEngine()
    engine.settings = SimpleNamespace(
        paper_exploration_max_positions=5,
        paper_exploration_recovery_slots=2,
    )
    recovery = PerformanceGuardReport(
        allowed=True,
        reason="recovery",
        trades_checked=5,
        win_rate=20,
        loss_streak=3,
        total_profit=-10,
        recovery_mode=True,
    )
    normal = PerformanceGuardReport(
        allowed=True,
        reason="passed",
        trades_checked=5,
        win_rate=60,
        loss_streak=0,
        total_profit=10,
    )

    assert engine._paper_exploration_position_limit(recovery) == 2
    assert engine._paper_exploration_position_limit(normal) == 5


def test_paper_exploration_enforces_absolute_micro_risk_cap():
    engine = TradingEngine()
    engine.settings = SimpleNamespace(
        paper_exploration_risk_percent=0.25,
        paper_exploration_max_risk_percent=0.15,
        paper_exploration_min_score=65,
    )

    updated = engine._paper_exploration_settings(risk_settings())

    assert updated.risk_percent == 0.15
    assert updated.min_rating == 65


@pytest.mark.asyncio
async def test_drawdown_limit_activates_only_close_and_critical_notification():
    class Result:
        def __init__(self, values=None, scalar=None):
            self.values = values or []
            self.scalar = scalar

        def scalars(self):
            return self

        def all(self):
            return self.values

        def scalar_one(self):
            return self.scalar

    class Db:
        def __init__(self):
            self.results = [Result(values=[100, -20]), Result(scalar=-40)]
            self.added = []

        async def execute(self, _statement):
            return self.results.pop(0)

        def add(self, value):
            self.added.append(value)

    class Control:
        def __init__(self):
            self.reason = None

        async def is_paused(self):
            return False, None

        async def panic(self, reason):
            self.reason = reason
            return True

    class Telegram:
        def __init__(self):
            self.messages = []

        async def broadcast(self, message):
            self.messages.append(message)
            return 1

    engine = TradingEngine()
    engine.settings = SimpleNamespace(max_drawdown_percent=5)
    engine.control = Control()
    engine.telegram = Telegram()
    db = Db()

    assessment = await engine._enforce_drawdown_limit(db, balance=1000)

    assert assessment.emergency is True
    assert engine.control.reason.startswith("risk_drawdown:5.45%")
    assert "ONLY CLOSE" in engine.telegram.messages[0]
    assert db.added[0].level == "CRITICAL"
