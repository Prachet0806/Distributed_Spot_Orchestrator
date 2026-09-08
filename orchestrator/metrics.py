from threading import Lock


class MetricsRegistry:
    def __init__(self):
        self._lock = Lock()
        self._counters = {}
        self._gauges = {}
        self._summaries = {}

    def inc(self, name, value=1):
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + value

    def get_counter(self, name):
        with self._lock:
            return self._counters.get(name, 0)

    def set_gauge(self, name, value):
        with self._lock:
            self._gauges[name] = value

    def observe(self, name, value):
        with self._lock:
            count, total = self._summaries.get(name, (0, 0.0))
            self._summaries[name] = (count + 1, total + float(value))

    def render_prometheus(self):
        with self._lock:
            lines = []
            for name, value in self._counters.items():
                lines.append(f"{name} {value}")
            for name, value in self._gauges.items():
                lines.append(f"{name} {value}")
            for name, (count, total) in self._summaries.items():
                lines.append(f"{name}_count {count}")
                lines.append(f"{name}_sum {total}")
            return "\n".join(lines) + "\n"


_GLOBAL_METRICS = MetricsRegistry()


def get_metrics():
    return _GLOBAL_METRICS
