from app.services.learning_progress import LearningProgressService


def test_learning_progress_milestones_are_bounded_and_actionable():
    service = LearningProgressService()

    milestones = service.build_milestones(
        closed_trades=12,
        learning_observations=140,
        active_rl_pairs=2,
        candle_pairs_ready=12,
        candle_pairs_total=12,
        trade_target=30,
        observation_target=100,
    )

    assert [item.key for item in milestones] == [
        "candle_coverage",
        "trade_lessons",
        "memory_observations",
        "active_rl_pairs",
    ]
    assert milestones[0].progress_percent == 100
    assert milestones[1].progress_percent == 40
    assert milestones[2].progress_percent == 100
    assert milestones[3].progress_percent == 16.67


def test_learning_progress_explains_common_trade_blockers():
    service = LearningProgressService()

    assert service.normalize_blocker("Skipped SOL/USDT: performance guard recovery position limit reached") == "RECOVERY_POSITION_LIMIT"
    assert service.normalize_blocker("Skipped ETH/USDT: pre-trade quality blocked: too few trades") == "PRETRADE_QUALITY"
    assert service.normalize_blocker("Skipped XRP/USDT: RL disagrees: strategy=BUY") == "RL_DISAGREEMENT"
