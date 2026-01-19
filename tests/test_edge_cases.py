"""
Edge case and error handling tests for FastLimit.

These tests validate:
- Input validation and error messages
- Extreme values (very high limits, very short/long windows)
- Connection handling and error recovery
- The new check_with_info() API (I1 fix)
- Algorithm-aware get_usage() and reset() (C6/NEW-C12 fixes)
"""

import asyncio
from datetime import datetime

import pytest

from fastlimit import RateLimiter, RateLimitExceeded
from fastlimit.exceptions import RateLimitConfigError
from fastlimit.models import CheckResult


@pytest.mark.asyncio
class TestInputValidation:
    """Tests for input validation and error handling."""

    async def test_invalid_algorithm_raises_error(self, clean_limiter):
        """Test that unknown algorithm raises RateLimitConfigError."""
        limiter = clean_limiter

        with pytest.raises(RateLimitConfigError) as exc_info:
            await limiter.check(key="test", rate="10/minute", algorithm="unknown_algo")

        assert "algorithm" in str(exc_info.value).lower()

    async def test_invalid_rate_format_raises_error(self, clean_limiter):
        """Test that invalid rate format raises error."""
        limiter = clean_limiter

        invalid_rates = [
            "10",  # Missing period
            "/minute",  # Missing number
            "10/week",  # Invalid period
            "abc/minute",  # Non-numeric
            "",  # Empty string
        ]

        for rate in invalid_rates:
            with pytest.raises((RateLimitConfigError, ValueError)) as exc_info:
                await limiter.check(key="test", rate=rate)
            # Should have informative error message
            assert exc_info.value is not None

    async def test_zero_cost_handled(self, clean_limiter):
        """Test that cost=0 is handled gracefully."""
        limiter = clean_limiter
        key = f"zero-cost-{datetime.utcnow().isoformat()}"

        # cost=0 should work but not consume any tokens
        result = await limiter.check(key=key, rate="10/minute", cost=0)
        assert result is True

        # Usage should show 0 consumed
        usage = await limiter.get_usage(key=key, rate="10/minute")
        assert usage["current"] == 0

    async def test_very_high_cost(self, clean_limiter):
        """Test that very high cost is handled correctly."""
        limiter = clean_limiter
        key = f"high-cost-{datetime.utcnow().isoformat()}"

        # Cost higher than limit should be denied
        with pytest.raises(RateLimitExceeded):
            await limiter.check(key=key, rate="10/minute", cost=15)


@pytest.mark.asyncio
class TestExtremeValues:
    """Tests for extreme rate limit values."""

    async def test_very_high_limit(self, clean_limiter):
        """Test that very high limit (1,000,000/hour) works."""
        limiter = clean_limiter
        key = f"high-limit-{datetime.utcnow().isoformat()}"
        rate = "1000000/hour"

        # Should allow many requests
        for _ in range(100):
            result = await limiter.check(key=key, rate=rate)
            assert result is True

        # Check usage
        usage = await limiter.get_usage(key=key, rate=rate)
        assert usage["current"] == 100
        assert usage["limit"] == 1000000
        assert usage["remaining"] == 999900

    async def test_very_short_window(self, clean_limiter):
        """Test that very short window (per second) works correctly."""
        limiter = clean_limiter
        key = f"short-window-{datetime.utcnow().isoformat()}"
        rate = "5/second"

        # Use up limit
        for _ in range(5):
            await limiter.check(key=key, rate=rate)

        # Should be rate limited
        with pytest.raises(RateLimitExceeded) as exc_info:
            await limiter.check(key=key, rate=rate)

        # retry_after should be <= 1 second
        assert exc_info.value.retry_after <= 1

    async def test_very_long_window(self, clean_limiter):
        """Test that very long window (per day) works correctly."""
        limiter = clean_limiter
        key = f"long-window-{datetime.utcnow().isoformat()}"
        rate = "1000/day"

        # Should work
        for _ in range(10):
            result = await limiter.check(key=key, rate=rate)
            assert result is True

        usage = await limiter.get_usage(key=key, rate=rate)
        assert usage["window_seconds"] == 86400
        assert usage["limit"] == 1000

    async def test_low_rate_token_bucket(self, clean_limiter):
        """
        Test token bucket with very low rate (C4 fix).

        This validates that "1/hour" doesn't cause divide-by-zero or crash.
        """
        limiter = clean_limiter
        key = f"low-rate-tb-{datetime.utcnow().isoformat()}"
        rate = "1/hour"

        # First request should succeed
        result = await limiter.check(key=key, rate=rate, algorithm="token_bucket")
        assert result is True

        # Second should be denied
        with pytest.raises(RateLimitExceeded):
            await limiter.check(key=key, rate=rate, algorithm="token_bucket")

    async def test_very_low_rate_token_bucket(self, clean_limiter):
        """Test token bucket with 10/day rate."""
        limiter = clean_limiter
        key = f"very-low-rate-{datetime.utcnow().isoformat()}"
        rate = "10/day"

        # Should allow requests
        for _ in range(10):
            result = await limiter.check(key=key, rate=rate, algorithm="token_bucket")
            assert result is True

        # Should be denied after limit
        with pytest.raises(RateLimitExceeded):
            await limiter.check(key=key, rate=rate, algorithm="token_bucket")


@pytest.mark.asyncio
class TestConnectionHandling:
    """Tests for connection handling and error recovery."""

    async def test_redis_connection_failure(self):
        """Test proper error on bad Redis connection."""
        limiter = RateLimiter(redis_url="redis://invalid-host:6379")

        with pytest.raises(Exception) as exc_info:
            await limiter.connect()

        # Should be a connection-related error
        error_msg = str(exc_info.value).lower()
        assert any(
            word in error_msg for word in ["connect", "resolve", "name", "address", "refused"]
        )

    async def test_health_check_before_connect(self, redis_url):
        """Test health check before connection."""
        limiter = RateLimiter(redis_url=redis_url)

        # Should return False when not connected
        health = await limiter.health_check()
        assert health is False

    async def test_health_check_after_connect(self, clean_limiter):
        """Test health check after connection."""
        limiter = clean_limiter

        health = await limiter.health_check()
        assert health is True

    async def test_health_check_after_close(self, redis_url):
        """Test health check after closing connection."""
        limiter = RateLimiter(redis_url=redis_url)
        await limiter.connect()
        await limiter.close()

        health = await limiter.health_check()
        assert health is False

    async def test_reconnection_after_disconnect(self, redis_url):
        """Test that limiter can reconnect after being closed."""
        limiter = RateLimiter(redis_url=redis_url, key_prefix="reconnect-test")

        # First connection
        await limiter.connect()
        result = await limiter.check(key="test1", rate="10/second")
        assert result is True

        # Close
        await limiter.close()

        # Reconnect
        await limiter.connect()
        result = await limiter.check(key="test2", rate="10/second")
        assert result is True

        await limiter.close()

    async def test_auto_connect_on_check(self, redis_url):
        """Test that check() auto-connects if not connected."""
        limiter = RateLimiter(redis_url=redis_url, key_prefix="auto-connect")

        # Don't explicitly connect - check() should auto-connect
        result = await limiter.check(key="auto-test", rate="10/minute")
        assert result is True

        await limiter.close()


@pytest.mark.asyncio
class TestCheckWithInfo:
    """Tests for check_with_info() API (I1 fix - eliminates double Redis read)."""

    async def test_returns_check_result(self, clean_limiter):
        """Test that check_with_info returns CheckResult dataclass."""
        limiter = clean_limiter
        key = f"info-result-{datetime.utcnow().isoformat()}"

        result = await limiter.check_with_info(key=key, rate="10/minute")

        assert isinstance(result, CheckResult)
        assert result.allowed is True
        assert result.limit == 10
        assert result.remaining == 9  # Used 1
        assert result.retry_after == 0  # Allowed, so no retry needed
        assert result.window_seconds == 60

    async def test_check_with_info_no_exception_when_allowed(self, clean_limiter):
        """Test that check_with_info doesn't raise when allowed."""
        limiter = clean_limiter
        key = f"info-no-exc-{datetime.utcnow().isoformat()}"

        # Should not raise, should return result
        result = await limiter.check_with_info(key=key, rate="10/minute")
        assert result.allowed is True

    async def test_check_with_info_when_denied(self, clean_limiter):
        """Test check_with_info when rate limit is exceeded."""
        limiter = clean_limiter
        key = f"info-denied-{datetime.utcnow().isoformat()}"
        rate = "5/minute"

        # Use up the limit
        for _ in range(5):
            await limiter.check_with_info(key=key, rate=rate)

        # 6th request should raise RateLimitExceeded (for backward compat)
        with pytest.raises(RateLimitExceeded) as exc_info:
            await limiter.check_with_info(key=key, rate=rate)

        # Exception should contain info
        assert exc_info.value.remaining == 0
        assert exc_info.value.retry_after > 0
        assert exc_info.value.limit == rate

    async def test_check_with_info_remaining_decrements(self, clean_limiter):
        """Test that remaining decrements with each request."""
        limiter = clean_limiter
        key = f"info-decrement-{datetime.utcnow().isoformat()}"
        rate = "10/minute"

        for i in range(10):
            result = await limiter.check_with_info(key=key, rate=rate)
            assert result.remaining == 9 - i

    async def test_check_with_info_all_algorithms(self, clean_limiter):
        """Test check_with_info with all algorithms."""
        limiter = clean_limiter
        algorithms = ["fixed_window", "token_bucket", "sliding_window"]

        for algo in algorithms:
            key = f"info-algo-{algo}-{datetime.utcnow().isoformat()}"
            result = await limiter.check_with_info(key=key, rate="10/minute", algorithm=algo)
            assert isinstance(result, CheckResult)
            assert result.allowed is True
            assert result.limit == 10


@pytest.mark.asyncio
class TestAlgorithmAwareGetUsage:
    """Tests for algorithm-aware get_usage() (C6 fix)."""

    async def test_get_usage_fixed_window(self, clean_limiter):
        """Test get_usage with fixed_window algorithm."""
        limiter = clean_limiter
        key = f"usage-fw-{datetime.utcnow().isoformat()}"
        rate = "100/minute"

        for _ in range(25):
            await limiter.check(key=key, rate=rate, algorithm="fixed_window")

        usage = await limiter.get_usage(key=key, rate=rate, algorithm="fixed_window")

        assert usage["current"] == 25
        assert usage["limit"] == 100
        assert usage["remaining"] == 75
        assert "ttl" in usage
        assert usage["ttl"] > 0
        assert usage["ttl"] <= 60

    async def test_get_usage_token_bucket(self, clean_limiter):
        """Test get_usage with token_bucket algorithm."""
        limiter = clean_limiter
        key = f"usage-tb-{datetime.utcnow().isoformat()}"
        rate = "100/minute"

        for _ in range(30):
            await limiter.check(key=key, rate=rate, algorithm="token_bucket")

        usage = await limiter.get_usage(key=key, rate=rate, algorithm="token_bucket")

        assert "tokens" in usage
        assert usage["limit"] == 100
        assert usage["remaining"] >= 68  # Started with 100, used 30, some refill
        assert usage["remaining"] <= 72

    async def test_get_usage_sliding_window(self, clean_limiter):
        """Test get_usage with sliding_window algorithm."""
        limiter = clean_limiter
        key = f"usage-sw-{datetime.utcnow().isoformat()}"
        rate = "100/minute"

        for _ in range(20):
            await limiter.check(key=key, rate=rate, algorithm="sliding_window")

        usage = await limiter.get_usage(key=key, rate=rate, algorithm="sliding_window")

        assert usage["current_window"] == 20
        assert usage["limit"] == 100
        assert usage["remaining"] == 80
        assert "weight" in usage
        assert "previous_window" in usage


@pytest.mark.asyncio
class TestAlgorithmAwareReset:
    """Tests for algorithm-aware reset() (NEW-C12 fix)."""

    async def test_reset_fixed_window(self, clean_limiter):
        """Test reset for fixed_window algorithm."""
        limiter = clean_limiter
        key = f"reset-fw-{datetime.utcnow().isoformat()}"
        rate = "5/minute"

        # Use up limit
        for _ in range(5):
            await limiter.check(key=key, rate=rate, algorithm="fixed_window")

        # Should be limited
        with pytest.raises(RateLimitExceeded):
            await limiter.check(key=key, rate=rate, algorithm="fixed_window")

        # Reset
        result = await limiter.reset(key=key, algorithm="fixed_window")
        assert result is True

        # Should work again
        result = await limiter.check(key=key, rate=rate, algorithm="fixed_window")
        assert result is True

    async def test_reset_token_bucket(self, clean_limiter):
        """Test reset for token_bucket algorithm."""
        limiter = clean_limiter
        key = f"reset-tb-{datetime.utcnow().isoformat()}"
        rate = "5/minute"

        # Use up tokens
        for _ in range(5):
            await limiter.check(key=key, rate=rate, algorithm="token_bucket")

        # Should be limited
        with pytest.raises(RateLimitExceeded):
            await limiter.check(key=key, rate=rate, algorithm="token_bucket")

        # Reset
        result = await limiter.reset(key=key, algorithm="token_bucket")
        assert result is True

        # Should work again
        result = await limiter.check(key=key, rate=rate, algorithm="token_bucket")
        assert result is True

    async def test_reset_sliding_window(self, clean_limiter):
        """Test reset for sliding_window algorithm."""
        limiter = clean_limiter
        key = f"reset-sw-{datetime.utcnow().isoformat()}"
        rate = "5/minute"

        # Use up limit
        for _ in range(5):
            await limiter.check(key=key, rate=rate, algorithm="sliding_window")

        # Should be limited
        with pytest.raises(RateLimitExceeded):
            await limiter.check(key=key, rate=rate, algorithm="sliding_window")

        # Reset
        result = await limiter.reset(key=key, algorithm="sliding_window")
        assert result is True

        # Should work again
        result = await limiter.check(key=key, rate=rate, algorithm="sliding_window")
        assert result is True

    async def test_reset_all_algorithms(self, clean_limiter):
        """Test reset with algorithm='all' clears all types."""
        limiter = clean_limiter
        key = f"reset-all-{datetime.utcnow().isoformat()}"
        rate = "5/minute"

        # Use up limits on all algorithms
        for algo in ["fixed_window", "token_bucket", "sliding_window"]:
            for _ in range(5):
                await limiter.check(key=key, rate=rate, algorithm=algo)

        # All should be limited
        for algo in ["fixed_window", "token_bucket", "sliding_window"]:
            with pytest.raises(RateLimitExceeded):
                await limiter.check(key=key, rate=rate, algorithm=algo)

        # Reset all
        result = await limiter.reset(key=key, algorithm="all")
        assert result is True

        # All should work again
        for algo in ["fixed_window", "token_bucket", "sliding_window"]:
            result = await limiter.check(key=key, rate=rate, algorithm=algo)
            assert result is True


@pytest.mark.asyncio
class TestContextManager:
    """Tests for async context manager usage."""

    async def test_context_manager_basic(self, redis_url):
        """Test using RateLimiter as async context manager."""
        async with RateLimiter(redis_url=redis_url, key_prefix="ctx-test") as limiter:
            result = await limiter.check(key="test", rate="10/minute")
            assert result is True

            health = await limiter.health_check()
            assert health is True

        # After exiting context, should be closed
        health = await limiter.health_check()
        assert health is False

    async def test_context_manager_exception(self, redis_url):
        """Test that context manager closes even on exception."""
        limiter = None
        try:
            async with RateLimiter(redis_url=redis_url, key_prefix="ctx-exc") as lim:
                limiter = lim
                await lim.check(key="test", rate="10/minute")
                raise ValueError("Test exception")
        except ValueError:
            pass

        # Should still be closed
        if limiter:
            health = await limiter.health_check()
            assert health is False


@pytest.mark.asyncio
class TestRetryAfterAccuracy:
    """Tests for retry_after accuracy."""

    async def test_retry_after_fixed_window(self, clean_limiter):
        """Test that retry_after is accurate for fixed window."""
        limiter = clean_limiter
        key = f"retry-fw-{datetime.utcnow().isoformat()}"
        rate = "5/second"

        # Use up limit
        for _ in range(5):
            await limiter.check(key=key, rate=rate)

        # Get retry_after
        try:
            await limiter.check(key=key, rate=rate)
        except RateLimitExceeded as e:
            retry_after = e.retry_after

        # retry_after should be <= window size (1 second)
        assert retry_after <= 1
        assert retry_after >= 0

    async def test_retry_after_token_bucket(self, clean_limiter):
        """Test that retry_after is accurate for token bucket."""
        limiter = clean_limiter
        key = f"retry-tb-{datetime.utcnow().isoformat()}"
        rate = "10/second"

        # Use up all tokens
        for _ in range(10):
            await limiter.check(key=key, rate=rate, algorithm="token_bucket")

        # Get retry_after
        try:
            await limiter.check(key=key, rate=rate, algorithm="token_bucket")
        except RateLimitExceeded as e:
            retry_after = e.retry_after

        # Should wait and then be allowed
        await asyncio.sleep(retry_after + 0.1)

        result = await limiter.check(key=key, rate=rate, algorithm="token_bucket")
        assert result is True


@pytest.mark.asyncio
class TestEmptyAndMissingKeys:
    """Tests for edge cases with empty or missing data."""

    async def test_get_usage_nonexistent_key(self, clean_limiter):
        """Test get_usage for a key that doesn't exist."""
        limiter = clean_limiter
        key = f"nonexistent-{datetime.utcnow().isoformat()}"

        usage = await limiter.get_usage(key=key, rate="10/minute")

        # Should return zero usage
        assert usage["current"] == 0
        assert usage["limit"] == 10
        assert usage["remaining"] == 10

    async def test_reset_nonexistent_key(self, clean_limiter):
        """Test reset for a key that doesn't exist."""
        limiter = clean_limiter
        key = f"nonexistent-reset-{datetime.utcnow().isoformat()}"

        # Should not raise, may return True or False
        result = await limiter.reset(key=key)
        # Just verify no exception
        assert result is not None
