"""
Demonstration and comparison of rate limiting algorithms.

This script shows the behavior differences between different algorithms
and helps visualize how rate limiting works.
"""

import asyncio
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

from moderato import RateLimiter, RateLimitExceeded  # noqa: E402


class RateLimitDemo:
    """Demo class for testing rate limiting algorithms."""

    def __init__(self, redis_url: str = "redis://localhost:6379"):
        self.limiter = RateLimiter(redis_url=redis_url)
        self.results: dict[str, list[dict[str, Any]]] = {}

    async def setup(self):
        """Connect to Redis."""
        await self.limiter.connect()
        print("Connected to Redis")

    async def cleanup(self):
        """Disconnect from Redis."""
        await self.limiter.close()
        print("Disconnected from Redis")

    async def test_fixed_window_basic(self):
        """Test basic Fixed Window behavior."""
        print("\n" + "=" * 60)
        print("TEST: Fixed Window - Basic Behavior")
        print("=" * 60)

        key = f"demo:fixed:basic:{datetime.utcnow().isoformat()}"
        rate = "5/second"

        print(f"Rate limit: {rate}")
        print("Sending 10 requests...")

        results = []
        for i in range(10):
            try:
                await self.limiter.check(key=key, rate=rate)
                results.append({"request": i + 1, "status": "Allowed"})
                print(f"Request {i+1}: Allowed")
            except RateLimitExceeded as e:
                results.append({"request": i + 1, "status": "Denied", "retry_after": e.retry_after})
                print(f"Request {i+1}: Denied (retry after {e.retry_after}s)")

            await asyncio.sleep(0.05)

        self.results["fixed_window_basic"] = results

        print("\nWaiting 1 second for window to reset...")
        await asyncio.sleep(1)

        print("Sending 1 more request after reset...")
        try:
            await self.limiter.check(key=key, rate=rate)
            print("Request 11: Allowed (window reset)")
        except RateLimitExceeded:
            print("Request 11: Denied")

    async def test_fixed_window_burst(self):
        """Test Fixed Window behavior with burst traffic."""
        print("\n" + "=" * 60)
        print("TEST: Fixed Window - Burst Traffic")
        print("=" * 60)

        key = f"demo:fixed:burst:{datetime.utcnow().isoformat()}"
        rate = "10/second"

        print(f"Rate limit: {rate}")
        print("Sending 20 requests concurrently (burst)...")

        tasks = []
        for i in range(20):
            tasks.append(self._make_request(key, rate, i + 1))

        results = await asyncio.gather(*tasks)

        allowed = sum(1 for r in results if r["allowed"])
        denied = sum(1 for r in results if not r["allowed"])

        print("\nResults:")
        print(f"  Allowed: {allowed}/20")
        print(f"  Denied: {denied}/20")
        print(f"  Success rate: {allowed/20*100:.1f}%")

        self.results["fixed_window_burst"] = results

    async def test_multi_window(self):
        """Test multiple time windows simultaneously."""
        print("\n" + "=" * 60)
        print("TEST: Multiple Time Windows")
        print("=" * 60)

        key = f"demo:multi:{datetime.utcnow().isoformat()}"

        windows = [
            ("2/second", 2),
            ("5/minute", 5),
            ("10/hour", 10),
        ]

        print("Testing with multiple rate limits:")
        for rate, _ in windows:
            print(f"  - {rate}")

        print("\nSending requests...")

        for i in range(8):
            print(f"\nRequest {i+1}:")

            for rate, limit in windows:
                try:
                    await self.limiter.check(key=key, rate=rate)
                    print(f"  {rate}: Allowed")
                except RateLimitExceeded:
                    print(f"  {rate}: Denied (limit {limit} reached)")

            if i == 1:
                print("  Waiting 1 second for per-second limit to reset...")
                await asyncio.sleep(1)

    async def test_tenant_isolation(self):
        """Test tenant isolation."""
        print("\n" + "=" * 60)
        print("TEST: Tenant Isolation")
        print("=" * 60)

        rate = "3/second"
        base_key = f"demo:tenant:{datetime.utcnow().isoformat()}"

        tenants = [
            ("tenant-a", "free"),
            ("tenant-b", "free"),
            ("tenant-a", "premium"),
        ]

        print(f"Rate limit: {rate}")
        print("Testing isolation between tenants and tiers...\n")

        for tenant_id, tier in tenants:
            print(f"Tenant: {tenant_id}, Tier: {tier}")
            key = f"{base_key}:{tenant_id}"

            for i in range(4):
                try:
                    await self.limiter.check(key=key, rate=rate, tenant_type=tier)
                    print(f"  Request {i+1}: Allowed")
                except RateLimitExceeded:
                    print(f"  Request {i+1}: Denied")
            print()

    async def test_cost_multiplication(self):
        """Test cost-based rate limiting."""
        print("\n" + "=" * 60)
        print("TEST: Cost-Based Rate Limiting")
        print("=" * 60)

        key = f"demo:cost:{datetime.utcnow().isoformat()}"
        rate = "10/second"

        print(f"Rate limit: {rate}")
        print("Testing with different request costs...\n")

        requests = [
            ("Normal request", 1),
            ("Normal request", 1),
            ("Expensive request", 5),
            ("Normal request", 1),
            ("Very expensive request", 10),
            ("Normal request", 1),
        ]

        total_cost = 0
        for desc, cost in requests:
            try:
                await self.limiter.check(key=key, rate=rate, cost=cost)
                total_cost += cost
                print(f"{desc} (cost={cost}): Allowed | Total: {total_cost}/10")
            except RateLimitExceeded:
                print(f"{desc} (cost={cost}): Denied | Limit exceeded")

    async def _make_request(self, key: str, rate: str, request_id: int):
        """Helper to make a single request."""
        try:
            await self.limiter.check(key=key, rate=rate)
            return {"request": request_id, "allowed": True}
        except RateLimitExceeded:
            return {"request": request_id, "allowed": False}

    async def benchmark_performance(self):
        """Benchmark rate limiter performance."""
        print("\n" + "=" * 60)
        print("BENCHMARK: Performance Test")
        print("=" * 60)

        iterations = 1000
        rate = "10000/minute"

        print(f"Testing performance with {iterations} requests...")
        print(f"Rate limit: {rate}\n")

        key = f"demo:benchmark:{datetime.utcnow().isoformat()}"
        await self.limiter.check(key=key, rate=rate)

        # Sequential test
        start = time.perf_counter()
        for i in range(iterations):
            key = f"demo:bench:seq:{i}"
            await self.limiter.check(key=key, rate=rate)
        sequential_time = time.perf_counter() - start

        print(f"Sequential ({iterations} requests):")
        print(f"  Time: {sequential_time:.3f}s")
        print(f"  Rate: {iterations/sequential_time:.1f} req/s")
        print(f"  Avg latency: {sequential_time/iterations*1000:.2f}ms")

        # Concurrent test
        start = time.perf_counter()
        tasks = []
        for i in range(iterations):
            key = f"demo:bench:con:{i}"
            tasks.append(self.limiter.check(key=key, rate=rate))
        await asyncio.gather(*tasks)
        concurrent_time = time.perf_counter() - start

        print(f"\nConcurrent ({iterations} requests):")
        print(f"  Time: {concurrent_time:.3f}s")
        print(f"  Rate: {iterations/concurrent_time:.1f} req/s")
        print(f"  Avg latency: {concurrent_time/iterations*1000:.2f}ms")

        print(f"\nSpeedup: {sequential_time/concurrent_time:.2f}x")

    def print_summary(self):
        """Print summary of results."""
        print("\n" + "=" * 60)
        print("SUMMARY")
        print("=" * 60)

        if "fixed_window_basic" in self.results:
            basic = self.results["fixed_window_basic"]
            allowed = sum(1 for r in basic if r["status"] == "Allowed")
            denied = sum(1 for r in basic if r["status"] == "Denied")
            print(f"Fixed Window Basic: {allowed} allowed, {denied} denied")

        if "fixed_window_burst" in self.results:
            burst = self.results["fixed_window_burst"]
            allowed = sum(1 for r in burst if r["allowed"])
            denied = sum(1 for r in burst if not r["allowed"])
            print(f"Fixed Window Burst: {allowed} allowed, {denied} denied")


async def main():
    """Run the demo."""
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")

    print("Moderato Rate Limiting Demo")
    print(f"Redis URL: {redis_url}")

    demo = RateLimitDemo(redis_url)

    try:
        await demo.setup()

        await demo.test_fixed_window_basic()
        await demo.test_fixed_window_burst()
        await demo.test_multi_window()
        await demo.test_tenant_isolation()
        await demo.test_cost_multiplication()
        await demo.benchmark_performance()

        demo.print_summary()

    except Exception as e:
        print(f"\nError: {e}")
    finally:
        await demo.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
