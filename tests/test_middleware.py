"""
Tests for rate limit headers middleware.
"""

import asyncio
from contextlib import asynccontextmanager

import pytest
import redis as sync_redis
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from moderato import BackendError, RateLimiter, RateLimitHeadersMiddleware
from moderato.models import CheckResult


@pytest.mark.asyncio
@pytest.mark.parametrize("integration", ["decorator", "asgi"])
async def test_backend_failure_returns_503_without_quota_headers(integration, monkeypatch):
    import httpx

    from moderato.decorators import RateLimitMiddleware

    limiter = RateLimiter()

    async def unavailable():
        raise BackendError("redis://user:secret@host:6379 is unavailable")

    monkeypatch.setattr(limiter, "connect", unavailable)
    app = FastAPI()
    if integration == "decorator":
        app.add_middleware(RateLimitHeadersMiddleware)
    else:
        app.add_middleware(RateLimitMiddleware, limiter=limiter)

    async def endpoint(request: Request):
        pytest.fail("Fail-closed request must not execute the endpoint")

    if integration == "decorator":
        endpoint = limiter.limit("10/day")(endpoint)
    app.get("/")(endpoint)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
    ) as client:
        response = await client.get("/")
    assert response.status_code == 503
    assert response.json() == {"error": "Rate limit backend unavailable"}
    assert not any(name.startswith("x-ratelimit-") for name in response.headers)
    assert "retry-after" not in response.headers
    assert "secret" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("integration", ["headers", "asgi"])
@pytest.mark.parametrize("operation", ["check", "get_usage", "reset", "unrelated"])
async def test_downstream_backend_error_translation(
    frozen_limiter, integration, operation, monkeypatch
):
    import httpx

    from moderato.decorators import RateLimitMiddleware

    manual = RateLimiter()

    async def unavailable():
        raise BackendError("backend unavailable")

    monkeypatch.setattr(manual, "connect", unavailable)
    app = FastAPI()
    if integration == "headers":
        app.add_middleware(RateLimitHeadersMiddleware)
    else:
        app.add_middleware(RateLimitMiddleware, limiter=frozen_limiter)

    @app.get("/")
    async def endpoint():
        if operation == "unrelated":
            raise RuntimeError("unrelated application failure")
        elif operation == "reset":
            await manual.reset("client")
        else:
            await getattr(manual, operation)("client", "10/day")
        pytest.fail("Backend failure must not fabricate operation success")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
    ) as client:
        response = await client.get("/")
    if operation == "unrelated":
        assert response.status_code == 500
        assert "Rate limit backend unavailable" not in response.text
    else:
        assert response.status_code == 503
        assert response.json() == {"error": "Rate limit backend unavailable"}
    assert "retry-after" not in response.headers
    assert not any(name.startswith("x-ratelimit-") for name in response.headers)


@pytest.mark.asyncio
async def test_asgi_backend_failure_after_response_start_is_not_replaced(frozen_limiter):
    from moderato.decorators import RateLimitMiddleware

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"partial", "more_body": True})
        raise BackendError("stream failed")

    async def receive():
        return {"type": "http.request", "body": b""}

    messages = []

    async def send(message):
        messages.append(message)

    middleware = RateLimitMiddleware(app, limiter=frozen_limiter)
    with pytest.raises(BackendError, match="stream failed"):
        await middleware(
            {"type": "http", "path": "/", "client": ("127.0.0.1", 1234), "headers": []},
            receive,
            send,
        )
    assert [
        message["status"] for message in messages if message["type"] == "http.response.start"
    ] == [200]
    assert messages[-1]["body"] == b"partial"


@pytest.fixture
def app_with_middleware(redis_url):
    """Create FastAPI app with rate limit middleware."""
    # Flush Redis so each test starts with clean rate limit state.
    # Without this, the fixed key_prefix + shared IP key means tests
    # consume each other's quotas and fail in an order-dependent way.
    client = sync_redis.from_url(redis_url)
    client.flushdb()
    client.close()

    limiter = RateLimiter(redis_url=redis_url, key_prefix="test:middleware")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await limiter.connect()
        yield
        await limiter.close()

    app = FastAPI(lifespan=lifespan)

    # Add middleware
    app.add_middleware(RateLimitHeadersMiddleware)

    @app.get("/limited")
    @limiter.limit("5/minute")
    async def limited_endpoint(request: Request):
        return {"message": "success"}

    @app.get("/limited-two")
    @limiter.limit("5/minute")
    async def second_limited_endpoint(request: Request):
        return {"message": "second"}

    @app.get("/no-limit")
    async def no_limit_endpoint(request: Request):
        return {"message": "no limit"}

    @app.get("/expensive")
    @limiter.limit("10/minute", cost=lambda req: 5)
    async def expensive_endpoint(request: Request):
        return {"message": "expensive"}

    app.state.limiter = limiter
    return app


class TestRateLimitHeadersMiddleware:
    """Test suite for rate limit headers middleware."""

    def test_successful_request_has_headers(self, app_with_middleware):
        """Test that successful requests include rate limit headers."""
        # Enter the client context so all requests share one event loop.
        # Without it, starlette spins up a new loop per request and the
        # Redis connection pool is left bound to a closed loop.
        with TestClient(app_with_middleware) as client:
            response = client.get("/limited")

            assert response.status_code == 200
            # Check that rate limit headers are present
            assert "X-RateLimit-Limit" in response.headers
            assert "X-RateLimit-Remaining" in response.headers
            assert "X-RateLimit-Reset" in response.headers

            # Verify header values
            assert response.headers["X-RateLimit-Limit"] == "5"
            remaining = int(response.headers["X-RateLimit-Remaining"])
            assert 0 <= remaining <= 5

    def test_rate_limited_request_has_retry_after(self, app_with_middleware):
        """Test that rate limited requests include Retry-After header."""
        with TestClient(app_with_middleware) as client:
            # Make 5 requests (the limit)
            for _ in range(5):
                response = client.get("/limited")
                assert response.status_code == 200

            # 6th request should be rate limited
            response = client.get("/limited")

            assert response.status_code == 429
            assert "X-RateLimit-Limit" in response.headers
            assert "X-RateLimit-Remaining" in response.headers
            assert response.headers["X-RateLimit-Remaining"] == "0"
            assert "Retry-After" in response.headers
            retry_after = int(response.headers["Retry-After"])
            assert retry_after > 0

    def test_remaining_count_decreases(self, app_with_middleware):
        """Test that remaining count decreases with each request."""
        with TestClient(app_with_middleware) as client:
            # Make multiple requests and verify remaining count
            for expected_remaining in [4, 3, 2, 1, 0]:
                response = client.get("/limited")
                assert response.status_code == 200
                remaining = int(response.headers["X-RateLimit-Remaining"])
                assert remaining == expected_remaining

    def test_same_rate_routes_have_independent_buckets(self, app_with_middleware):
        with TestClient(app_with_middleware) as client:
            for _ in range(5):
                assert client.get("/limited").status_code == 200
            for _ in range(15):
                if client.get("/limited").status_code == 429:
                    break
            else:
                pytest.fail("/limited did not reach its rate limit")
            assert client.get("/limited-two").status_code == 200

    def test_endpoint_without_rate_limit(self, app_with_middleware):
        """Test that endpoints without rate limits don't add headers."""
        with TestClient(app_with_middleware) as client:
            response = client.get("/no-limit")

            assert response.status_code == 200
            # These endpoints shouldn't have rate limit headers
            assert "X-RateLimit-Limit" not in response.headers

    def test_reset_timestamp_in_future(self, app_with_middleware):
        """Test that reset timestamp is in the future."""
        import time

        with TestClient(app_with_middleware) as client:
            response = client.get("/limited")
            assert response.status_code == 200

            reset_timestamp = int(response.headers["X-RateLimit-Reset"])
            current_time = int(time.time())

            # Reset should be in the future (within 60 seconds for minute limit)
            assert reset_timestamp > current_time
            assert reset_timestamp <= current_time + 60

    def test_expensive_request_with_cost(self, app_with_middleware):
        """Test that cost-based rate limiting works with headers."""
        with TestClient(app_with_middleware) as client:
            # First request with cost=5 should use half the limit (10/minute, cost=5)
            response = client.get("/expensive")
            assert response.status_code == 200
            assert "X-RateLimit-Limit" in response.headers
            assert response.headers["X-RateLimit-Limit"] == "10"

            remaining = int(response.headers["X-RateLimit-Remaining"])
            # Should have 5 remaining (10 - 5)
            assert remaining == 5

            # Second request should use remaining 5
            response = client.get("/expensive")
            assert response.status_code == 200
            remaining = int(response.headers["X-RateLimit-Remaining"])
            assert remaining == 0

            # Third request should be rate limited
            response = client.get("/expensive")
            assert response.status_code == 429

    def test_rate_limit_error_response(self, app_with_middleware):
        """Test that rate limit error responses are properly formatted."""
        with TestClient(app_with_middleware) as client:
            # Exhaust the limit
            for _ in range(5):
                client.get("/limited")

            # Next request should return 429 with error details
            response = client.get("/limited")

            assert response.status_code == 429
            data = response.json()
            assert "error" in data
            assert "retry_after" in data
            assert data["error"] == "Rate limit exceeded"

    async def test_concurrent_requests(self, app_with_middleware):
        """Test that headers are correct with concurrent requests."""
        import httpx

        # Drive the ASGI app with a single event loop. TestClient called from
        # multiple threads runs each request in its own anyio portal event
        # loop, and the shared Redis client / asyncio.Lock are loop-bound,
        # which deadlocks the process at exit.
        transport = httpx.ASGITransport(app=app_with_middleware)
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:

                async def make_request():
                    return await client.get("/limited")

                responses = await asyncio.gather(*[make_request() for _ in range(5)])

            # All should succeed (within limit)
            assert all(r.status_code == 200 for r in responses)

            # All should have rate limit headers
            assert all("X-RateLimit-Remaining" in r.headers for r in responses)
        finally:
            # ASGITransport never runs lifespan events, so close the limiter
            # explicitly to release its Redis connection on this loop - even
            # when an assertion above fails.
            await app_with_middleware.state.limiter.close()

    def test_headers_with_different_ips(self, app_with_middleware):
        """Test that different IPs get separate rate limits."""
        # Note: TestClient doesn't easily support different IPs,
        # but we can verify that the same client maintains state
        with TestClient(app_with_middleware) as client1:
            response1 = client1.get("/limited")
            response2 = client1.get("/limited")

            assert response1.status_code == 200
            assert response2.status_code == 200

            remaining1 = int(response1.headers["X-RateLimit-Remaining"])
            remaining2 = int(response2.headers["X-RateLimit-Remaining"])

            # Second request should have less remaining
            assert remaining2 < remaining1


class TestMiddlewareIntegration:
    """Integration tests for middleware with actual rate limiter."""

    @pytest.mark.parametrize("algorithm", ["fixed_window", "token_bucket", "sliding_window"])
    async def test_denial_preserves_remaining_capacity(self, frozen_limiter, algorithm):
        import httpx

        app = FastAPI()
        app.add_middleware(RateLimitHeadersMiddleware)

        @app.get("/weighted")
        @frozen_limiter.limit(
            "10/day",
            algorithm=algorithm,
            key=lambda request: "client",
            cost=lambda request: int(request.headers["x-cost"]),
            scope="metadata",
        )
        async def weighted(request: Request):
            return {"ok": True}

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            first = await client.get("/weighted", headers={"x-cost": "8"})
            assert first.status_code == 200
            assert first.headers["X-RateLimit-Remaining"] == "2"
            denied = await client.get("/weighted", headers={"x-cost": "3"})
            assert denied.status_code == 429
            assert denied.headers["X-RateLimit-Remaining"] == "2"
            usage = await frozen_limiter.get_usage(
                key="client", rate="10/day", algorithm=algorithm, scope="metadata"
            )
            assert usage["remaining"] == 2
            smaller = await client.get("/weighted", headers={"x-cost": "2"})
            assert smaller.status_code == 200
            assert smaller.headers["X-RateLimit-Remaining"] == "0"

    async def test_429_preserves_exception_remaining(self):
        import httpx

        from moderato import RateLimitExceeded

        app = FastAPI()
        app.add_middleware(RateLimitHeadersMiddleware)

        @app.get("/denied")
        async def denied(request: Request):
            raise RateLimitExceeded(retry_after=7, limit="10/minute", remaining=2)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/denied")
        assert response.status_code == 429
        assert response.headers["X-RateLimit-Remaining"] == "2"
        assert response.headers["Retry-After"] == "7"

    async def test_middleware_with_limiter_check(self, clean_limiter):
        """Test middleware integration with actual limiter."""
        import httpx
        from fastapi import FastAPI, Request

        app = FastAPI()
        limiter = clean_limiter

        app.add_middleware(RateLimitHeadersMiddleware)

        @app.get("/test")
        @limiter.limit("3/minute")
        async def test_endpoint(request: Request):
            return {"status": "ok"}

        # The limiter is connected on the test's event loop (async fixture),
        # so the app must be driven on that same loop - not in TestClient's portal.
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # Make requests up to the limit
            for _ in range(3):
                response = await client.get("/test")
                assert response.status_code == 200
                assert "X-RateLimit-Limit" in response.headers

            # Next request should fail
            response = await client.get("/test")
            assert response.status_code == 429
            assert "Retry-After" in response.headers

    async def test_middleware_preserves_response_body(self, clean_limiter):
        """Test that middleware doesn't alter response body."""
        import httpx
        from fastapi import FastAPI, Request

        app = FastAPI()
        limiter = clean_limiter

        app.add_middleware(RateLimitHeadersMiddleware)

        @app.get("/data")
        @limiter.limit("10/minute")
        async def data_endpoint(request: Request):
            return {"data": "test", "count": 123}

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/data")
            assert response.status_code == 200

            # Response body should be intact
            data = response.json()
            assert data["data"] == "test"
            assert data["count"] == 123

            # Headers should be added
            assert "X-RateLimit-Limit" in response.headers

    async def test_callback_failure_is_a_service_unavailable_response(self, clean_limiter):
        import httpx

        app = FastAPI()
        app.add_middleware(RateLimitHeadersMiddleware)

        @app.get("/data")
        @clean_limiter.limit("10/minute", key=lambda request: (_ for _ in ()).throw(ValueError()))
        async def data_endpoint(request: Request):
            return {"data": "unreachable"}

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/data")

        assert response.status_code == 503
        assert response.json() == {"error": "Rate limit callback failed", "callback": "key"}


class TestRateLimitMiddleware:
    """Tests for the lower-level ASGI RateLimitMiddleware."""

    @pytest.fixture
    def app_with_rate_limit_middleware(self, redis_url):
        import uuid

        from moderato.decorators import RateLimitMiddleware

        limiter = RateLimiter(
            redis_url=redis_url, key_prefix=f"test:asgi-middleware:{uuid.uuid4().hex[:8]}"
        )

        @asynccontextmanager
        async def lifespan(app: FastAPI):
            await limiter.connect()
            yield
            await limiter.close()

        app = FastAPI(lifespan=lifespan)
        app.add_middleware(RateLimitMiddleware, limiter=limiter, default_rate="5/minute")

        @app.get("/anything")
        async def anything(request: Request):
            return {"message": "ok"}

        return app

    def test_429_response_has_standard_headers(self, app_with_rate_limit_middleware):
        import time

        with TestClient(app_with_rate_limit_middleware) as client:
            for _ in range(5):
                assert client.get("/anything").status_code == 200

            before = int(time.time())
            # If the 5-request fill straddled a minute boundary, the bucket
            # reset and the probe succeeds; keep probing so a boundary straddle
            # cannot flake this.
            response = None
            for _ in range(15):
                response = client.get("/anything")
                if response.status_code == 429:
                    break

        assert response.status_code == 429
        assert response.headers["X-RateLimit-Limit"] == "5"
        assert response.headers["X-RateLimit-Remaining"] == "0"
        assert int(response.headers["Retry-After"]) > 0
        assert int(response.headers["X-RateLimit-Reset"]) > before

    async def test_429_uses_reset_timestamp_from_decision(self):
        import httpx

        from moderato.decorators import RateLimitMiddleware

        class DenyingLimiter:
            async def check_with_info(self, **kwargs):
                return CheckResult(
                    allowed=False,
                    limit=5,
                    remaining=0,
                    retry_after=2,
                    reset_at=1_234_567_890,
                    window_seconds=60,
                )

        async def app(scope, receive, send):
            raise AssertionError("Denied request reached the application")

        transport = httpx.ASGITransport(
            app=RateLimitMiddleware(app, limiter=DenyingLimiter(), default_rate="5/minute")
        )
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/")

        assert response.status_code == 429
        assert response.headers["Retry-After"] == "2"
        assert response.headers["X-RateLimit-Reset"] == "1234567890"

    async def test_429_survives_non_parseable_limit(self):
        """A manually raised RateLimitExceeded with a plain-number limit must still 429."""

        import httpx

        from moderato.exceptions import RateLimitExceeded

        app = FastAPI()
        app.add_middleware(RateLimitHeadersMiddleware)

        @app.get("/boom")
        async def boom(request: Request):
            raise RateLimitExceeded(retry_after=7, limit=5, remaining=0)

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/boom")

        assert response.status_code == 429
        assert response.headers["X-RateLimit-Limit"] == "5"
        assert "X-RateLimit-Reset" not in response.headers
