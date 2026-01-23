"""
Tests for rate limiting decorators.
"""


import pytest

from fastlimit import RateLimitExceeded


class TestDecorators:
    """Test suite for decorator functionality."""

    @pytest.mark.asyncio
    async def test_basic_decorator(self, clean_limiter, mock_request):
        """Test basic decorator functionality."""
        limiter = clean_limiter
        request = mock_request()

        # Create decorated function
        @limiter.limit("5/minute")
        async def my_endpoint(request):
            return {"status": "ok"}

        # First 5 calls should work
        for _ in range(5):
            result = await my_endpoint(request)
            assert result == {"status": "ok"}

        # 6th call should raise RateLimitExceeded
        with pytest.raises(RateLimitExceeded):
            await my_endpoint(request)

    @pytest.mark.asyncio
    async def test_custom_key_function(self, clean_limiter, make_request):
        """Test decorator with custom key extraction."""
        limiter = clean_limiter

        # Decorator with custom key function
        @limiter.limit("3/minute", key=lambda req: req.headers.get("X-User-ID", "anonymous"))
        async def my_endpoint(request):
            return {"user": request.headers.get("X-User-ID", "anonymous")}

        # Requests from user1
        user1_request = make_request(headers={"X-User-ID": "user1"})
        for _ in range(3):
            result = await my_endpoint(user1_request)
            assert result["user"] == "user1"

        # user1 should be rate limited
        with pytest.raises(RateLimitExceeded):
            await my_endpoint(user1_request)

        # But user2 should work
        user2_request = make_request(headers={"X-User-ID": "user2"})
        result = await my_endpoint(user2_request)
        assert result["user"] == "user2"

    @pytest.mark.asyncio
    async def test_tenant_type_function(self, clean_limiter, make_request):
        """Test decorator with tenant type extraction."""
        limiter = clean_limiter

        @limiter.limit(
            "5/minute",
            key=lambda req: req.headers.get("X-API-Key"),
            tenant_type=lambda req: req.headers.get("X-Tenant-Tier", "free"),
        )
        async def api_endpoint(request):
            return {"tier": request.headers.get("X-Tenant-Tier", "free")}

        # Premium tenant requests
        premium_request = make_request(headers={"X-API-Key": "key1", "X-Tenant-Tier": "premium"})
        for _ in range(5):
            result = await api_endpoint(premium_request)
            assert result["tier"] == "premium"

        # Premium tenant should be limited
        with pytest.raises(RateLimitExceeded):
            await api_endpoint(premium_request)

        # Free tenant with same API key should work (different tenant type)
        free_request = make_request(headers={"X-API-Key": "key1", "X-Tenant-Tier": "free"})
        result = await api_endpoint(free_request)
        assert result["tier"] == "free"

    @pytest.mark.asyncio
    async def test_cost_function(self, clean_limiter, make_request):
        """Test decorator with dynamic cost calculation."""
        limiter = clean_limiter

        @limiter.limit("10/minute", cost=lambda req: 5 if req.headers.get("X-Premium") else 1)
        async def expensive_endpoint(request):
            return {"premium": bool(request.headers.get("X-Premium"))}

        # Premium request (cost=5)
        premium_request = make_request(headers={"X-Premium": "true"})
        result = await expensive_endpoint(premium_request)
        assert result["premium"] is True

        # Another premium request should exhaust the limit (5+5=10)
        result = await expensive_endpoint(premium_request)
        assert result["premium"] is True

        # Should be rate limited now
        with pytest.raises(RateLimitExceeded):
            await expensive_endpoint(premium_request)

        # Regular request should also be limited (already at 10)
        regular_request = make_request()
        with pytest.raises(RateLimitExceeded):
            await expensive_endpoint(regular_request)

    @pytest.mark.asyncio
    async def test_multiple_decorators(self, clean_limiter, mock_request):
        """Test multiple rate limit decorators on same function."""
        limiter = clean_limiter
        request = mock_request()

        # Note: In practice, you'd typically use one decorator,
        # but this tests the stacking behavior
        @limiter.limit("10/minute")
        @limiter.limit("5/second")
        async def multi_limited(request):
            return {"status": "ok"}

        # Should be limited by the stricter limit (5/second)
        for _ in range(5):
            await multi_limited(request)

        with pytest.raises(RateLimitExceeded):
            await multi_limited(request)

    @pytest.mark.asyncio
    async def test_decorator_with_path_params(self, clean_limiter):
        """Test decorator extracting key from path parameters."""
        limiter = clean_limiter

        @limiter.limit("3/minute", key=lambda req: req.path_params.get("user_id"))
        async def get_user(request):
            return {"user_id": request.path_params.get("user_id")}

        # Create request with path params
        class RequestWithParams:
            def __init__(self, user_id):
                self.path_params = {"user_id": user_id}
                self.client = type("Client", (), {"host": "127.0.0.1"})()
                self.state = type("State", (), {})()

        # Requests for user1
        user1_req = RequestWithParams("user1")
        for _ in range(3):
            result = await get_user(user1_req)
            assert result["user_id"] == "user1"

        # user1 should be limited
        with pytest.raises(RateLimitExceeded):
            await get_user(user1_req)

        # user2 should work
        user2_req = RequestWithParams("user2")
        result = await get_user(user2_req)
        assert result["user_id"] == "user2"

    @pytest.mark.asyncio
    async def test_rate_limit_headers_in_state(self, clean_limiter, mock_request):
        """Test that rate limit headers are added to request state."""
        limiter = clean_limiter
        request = mock_request()

        @limiter.limit("5/minute")
        async def endpoint_with_headers(request):
            return {"status": "ok"}

        # Use up the limit
        for _ in range(5):
            await endpoint_with_headers(request)

        # Next request should set headers in state
        with pytest.raises(RateLimitExceeded):
            await endpoint_with_headers(request)

        # Check that headers were set in request state
        assert hasattr(request.state, "rate_limit_headers")
        headers = request.state.rate_limit_headers
        assert "X-RateLimit-Limit" in headers
        assert headers["X-RateLimit-Limit"] == "5/minute"
        assert "X-RateLimit-Remaining" in headers
        assert "Retry-After" in headers

    @pytest.mark.asyncio
    async def test_decorator_error_handling(self, clean_limiter):
        """Test decorator handles errors in key/tenant functions gracefully."""
        limiter = clean_limiter

        # Key function that raises an error
        @limiter.limit(
            "5/minute", key=lambda req: req.nonexistent_attribute  # This will raise AttributeError
        )
        async def endpoint_with_error(request):
            return {"status": "ok"}

        # Should fall back to default key extraction (IP)
        request = type("Request", (), {"client": type("Client", (), {"host": "192.168.1.1"})()})()

        # Should work despite error in key function
        result = await endpoint_with_error(request)
        assert result == {"status": "ok"}

    @pytest.mark.asyncio
    async def test_ip_extraction_fallbacks(self, clean_limiter):
        """Test various IP extraction methods."""
        limiter = clean_limiter

        @limiter.limit("10/minute")
        async def ip_limited(request):
            return {"status": "ok"}

        # Test with regular client IP
        request1 = type(
            "Request",
            (),
            {
                "client": type("Client", (), {"host": "192.168.1.1"})(),
                "state": type("State", (), {})(),
            },
        )()
        result = await ip_limited(request1)
        assert result == {"status": "ok"}

        # Test with X-Forwarded-For header
        request2 = type(
            "Request",
            (),
            {
                "client": None,
                "headers": {"X-Forwarded-For": "10.0.0.1, 192.168.1.1"},
                "state": type("State", (), {})(),
            },
        )()
        result = await ip_limited(request2)
        assert result == {"status": "ok"}

        # Test with X-Real-IP header
        request3 = type(
            "Request",
            (),
            {
                "client": None,
                "headers": {"X-Real-IP": "172.16.0.1"},
                "state": type("State", (), {})(),
            },
        )()
        result = await ip_limited(request3)
        assert result == {"status": "ok"}

        # Test with no IP information (falls back to "unknown")
        # Must have client or headers attribute to be recognized as a request
        request4 = type(
            "Request", (), {"client": None, "headers": {}, "state": type("State", (), {})()}
        )()
        result = await ip_limited(request4)
        assert result == {"status": "ok"}

    @pytest.mark.asyncio
    async def test_sync_function_wrapper(self, clean_limiter):
        """Test that sync functions are properly wrapped."""
        limiter = clean_limiter

        # Sync function (will be wrapped to async)
        @limiter.limit("5/minute")
        def sync_endpoint(request):
            return {"status": "ok", "type": "sync"}

        # Create a request
        request = type(
            "Request",
            (),
            {
                "client": type("Client", (), {"host": "127.0.0.1"})(),
                "state": type("State", (), {})(),
            },
        )()

        # Should work as async function
        result = await sync_endpoint(request)
        assert result == {"status": "ok", "type": "sync"}

    @pytest.mark.asyncio
    async def test_algorithm_parameter(self, clean_limiter, mock_request):
        """Test decorator with specific algorithm parameter."""
        limiter = clean_limiter
        request = mock_request()

        @limiter.limit("10/minute", algorithm="fixed_window")
        async def endpoint_with_algorithm(request):
            return {"algorithm": "fixed_window"}

        # Should work with specified algorithm
        result = await endpoint_with_algorithm(request)
        assert result == {"algorithm": "fixed_window"}

        # Test with invalid algorithm
        with pytest.raises(Exception) as exc_info:

            @limiter.limit("10/minute", algorithm="invalid_algo")
            async def bad_endpoint(request):
                return {}

            await bad_endpoint(request)

        assert "algorithm" in str(exc_info.value).lower()
