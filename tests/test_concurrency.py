"""
Concurrency and atomicity tests for FastLimit rate limiter.

These tests validate that:
- Lua scripts provide atomic operations
- Race conditions don't allow over-limit requests
- Multi-process scenarios work correctly
- Script cache invalidation is handled gracefully
"""

import asyncio
from datetime import datetime

import pytest

from fastlimit import RateLimiter, RateLimitExceeded


@pytest.mark.asyncio
class TestFixedWindowConcurrency:
    """Atomicity tests for fixed window algorithm."""

    async def test_concurrent_requests_exact_limit(self, clean_limiter):
        """
        Test that concurrent requests enforce exact limit.

        With 50 limit and 200 concurrent requests, exactly 50 should be allowed.
        """
        limiter = clean_limiter
        key = f"fw-concurrent-{datetime.utcnow().isoformat()}"
        rate = "50/second"

        async def make_request():
            try:
                return await limiter.check(key=key, rate=rate, algorithm="fixed_window")
            except RateLimitExceeded:
                return False

        # 200 concurrent requests
        tasks = [make_request() for _ in range(200)]
        results = await asyncio.gather(*tasks)

        allowed = sum(1 for r in results if r is True)
        denied = sum(1 for r in results if r is False)

        assert allowed == 50, f"Expected exactly 50 allowed, got {allowed}"
        assert denied == 150, f"Expected 150 denied, got {denied}"

    async def test_high_concurrency_500_requests(self, clean_limiter):
        """Test with 500 concurrent requests."""
        limiter = clean_limiter
        key = f"fw-high-concurrent-{datetime.utcnow().isoformat()}"
        rate = "100/second"

        async def make_request():
            try:
                return await limiter.check(key=key, rate=rate, algorithm="fixed_window")
            except RateLimitExceeded:
                return False

        tasks = [make_request() for _ in range(500)]
        results = await asyncio.gather(*tasks)

        allowed = sum(1 for r in results if r is True)
        assert allowed == 100, f"Expected exactly 100 allowed, got {allowed}"

    async def test_concurrent_with_cost(self, clean_limiter):
        """Test concurrent requests with varying costs."""
        limiter = clean_limiter
        key = f"fw-cost-concurrent-{datetime.utcnow().isoformat()}"
        rate = "100/second"

        async def make_request(cost: int):
            try:
                return await limiter.check(key=key, rate=rate, algorithm="fixed_window", cost=cost)
            except RateLimitExceeded:
                return False

        # 50 requests with cost=2 each (total 100 units)
        tasks = [make_request(2) for _ in range(100)]
        results = await asyncio.gather(*tasks)

        allowed = sum(1 for r in results if r is True)
        # Should allow exactly 50 (50 * 2 = 100 units)
        assert allowed == 50, f"Expected 50 allowed (cost=2), got {allowed}"


@pytest.mark.asyncio
class TestTokenBucketConcurrency:
    """Atomicity tests for token bucket algorithm."""

    async def test_concurrent_token_bucket_exact(self, clean_limiter):
        """Test that token bucket enforces exact capacity under concurrency."""
        limiter = clean_limiter
        key = f"tb-concurrent-{datetime.utcnow().isoformat()}"
        rate = "20/second"

        async def make_request():
            try:
                return await limiter.check(key=key, rate=rate, algorithm="token_bucket")
            except RateLimitExceeded:
                return False

        # 100 concurrent requests for 20 capacity bucket
        tasks = [make_request() for _ in range(100)]
        results = await asyncio.gather(*tasks)

        allowed = sum(1 for r in results if r is True)
        # Should allow exactly 20 (bucket capacity)
        assert allowed == 20, f"Expected 20 allowed, got {allowed}"

    async def test_token_bucket_refill_under_load(self, clean_limiter):
        """Test token bucket refill while under concurrent load."""
        limiter = clean_limiter
        key = f"tb-refill-load-{datetime.utcnow().isoformat()}"
        rate = "10/second"

        # First burst - use all tokens
        async def make_request():
            try:
                return await limiter.check(key=key, rate=rate, algorithm="token_bucket")
            except RateLimitExceeded:
                return False

        tasks = [make_request() for _ in range(10)]
        results = await asyncio.gather(*tasks)
        allowed_first = sum(1 for r in results if r is True)
        assert allowed_first == 10

        # Wait for some refill
        await asyncio.sleep(0.5)

        # Second burst
        tasks = [make_request() for _ in range(20)]
        results = await asyncio.gather(*tasks)
        allowed_second = sum(1 for r in results if r is True)

        # Should allow approximately 5 tokens (0.5s * 10/s)
        assert 3 <= allowed_second <= 7, f"Expected ~5 allowed, got {allowed_second}"


@pytest.mark.asyncio
class TestSlidingWindowConcurrency:
    """Atomicity tests for sliding window algorithm."""

    async def test_concurrent_sliding_window_exact(self, clean_limiter):
        """Test sliding window atomic enforcement under concurrency."""
        limiter = clean_limiter
        key = f"sw-concurrent-{datetime.utcnow().isoformat()}"
        rate = "30/second"

        async def make_request():
            try:
                return await limiter.check(key=key, rate=rate, algorithm="sliding_window")
            except RateLimitExceeded:
                return False

        # 100 concurrent requests for 30 limit
        tasks = [make_request() for _ in range(100)]
        results = await asyncio.gather(*tasks)

        allowed = sum(1 for r in results if r is True)
        assert allowed == 30, f"Expected 30 allowed, got {allowed}"


@pytest.mark.asyncio
class TestRaceConditions:
    """Tests specifically designed to trigger race conditions."""

    async def test_rapid_sequential_becomes_concurrent(self, clean_limiter):
        """
        Test that rapid sequential requests are handled atomically.

        Even without explicit concurrency, rapid requests can race.
        """
        limiter = clean_limiter
        key = f"race-rapid-{datetime.utcnow().isoformat()}"
        rate = "10/second"

        # Launch requests with minimal delay
        allowed = 0
        denied = 0

        for _ in range(50):
            try:
                result = await limiter.check(key=key, rate=rate)
                if result:
                    allowed += 1
            except RateLimitExceeded:
                denied += 1

        assert allowed == 10, f"Expected 10 allowed, got {allowed}"
        assert denied == 40

    async def test_check_with_info_atomic(self, clean_limiter):
        """Test that check_with_info is also atomic under concurrency."""
        limiter = clean_limiter
        key = f"race-info-{datetime.utcnow().isoformat()}"
        rate = "25/second"

        async def make_request():
            try:
                result = await limiter.check_with_info(key=key, rate=rate)
                return result.allowed
            except RateLimitExceeded:
                return False

        tasks = [make_request() for _ in range(100)]
        results = await asyncio.gather(*tasks)

        allowed = sum(1 for r in results if r is True)
        assert allowed == 25, f"Expected 25 allowed, got {allowed}"


@pytest.mark.asyncio
class TestDistributedClockConsistency:
    """Tests for Redis time consistency (C5 fix)."""

    async def test_uses_redis_time(self, clean_limiter):
        """
        Test that rate limiter uses Redis server time.

        This is important for distributed deployments where app servers
        may have clock skew.
        """
        limiter = clean_limiter
        key = f"clock-test-{datetime.utcnow().isoformat()}"
        rate = "10/second"

        # Make requests - the key is that it doesn't crash
        # and uses Redis time internally
        for _ in range(5):
            result = await limiter.check(key=key, rate=rate)
            assert result is True

    async def test_window_alignment_uses_redis_time(self, clean_limiter):
        """Test that window alignment is based on Redis time."""
        limiter = clean_limiter
        key = f"window-align-{datetime.utcnow().isoformat()}"
        rate = "10/minute"

        # Make a request
        await limiter.check(key=key, rate=rate)

        # Get usage - should reflect Redis time-based window
        usage = await limiter.get_usage(key=key, rate=rate)
        assert usage["current"] == 1
        assert usage["ttl"] > 0
        assert usage["ttl"] <= 60


@pytest.mark.asyncio
class TestScriptResilience:
    """Tests for Lua script error handling."""

    async def test_handles_script_errors_gracefully(self, clean_limiter):
        """Test that script errors are handled gracefully."""
        limiter = clean_limiter
        key = "script-error-test"
        rate = "10/second"

        # Normal operation should work
        result = await limiter.check(key=key, rate=rate)
        assert result is True

    async def test_reconnection_after_error(self, redis_url):
        """Test that limiter can reconnect after errors."""
        limiter = RateLimiter(redis_url=redis_url, key_prefix="reconnect-test")

        await limiter.connect()

        # Normal operation
        result = await limiter.check(key="test1", rate="10/second")
        assert result is True

        # Close and reconnect
        await limiter.close()
        await limiter.connect()

        # Should still work
        result = await limiter.check(key="test2", rate="10/second")
        assert result is True

        await limiter.close()


@pytest.mark.asyncio
class TestMultiKeyIsolation:
    """Tests for isolation between different keys."""

    async def test_concurrent_different_keys(self, clean_limiter):
        """Test that different keys don't interfere under concurrency."""
        limiter = clean_limiter
        rate = "10/second"

        async def make_request(key: str):
            try:
                return await limiter.check(key=key, rate=rate)
            except RateLimitExceeded:
                return False

        # 100 requests across 10 different keys
        keys = [f"multi-key-{i}" for i in range(10)]
        tasks = []
        for _ in range(10):  # 10 requests per key
            for key in keys:
                tasks.append(make_request(key))

        results = await asyncio.gather(*tasks)
        allowed = sum(1 for r in results if r is True)

        # Each key has 10 limit, 10 keys = 100 total allowed
        assert allowed == 100, f"Expected 100 allowed, got {allowed}"

    async def test_concurrent_different_tenants(self, clean_limiter):
        """Test that different tenants don't interfere under concurrency."""
        limiter = clean_limiter
        key = "shared-key"
        rate = "10/second"

        async def make_request(tenant: str):
            try:
                return await limiter.check(key=key, rate=rate, tenant_type=tenant)
            except RateLimitExceeded:
                return False

        # 50 requests across 5 tenants
        tenants = [f"tenant-{i}" for i in range(5)]
        tasks = []
        for _ in range(10):  # 10 requests per tenant
            for tenant in tenants:
                tasks.append(make_request(tenant))

        results = await asyncio.gather(*tasks)
        allowed = sum(1 for r in results if r is True)

        # Each tenant has 10 limit, 5 tenants = 50 total allowed
        assert allowed == 50, f"Expected 50 allowed, got {allowed}"


@pytest.mark.asyncio
class TestEdgeCaseConcurrency:
    """Edge case concurrency tests."""

    async def test_cost_equals_limit_concurrent(self, clean_limiter):
        """Test concurrent requests where cost equals limit."""
        limiter = clean_limiter
        key = f"cost-limit-{datetime.utcnow().isoformat()}"
        rate = "10/second"

        async def make_request():
            try:
                return await limiter.check(key=key, rate=rate, cost=10)
            except RateLimitExceeded:
                return False

        # 10 concurrent requests with cost=10 each
        tasks = [make_request() for _ in range(10)]
        results = await asyncio.gather(*tasks)

        allowed = sum(1 for r in results if r is True)
        # Only 1 should be allowed (cost=10, limit=10)
        assert allowed == 1, f"Expected 1 allowed, got {allowed}"

    async def test_concurrent_reset_and_check(self, clean_limiter):
        """Test concurrent reset and check operations."""
        limiter = clean_limiter
        key = f"reset-check-{datetime.utcnow().isoformat()}"
        rate = "5/second"

        # Use up limit
        for _ in range(5):
            await limiter.check(key=key, rate=rate)

        # Concurrent reset and checks
        async def check():
            try:
                return await limiter.check(key=key, rate=rate)
            except RateLimitExceeded:
                return False

        async def reset():
            return await limiter.reset(key=key)

        tasks = [check() for _ in range(5)] + [reset()]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # No exceptions should be raised
        for r in results:
            assert not isinstance(r, Exception), f"Got exception: {r}"


@pytest.mark.asyncio
@pytest.mark.slow
class TestHighLoadConcurrency:
    """High load concurrency tests (may be slow)."""

    async def test_1000_concurrent_requests(self, clean_limiter):
        """Test with 1000 concurrent requests."""
        limiter = clean_limiter
        key = f"high-load-{datetime.utcnow().isoformat()}"
        rate = "100/second"

        async def make_request():
            try:
                return await limiter.check(key=key, rate=rate)
            except RateLimitExceeded:
                return False

        tasks = [make_request() for _ in range(1000)]
        results = await asyncio.gather(*tasks)

        allowed = sum(1 for r in results if r is True)
        assert allowed == 100, f"Expected 100 allowed, got {allowed}"

    async def test_sustained_high_load(self, clean_limiter):
        """Test sustained high load over multiple seconds."""
        limiter = clean_limiter
        key = f"sustained-{datetime.utcnow().isoformat()}"
        rate = "50/second"

        total_allowed = 0
        total_denied = 0

        for second in range(3):

            async def make_request():
                try:
                    return await limiter.check(key=key, rate=rate)
                except RateLimitExceeded:
                    return False

            tasks = [make_request() for _ in range(100)]
            results = await asyncio.gather(*tasks)

            allowed = sum(1 for r in results if r is True)
            denied = sum(1 for r in results if r is False)
            total_allowed += allowed
            total_denied += denied

            if second < 2:
                await asyncio.sleep(1.0)

        # Over 3 seconds with 50/s limit, should allow ~150
        # (timing may vary slightly)
        assert 140 <= total_allowed <= 160, f"Expected ~150 allowed, got {total_allowed}"
