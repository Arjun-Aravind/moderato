"""
Tests for Token Bucket rate limiting algorithm.
"""

import asyncio
import math
import random
from fractions import Fraction
from pathlib import Path
from uuid import uuid4

import pytest

from moderato import RateLimitExceeded
from moderato.utils import generate_key
from tests.conftest import sleep_past_window_boundary


@pytest.fixture
def token_clock(redis_client):
    """Execute the actual script with deterministic TIME; TTL stays real."""
    source = (Path(__file__).parents[1] / "moderato/scripts/token_bucket.lua").read_text()
    key = f"test:token-clock:{uuid4().hex}"
    base_ms = 2_000_000_000_000

    async def check(elapsed_ms, cost=1000, capacity=1000, window_seconds=3600):
        now = base_ms + elapsed_ms
        assert source.count("redis.call('TIME')") == 1
        script = source.replace(
            "redis.call('TIME')", f"{{'{now // 1000}', '{(now % 1000) * 1000}'}}"
        )
        return await redis_client.eval(
            script, 1, key, capacity, capacity / window_seconds, window_seconds, cost
        )

    return check, key, base_ms


@pytest.mark.asyncio
class TestTokenBucket:
    """Test suite for Token Bucket algorithm."""

    async def test_versioned_keys_isolate_legacy_writers(self, clean_limiter, redis_client):
        identity = f"rolling-{uuid4().hex}"
        legacy_key = generate_key(
            clean_limiter.config.key_prefix, identity, "default", "p2x3600", "bucket"
        )
        now_ms = await clean_limiter.backend.get_redis_time_ms()
        await redis_client.hset(legacy_key, mapping={"tokens": 0, "last_refill_ms": now_ms})
        await redis_client.expire(legacy_key, 7260)
        legacy_before = await redis_client.hgetall(legacy_key)

        result = await clean_limiter.check_with_info(
            key=identity, rate="2/hour", algorithm="token_bucket"
        )
        assert result.allowed
        assert result.remaining == 1
        assert await redis_client.hgetall(legacy_key) == legacy_before
        versioned_key = f"{legacy_key}:v2"
        assert await redis_client.exists(versioned_key)

        # Simulate an old writer moving its timestamp. New usage/admission
        # must still read only the versioned hash, not the legacy state.
        await redis_client.hset(legacy_key, mapping={"tokens": 2000, "last_refill_ms": now_ms})
        legacy_after = await redis_client.hgetall(legacy_key)
        usage = await clean_limiter.get_usage(key=identity, rate="2/hour", algorithm="token_bucket")
        assert usage["remaining"] == 1
        assert await clean_limiter.check(key=identity, rate="2/hour", algorithm="token_bucket")
        assert not (
            await clean_limiter.check_with_info(
                key=identity, rate="2/hour", algorithm="token_bucket"
            )
        ).allowed
        assert await redis_client.hgetall(legacy_key) == legacy_after

    @pytest.mark.parametrize("tenant_type", [None, "default"])
    async def test_reset_finds_versioned_token_keys(self, clean_limiter, redis_client, tenant_type):
        identity = f"version-reset-{uuid4().hex}"
        token_key = generate_key(
            clean_limiter.config.key_prefix, identity, "default", "p1x3600", "bucket:v2"
        )
        unrelated = generate_key(
            clean_limiter.config.key_prefix, identity, "default", "p1x3600", "bucket:v3"
        )
        await redis_client.hset(token_key, mapping={"tokens": 0, "last_refill_ms": 1})
        await redis_client.set(unrelated, "keep")
        await clean_limiter.check(key=identity, rate="1/hour", algorithm="fixed_window")
        assert await clean_limiter.reset(
            identity, algorithm="token_bucket", tenant_type=tenant_type
        )
        assert not await redis_client.exists(token_key)
        assert await redis_client.get(unrelated) == "keep"
        assert (await clean_limiter.get_usage(key=identity, rate="1/hour"))["current"] == 1

    async def test_polling_preserves_fractional_refill(self, token_clock):
        check, _, _ = token_clock
        assert (await check(0))[0] == 1
        for elapsed_ms in range(5000, 3_600_000, 5000):
            assert (await check(elapsed_ms))[0] == 0
        # Independent capacity/window calculation: exactly one request/hour.
        assert (await check(3_600_000))[:2] == [1, 0]

    async def test_uninterrupted_refill_and_capacity_cap(self, token_clock):
        check, _, _ = token_clock
        assert (await check(0))[:2] == [1, 0]
        assert (await check(3_600_000))[:2] == [1, 0]
        assert (await check(10_800_000))[:2] == [1, 0]
        assert (await check(10_800_000))[0] == 0

    async def test_full_bucket_discards_idle_fraction(self, token_clock, redis_client):
        check, key, base_ms = token_clock
        # Full-state boundary fixture; integer public costs, no claim that
        # every normal request history reaches this particular hash state.
        await redis_client.hset(key, mapping={"tokens": 1000, "last_refill_ms": base_ms})
        depletion = await check(3000)
        assert depletion[:2] == [1, 0]
        # 3597 seconds since depletion yields only 999.166... scaled units.
        assert (await check(3_600_000))[0] == 0
        assert (await check(3_603_000))[:2] == [1, 0]
        assert depletion[3] == base_ms // 1000 + 3603

    async def test_refill_survives_partial_consumption(self, token_clock):
        check, _, _ = token_clock
        assert (await check(0))[0] == 1
        # Low-level scaled costs exercise remainder across admissions too.
        assert (await check(5000, cost=1))[:2] == [1, 0]
        assert (await check(3_600_000, cost=999))[:2] == [1, 0]
        assert (await check(3_600_000, cost=1))[0] == 0

    async def test_usage_does_not_credit_refill_twice(
        self, clean_limiter, redis_client, monkeypatch
    ):
        key = f"refill-usage-{uuid4().hex}"
        await clean_limiter.check(key=key, rate="2/hour", algorithm="token_bucket", cost=2)
        keys = [
            key
            async for key in clean_limiter.backend.iter_keys(f"{clean_limiter.config.key_prefix}:*")
        ]
        assert len(keys) == 1
        origin_ms = 2_000_000_000_000
        await redis_client.hset(
            keys[0],
            mapping={"tokens": 1000, "last_refill_ms": origin_ms, "refill_units": 1000},
        )

        async def half_hour_later():
            return origin_ms // 1000 + 1800, 0

        monkeypatch.setattr(clean_limiter.backend, "get_redis_time", half_hour_later)
        usage = await clean_limiter.get_usage(key=key, rate="2/hour", algorithm="token_bucket")
        # The 1000 units accrued over this half hour are already in tokens.
        assert usage["tokens"] == 1
        assert usage["remaining"] == 1

    @pytest.mark.parametrize("window_seconds", [1, 3, 7, 60, 3600, 86400])
    async def test_refill_matches_rational_model(self, token_clock, window_seconds):
        check, _, _ = token_clock
        rng = random.Random(40406392)
        capacity = 11000
        balance = Fraction(capacity)
        elapsed_ms = 0
        for _ in range(200):
            gap = rng.choice([0, 1, 999, 5000, window_seconds * 1000, 999_999])
            elapsed_ms += gap
            balance = min(capacity, balance + Fraction(capacity * gap, window_seconds * 1000))
            cost = rng.choice([1, 1000, 3000, capacity])
            allowed = balance >= cost
            if allowed:
                balance -= cost
            actual = await check(elapsed_ms, cost, capacity, window_seconds)
            assert actual[:2] == [int(allowed), int(balance) if allowed else 0], (
                window_seconds,
                elapsed_ms,
                cost,
                balance,
                actual,
            )

    async def test_time_is_read_inside_lua(self, clean_limiter, monkeypatch):
        async def unexpected_time_call():
            raise AssertionError("token-bucket check must get time inside Lua")

        monkeypatch.setattr(clean_limiter.backend, "get_redis_time", unexpected_time_call)

        assert await clean_limiter.check(
            key=f"lua-time-{uuid4().hex}", rate="1/minute", algorithm="token_bucket"
        )

    async def test_denied_reset_at_not_before_retry_after(self, clean_limiter):
        key = f"tb-reset-{uuid4().hex}"
        await clean_limiter.check(key=key, rate="1/hour", algorithm="token_bucket")
        # At 1/hour no token accrues in this gap, so the bucket's refill clock
        # stays at the first check while Retry-After counts from the second.
        await asyncio.sleep(1.1)
        before_ms = await clean_limiter.backend.get_redis_time_ms()

        result = await clean_limiter.check_with_info(
            key=key, rate="1/hour", algorithm="token_bucket"
        )

        assert not result.allowed
        assert result.reset_at >= math.ceil(before_ms / 1000 + result.retry_after)

    async def test_basic_token_bucket(self, clean_limiter):
        """Test basic token bucket rate limiting."""
        limiter = clean_limiter
        key = "token-bucket-test"
        rate = "10/second"

        # First request should succeed (bucket starts full)
        result = await limiter.check(key=key, rate=rate, algorithm="token_bucket")
        assert result is True

        # Should allow up to 10 requests total
        tasks = [limiter.check(key=key, rate=rate, algorithm="token_bucket") for _ in range(9)]
        results = await asyncio.gather(*tasks)
        assert all(r is True for r in results)

        # 11th request should be rate limited. cost=10 (full capacity) makes
        # the denial timing-proof: a slow runner's drain can leak a token or
        # two back via refill, but a full bucket takes a full second.
        with pytest.raises(RateLimitExceeded):
            await limiter.check(key=key, rate=rate, algorithm="token_bucket", cost=10)

    async def test_token_refill(self, clean_limiter):
        """Test that tokens refill over time."""
        limiter = clean_limiter
        key = "token-refill-test"
        rate = "10/second"  # 10 tokens/sec refill rate

        # Consume all tokens at once (a sequential fill is slower than the
        # 10/s refill on slow runners, which tops the bucket back up)
        tasks = [limiter.check(key=key, rate=rate, algorithm="token_bucket") for _ in range(10)]
        await asyncio.gather(*tasks)

        # Next request should fail (cost=10: refill during a slow drain can
        # leak a token or two, but not a full bucket)
        with pytest.raises(RateLimitExceeded):
            await limiter.check(key=key, rate=rate, algorithm="token_bucket", cost=10)

        # Wait 0.5 seconds (should refill ~5 tokens)
        await asyncio.sleep(0.5)

        # Should be able to make ~5 requests now
        for _ in range(4):  # Use 4 to be safe with timing
            result = await limiter.check(key=key, rate=rate, algorithm="token_bucket")
            assert result is True

    async def test_burst_capacity(self, clean_limiter):
        """Test that token bucket allows controlled bursts."""
        limiter = clean_limiter
        key = "burst-test"
        rate = "100/minute"  # ~1.67 tokens/sec, 100 token capacity

        # Should allow burst of 100 requests immediately
        tasks = [limiter.check(key=key, rate=rate, algorithm="token_bucket") for _ in range(100)]
        results = await asyncio.gather(*tasks)
        assert all(r is True for r in results)

        # 101st request should fail (cost=100: refill during the drain can
        # leak a few tokens, not a full 100-token bucket)
        with pytest.raises(RateLimitExceeded):
            await limiter.check(key=key, rate=rate, algorithm="token_bucket", cost=100)

    async def test_smooth_rate_limiting(self, clean_limiter):
        """Test that token bucket provides smooth rate limiting."""
        limiter = clean_limiter
        key = "smooth-test"
        rate = "10/second"

        # Consume all tokens at once (drain faster than the 10/s refill)
        tasks = [limiter.check(key=key, rate=rate, algorithm="token_bucket") for _ in range(10)]
        await asyncio.gather(*tasks)

        # Should fail immediately (cost=10: drain refill can leak a token
        # or two on slow runners, but not a full bucket)
        with pytest.raises(RateLimitExceeded):
            await limiter.check(key=key, rate=rate, algorithm="token_bucket", cost=10)

        # Wait exactly 1 second (should refill 10 tokens)
        await asyncio.sleep(1.1)  # Add buffer for timing

        # Should allow ~10 more requests
        for _ in range(10):
            result = await limiter.check(key=key, rate=rate, algorithm="token_bucket")
            assert result is True

    async def test_cost_parameter_token_bucket(self, clean_limiter):
        """Test that cost parameter works with token bucket."""
        limiter = clean_limiter
        key = "cost-test-tb"
        # Minute rate: keeps refill negligible so the final denial is robust
        rate = "10/minute"

        # Request with cost=5 should consume 5 tokens
        await limiter.check(key=key, rate=rate, algorithm="token_bucket", cost=5)

        # Should have 5 tokens remaining (can make 5 more requests)
        tasks = [
            limiter.check(key=key, rate=rate, algorithm="token_bucket", cost=1) for _ in range(5)
        ]
        results = await asyncio.gather(*tasks)
        assert all(r is True for r in results)

        # Next request should fail (no tokens left)
        with pytest.raises(RateLimitExceeded):
            await limiter.check(key=key, rate=rate, algorithm="token_bucket", cost=1)

    async def test_token_bucket_vs_fixed_window(self, clean_limiter):
        """Compare token bucket vs fixed window behavior."""
        limiter = clean_limiter
        tb_key = "tb-compare"
        fw_key = "fw-compare"
        rate = "10/second"

        # Align to a fresh 1-second window boundary so the fixed-window
        # drain below lands in a single window on slow runners.
        await sleep_past_window_boundary(limiter)

        # Both should allow initial burst. Drain concurrently: a sequential
        # fill is slower than the 10/s refill on slow CI runners, which
        # keeps the token bucket topped up and the next check would pass.
        tb_tasks = [
            limiter.check(key=tb_key, rate=rate, algorithm="token_bucket") for _ in range(10)
        ]
        fw_tasks = [
            limiter.check(key=fw_key, rate=rate, algorithm="fixed_window") for _ in range(10)
        ]
        tb_results = await asyncio.gather(*tb_tasks)
        fw_results = await asyncio.gather(*fw_tasks)
        assert all(r is True for r in tb_results)
        assert all(r is True for r in fw_results)

        # Both should be rate limited now. Token bucket: cost=10 so drain
        # refill (a token or two on slow runners) cannot satisfy it.
        with pytest.raises(RateLimitExceeded):
            await limiter.check(key=tb_key, rate=rate, algorithm="token_bucket", cost=10)

        with pytest.raises(RateLimitExceeded):
            await limiter.check(key=fw_key, rate=rate, algorithm="fixed_window")

        # Wait 1 second
        await asyncio.sleep(1.1)

        # Token bucket should allow ~10 more (smooth refill)
        success_tb = 0
        for _ in range(10):
            try:
                await limiter.check(key=tb_key, rate=rate, algorithm="token_bucket")
                success_tb += 1
            except RateLimitExceeded:
                break

        # Fixed window should reset completely (new window)
        success_fw = 0
        for _ in range(10):
            try:
                await limiter.check(key=fw_key, rate=rate, algorithm="fixed_window")
                success_fw += 1
            except RateLimitExceeded:
                break

        # Both should allow requests, but behavior differs
        assert success_tb >= 8  # Token bucket refilled
        assert success_fw >= 8  # Fixed window reset

    async def test_concurrent_token_bucket_requests(self, clean_limiter):
        """Test token bucket with concurrent requests."""
        limiter = clean_limiter
        key = "concurrent-tb-test"
        rate = "20/second"

        async def make_request():
            try:
                return await limiter.check(key=key, rate=rate, algorithm="token_bucket")
            except RateLimitExceeded:
                return False

        # Make 30 concurrent requests (limit is 20)
        tasks = [make_request() for _ in range(30)]
        results = await asyncio.gather(*tasks)

        # Exactly 20 should succeed (bucket capacity)
        successful = sum(1 for r in results if r is True)
        assert 18 <= successful <= 20  # Allow small variance for timing

    async def test_multiple_time_windows(self, clean_limiter):
        """Test token bucket with different time windows."""
        limiter = clean_limiter

        # Test different rate formats
        rates = [
            ("per-second", "10/second"),
            ("per-minute", "100/minute"),
            ("per-hour", "1000/hour"),
        ]

        for key_suffix, rate in rates:
            key = f"window-test-{key_suffix}"

            # First request should always succeed
            result = await limiter.check(key=key, rate=rate, algorithm="token_bucket")
            assert result is True

    async def test_tenant_isolation_token_bucket(self, clean_limiter):
        """Test that different tenants have isolated token buckets."""
        limiter = clean_limiter
        # Minute rate: keeps refill negligible so the denial is robust
        rate = "5/minute"

        # Tenant 1 uses up their tokens (concurrently, faster than the refill)
        t1_tasks = [
            limiter.check(key="user:1", rate=rate, algorithm="token_bucket", tenant_type="tenant1")
            for _ in range(5)
        ]
        await asyncio.gather(*t1_tasks)

        # Tenant 1 should be rate limited
        with pytest.raises(RateLimitExceeded):
            await limiter.check(
                key="user:1", rate=rate, algorithm="token_bucket", tenant_type="tenant1"
            )

        # Tenant 2 should still have full bucket
        for _ in range(5):
            result = await limiter.check(
                key="user:1", rate=rate, algorithm="token_bucket", tenant_type="tenant2"
            )
            assert result is True

    async def test_token_bucket_reset(self, clean_limiter):
        """Test resetting a token bucket."""
        limiter = clean_limiter
        key = "reset-test-tb"
        rate = "10/second"

        # Consume all tokens at once (drain faster than the 10/s refill)
        tasks = [limiter.check(key=key, rate=rate, algorithm="token_bucket") for _ in range(10)]
        await asyncio.gather(*tasks)

        # Should be rate limited (cost=10: drain refill can leak a token or
        # two on slow runners, but not a full bucket)
        with pytest.raises(RateLimitExceeded):
            await limiter.check(key=key, rate=rate, algorithm="token_bucket", cost=10)

        # Reset the bucket (specify algorithm for proper reset)
        await limiter.reset(key=key, algorithm="token_bucket")

        # Should be able to make requests again (bucket refilled)
        for _ in range(10):
            result = await limiter.check(key=key, rate=rate, algorithm="token_bucket")
            assert result is True

    async def test_token_bucket_usage_stats(self, clean_limiter):
        """Test getting token bucket usage statistics."""
        limiter = clean_limiter
        key = "usage-test-tb"
        rate = "10/second"

        # Make some requests
        await limiter.check(key=key, rate=rate, algorithm="token_bucket")
        await limiter.check(key=key, rate=rate, algorithm="token_bucket")

        # Get usage stats for token bucket
        usage = await limiter.get_usage(key=key, rate=rate, algorithm="token_bucket")

        assert "tokens" in usage
        assert "limit" in usage
        assert "remaining" in usage
        assert usage["limit"] == 10
        # Should have ~8 tokens remaining (started with 10, used 2)
        assert 7 <= usage["remaining"] <= 9

    async def test_no_window_boundary_burst(self, clean_limiter):
        """Test that token bucket doesn't have window boundary bursts."""
        limiter = clean_limiter
        key = "no-burst-test"
        rate = "10/second"

        # Consume all tokens at once (drain faster than the 10/s refill)
        tasks = [limiter.check(key=key, rate=rate, algorithm="token_bucket") for _ in range(10)]
        await asyncio.gather(*tasks)

        # Should be rate limited (cost=10: drain refill can leak a token or
        # two on slow runners, but not a full bucket, which takes 1s)
        with pytest.raises(RateLimitExceeded):
            await limiter.check(key=key, rate=rate, algorithm="token_bucket", cost=10)

        # Wait 0.1 second (should refill ~1 token)
        await asyncio.sleep(0.15)

        # Should allow exactly 1 request
        result = await limiter.check(key=key, rate=rate, algorithm="token_bucket")
        assert result is True

        # Should be rate limited again: only ~1.5 tokens refilled, far from
        # the full bucket a cost=10 request needs
        with pytest.raises(RateLimitExceeded):
            await limiter.check(key=key, rate=rate, algorithm="token_bucket", cost=10)

    async def test_high_burst_rate(self, clean_limiter):
        """Test token bucket with very high burst rate."""
        limiter = clean_limiter
        key = "high-burst-test"
        rate = "100/second"  # Use 100/s for more reasonable test

        # Send concurrent requests to test high burst
        async def make_request():
            try:
                return await limiter.check(key=key, rate=rate, algorithm="token_bucket")
            except RateLimitExceeded:
                return False

        tasks = [make_request() for _ in range(200)]
        results = await asyncio.gather(*tasks)

        allowed = sum(1 for r in results if r is True)
        # Should allow approximately 100 (bucket capacity, with possible refill)
        # Allow some variance due to refill during test execution
        assert 95 <= allowed <= 110, f"Expected ~100 allowed, got {allowed}"

    async def test_slow_refill_rate(self, clean_limiter):
        """Test token bucket with slow refill rate."""
        limiter = clean_limiter
        key = "slow-refill-test"
        rate = "10/minute"  # ~0.167 tokens/sec

        # Use 5 tokens
        for _ in range(5):
            await limiter.check(key=key, rate=rate, algorithm="token_bucket")

        # Wait 3 seconds (should refill ~0.5 tokens, not enough for 1 request)
        await asyncio.sleep(3)

        # Might not have refilled enough yet
        # Just verify it doesn't crash
        try:
            await limiter.check(key=key, rate=rate, algorithm="token_bucket")
        except RateLimitExceeded:
            pass  # Expected if not enough tokens yet

    async def test_fractional_cost(self, clean_limiter):
        """Test token bucket with fractional cost values."""
        limiter = clean_limiter
        key = "fractional-cost-test"
        rate = "10/second"

        # Cost should be multiplied by 1000 internally
        # cost=0.5 becomes 500 (half a token)
        # But our API only supports integer cost
        # This test verifies cost=1 works correctly

        for _ in range(5):
            await limiter.check(key=key, rate=rate, algorithm="token_bucket", cost=1)

        # Should have ~5 tokens remaining (used 5 of 10)
        usage = await limiter.get_usage(key=key, rate=rate, algorithm="token_bucket")
        assert 3 <= usage["remaining"] <= 6, f"Expected ~5 remaining, got {usage['remaining']}"
