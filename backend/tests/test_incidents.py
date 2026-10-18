from app.services.incidents import IncidentTracker


def test_first_error_hourly_reminder_and_single_recovery():
    now = [0]
    tracker = IncidentTracker(clock=lambda: now[0])
    assert tracker.should_report("network")
    now[0] = 300
    assert not tracker.should_report("network")
    assert tracker.should_report("database")
    now[0] = 3600
    assert tracker.should_report("network")
    assert tracker.recover()
    assert not tracker.recover()
    assert tracker.should_report("network")
