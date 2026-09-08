"""
Tests for multi-tenant rate limiting scenarios.
"""

import asyncio
from datetime import datetime

import pytest

from fastlimit import RateLimitExceeded


class TestMultiTenant:
    """Test suite for multi-tenant rate limiting."""

    @pytest.mark.asyncio
    async def test_tenant_isolation(self, clean_limiter):
        """Test that tenants are properly isolated."""
        limiter = clean_limiter

        tenants = [
            ("tenant-1", "free"),
            ("tenant-2", "free"),
            ("tenant-1", "premium"),
            ("tenant-2", "premium"),
        ]

        # Each tenant+type combination should have its own limit
        for tenant_id, tenant_type in tenants:
            for i in range(5):
                result = await limiter.check(
                    key=tenant_id, rate="5/minute", tenant_type=tenant_type
                )
                assert result is True, f"Failed for {tenant_id}/{tenant_type} request {i+1}"

            # Each should be at their limit
            with pytest.raises(RateLimitExceeded) as exc_info:
                await limiter.check(key=tenant_id, rate="5/minute", tenant_type=tenant_type)
            assert exc_info.value.remaining == 0

    @pytest.mark.asyncio
    async def test_different_tier_limits(self, clean_limiter):
        """Test different rate limits for different tenant tiers."""
        limiter = clean_limiter

        # Define tier-specific limits
        tier_limits = {
            "free": "10/minute",
            "premium": "100/minute",
            "enterprise": "1000/minute",
        }

        tenant_id = "multi-tier-test"

        # Test each tier
        for tier, limit in tier_limits.items():
            # Extract expected count from limit

            # Make requests up to 10 (free tier limit)
            for _ in range(10):
                result = await limiter.check(key=tenant_id, rate=limit, tenant_type=tier)
                assert result is True

            # Free tier should be exhausted, others should continue
            if tier == "free":
                with pytest.raises(RateLimitExceeded):
                    await limiter.check(key=tenant_id, rate=limit, tenant_type=tier)
            else:
                # Premium and Enterprise can continue
                result = await limiter.check(key=tenant_id, rate=limit, tenant_type=tier)
                assert result is True

    @pytest.mark.asyncio
    async def test_tenant_upgrade_scenario(self, clean_limiter):
        """Test tenant upgrading from free to premium tier."""
        limiter = clean_limiter
        tenant_id = "upgrade-test"

        # Start as free tier
        free_limit = "5/minute"
        for _ in range(5):
            await limiter.check(key=tenant_id, rate=free_limit, tenant_type="free")

        # Free tier exhausted
        with pytest.raises(RateLimitExceeded):
            await limiter.check(key=tenant_id, rate=free_limit, tenant_type="free")

        # "Upgrade" to premium - should have separate limit
        premium_limit = "100/minute"
        for _ in range(10):
            result = await limiter.check(key=tenant_id, rate=premium_limit, tenant_type="premium")
            assert result is True

        # Free tier should still be exhausted
        with pytest.raises(RateLimitExceeded):
            await limiter.check(key=tenant_id, rate=free_limit, tenant_type="free")

    @pytest.mark.asyncio
    async def test_api_key_based_tenant(self, clean_limiter, make_request):
        """Test rate limiting based on API keys."""
        limiter = clean_limiter

        # Simulate API key to tenant mapping
        api_key_tiers = {
            "key_free_001": "free",
            "key_free_002": "free",
            "key_premium_001": "premium",
            "key_enterprise_001": "enterprise",
        }

        @limiter.limit(
            "10/minute",
            key=lambda req: req.headers.get("X-API-Key"),
            tenant_type=lambda req: api_key_tiers.get(req.headers.get("X-API-Key"), "free"),
        )
        async def api_endpoint(request):
            return {"key": request.headers.get("X-API-Key")}

        # Test each API key
        for api_key, tier in api_key_tiers.items():  # noqa: B007
            request = make_request(headers={"X-API-Key": api_key})

            # Make 10 requests (the base limit)
            for _ in range(10):
                result = await api_endpoint(request)
                assert result["key"] == api_key

            # 11th request should fail
            with pytest.raises(RateLimitExceeded):
                await api_endpoint(request)

    @pytest.mark.asyncio
    async def test_concurrent_multi_tenant(self, clean_limiter):
        """Test concurrent requests from multiple tenants."""
        limiter = clean_limiter

        async def make_tenant_requests(tenant_id: str, tenant_type: str, count: int):
            """Helper to make requests for a tenant."""
            results = []
            for _ in range(count):
                try:
                    await limiter.check(key=tenant_id, rate="50/second", tenant_type=tenant_type)
                    results.append(True)
                except RateLimitExceeded:
                    results.append(False)
            return results

        # Create tasks for multiple tenants
        tasks = [
            make_tenant_requests("tenant-a", "free", 60),
            make_tenant_requests("tenant-b", "free", 60),
            make_tenant_requests("tenant-c", "premium", 60),
            make_tenant_requests("tenant-a", "premium", 60),  # Same tenant, different tier
        ]

        # Run concurrently
        all_results = await asyncio.gather(*tasks)

        # Each tenant+tier should have exactly 50 successful requests
        for tenant_results in all_results:
            successful = sum(1 for r in tenant_results if r is True)
            failed = sum(1 for r in tenant_results if r is False)
            assert successful == 50, f"Expected 50 successful, got {successful}"
            assert failed == 10, f"Expected 10 failed, got {failed}"

    @pytest.mark.asyncio
    async def test_tenant_specific_windows(self, clean_limiter):
        """Test that time windows are tenant-specific."""
        limiter = clean_limiter
        base_time = datetime.utcnow().isoformat()

        # Make requests for different tenants in same time window
        tenants = ["tenant-x", "tenant-y", "tenant-z"]

        for tenant in tenants:
            # Each tenant should have independent windows
            result = await limiter.check(
                key=f"{tenant}-{base_time}", rate="1/second", tenant_type="standard"
            )
            assert result is True

        # Wait for window to expire
        await asyncio.sleep(1.1)

        # All tenants should be able to make another request
        for tenant in tenants:
            result = await limiter.check(
                key=f"{tenant}-{base_time}", rate="1/second", tenant_type="standard"
            )
            assert result is True

    @pytest.mark.asyncio
    async def test_tenant_rate_limit_headers(self, clean_limiter, make_request):
        """Test that rate limit headers are tenant-aware."""
        limiter = clean_limiter

        @limiter.limit(
            "5/minute",
            key=lambda req: req.headers.get("X-Tenant-ID"),
            tenant_type=lambda req: req.headers.get("X-Tenant-Type", "free"),
        )
        async def tenant_endpoint(request):
            return {"tenant": request.headers.get("X-Tenant-ID")}

        # Make requests for a tenant
        request = make_request(headers={"X-Tenant-ID": "test-tenant", "X-Tenant-Type": "premium"})

        # Use up the limit
        for _ in range(5):
            await tenant_endpoint(request)

        # Next request should fail and set headers
        with pytest.raises(RateLimitExceeded):
            await tenant_endpoint(request)

        # Headers should reflect tenant-specific limit
        assert hasattr(request.state, "rate_limit_headers")
        headers = request.state.rate_limit_headers
        assert headers["X-RateLimit-Limit"] == "5/minute"
        assert headers["X-RateLimit-Remaining"] == "0"

    @pytest.mark.asyncio
    async def test_tenant_usage_tracking(self, clean_limiter):
        """Test tracking usage per tenant."""
        limiter = clean_limiter

        tenants = [
            ("tenant-alpha", "free", "20/minute", 15),
            ("tenant-beta", "premium", "100/minute", 50),
            ("tenant-gamma", "enterprise", "1000/minute", 100),
        ]

        for tenant_id, tenant_type, rate, request_count in tenants:
            # Make specific number of requests
            for _ in range(request_count):
                await limiter.check(key=tenant_id, rate=rate, tenant_type=tenant_type)

            # Check usage
            usage = await limiter.get_usage(key=tenant_id, rate=rate, tenant_type=tenant_type)

            assert usage["current"] == request_count
            expected_limit = int(rate.split("/")[0])
            assert usage["limit"] == expected_limit
            assert usage["remaining"] == expected_limit - request_count

    @pytest.mark.asyncio
    async def test_tenant_reset(self, clean_limiter):
        """Test resetting limits for specific tenants."""
        limiter = clean_limiter
        tenant_id = "reset-tenant"

        # Use up limits for different tenant types
        for tenant_type in ["free", "premium"]:
            for _ in range(5):
                await limiter.check(key=tenant_id, rate="5/minute", tenant_type=tenant_type)

            # Should be limited
            with pytest.raises(RateLimitExceeded):
                await limiter.check(key=tenant_id, rate="5/minute", tenant_type=tenant_type)

        # Reset only the free tier
        await limiter.reset(key=tenant_id, tenant_type="free")

        # Free tier should work
        result = await limiter.check(key=tenant_id, rate="5/minute", tenant_type="free")
        assert result is True

        # Premium should still be limited
        with pytest.raises(RateLimitExceeded):
            await limiter.check(key=tenant_id, rate="5/minute", tenant_type="premium")

    @pytest.mark.asyncio
    async def test_wildcard_tenant_operations(self, clean_limiter):
        """Test operations across all tenant types."""
        limiter = clean_limiter
        tenant_id = "wildcard-test"

        # Create limits for multiple tenant types
        for tenant_type in ["free", "premium", "enterprise"]:
            for _ in range(3):
                await limiter.check(key=tenant_id, rate="3/minute", tenant_type=tenant_type)

        # Reset all (by not specifying tenant_type)
        result = await limiter.reset(key=tenant_id)
        assert result is True

        # All should work again
        for tenant_type in ["free", "premium", "enterprise"]:
            result = await limiter.check(key=tenant_id, rate="3/minute", tenant_type=tenant_type)
            assert result is True
