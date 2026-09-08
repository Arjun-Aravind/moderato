"""
Tests for Fixed Window rate limiting algorithm.
"""

import asyncio
from datetime import datetime

import pytest

from fastlimit import RateLimiter, RateLimitExceeded
from tests.conftest import sleep_past_window_boundary


class TestFixedWindow:
    """Test suite for Fixed Window algorithm."""

    @pytest.mark.asyncio
    async def test_basic_rate_limiting(self, clean_limiter):
        """Test that basic rate limiting allows and denies correctly."""
        limiter = clean_limiter
        key = "test-user"
        rate = "5/minute"

        # First 5 requests should pass
        for i in range(5):
            result = await limiter.check(key=key, rate=rate)
            assert result is True, f"Request {i + 1} should be allowed"

        # 6th request should be denied
        with pytest.raises(RateLimitExceeded) as exc_info:
            await limiter.check(key=key, rate=rate)

        assert exc_info.value.retry_after > 0
        assert exc_info.value.retry_after <= 60
        assert exc_info.value.remaining == 0
        assert exc_info.value.limit == rate

    @pytest.mark.asyncio
    async def test_burst_behavior(self, clean_limiter):
        """Test handling of burst requests."""
        limiter = clean_limiter
        key = f"burst-test-{datetime.utcnow().isoformat()}"
        # Hour window: a 1-second window can expire mid-burst on slow CI
        # runners and over-allow; an hour cannot.
        rate = "50/hour"

        # Send 100 requests concurrently
        tasks = []
        for _ in range(100):
            tasks.append(limiter.check(key=key, rate=rate))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Count successful and failed requests
        successful = sum(1 for r in results if r is True)
        failed = sum(1 for r in results if isinstance(r, RateLimitExceeded))

        assert successful == 50, f"Expected 50 successful, got {successful}"
        assert failed == 50, f"Expected 50 failed, got {failed}"

    @pytest.mark.asyncio
    async def test_window_reset(self, clean_limiter):
        """Test that rate limit resets after window expires."""
        limiter = clean_limiter
        key = f"window-test-{datetime.utcnow().isoformat()}"
        rate = "3/second"

        # Start just after a window boundary so the fill below cannot
        # straddle into a second window on slow runners
        await sleep_past_window_boundary(limiter)

        # Use up the limit
        for _ in range(3):
            await limiter.check(key=key, rate=rate)

        # Should be rate limited now
        with pytest.raises(RateLimitExceeded):
            await limiter.check(key=key, rate=rate)

        # Wait for window to reset (add small buffer for timing)
        await asyncio.sleep(1.2)

        # Should work again
        result = await limiter.check(key=key, rate=rate)
        assert result is True

    @pytest.mark.asyncio
    async def test_multi_tenant_isolation(self, clean_limiter):
        """Test that different tenants have isolated rate limits."""
        limiter = clean_limiter

        # Tenant A uses their limit
        for _ in range(5):
            await limiter.check(key="tenant-a", rate="5/minute", tenant_type="premium")

        # Tenant A should be limited
        with pytest.raises(RateLimitExceeded):
            await limiter.check(key="tenant-a", rate="5/minute", tenant_type="premium")

        # But Tenant B should still work
        for _ in range(5):
            result = await limiter.check(key="tenant-b", rate="5/minute", tenant_type="free")
            assert result is True

        # And same tenant with different type should work
        for _ in range(5):
            result = await limiter.check(key="tenant-a", rate="5/minute", tenant_type="free")
            assert result is True

    @pytest.mark.asyncio
    async def test_different_rate_formats(self, clean_limiter):
        """Test parsing of different rate format strings."""
        limiter = clean_limiter

        test_cases = [
            ("10/second", 10, 1),
            ("100/minute", 100, 60),
            ("1000/hour", 1000, 3600),
            ("10000/day", 10000, 86400),
        ]

        for rate_str, expected_requests, expected_window in test_cases:
            from fastlimit.utils import parse_rate

            requests, window = parse_rate(rate_str)
            assert requests == expected_requests
            assert window == expected_window

            # Test that rate limiting works with this format
            key = f"format-test-{rate_str}"
            result = await limiter.check(key=key, rate=rate_str)
            assert result is True

    @pytest.mark.asyncio
    async def test_concurrent_requests_same_key(self, clean_limiter):
        """Test race conditions with concurrent requests to same key."""
        limiter = clean_limiter
        key = "concurrent-test"
        rate = "10/hour"

        # Send 20 concurrent requests
        tasks = []
        for _ in range(20):
            tasks.append(limiter.check(key=key, rate=rate))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Exactly 10 should succeed, 10 should fail
        successful = sum(1 for r in results if r is True)
        failed = sum(1 for r in results if isinstance(r, RateLimitExceeded))

        assert successful == 10
        assert failed == 10

    @pytest.mark.asyncio
    async def test_cost_parameter(self, clean_limiter):
        """Test that cost parameter correctly multiplies request count."""
        limiter = clean_limiter
        key = "cost-test"
        rate = "10/minute"

        # First request with cost=5 should use half the limit
        result = await limiter.check(key=key, rate=rate, cost=5)
        assert result is True

        # Second request with cost=5 should use the remaining limit
        result = await limiter.check(key=key, rate=rate, cost=5)
        assert result is True

        # Third request should be denied (10 total used)
        with pytest.raises(RateLimitExceeded):
            await limiter.check(key=key, rate=rate, cost=1)

    @pytest.mark.asyncio
    async def test_get_usage_stats(self, clean_limiter):
        """Test getting usage statistics for a key."""
        limiter = clean_limiter
        key = "usage-test"
        rate = "100/minute"

        # Make some requests
        for _ in range(42):
            await limiter.check(key=key, rate=rate)

        # Get usage stats
        usage = await limiter.get_usage(key=key, rate=rate)

        assert usage["current"] == 42
        assert usage["limit"] == 100
        assert usage["remaining"] == 58
        assert usage["ttl"] > 0
        assert usage["ttl"] <= 60

    @pytest.mark.asyncio
    async def test_reset_functionality(self, clean_limiter):
        """Test manual reset of rate limits."""
        limiter = clean_limiter
        key = "reset-test"
        rate = "5/minute"

        # Use up the limit
        for _ in range(5):
            await limiter.check(key=key, rate=rate)

        # Should be rate limited
        with pytest.raises(RateLimitExceeded):
            await limiter.check(key=key, rate=rate)

        # Reset the limit
        reset_result = await limiter.reset(key=key)
        assert reset_result is True

        # Should work again
        result = await limiter.check(key=key, rate=rate)
        assert result is True

    @pytest.mark.asyncio
    async def test_key_sanitization(self, clean_limiter):
        """Test that keys with special characters are handled correctly."""
        limiter = clean_limiter

        # Keys with various special characters
        test_keys = [
            "user:123:session",
            "ip:192.168.1.1",
            "email@example.com",
            "path/to/resource",
            "key with spaces",
            "key[with]brackets",
            "key{with}braces",
        ]

        for key in test_keys:
            result = await limiter.check(key=key, rate="10/minute")
            assert result is True, f"Failed for key: {key}"

    @pytest.mark.asyncio
    async def test_redis_connection_failure(self):
        """Test graceful handling of Redis connection failures."""
        limiter = RateLimiter(redis_url="redis://invalid:6379")

        with pytest.raises(Exception) as exc_info:
            await limiter.connect()

        assert "connect" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_health_check(self, clean_limiter):
        """Test health check functionality."""
        limiter = clean_limiter

        # Should be healthy after connection
        health = await limiter.health_check()
        assert health is True

        # Close connection
        await limiter.close()

        # Should be unhealthy after closing
        health = await limiter.health_check()
        assert health is False

    @pytest.mark.asyncio
    async def test_context_manager(self, redis_url):
        """Test using RateLimiter as async context manager."""
        async with RateLimiter(redis_url=redis_url) as limiter:
            result = await limiter.check(key="context-test", rate="10/minute")
            assert result is True

        # Connection should be closed after exiting context
        health = await limiter.health_check()
        assert health is False

    @pytest.mark.asyncio
    async def test_different_time_windows(self, clean_limiter):
        """Test that different time windows work correctly."""
        limiter = clean_limiter
        base_key = "window"

        # These should all be independent
        rates = ["5/second", "10/minute", "100/hour", "1000/day"]

        for rate in rates:
            # Each rate should work independently
            result = await limiter.check(key=base_key, rate=rate)
            assert result is True

        # Exhaust the second-based limit, aligned to a fresh window boundary
        # so the fill cannot straddle on slow runners. The earlier loop's
        # 5/second check lives in the previous window, so base_key starts
        # at 0 here and the same-key cross-window independence is preserved.
        await sleep_past_window_boundary(limiter)
        for _ in range(5):
            assert await limiter.check(key=base_key, rate="5/second") is True

        # Second-based should be exhausted
        with pytest.raises(RateLimitExceeded):
            await limiter.check(key=base_key, rate="5/second")

        # But minute-based should still work (different window, same key)
        result = await limiter.check(key=base_key, rate="10/minute")
        assert result is True

    @pytest.mark.asyncio
    @pytest.mark.slow
    async def test_performance_benchmark(self, clean_limiter, benchmark):
        """Benchmark rate limiter performance."""
        limiter = clean_limiter
        iterations = 1000

        benchmark.start()

        tasks = []
        for i in range(iterations):
            key = f"perf-test-{i % 100}"  # Use 100 different keys
            tasks.append(limiter.check(key=key, rate="1000/minute"))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        benchmark.stop()
        benchmark.iterations = iterations

        # All should succeed (different keys, high limit)
        successful = sum(1 for r in results if r is True)
        assert successful == iterations

        # Check performance (should handle >500 ops/sec)
        assert benchmark.rate > 500, f"Performance too low: {benchmark.rate:.1f} ops/sec"
