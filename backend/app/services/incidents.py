import time


class IncidentTracker:
    """First occurrence, hourly reminder, and one recovery per incident."""
    def __init__(self, interval=3600, clock=time.monotonic):
        self.interval = interval
        self.clock = clock
        self.active = False
        self.last = {}

    def should_report(self, kind):
        self.active = True
        now = self.clock()
        if kind in self.last and now - self.last[kind] < self.interval:
            return False
        self.last[kind] = now
        return True

    def recover(self):
        was_active = self.active
        self.active = False
        self.last.clear()
        return was_active
