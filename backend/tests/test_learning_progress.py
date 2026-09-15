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
    assert service.normalize_blocker("Skipped ETH/USDT: strategy WAIT score=62, rating=70") == "STRATEGY_WAIT"
    assert service.normalize_blocker("Skipped SOL/USDT: micro gate blocked strong opposing flow") == "MICROSTRUCTURE"
    assert service.normalize_blocker("Skipped AAVE/USDT: committee rejected: consensus=0.67") == "COMMITTEE"


def test_rl_fleet_separates_pair_coverage_from_experiment_history():
    fleet = LearningProgressService().build_rl_fleet(
        status_counts={"ACTIVE": 5, "SHADOW": 2, "REJECTED": 500, "RETIRED": 5},
        active_symbols={"BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT"},
        target_symbols=[
            "BTC/USDT",
            "ETH/USDT",
            "BNB/USDT",
            "SOL/USDT",
            "XRP/USDT",
            "ADA/USDT",
            "DOGE/USDT",
            "LINK/USDT",
            "AVAX/USDT",
            "DOT/USDT",
            "LTC/USDT",
            "TRX/USDT",
        ],
        target_timeframes=["1h"],
        decision_counts={"rl_policy": 25, "rl_shadow": 8},
    )

    assert fleet.active_pairs == 5
    assert fleet.target_pairs == 12
    assert fleet.target_models == 12
    assert fleet.total_experiments == 512
    assert fleet.promoted_experiments == 10
    assert fleet.promotion_rate_percent == 1.95
    assert fleet.active_decisions_24h == 25
    assert fleet.shadow_decisions_24h == 8
    assert fleet.uncovered_pairs == [
        "ADA/USDT",
        "DOGE/USDT",
        "LINK/USDT",
        "AVAX/USDT",
        "DOT/USDT",
        "LTC/USDT",
        "TRX/USDT",
    ]


def test_rl_fleet_active_model_count_is_scoped_to_current_universe():
    fleet = LearningProgressService().build_rl_fleet(
        status_counts={"ACTIVE": 7, "RETIRED": 3, "REJECTED": 90},
        active_symbols={"ETH/USDT", "SOL/USDT"},
        target_symbols=["ETH/USDT", "SOL/USDT"],
        target_timeframes=["1h"],
        active_model_count=2,
    )

    assert fleet.active_models == 2
    assert fleet.active_pairs == 2
    assert fleet.total_experiments == 100
    assert fleet.promoted_experiments == 10


def test_rl_milestone_uses_rl_target_not_candle_target():
    milestones = LearningProgressService().build_milestones(
        closed_trades=30,
        learning_observations=100,
        active_rl_pairs=5,
        candle_pairs_ready=20,
        candle_pairs_total=20,
        rl_pairs_total=12,
        trade_target=30,
        observation_target=100,
    )

    assert milestones[-1].current == 5
    assert milestones[-1].target == 12
    assert milestones[-1].progress_percent == 41.67
