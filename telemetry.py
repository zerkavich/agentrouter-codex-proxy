"""Fixed-size timing aggregates; no requests, headers or credentials are retained."""
from __future__ import annotations

import asyncio
from bisect import bisect_left
import inspect
import math
import time

STAGES = (
    "client_upload", "payload_inspect", "payload_prepare", "rate_limit_gate", "retry_backoff",
    "upstream_admission", "upstream_connect", "upstream_tls", "upstream_upload",
    "upstream_headers", "upstream_body", "sse_prime", "response_ready", "sse_delivery",
)
BOUNDS = (
    0.00025, 0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25,
    0.5, 1, 2.5, 5, 10, 20, 45, 90, 300, 600, math.inf,
)
TRACE_STAGES = {
    "connection.connect_tcp": "upstream_connect",
    "connection.start_tls": "upstream_tls",
    "http11.send_request_body": "upstream_upload",
    "http11.receive_response_headers": "upstream_headers",
}


class TimingHistogram:
    __slots__ = ("count", "total", "maximum", "active", "failed", "cancelled", "bins")

    def __init__(self):
        self.count = 0
        self.total = 0.0
        self.maximum = 0.0
        self.active = 0
        self.failed = 0
        self.cancelled = 0
        self.bins = [0] * len(BOUNDS)

    def record(self, elapsed, outcome="ok"):
        elapsed = max(0.0, elapsed)
        self.count += 1
        self.total += elapsed
        self.maximum = max(self.maximum, elapsed)
        self.bins[bisect_left(BOUNDS, elapsed)] += 1
        self.failed += outcome == "error"
        self.cancelled += outcome == "cancelled"

    def snapshot(self):
        def upper_bound(fraction):
            if not self.count:
                return None
            target = math.ceil(self.count * fraction)
            total = 0
            for bound, count in zip(BOUNDS, self.bins):
                total += count
                if total >= target:
                    return round(bound * 1000, 4) if math.isfinite(bound) else None
        return {
            "count": self.count, "active": self.active, "failed": self.failed,
            "cancelled": self.cancelled, "total_ms": round(self.total * 1000, 4),
            "mean_ms": round(self.total * 1000 / self.count, 4) if self.count else None,
            "max_ms": round(self.maximum * 1000, 4),
            "p50_bucket_upper_ms": upper_bound(0.5),
            "p95_bucket_upper_ms": upper_bound(0.95),
        }


class _NoTiming:
    __slots__ = ()

    def __enter__(self):
        return None

    def __exit__(self, exc_type, exc, traceback):
        return False


_NO_TIMING = _NoTiming()


class _Measurement:
    __slots__ = ("histogram", "started")

    def __init__(self, histogram):
        self.histogram = histogram
        histogram.active += 1
        self.started = time.perf_counter()

    def __enter__(self):
        return None

    def __exit__(self, exc_type, exc, traceback):
        outcome = "ok" if exc_type is None else ("cancelled" if isinstance(exc, asyncio.CancelledError) else "error")
        self.histogram.active -= 1
        self.histogram.record(time.perf_counter() - self.started, outcome)
        return False


class RuntimeTimings:
    def __init__(self, enabled=True):
        self.always_enabled = enabled
        self.profile_until = 0.0
        self.stages = {name: TimingHistogram() for name in STAGES}

    @property
    def enabled(self):
        if self.always_enabled:
            return True
        if not self.profile_until:
            return False
        if time.monotonic() < self.profile_until:
            return True
        self.profile_until = 0.0
        return False

    def profile(self, seconds):
        if not math.isfinite(seconds) or not 0 < seconds <= 300:
            raise ValueError("Profile duration must be finite and between 0 and 300 seconds")
        self.profile_until = max(self.profile_until, time.monotonic() + seconds)

    def profile_status(self):
        enabled = self.enabled
        return {
            "timings_enabled": enabled,
            "timing_mode": "always" if self.always_enabled else ("temporary" if enabled else "off"),
            "profile_remaining_s": round(max(0.0, self.profile_until - time.monotonic()), 3) if not self.always_enabled else 0.0,
        }

    def begin(self, stage):
        if not self.enabled:
            return None
        self.stages[stage].active += 1
        return time.perf_counter()

    def finish(self, stage, started, outcome="ok"):
        if started is None:
            return
        histogram = self.stages[stage]
        histogram.active -= 1
        histogram.record(time.perf_counter() - started, outcome)

    def measure(self, stage):
        # The normal fast mode neither allocates a timer/context generator nor
        # installs HTTPcore trace callbacks. Captured intervals finish normally
        # if a temporary profile expires while they are active.
        return _Measurement(self.stages[stage]) if self.enabled else _NO_TIMING

    def snapshot(self):
        return {name: histogram.snapshot() for name, histogram in self.stages.items()}

    def upstream_trace(self, previous=None):
        return _UpstreamTrace(self, previous)



class _UpstreamTrace:
    __slots__ = ("timings", "previous", "started")

    def __init__(self, timings, previous):
        self.timings = timings
        self.previous = previous
        self.started = {}

    async def __call__(self, event, info):
        # HTTPcore's info can contain request headers; it is never read or stored.
        base, _, action = event.rpartition(".")
        stage = TRACE_STAGES.get(base)
        if stage is not None:
            if action == "started":
                self.started[stage] = self.timings.begin(stage)
            elif action in ("complete", "failed") and stage in self.started:
                self.timings.finish(stage, self.started.pop(stage),
                                    "error" if action == "failed" else "ok")
        if self.previous is not None:
            outcome = self.previous(event, info)
            if inspect.isawaitable(outcome):
                await outcome

    def close(self, outcome="cancelled"):
        for stage, started in self.started.items():
            self.timings.finish(stage, started, outcome)
        self.started.clear()
