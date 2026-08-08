# Copyright (c) 2026 BANKON — all rights reserved.
# Licensed under the Apache License, Version 2.0 (the "BANKON License"). See LICENSE.
"""
Prometheus-format metrics, with no Prometheus client dependency.

The exposition format is a documented text protocol; emitting it directly costs
about eighty lines and keeps the dependency list at aiohttp + fastapi. For a
service whose whole argument is "no heavyweight dependencies to read a chain",
pulling in a metrics library to count four things would be inconsistent.

What is measured is chosen to answer the questions an operator actually has at
3am: is it up, is it slow, is it the upstream's fault, and is the cache working.
"""

from __future__ import annotations

import threading
from collections import defaultdict

#: Latency buckets in seconds. Spread wide because the upstream is a public
#: endpoint on the open internet, not a service in the same rack.
LATENCY_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)


class Metrics:
    """Thread-safe counters and histograms."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.requests: dict[tuple[str, int], int] = defaultdict(int)
        self.upstream: dict[tuple[str, str], int] = defaultdict(int)
        self.latency_buckets: dict[str, list[int]] = defaultdict(lambda: [0] * (len(LATENCY_BUCKETS) + 1))
        self.latency_sum: dict[str, float] = defaultdict(float)
        self.latency_count: dict[str, int] = defaultdict(int)

    def observe_request(self, route: str, status: int, duration_s: float) -> None:
        with self._lock:
            self.requests[(route, status)] += 1
            buckets = self.latency_buckets[route]
            placed = False
            for index, edge in enumerate(LATENCY_BUCKETS):
                if duration_s <= edge:
                    buckets[index] += 1
                    placed = True
                    break
            if not placed:
                buckets[-1] += 1
            self.latency_sum[route] += duration_s
            self.latency_count[route] += 1

    def observe_upstream(self, upstream: str, outcome: str) -> None:
        """outcome: ok | caller_error | upstream_error | retry | timeout"""
        with self._lock:
            self.upstream[(upstream, outcome)] += 1

    # ------------------------------------------------------------- exposition

    def render(self, cache_stats: object | None = None, extra: dict[str, float] | None = None) -> str:
        lines: list[str] = []

        with self._lock:
            requests = dict(self.requests)
            upstream = dict(self.upstream)
            buckets = {k: list(v) for k, v in self.latency_buckets.items()}
            lat_sum = dict(self.latency_sum)
            lat_count = dict(self.latency_count)

        lines += [
            "# HELP algorandscout_requests_total HTTP requests by route and status.",
            "# TYPE algorandscout_requests_total counter",
        ]
        for (route, status), count in sorted(requests.items()):
            lines.append(f'algorandscout_requests_total{{route="{_esc(route)}",status="{status}"}} {count}')

        lines += [
            "# HELP algorandscout_upstream_total Upstream calls by target and outcome.",
            "# TYPE algorandscout_upstream_total counter",
        ]
        for (target, outcome), count in sorted(upstream.items()):
            lines.append(f'algorandscout_upstream_total{{upstream="{_esc(target)}",outcome="{_esc(outcome)}"}} {count}')

        lines += [
            "# HELP algorandscout_request_duration_seconds Request latency by route.",
            "# TYPE algorandscout_request_duration_seconds histogram",
        ]
        for route, counts in sorted(buckets.items()):
            cumulative = 0
            for index, edge in enumerate(LATENCY_BUCKETS):
                cumulative += counts[index]
                lines.append(
                    f'algorandscout_request_duration_seconds_bucket{{route="{_esc(route)}",le="{edge}"}} {cumulative}'
                )
            cumulative += counts[-1]
            lines.append(
                f'algorandscout_request_duration_seconds_bucket{{route="{_esc(route)}",le="+Inf"}} {cumulative}'
            )
            lines.append(
                f'algorandscout_request_duration_seconds_sum{{route="{_esc(route)}"}} {lat_sum.get(route, 0.0):.6f}'
            )
            lines.append(
                f'algorandscout_request_duration_seconds_count{{route="{_esc(route)}"}} {lat_count.get(route, 0)}'
            )

        if cache_stats is not None:
            lines += [
                "# HELP algorandscout_cache_total Cache outcomes.",
                "# TYPE algorandscout_cache_total counter",
                f'algorandscout_cache_total{{outcome="hit"}} {getattr(cache_stats, "hits", 0)}',
                f'algorandscout_cache_total{{outcome="miss"}} {getattr(cache_stats, "misses", 0)}',
                f'algorandscout_cache_total{{outcome="expired"}} {getattr(cache_stats, "expirations", 0)}',
                f'algorandscout_cache_total{{outcome="evicted"}} {getattr(cache_stats, "evictions", 0)}',
            ]

        for name, value in (extra or {}).items():
            lines += [
                f"# HELP algorandscout_{name} Gauge.",
                f"# TYPE algorandscout_{name} gauge",
                f"algorandscout_{name} {value}",
            ]

        return "\n".join(lines) + "\n"


def _esc(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "")


METRICS = Metrics()
