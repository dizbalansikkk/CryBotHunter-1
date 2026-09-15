from datetime import datetime, timedelta, timezone

from app.services.heartbeat import heartbeat_transition, worker_is_healthy, worker_stale_seconds


def test_heartbeat_transition_alerts_once_and_reports_recovery():
    now = datetime.now(timezone.utc)
    old = now - timedelta(seconds=181)
    fresh = now - timedelta(seconds=10)

    assert heartbeat_transition(last_seen=old, stale_alerted=False, now=now, stale_seconds=180) == "STALE"
    assert heartbeat_transition(last_seen=old, stale_alerted=True, now=now, stale_seconds=180) is None
    assert heartbeat_transition(last_seen=fresh, stale_alerted=True, now=now, stale_seconds=180) == "RECOVERED"
    assert heartbeat_transition(last_seen=fresh, stale_alerted=False, now=now, stale_seconds=180) is None


def test_worker_stale_limit_respects_startup_and_training_grace():
    assert worker_stale_seconds(base_seconds=180, status="STARTING") == 600
    assert worker_stale_seconds(base_seconds=180, status="TRAINING") == 900
    assert worker_stale_seconds(
        base_seconds=180,
        status="RUNNING",
        detail={"long_running": True, "stale_after_seconds": 1200},
    ) == 1200
    assert worker_stale_seconds(
        base_seconds=180,
        status="RUNNING",
        detail={"stale_after_seconds": 99999},
    ) == 3600


def test_worker_health_treats_fresh_training_as_operational_but_errors_as_unhealthy():
    assert worker_is_healthy(
        status="TRAINING",
        age_seconds=400,
        base_seconds=180,
        detail={"long_running": True},
    )
    assert not worker_is_healthy(
        status="ERROR",
        age_seconds=5,
        base_seconds=180,
    )
