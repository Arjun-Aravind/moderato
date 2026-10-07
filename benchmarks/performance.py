"""
Reproducible performance benchmarks for the Moderato rate limiter.

Methodology:
- Every scenario runs multiple trials; each published number carries the
  trial mean, sample standard deviation, and min/median/max spread.
- Redis keys are derived from a deterministic run id (--run-id) and swept
  before and after the run, so re-running with the same id replays the
  same key layout from an empty state.
- Results are written as machine-readable JSON alongside the human summary.
  The JSON files committed under benchmarks/results/ back the numbers in
  BENCHMARKS.md.
- Memory is read from Redis itself (MEMORY USAGE), never estimated.
- Latency is measured per request around the await; concurrent scenarios
  therefore report per-request latency including client-side queueing.

Usage:
    python benchmarks/performance.py --quick
    python benchmarks/performance.py --trials 5 --run-id 20260928T120000Z
"""

import argparse
import asyncio
import json
import math
import os
import platform
import re
import statistics
import sys
import time
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import redis.asyncio as redis

sys.path.insert(0, str(Path(__file__).parent.parent))

from moderato import RateLimiter, RateLimitExceeded  # noqa: E402
from moderato.utils import normalize_key_component  # noqa: E402

REDIS_URL_DEFAULT = "redis://localhost:6379"

# Request counts and trial counts per profile. "quick" exists for local
# smoke runs (e.g. `make benchmark-quick`); published numbers use "full".
FULL_PROFILE: dict[str, Any] = {
    "throughput_trials": 5,
    "throughput_requests": 3000,
    "latency_trials": 3,
    "latency_samples": 2000,
    "sweep_trials": 3,
    "sweep_client_counts": [1, 2, 5, 10, 25, 50, 100, 200],
    "sweep_requests_per_client": 100,
    "algorithm_trials": 3,
    "algorithm_samples": 1000,
    "accuracy_limit": 50,
    "accuracy_multiplier": 4,
    "memory_identities": 4000,
    "tenant_trials": 3,
    "tenant_count": 100,
    "tenant_requests_per_tenant": 100,
}

QUICK_PROFILE: dict[str, Any] = {
    "throughput_trials": 2,
    "throughput_requests": 1000,
    "latency_trials": 1,
    "latency_samples": 500,
    "sweep_trials": 1,
    "sweep_client_counts": [1, 10, 50, 100],
    "sweep_requests_per_client": 50,
    "algorithm_trials": 1,
    "algorithm_samples": 300,
    "accuracy_limit": 50,
    "accuracy_multiplier": 4,
    "memory_identities": 1000,
    "tenant_trials": 1,
    "tenant_count": 20,
    "tenant_requests_per_tenant": 20,
}

HIGH_RATE = "100000/minute"  # High enough that no benchmark request is denied


def _read_cpu_model() -> str:
    """CPU model string from /proc/cpuinfo, or a best-effort fallback."""
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.lower().replace(" ", "").startswith("modelname"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or "unknown"


def _read_total_ram_mib() -> Optional[int]:
    """Total system RAM in MiB from /proc/meminfo, when available."""
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def _logical_cpu_count() -> int:
    """CPUs usable by this process (respects cgroup/affinity limits)."""
    try:
        return len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        return os.cpu_count() or 0


def _redis_placement(url: str) -> str:
    """Describe where Redis lives relative to this process."""
    host = urlparse(url).hostname or "unknown"
    if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return "localhost (same machine)"
    return host


def percentile(sorted_values: Sequence[float], pct: float) -> float:
    """Nearest-rank percentile of an ascending-sorted sequence."""
    if not sorted_values:
        return 0.0
    rank = math.ceil((pct / 100.0) * len(sorted_values))
    idx = max(0, min(len(sorted_values) - 1, rank - 1))
    return sorted_values[idx]


def summarize(values: Sequence[float]) -> dict[str, Any]:
    """Trial statistics: mean, sample stdev, CV, and the min/median/max spread."""
    vals = [float(v) for v in values]
    mean = statistics.mean(vals)
    stdev = statistics.stdev(vals) if len(vals) > 1 else 0.0
    return {
        "trials": len(vals),
        "values": [round(v, 4) for v in vals],
        "mean": round(mean, 4),
        "stdev": round(stdev, 4),
        "cv_pct": round((stdev / mean) * 100, 2) if mean else 0.0,
        "min": round(min(vals), 4),
        "median": round(statistics.median(vals), 4),
        "max": round(max(vals), 4),
    }


def latency_summary(trials_latencies: Sequence[Sequence[float]]) -> dict[str, Any]:
    """Percentiles pooled across trials, plus per-trial spread of p50/p99."""
    pooled: list[float] = []
    per_trial_p50: list[float] = []
    per_trial_p99: list[float] = []
    for trial in trials_latencies:
        ordered = sorted(trial)
        per_trial_p50.append(percentile(ordered, 50))
        per_trial_p99.append(percentile(ordered, 99))
        pooled.extend(trial)
    ordered = sorted(pooled)
    return {
        "samples": len(pooled),
        "pooled_ms": {
            "min": round(ordered[0], 4),
            "p50": round(percentile(ordered, 50), 4),
            "p90": round(percentile(ordered, 90), 4),
            "p95": round(percentile(ordered, 95), 4),
            "p99": round(percentile(ordered, 99), 4),
            "p99.9": round(percentile(ordered, 99.9), 4),
            "max": round(ordered[-1], 4),
            "mean": round(statistics.mean(pooled), 4),
        },
        "p50_ms": summarize(per_trial_p50),
        "p99_ms": summarize(per_trial_p99),
    }


class PerformanceBenchmark:
    """Run the moderato benchmark scenarios and record results with variance."""

    def __init__(
        self,
        redis_url: str,
        profile: str,
        config: dict[str, Any],
        run_id: str,
    ):
        self.redis_url = redis_url
        self.profile = profile
        self.config = config
        self.run_id = run_id
        self.results: dict[str, Any] = {}
        self.limiter: Optional[RateLimiter] = None
        self.probe: Optional[redis.Redis] = None
        self.redis_info: dict[str, Any] = {}
        sweep_max = max(config["sweep_client_counts"])
        self.pool_size = sweep_max + 50

    # -- infrastructure -----------------------------------------------------

    def bench_key(self, *parts: Any) -> str:
        """Deterministic limiter key for this run (prefixed by the limiter)."""
        return ":".join(["bench", self.run_id, *[str(p) for p in parts]])

    def redis_key_pattern(self, *parts: Any) -> str:
        """SCAN pattern for this run's keys.

        moderato URL-encodes key components (colons become %3A), so the
        pattern must be built through the library's own encoder, including
        the trailing separator.
        """
        prefix = self.limiter.config.key_prefix if self.limiter else "ratelimit"
        component = ":".join(["bench", self.run_id, *[str(p) for p in parts]]) + ":"
        return f"{prefix}:{normalize_key_component(component)}*"

    async def _start_of_next_second(self, margin: float = 0.002) -> None:
        """Sleep until just after the next whole second on the Redis clock.

        Window boundaries are decided by Redis TIME, so alignment uses the
        same clock rather than the local one; a remote Redis may be skewed.
        """
        assert self.probe is not None
        seconds, micros = await self.probe.time()
        await asyncio.sleep(1.0 - micros / 1_000_000.0 + margin)

    async def _redis_cpu_seconds(self) -> float:
        """Cumulative CPU seconds consumed by the Redis server process."""
        assert self.probe is not None
        info = await self.probe.info("cpu")
        return float(info["used_cpu_sys"]) + float(info["used_cpu_user"])

    async def _delete_bench_keys(self) -> int:
        assert self.probe is not None
        deleted = 0
        batch: list[str] = []
        async for key in self.probe.scan_iter(match=self.redis_key_pattern(), count=500):
            batch.append(key)
            if len(batch) >= 500:
                deleted += await self.probe.delete(*batch)
                batch = []
        if batch:
            deleted += await self.probe.delete(*batch)
        return deleted

    async def setup(self) -> None:
        # The default pool (max_connections=50) rejects in-flight checks past
        # ~50 true concurrency with redis-py's non-blocking pool, which would
        # clip the high-concurrency levels. Raise it through the public
        # RateLimitConfig field so scaling sections measure the limiter, and
        # measure the default-pool boundary separately (pool_capacity probe).
        self.limiter = RateLimiter(redis_url=self.redis_url)
        self.limiter.config.max_connections = self.pool_size
        await self.limiter.connect()
        self.probe = redis.from_url(self.redis_url, decode_responses=True)
        await self.probe.ping()
        swept = await self._delete_bench_keys()
        # Hostname only: the URL may embed a password.
        print(
            f"Connected to Redis at {urlparse(self.redis_url).hostname}" f" (run id: {self.run_id})"
        )
        print(
            f"Limiter connection pool raised to {self.pool_size} "
            f"(default 50) for the concurrency sections"
        )
        if swept:
            print(f"Swept {swept} leftover keys from a previous run with this id")

    async def teardown(self) -> None:
        if self.probe is not None:
            swept = await self._delete_bench_keys()
            await self.probe.aclose()
            print(f"\nCleaned up {swept} benchmark keys")
        if self.limiter is not None:
            await self.limiter.close()
        print("Benchmarks completed")

    # -- scenarios ------------------------------------------------------------

    async def benchmark_throughput(self) -> None:
        """Max throughput, sequential vs concurrently issued requests."""
        requests = self.config["throughput_requests"]
        trials = self.config["throughput_trials"]
        print(f"Throughput ({requests} requests x {trials} trials)")
        print("-" * 60)

        seq_rates: list[float] = []
        con_rates: list[float] = []
        assert self.limiter is not None

        for trial in range(trials):
            start = time.perf_counter()
            for i in range(requests):
                await self.limiter.check(key=self.bench_key("throughput", "seq", i), rate=HIGH_RATE)
            seq_time = time.perf_counter() - start
            seq_rates.append(requests / seq_time)

            start = time.perf_counter()
            tasks = [
                self.limiter.check(key=self.bench_key("throughput", "con", i), rate=HIGH_RATE)
                for i in range(requests)
            ]
            await asyncio.gather(*tasks)
            con_time = time.perf_counter() - start
            con_rates.append(requests / con_time)

            print(
                f"  trial {trial + 1}/{trials}: sequential {seq_rates[-1]:8.1f} req/s | "
                f"concurrent {con_rates[-1]:8.1f} req/s"
            )

        self.results["throughput"] = {
            "requests_per_trial": requests,
            "sequential_req_s": summarize(seq_rates),
            "concurrent_req_s": summarize(con_rates),
            "speedup": summarize([c / s for c, s in zip(con_rates, seq_rates)]),
        }
        t = self.results["throughput"]
        print(
            f"  sequential: {t['sequential_req_s']['mean']:,.0f} "
            f"± {t['sequential_req_s']['stdev']:,.0f} req/s (CV "
            f"{t['sequential_req_s']['cv_pct']}%)\n"
            f"  concurrent: {t['concurrent_req_s']['mean']:,.0f} "
            f"± {t['concurrent_req_s']['stdev']:,.0f} req/s (CV "
            f"{t['concurrent_req_s']['cv_pct']}%)\n"
        )

    async def benchmark_latency(self) -> None:
        """Latency distribution of sequential single checks."""
        samples = self.config["latency_samples"]
        trials = self.config["latency_trials"]
        print(f"Latency distribution ({samples} samples x {trials} trials)")
        print("-" * 60)
        assert self.limiter is not None

        trials_latencies: list[list[float]] = []
        for _ in range(trials):
            latencies: list[float] = []
            for i in range(samples):
                start = time.perf_counter()
                await self.limiter.check(key=self.bench_key("latency", i), rate=HIGH_RATE)
                latencies.append((time.perf_counter() - start) * 1000.0)
            trials_latencies.append(latencies)

        stats = latency_summary(trials_latencies)
        pooled = stats["pooled_ms"]
        self.results["latency"] = stats
        print(
            f"  p50 {pooled['p50']:.3f}ms | p90 {pooled['p90']:.3f}ms | p95 {pooled['p95']:.3f}ms"
            f" | p99 {pooled['p99']:.3f}ms | p99.9 {pooled['p99.9']:.3f}ms |"
            f" max {pooled['max']:.3f}ms"
        )
        print(
            f"  per-trial spread: p50 {stats['p50_ms']['mean']:.3f}"
            f"±{stats['p50_ms']['stdev']:.3f}ms, "
            f"p99 {stats['p99_ms']['mean']:.3f}±{stats['p99_ms']['stdev']:.3f}ms\n"
        )

    async def benchmark_concurrency_sweep(self) -> None:
        """Throughput and latency as client concurrency grows."""
        client_counts = self.config["sweep_client_counts"]
        per_client = self.config["sweep_requests_per_client"]
        trials = self.config["sweep_trials"]
        print(
            f"Concurrency sweep ({client_counts} clients, {per_client} req/client, "
            f"{trials} trials)"
        )
        print("-" * 60)
        assert self.limiter is not None

        rows = []
        for num_clients in client_counts:
            rates: list[float] = []
            trials_latencies: list[list[float]] = []

            async def client_work(client_id: int, trial: int) -> list[float]:
                latencies = []
                for i in range(per_client):
                    start = time.perf_counter()
                    await self.limiter.check(
                        key=self.bench_key("sweep", trial, client_id, i), rate=HIGH_RATE
                    )
                    latencies.append((time.perf_counter() - start) * 1000.0)
                return latencies

            redis_cpu_before = await self._redis_cpu_seconds()
            client_cpu_before = time.process_time()
            wall_before = time.perf_counter()
            for trial in range(trials):
                start = time.perf_counter()
                latencies = await asyncio.gather(
                    *[client_work(c, trial) for c in range(num_clients)]
                )
                elapsed = time.perf_counter() - start
                rates.append((num_clients * per_client) / elapsed)
                trials_latencies.extend(latencies)
            wall = time.perf_counter() - wall_before
            client_cpu = 100.0 * (time.process_time() - client_cpu_before) / wall
            redis_cpu = 100.0 * (await self._redis_cpu_seconds() - redis_cpu_before) / wall

            summary = latency_summary(trials_latencies)
            row = {
                "clients": num_clients,
                "requests_per_trial": num_clients * per_client,
                "throughput_req_s": summarize(rates),
                "latency": summary,
                "cpu_pct_of_one_core": {
                    "benchmark_client": round(client_cpu, 1),
                    "redis_server": round(redis_cpu, 1),
                },
            }
            rows.append(row)
            th = row["throughput_req_s"]
            print(
                f"  {num_clients:4} clients: {th['mean']:9,.1f} ± {th['stdev']:9,.1f} req/s | "
                f"p50 {summary['pooled_ms']['p50']:6.3f}ms | "
                f"p99 {summary['pooled_ms']['p99']:6.3f}ms | "
                f"cpu client {client_cpu:5.1f}% redis {redis_cpu:5.1f}%"
            )

        self.results["concurrency_sweep"] = rows
        print()

    async def benchmark_algorithm_comparison(self) -> None:
        """Sequential latency and throughput per rate limiting algorithm."""
        algorithms = ["fixed_window", "token_bucket", "sliding_window"]
        samples = self.config["algorithm_samples"]
        trials = self.config["algorithm_trials"]
        print(f"Algorithm comparison ({samples} samples x {trials} trials, sequential)")
        print("-" * 60)
        assert self.limiter is not None

        results = {}
        for algorithm in algorithms:
            trials_latencies: list[list[float]] = []
            for _ in range(trials):
                latencies: list[float] = []
                for i in range(samples):
                    start = time.perf_counter()
                    await self.limiter.check(
                        key=self.bench_key("algorithm", algorithm, i),
                        rate=HIGH_RATE,
                        algorithm=algorithm,
                    )
                    latencies.append((time.perf_counter() - start) * 1000.0)
                trials_latencies.append(latencies)

            stats = latency_summary(trials_latencies)
            throughput = summarize(
                [len(trial) / (sum(trial) / 1000.0) for trial in trials_latencies]
            )
            stats["throughput_req_s"] = throughput
            results[algorithm] = stats
            pooled = stats["pooled_ms"]
            print(
                f"  {algorithm:15} | p50 {pooled['p50']:6.3f}ms | p99 {pooled['p99']:6.3f}ms | "
                f"{throughput['mean']:7,.0f} ± {throughput['stdev']:7,.0f} req/s"
            )

        self.results["algorithms"] = results
        print()

    async def benchmark_accuracy_under_load(self) -> None:
        """Accuracy under a concurrent burst, compared to analytic expectations.

        Fires ``multiplier * limit`` concurrent requests against a fresh key
        at ``limit/second``. Window algorithms refill nothing mid-window, so
        the analytic expectation is exactly ``limit`` accepts while the burst
        stays inside one window. The token bucket refills continuously, so
        the analytic expectation is ``limit + refill_rate * burst_span``;
        the observed count must fall inside that analytic band.
        """
        limit = self.config["accuracy_limit"]
        total = limit * self.config["accuracy_multiplier"]
        rate = f"{limit}/second"
        print(f"Accuracy under load (burst of {total} concurrent, limit {limit}/second)")
        print("-" * 60)
        assert self.limiter is not None
        assert self.probe is not None

        algorithm_results = {}
        for algorithm in ("fixed_window", "token_bucket", "sliding_window"):
            key = self.bench_key("accuracy", algorithm)

            async def attempt(k: str = key, a: str = algorithm) -> bool:
                try:
                    return await self.limiter.check(key=k, rate=rate, algorithm=a)
                except RateLimitExceeded:
                    return False

            await self._start_of_next_second()
            start_s, start_us = await self.probe.time()
            allowed = sum(await asyncio.gather(*[attempt() for _ in range(total)]))
            end_s, end_us = await self.probe.time()
            span = (end_s - start_s) + (end_us - start_us) / 1_000_000.0
            windows_touched = int(end_s) - int(start_s) + 1

            if algorithm == "token_bucket":
                # Bucket starts full; only the refill inside the burst adds
                # capacity. The Redis clock span measured here brackets the
                # span the script saw, so the band is a defensible bound.
                refill_per_second = limit / 1.0
                expected_low = limit
                expected_high = math.ceil(limit + refill_per_second * span) + 1
                expected_point = round(limit + refill_per_second * span, 2)
            else:
                # No mid-window refill; a boundary crossing starts a fresh
                # window with another ``limit`` requests of capacity.
                expected_low = limit
                expected_high = limit * windows_touched
                expected_point = limit if windows_touched == 1 else None

            passed = expected_low <= allowed <= expected_high
            algorithm_results[algorithm] = {
                "allowed": allowed,
                "denied": total - allowed,
                "expected_point": expected_point,
                "expected_low": expected_low,
                "expected_high": expected_high,
                "burst_span_seconds": round(span, 4),
                "windows_touched": windows_touched,
                "pass": passed,
            }
            marker = "ok" if passed else "FAIL"
            print(
                f"  [{marker}] {algorithm:15} allowed {allowed:3} | analytic band "
                f"[{expected_low}, {expected_high}]"
            )

        self.results["accuracy_under_load"] = algorithm_results
        print()

    async def _memory_usage(self, pattern: str) -> tuple[list[int], list[str]]:
        """Scan this run's keys matching ``pattern`` and read MEMORY USAGE for each."""
        assert self.probe is not None
        keys = [key async for key in self.probe.scan_iter(match=pattern, count=500)]
        usages: list[int] = []
        for chunk_start in range(0, len(keys), 1000):
            chunk = keys[chunk_start : chunk_start + 1000]
            pipe = self.probe.pipeline()
            for key in chunk:
                pipe.memory_usage(key)
            usages.extend(value for value in await pipe.execute() if value is not None)
        return usages, keys

    async def benchmark_memory_usage(self) -> None:
        """Real per-key memory from Redis MEMORY USAGE, per algorithm.

        Two measurements per algorithm. The first checks many identities
        once each and scans every key the run created. The second checks a
        smaller set twice, one window apart, and measures every key still
        alive: at steady state the sliding window keeps the previous
        window's key alongside the current one (its TTL is 2x the window),
        so it costs two keys per live identity while the other algorithms
        cost one.
        """
        identities = self.config["memory_identities"]
        rate = "100/minute"
        print(f"Memory usage (MEMORY USAGE over {identities} identities per algorithm)")
        print("-" * 60)
        assert self.limiter is not None

        algorithms = ["fixed_window", "token_bucket", "sliding_window"]
        results = {}
        for algorithm in algorithms:
            # Fixed-window keys expire at the minute boundary, so a batch
            # created across one would lose its older keys before the scan.
            # Start only when the whole batch fits before the next
            # boundary; the wait is skipped whenever there is enough
            # runway.
            now = time.time()
            runway = math.ceil(now / 60) * 60 - now
            if runway < 10.0:
                await asyncio.sleep(runway + 0.05)

            for i in range(identities):
                await self.limiter.check(
                    key=self.bench_key("memory", algorithm, i), rate=rate, algorithm=algorithm
                )

            usages, keys = await self._memory_usage(self.redis_key_pattern("memory", algorithm))
            if len(usages) != identities:
                raise RuntimeError(
                    f"{algorithm}: measured {len(usages)} of {identities} keys; "
                    "a window boundary expired keys before the scan"
                )
            total_bytes = sum(usages)
            results[algorithm] = {
                "identities": identities,
                "keys": len(keys),
                "keys_per_identity": round(len(keys) / identities, 3),
                "bytes_per_key": summarize(usages),
                "bytes_per_identity": round(total_bytes / identities, 1),
                "measured_via": "MEMORY USAGE",
            }
            bpk = results[algorithm]["bytes_per_key"]
            print(
                f"  {algorithm:15} | {len(keys):5} keys | per key {bpk['mean']:6.1f} ± "
                f"{bpk['stdev']:5.1f} bytes (median {bpk['median']:6.1f}) | "
                f"per identity {results[algorithm]['bytes_per_identity']:6.1f} bytes"
            )

        # Steady state: two checks one window apart, then measure everything
        # still alive. Both batches start just after a 1-second boundary so
        # the key sets are deterministic: the fixed window's first keys
        # expire exactly at that boundary, the sliding window's first keys
        # (TTL 2x window) stay alive alongside the second batch's, and the
        # token bucket keeps its single key.
        steady_identities = 200
        steady_rate = "100/second"
        print(f"Steady state ({steady_identities} identities, two checks one window apart)")

        for algorithm in algorithms:
            steady_keys = [self.bench_key("steady", algorithm, i) for i in range(steady_identities)]
            await self._start_of_next_second()
            for key in steady_keys:
                await self.limiter.check(key=key, rate=steady_rate, algorithm=algorithm)
            await self._start_of_next_second()
            for key in steady_keys:
                await self.limiter.check(key=key, rate=steady_rate, algorithm=algorithm)

            usages, keys = await self._memory_usage(self.redis_key_pattern("steady", algorithm))
            total_bytes = sum(usages)
            steady = {
                "identities": steady_identities,
                "keys": len(keys),
                "keys_per_identity": round(len(keys) / steady_identities, 3),
                "bytes_per_key": summarize(usages),
                "bytes_per_identity": round(total_bytes / steady_identities, 1),
                "measured_via": "MEMORY USAGE after two checks one window apart",
            }
            results[algorithm]["steady"] = steady
            bpk = steady["bytes_per_key"]
            print(
                f"  {algorithm:15} | {len(keys):5} keys | per key {bpk['mean']:6.1f} ± "
                f"{bpk['stdev']:5.1f} bytes | per identity "
                f"{steady['bytes_per_identity']:6.1f} bytes"
            )

        self.results["memory"] = results
        print()

    async def benchmark_pool_capacity(self) -> None:
        """Probe the default connection pool under rising true concurrency.

        The shipped default pool (max_connections=50) rejects a borrow once
        no connection is idle and all 50 are checked out (redis-py raises
        ``Too many connections``). This probe splits 1,000 checks across
        ``num_clients`` workers so exactly ``num_clients`` checks are in
        flight, and records both rejections and the observed peak pool
        usage per level: on a localhost Redis each check completes before
        many workers overlap, so the boundary is timing- and
        deployment-dependent rather than a fixed client count.
        """
        levels = [25, 50, 75, 100, 200]
        checks_per_level = 1000
        print(f"Default pool capacity probe (levels {levels}, {checks_per_level} checks)")
        print("-" * 60)

        from moderato import BackendError

        probe_limiter = RateLimiter(redis_url=self.redis_url)
        await probe_limiter.connect()
        # Instrument the backend's redis-py pool to observe peak usage; the
        # wrappers delegate to the originals and are not restored because the
        # probe limiter is closed below and used by nothing else.
        backend_pool = getattr(
            getattr(probe_limiter.backend, "_redis", None), "connection_pool", None
        )
        orig_get = getattr(backend_pool, "get_available_connection", None)
        orig_release = getattr(backend_pool, "release", None)
        instrumented = (
            backend_pool is not None and orig_get is not None and orig_release is not None
        )
        pool_stats: dict[str, int] = {"in_use": 0, "peak": 0}
        if instrumented:

            def _get_available_connection() -> Any:
                conn = orig_get()
                pool_stats["in_use"] += 1
                pool_stats["peak"] = max(pool_stats["peak"], pool_stats["in_use"])
                return conn

            async def _release(conn: Any) -> Any:
                pool_stats["in_use"] -= 1
                return await orig_release(conn)

            backend_pool.get_available_connection = _get_available_connection  # type: ignore[assignment]
            backend_pool.release = _release  # type: ignore[assignment]
        try:
            rows = []
            for num_clients in levels:
                outcomes: dict[str, int] = {}
                pool_stats["in_use"] = 0
                pool_stats["peak"] = 0

                async def worker(
                    worker_id: int,
                    level: int = num_clients,
                    tally: dict[str, int] = outcomes,
                ) -> None:
                    # Stride the check indexes across workers so exactly
                    # ``level`` checks are in flight at a time; scheduling all
                    # checks as one coroutine wave would never vary concurrency.
                    for i in range(worker_id, checks_per_level, level):
                        try:
                            await probe_limiter.check(
                                key=self.bench_key("pool", level, i), rate=HIGH_RATE
                            )
                            tally["ok"] = tally.get("ok", 0) + 1
                        except BackendError as exc:
                            cause = exc.__cause__.__class__.__name__ if exc.__cause__ else "error"
                            tally[cause] = tally.get(cause, 0) + 1

                await asyncio.gather(*[worker(w) for w in range(num_clients)])
                row: dict[str, Any] = {
                    "clients": num_clients,
                    "outcomes": outcomes,
                    "peak_in_use_connections": pool_stats["peak"] if instrumented else None,
                }
                rows.append(row)
                limit = probe_limiter.config.max_connections
                peak = f"{pool_stats['peak']}/{limit}" if instrumented else "n/a"
                print(f"  {num_clients:4} concurrent: {outcomes} (peak pool in use: {peak})")

            self.results["pool_capacity"] = {
                "default_max_connections": 50,
                "checks_per_level": checks_per_level,
                "levels": rows,
            }
        finally:
            await probe_limiter.close()
        print()

    async def benchmark_multi_tenant(self) -> None:
        """Concurrent checks across many tenants and tenant types."""
        tenants = self.config["tenant_count"]
        per_tenant = self.config["tenant_requests_per_tenant"]
        trials = self.config["tenant_trials"]
        rate = "1000/minute"
        print(f"Multi-tenant ({tenants} tenants x {per_tenant} requests x {trials} trials)")
        print("-" * 60)
        assert self.limiter is not None

        # Tier distribution: 70% free, 25% premium, 5% enterprise.
        tiers = ["free"] * 70 + ["premium"] * 25 + ["enterprise"] * 5
        rates: list[float] = []

        async def tenant_requests(tenant_id: int, trial: int, tier: str) -> None:
            for _ in range(per_tenant):
                await self.limiter.check(
                    key=self.bench_key("tenant", trial, tenant_id),
                    rate=rate,
                    tenant_type=tier,
                )

        for trial in range(trials):
            start = time.perf_counter()
            await asyncio.gather(
                *[
                    tenant_requests(i % tenants, trial, tiers[i % len(tiers)])
                    for i in range(tenants)
                ]
            )
            elapsed = time.perf_counter() - start
            rates.append((tenants * per_tenant) / elapsed)
            print(f"  trial {trial + 1}/{trials}: {rates[-1]:,.1f} req/s")

        self.results["multi_tenant"] = {
            "tenants": tenants,
            "requests_per_trial": tenants * per_tenant,
            "throughput_req_s": summarize(rates),
        }
        th = self.results["multi_tenant"]["throughput_req_s"]
        print(f"  throughput: {th['mean']:,.1f} ± {th['stdev']:,.1f} req/s\n")

    # -- output ---------------------------------------------------------------

    def collect_environment(self) -> dict[str, Any]:
        """Environment metadata so results can be compared across machines."""
        info = self.redis_info
        return {
            "platform": platform.system(),
            "cpu_model": _read_cpu_model(),
            "cpu_count": _logical_cpu_count(),
            "ram_total_mib": _read_total_ram_mib(),
            "kernel": platform.release(),
            "python_version": platform.python_version(),
            "python_implementation": platform.python_implementation(),
            "redis_version": info.get("redis_version") if info else None,
            "redis_mode": info.get("redis_mode") if info else None,
            "redis_url_host": urlparse(self.redis_url).hostname,
            "redis_placement": _redis_placement(self.redis_url),
            "moderato_version": _moderato_version(),
        }

    async def run(self) -> None:
        await self.setup()
        try:
            assert self.probe is not None
            self.redis_info = dict(await self.probe.info("server"))
            await self.benchmark_throughput()
            await self.benchmark_latency()
            await self.benchmark_algorithm_comparison()
            await self.benchmark_accuracy_under_load()
            await self.benchmark_memory_usage()
            await self.benchmark_pool_capacity()
            await self.benchmark_concurrency_sweep()
            await self.benchmark_multi_tenant()
        finally:
            await self.teardown()

    def to_json(self, started_at: datetime) -> dict[str, Any]:
        ended_at = datetime.now(timezone.utc)
        return {
            "schema_version": 1,
            "benchmark": "moderato-performance",
            "profile": self.profile,
            "run_id": self.run_id,
            "started_at_utc": started_at.isoformat(),
            "ended_at_utc": ended_at.isoformat(),
            "duration_seconds": round((ended_at - started_at).total_seconds(), 2),
            "redis_url_host": urlparse(self.redis_url).hostname,
            "environment": self.collect_environment(),
            "configuration": {
                **dict(self.config),
                "connection_pool_size": self.pool_size,
                "connection_pool_note": (
                    "raised from the default 50 via RateLimitConfig.max_connections so "
                    "high-concurrency levels measure the limiter; the default-pool "
                    "boundary is measured in results.pool_capacity"
                ),
            },
            "results": self.results,
        }


def _moderato_version() -> str:
    from moderato import __version__

    return __version__


def slim_payload(payload: dict[str, Any]) -> None:
    """Reduce an evidence JSON to summary-level fields, in place.

    The committed evidence only needs the summaries; the per-sample arrays
    (4,000 per-key memory samples per algorithm plus the per-client sweep
    percentiles) are what make each run ~15k lines instead of ~900. Rerun
    without --slim to keep them for local analysis.
    """
    for stats in payload["results"]["memory"].values():
        stats["bytes_per_key"].pop("values", None)
        steady = stats.get("steady")
        if steady:
            steady["bytes_per_key"].pop("values", None)
    for level in payload["results"]["concurrency_sweep"]:
        for key in ("p50_ms", "p99_ms"):
            level["latency"][key].pop("values", None)
    payload["slim"] = True
    payload["slim_note"] = (
        "per-sample arrays omitted (memory bytes-per-key samples, including "
        "steady state, and sweep per-client percentile samples); rerun without "
        "--slim for the full samples"
    )


async def main(
    quick: bool, trials: Optional[int], run_id: str, json_path: Path, slim: bool = False
) -> None:
    redis_url = os.getenv("REDIS_URL", REDIS_URL_DEFAULT)
    profile = "quick" if quick else "full"
    config = dict(QUICK_PROFILE if quick else FULL_PROFILE)
    if trials is not None:
        for key in config:
            if key.endswith("_trials"):
                config[key] = trials

    started_at = datetime.now(timezone.utc)
    benchmark = PerformanceBenchmark(redis_url, profile, config, run_id)

    try:
        await benchmark.run()
    except Exception as exc:
        print(f"\nBenchmark failed: {exc}")
        raise

    payload = benchmark.to_json(started_at)
    if slim:
        slim_payload(payload)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"Machine-readable results: {json_path}")

    failures = sorted(
        algorithm
        for algorithm, outcome in payload["results"]["accuracy_under_load"].items()
        if not outcome["pass"]
    )
    if failures:
        print(f"Accuracy check failed for: {', '.join(failures)}")
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Moderato performance benchmarks")
    parser.add_argument("--quick", action="store_true", help="Reduced sizes for local smoke runs")
    parser.add_argument("--trials", type=int, default=None, help="Override trial counts")
    parser.add_argument(
        "--run-id", default=None, help="Deterministic run id used in Redis keys (default: UTC now)"
    )
    parser.add_argument(
        "--json",
        type=Path,
        default=None,
        help="JSON output path (default: benchmarks/results/<profile>-<run-id>.json)",
    )
    parser.add_argument(
        "--slim",
        action="store_true",
        help="Write summary-level JSON without per-sample arrays (for committing evidence)",
    )
    args = parser.parse_args()

    resolved_run_id = args.run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    # The run id becomes part of Redis key components; moderato hashes
    # components over 100 chars, which would break the run's SCAN cleanup.
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", resolved_run_id):
        parser.error("--run-id must match [A-Za-z0-9._-]{1,64}")
    output = args.json or Path("benchmarks/results") / (
        f"{'quick' if args.quick else 'full'}-{resolved_run_id}.json"
    )
    asyncio.run(
        main(
            quick=args.quick,
            trials=args.trials,
            run_id=resolved_run_id,
            json_path=output,
            slim=args.slim,
        )
    )
