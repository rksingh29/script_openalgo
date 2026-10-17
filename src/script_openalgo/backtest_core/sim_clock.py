from datetime import datetime, timedelta


class SimClock:
    """Simulated time. now() returns a real datetime on the sim date; sleep() just advances it."""

    def __init__(self, day, start="09:00", hard_stop="16:00"):
        h, m = map(int, start.split(":"))
        self._t = datetime(day.year, day.month, day.day, h, m)
        h, m = map(int, hard_stop.split(":"))
        self._stop = datetime(day.year, day.month, day.day, h, m)

    def now(self):
        return self._t

    def sleep(self, seconds):
        self._t += timedelta(seconds=max(0.0, seconds))
        if self._t > self._stop:
            raise RuntimeError(f"simulation ran past {self._stop:%H:%M}; strategy did not finish")
