"""
swiftdeploy.metrics
~~~~~~~~~~~~~~~~~~~
Scrape Prometheus-format /metrics and compute derived signals.

Signals produced
----------------
* req_per_second   – request throughput over the scrape window
* error_rate_pct   – HTTP 5xx share of all requests (%)
* p99_latency_ms   – 99th-percentile response latency from histogram buckets
* sample_count     – total request count seen in the window
"""

from __future__ import annotations

import re
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from typing import Optional


# ── Data classes ──────────────────────────────────────────────────────────────


@dataclass
class Snapshot:
    """A single /metrics scrape, parsed into the values we care about."""

    timestamp: float = field(default_factory=time.time)
    # Raw counters from the exposition
    requests_total: dict[str, float] = field(default_factory=dict)  # {status: count}
    latency_buckets: dict[float, float] = field(default_factory=dict)  # {le: count}
    latency_sum: float = 0.0
    latency_count: float = 0.0
    raw_lines: list[str] = field(default_factory=list, repr=False)

    @property
    def total_requests(self) -> float:
        return sum(self.requests_total.values())

    @property
    def error_requests(self) -> float:
        return sum(v for k, v in self.requests_total.items() if k.startswith("5"))


@dataclass
class WindowMetrics:
    """Derived metrics computed across two snapshots."""

    error_rate_pct: float
    p99_latency_ms: float
    req_per_second: float
    sample_count: int
    window_seconds: float


# ── Scraper ───────────────────────────────────────────────────────────────────


class MetricsScraper:
    """Scrapes a Prometheus /metrics endpoint and computes window metrics.

    Expects these metric families (nginx-prometheus-exporter compatible, or
    any app exposing Prometheus-format metrics):

        http_requests_total{status="200"} 1234
        http_request_duration_seconds_bucket{le="0.1"} 999
        http_request_duration_seconds_sum 45.2
        http_request_duration_seconds_count 1000

    Falls back gracefully to zeros if metric families are absent.
    """

    # Patterns for the metric families we care about
    _RE_REQUESTS = re.compile(
        r'^http_requests_total\{.*?status="(\d+)".*?\}\s+([\d.e+]+)', re.MULTILINE
    )
    _RE_BUCKET = re.compile(
        r'^http_request_duration_seconds_bucket\{.*?le="([^"]+)".*?\}\s+([\d.e+]+)',
        re.MULTILINE,
    )
    _RE_SUM = re.compile(
        r"^http_request_duration_seconds_sum\s+([\d.e+]+)", re.MULTILINE
    )
    _RE_COUNT = re.compile(
        r"^http_request_duration_seconds_count\s+([\d.e+]+)", re.MULTILINE
    )

    def __init__(self, base_url: str, timeout: float = 5.0):
        self.metrics_url = base_url.rstrip("/") + "/metrics"
        self.timeout = timeout

    def scrape(self) -> Optional[Snapshot]:
        """Fetch and parse /metrics.  Returns None on any error."""
        try:
            req = urllib.request.Request(self.metrics_url, method="GET")
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode(errors="replace")
        except (urllib.error.URLError, OSError):
            return None

        snap = Snapshot(timestamp=time.time(), raw_lines=body.splitlines())

        for m in self._RE_REQUESTS.finditer(body):
            status, count = m.group(1), float(m.group(2))
            snap.requests_total[status] = snap.requests_total.get(status, 0) + count

        for m in self._RE_BUCKET.finditer(body):
            le_str, count = m.group(1), float(m.group(2))
            le = float("inf") if le_str == "+Inf" else float(le_str)
            snap.latency_buckets[le] = float(count)

        m = self._RE_SUM.search(body)
        if m:
            snap.latency_sum = float(m.group(1))

        m = self._RE_COUNT.search(body)
        if m:
            snap.latency_count = float(m.group(1))

        return snap

    def compute_window(self, before: Snapshot, after: Snapshot) -> WindowMetrics:
        """Compute derived metrics between two snapshots."""
        elapsed = max(after.timestamp - before.timestamp, 0.001)

        # ── Request throughput ────────────────────────────────────────────────
        delta_total = max(after.total_requests - before.total_requests, 0)
        delta_errors = max(after.error_requests - before.error_requests, 0)
        req_per_second = delta_total / elapsed

        error_rate_pct = (delta_errors / delta_total * 100) if delta_total > 0 else 0.0

        # ── P99 from histogram buckets ────────────────────────────────────────
        p99_latency_ms = _p99_from_buckets(
            before.latency_buckets,
            after.latency_buckets,
            before.latency_count,
            after.latency_count,
        )

        return WindowMetrics(
            error_rate_pct=round(error_rate_pct, 4),
            p99_latency_ms=round(p99_latency_ms, 2),
            req_per_second=round(req_per_second, 3),
            sample_count=int(delta_total),
            window_seconds=round(elapsed, 1),
        )

    def scrape_window(self, window_seconds: int = 30) -> Optional[WindowMetrics]:
        """Convenience: take two scrapes separated by ``window_seconds`` and
        return derived metrics.  Returns None if either scrape fails."""
        before = self.scrape()
        if before is None:
            return None
        time.sleep(window_seconds)
        after = self.scrape()
        if after is None:
            return None
        return self.compute_window(before, after)


# ── P99 interpolation ─────────────────────────────────────────────────────────


def _p99_from_buckets(
    before: dict[float, float],
    after: dict[float, float],
    before_count: float,
    after_count: float,
) -> float:
    """Compute P99 latency in milliseconds from histogram bucket deltas.

    Uses linear interpolation within the bucket that contains the 99th
    percentile observation.  Returns 0.0 if there is insufficient data.
    """
    if not before or not after:
        return 0.0

    # Compute per-bucket deltas
    all_les = sorted(set(before) | set(after))
    deltas: list[tuple[float, float]] = []
    for le in all_les:
        delta = max((after.get(le, 0) - before.get(le, 0)), 0)
        deltas.append((le, delta))

    total_delta = max(after_count - before_count, 0)
    if total_delta == 0:
        return 0.0

    target = 0.99 * total_delta
    prev_le = 0.0
    prev_count = 0.0

    for le, cumulative in deltas:
        if le == float("inf"):
            break
        if cumulative >= target:
            # Interpolate within this bucket
            bucket_width = le - prev_le
            bucket_count = cumulative - prev_count
            if bucket_count == 0:
                p99_seconds = le
            else:
                fraction = (target - prev_count) / bucket_count
                p99_seconds = prev_le + fraction * bucket_width
            return p99_seconds * 1000  # → milliseconds
        prev_le = le
        prev_count = cumulative

    # All observations fell in the last finite bucket
    if deltas:
        last_finite = next(
            (le for le, _ in reversed(deltas) if le != float("inf")), 0.0
        )
        return last_finite * 1000

    return 0.0
