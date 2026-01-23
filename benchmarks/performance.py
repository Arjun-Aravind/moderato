"""
Performance benchmarks for FastLimit rate limiter.

Tests throughput, latency, and scalability under various conditions.

Performance Targets:
- p50 latency: < 2ms
- p99 latency: < 10ms
- Throughput (sequential): > 3,000 req/s
- Throughput (concurrent): > 10,000 req/s
- Memory per key: < 200 bytes
- Accuracy at limit: 100%
"""

import argparse
import asyncio
import os
import statistics

# Add parent directory to path
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

from fastlimit import RateLimiter, RateLimitExceeded  # noqa: E402


class PerformanceBenchmark:
    """Performance testing for rate limiter."""

    # Performance targets
    TARGETS = {
        "p50_latency_ms": 2.0,
        "p99_latency_ms": 10.0,
        "throughput_sequential": 3000,
        "throughput_concurrent": 10000,
        "accuracy_percent": 100.0,
    }

    def __init__(self, redis_url: str = "redis://localhost:6379", quick: bool = False):
        self.redis_url = redis_url
        self.results: dict[str, Any] = {}
        self.quick = quick  # Run faster with fewer iterations
        self.passed_targets = []
        self.failed_targets = []

    async def setup(self):
        """Setup benchmark environment."""
        self.limiter = RateLimiter(redis_url=self.redis_url)
        await self.limiter.connect()
        print("Connected to Redis")
        print("Starting performance benchmarks...\n")

    async def teardown(self):
        """Cleanup after benchmarks."""
        await self.limiter.close()
        print("\nBenchmarks completed")

    def _check_target(self, name: str, value: float, target: float, higher_is_better: bool = True):
        """Check if a metric meets its target."""
        if higher_is_better:
            passed = value >= target
        else:
            passed = value <= target

        if passed:
            self.passed_targets.append(name)
        else:
            self.failed_targets.append(name)

        return passed

    async def benchmark_throughput(self, requests: int = None):
        """Test maximum throughput."""
        if requests is None:
            requests = 1000 if self.quick else 5000

        print(f"Throughput Test ({requests} requests)")
        print("-" * 50)

        rate = "100000/minute"  # Very high limit to avoid rate limiting

        # Warm up
        await self.limiter.check(key="warmup", rate=rate)

        # Sequential throughput
        start = time.perf_counter()
        for i in range(requests):
            key = f"throughput:seq:{i}"
            await self.limiter.check(key=key, rate=rate)
        seq_time = time.perf_counter() - start
        seq_throughput = requests / seq_time

        print(f"Sequential: {seq_throughput:.1f} req/s ({seq_time:.2f}s total)")

        # Concurrent throughput
        start = time.perf_counter()
        tasks = []
        for i in range(requests):
            key = f"throughput:con:{i}"
            tasks.append(self.limiter.check(key=key, rate=rate))
        await asyncio.gather(*tasks)
        con_time = time.perf_counter() - start
        con_throughput = requests / con_time

        print(f"Concurrent: {con_throughput:.1f} req/s ({con_time:.2f}s total)")
        print(f"Speedup: {con_throughput/seq_throughput:.2f}x\n")

        self.results["throughput"] = {
            "sequential": seq_throughput,
            "concurrent": con_throughput,
            "speedup": con_throughput / seq_throughput,
        }

    async def benchmark_latency(self, samples: int = None):
        """Test latency distribution."""
        if samples is None:
            samples = 200 if self.quick else 1000

        print(f"Latency Test ({samples} samples)")
        print("-" * 50)

        rate = "100000/minute"
        latencies = []

        for i in range(samples):
            key = f"latency:{i}"
            start = time.perf_counter()
            await self.limiter.check(key=key, rate=rate)
            latency = (time.perf_counter() - start) * 1000  # Convert to ms
            latencies.append(latency)

        latencies.sort()

        stats = {
            "min": min(latencies),
            "max": max(latencies),
            "mean": statistics.mean(latencies),
            "median": statistics.median(latencies),
            "p95": latencies[int(len(latencies) * 0.95)],
            "p99": latencies[int(len(latencies) * 0.99)],
            "stdev": statistics.stdev(latencies) if len(latencies) > 1 else 0,
        }

        print(f"Min: {stats['min']:.2f}ms")
        print(f"Median: {stats['median']:.2f}ms")
        print(f"Mean: {stats['mean']:.2f}ms")
        print(f"P95: {stats['p95']:.2f}ms")
        print(f"P99: {stats['p99']:.2f}ms")
        print(f"Max: {stats['max']:.2f}ms")
        print(f"StdDev: {stats['stdev']:.2f}ms\n")

        self.results["latency"] = stats

    async def benchmark_concurrent_clients(self):
        """Test with varying number of concurrent clients."""
        print("Concurrent Clients Test")
        print("-" * 50)

        client_counts = [1, 10, 50, 100, 500, 1000]
        requests_per_client = 100
        rate = "100000/minute"

        results = []

        for num_clients in client_counts:

            async def client_work(client_id: int):
                """Simulate client making requests."""
                for i in range(requests_per_client):
                    key = f"client:{client_id}:req:{i}"
                    await self.limiter.check(key=key, rate=rate)

            start = time.perf_counter()
            tasks = [client_work(i) for i in range(num_clients)]
            await asyncio.gather(*tasks)
            elapsed = time.perf_counter() - start

            total_requests = num_clients * requests_per_client
            throughput = total_requests / elapsed

            print(f"{num_clients:4} clients: {throughput:8.1f} req/s ({elapsed:.2f}s)")

            results.append({"clients": num_clients, "throughput": throughput, "time": elapsed})

        self.results["concurrent_clients"] = results
        print()

    async def benchmark_rate_limiting_accuracy(self):
        """Test rate limiting accuracy."""
        print("Rate Limiting Accuracy Test")
        print("-" * 50)

        test_cases = [
            ("10/second", 10, 1.0),
            ("100/minute", 100, 60.0),
            ("50/second", 50, 1.0),
        ]

        for rate_str, expected_allowed, _window_seconds in test_cases:
            key = f"accuracy:{rate_str}:{datetime.utcnow().isoformat()}"

            # Send requests rapidly
            allowed = 0
            denied = 0

            for _ in range(expected_allowed * 2):  # Try double the limit
                try:
                    await self.limiter.check(key=key, rate=rate_str)
                    allowed += 1
                except RateLimitExceeded:
                    denied += 1

            accuracy = (allowed / expected_allowed) * 100
            print(
                f"{rate_str:12} - Allowed: {allowed}/{expected_allowed} "
                f"({accuracy:.1f}% accurate)"
            )

        print()

    async def benchmark_memory_usage(self):
        """Estimate memory usage per key."""
        print("Memory Usage Test")
        print("-" * 50)

        # Create many keys
        num_keys = 10000
        rate = "100/minute"

        print(f"Creating {num_keys} rate limit keys...")

        for i in range(num_keys):
            key = f"memory:test:{i}"
            await self.limiter.check(key=key, rate=rate)

        # Estimate memory per key (approximate)
        # Each key stores: counter (8 bytes) + TTL + key name
        estimated_per_key = 100  # bytes (conservative estimate)
        total_memory = num_keys * estimated_per_key

        print(f"Keys created: {num_keys}")
        print(f"Estimated memory per key: ~{estimated_per_key} bytes")
        print(f"Total estimated memory: ~{total_memory / 1024:.1f} KB\n")

        self.results["memory"] = {
            "keys": num_keys,
            "per_key_bytes": estimated_per_key,
            "total_kb": total_memory / 1024,
        }

    async def benchmark_multi_tenant(self):
        """Test multi-tenant performance."""
        print("Multi-Tenant Performance Test")
        print("-" * 50)

        num_tenants = 20 if self.quick else 100
        requests_per_tenant = 20 if self.quick else 100
        rate = "1000/minute"

        async def tenant_requests(tenant_id: int, tier: str):
            """Simulate tenant making requests."""
            for _ in range(requests_per_tenant):
                key = f"tenant:{tenant_id}"
                await self.limiter.check(key=key, rate=rate, tenant_type=tier)

        # Test different tier distributions
        tiers = ["free"] * 70 + ["premium"] * 25 + ["enterprise"] * 5

        start = time.perf_counter()
        tasks = [tenant_requests(i, tiers[i % len(tiers)]) for i in range(num_tenants)]
        await asyncio.gather(*tasks)
        elapsed = time.perf_counter() - start

        total_requests = num_tenants * requests_per_tenant
        throughput = total_requests / elapsed

        print(f"Tenants: {num_tenants}")
        print(f"Total requests: {total_requests}")
        print(f"Time: {elapsed:.2f}s")
        print(f"Throughput: {throughput:.1f} req/s\n")

        self.results["multi_tenant"] = {
            "tenants": num_tenants,
            "throughput": throughput,
            "time": elapsed,
        }

    async def benchmark_algorithm_comparison(self):
        """
        Compare all three algorithms: fixed_window, token_bucket, sliding_window.

        Measures latency and throughput for each algorithm.
        """
        print("Algorithm Comparison Benchmark")
        print("-" * 50)

        algorithms = ["fixed_window", "token_bucket", "sliding_window"]
        samples = 200 if self.quick else 1000
        rate = "100000/minute"  # High limit to avoid rate limiting

        results = {}

        for algo in algorithms:
            latencies = []

            for i in range(samples):
                key = f"algo-compare-{algo}-{i}"
                start = time.perf_counter()
                await self.limiter.check(key=key, rate=rate, algorithm=algo)
                latency = (time.perf_counter() - start) * 1000  # ms
                latencies.append(latency)

            latencies.sort()

            results[algo] = {
                "p50": latencies[int(len(latencies) * 0.50)],
                "p99": latencies[int(len(latencies) * 0.99)],
                "throughput": samples / sum(latencies) * 1000,  # req/s
            }

            print(
                f"{algo:15} | p50: {results[algo]['p50']:.2f}ms | "
                f"p99: {results[algo]['p99']:.2f}ms | "
                f"{results[algo]['throughput']:.0f} req/s"
            )

        self.results["algorithm_comparison"] = results
        print()

    async def benchmark_accuracy_under_load(self):
        """
        Test rate limiting accuracy under concurrent load.

        Verifies that exactly the limit number of requests are allowed,
        no more, no less.
        """
        print("Accuracy Under Load Test")
        print("-" * 50)

        test_cases = [
            ("fixed_window", 50),
            ("token_bucket", 50),
            ("sliding_window", 50),
        ]

        all_passed = True

        for algo, limit in test_cases:
            test_key = f"accuracy-{algo}-{datetime.utcnow().isoformat()}"
            test_rate = f"{limit}/second"
            test_algo = algo

            async def make_request(k=test_key, r=test_rate, a=test_algo):
                try:
                    return await self.limiter.check(key=k, rate=r, algorithm=a)
                except RateLimitExceeded:
                    return False

            # Send 4x the limit concurrently
            tasks = [make_request() for _ in range(limit * 4)]
            results = await asyncio.gather(*tasks)

            allowed = sum(1 for r in results if r is True)
            accuracy = (allowed / limit) * 100
            passed = allowed == limit

            status = "✅" if passed else "❌"
            print(
                f"{status} {algo:15} | Limit {limit}: {allowed}/{limit} allowed "
                f"({accuracy:.1f}% accurate)"
            )

            if not passed:
                all_passed = False

        self.results["accuracy"] = {
            "all_passed": all_passed,
        }

        # Check target
        self._check_target(
            "accuracy_percent",
            100.0 if all_passed else 0.0,
            self.TARGETS["accuracy_percent"],
            higher_is_better=True,
        )

        print()

    def print_summary(self):
        """Print benchmark summary."""
        print("=" * 60)
        print("BENCHMARK SUMMARY")
        print("=" * 60)

        if "throughput" in self.results:
            t = self.results["throughput"]
            seq_status = (
                "✅"
                if self._check_target(
                    "throughput_sequential", t["sequential"], self.TARGETS["throughput_sequential"]
                )
                else "❌"
            )
            con_status = (
                "✅"
                if self._check_target(
                    "throughput_concurrent", t["concurrent"], self.TARGETS["throughput_concurrent"]
                )
                else "❌"
            )
            print(
                f"{seq_status} Sequential Throughput: {t['sequential']:.1f} req/s "
                f"(target: >{self.TARGETS['throughput_sequential']})"
            )
            print(
                f"{con_status} Concurrent Throughput: {t['concurrent']:.1f} req/s "
                f"(target: >{self.TARGETS['throughput_concurrent']})"
            )

        if "latency" in self.results:
            lat = self.results["latency"]
            p50_status = (
                "✅"
                if self._check_target(
                    "p50_latency_ms",
                    lat["median"],
                    self.TARGETS["p50_latency_ms"],
                    higher_is_better=False,
                )
                else "❌"
            )
            p99_status = (
                "✅"
                if self._check_target(
                    "p99_latency_ms",
                    lat["p99"],
                    self.TARGETS["p99_latency_ms"],
                    higher_is_better=False,
                )
                else "❌"
            )
            print(
                f"{p50_status} p50 Latency: {lat['median']:.2f}ms "
                f"(target: <{self.TARGETS['p50_latency_ms']}ms)"
            )
            print(
                f"{p99_status} p99 Latency: {lat['p99']:.2f}ms "
                f"(target: <{self.TARGETS['p99_latency_ms']}ms)"
            )

        if "algorithm_comparison" in self.results:
            print("\nAlgorithm Performance:")
            for algo, stats in self.results["algorithm_comparison"].items():
                print(
                    f"  {algo:15} | p50: {stats['p50']:.2f}ms | "
                    f"p99: {stats['p99']:.2f}ms | {stats['throughput']:.0f} req/s"
                )

        if "accuracy" in self.results:
            acc = self.results["accuracy"]
            status = "✅" if acc["all_passed"] else "❌"
            acc_msg = "All tests passed" if acc["all_passed"] else "Some tests failed"
            print(f"\n{status} Accuracy: {acc_msg}")

        if "concurrent_clients" in self.results:
            max_clients = self.results["concurrent_clients"][-1]
            print(
                f"\nConcurrent clients: {max_clients['clients']} "
                f"@ {max_clients['throughput']:.1f} req/s"
            )

        if "multi_tenant" in self.results:
            mt = self.results["multi_tenant"]
            print(f"Multi-tenant: {mt['tenants']} tenants " f"@ {mt['throughput']:.1f} req/s")

        # Final status
        print("\n" + "=" * 60)
        passed = len(self.passed_targets)
        failed = len(self.failed_targets)
        total = passed + failed

        if failed == 0:
            print(f"✅ ALL TARGETS PASSED ({passed}/{total})")
        else:
            print(f"❌ SOME TARGETS FAILED ({passed}/{total} passed)")
            print(f"   Failed: {', '.join(self.failed_targets)}")


async def main(quick: bool = False):
    """Run all benchmarks."""
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")

    benchmark = PerformanceBenchmark(redis_url, quick=quick)

    try:
        await benchmark.setup()

        # Run benchmarks
        await benchmark.benchmark_throughput()
        await benchmark.benchmark_latency()
        await benchmark.benchmark_algorithm_comparison()
        await benchmark.benchmark_accuracy_under_load()

        if not quick:
            await benchmark.benchmark_concurrent_clients()
            await benchmark.benchmark_rate_limiting_accuracy()
            await benchmark.benchmark_memory_usage()
            await benchmark.benchmark_multi_tenant()

        # Print summary
        benchmark.print_summary()

    except Exception as e:
        print(f"\nBenchmark failed: {e}")
        raise
    finally:
        await benchmark.teardown()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="FastLimit Performance Benchmarks")
    parser.add_argument(
        "--quick", action="store_true", help="Run quick benchmark with fewer iterations"
    )
    args = parser.parse_args()

    asyncio.run(main(quick=args.quick))
