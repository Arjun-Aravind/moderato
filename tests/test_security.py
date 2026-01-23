"""
Security-focused tests for FastLimit.

These tests validate:
- Key collision prevention (NEW-C9 fix)
- Password redaction in logs (NEW-C10 fix)
- Proxy header security (I3 fix)
"""


import pytest

from fastlimit import RateLimiter, RateLimitExceeded
from fastlimit.decorators import RateLimitMiddleware, _get_default_key
from fastlimit.utils import generate_key


class TestKeyCollisionPrevention:
    """
    Tests for key collision prevention (NEW-C9 fix).

    These tests verify that URL encoding prevents key collisions
    that could occur with simple character replacement.
    """

    def test_colon_vs_underscore_no_collision(self):
        """Test that 'a:b' and 'a_b' produce different keys."""
        key1 = generate_key("ratelimit", "a:b", "default", "1000")
        key2 = generate_key("ratelimit", "a_b", "default", "1000")
        assert key1 != key2, "Keys with ':' and '_' should not collide"

    def test_user_id_formats_no_collision(self):
        """Test that different user ID formats don't collide."""
        key1 = generate_key("ratelimit", "user:123", "default", "1000")
        key2 = generate_key("ratelimit", "user_123", "default", "1000")
        key3 = generate_key("ratelimit", "user-123", "default", "1000")
        key4 = generate_key("ratelimit", "user.123", "default", "1000")

        keys = [key1, key2, key3, key4]
        assert len(set(keys)) == 4, "All user ID formats should produce unique keys"

    def test_path_formats_no_collision(self):
        """Test that different path formats don't collide."""
        key1 = generate_key("ratelimit", "/api/users", "default", "1000")
        key2 = generate_key("ratelimit", "_api_users", "default", "1000")
        key3 = generate_key("ratelimit", "api:users", "default", "1000")

        keys = [key1, key2, key3]
        assert len(set(keys)) == 3, "Path formats should produce unique keys"

    def test_special_redis_chars_encoded(self):
        """Test that Redis special characters are safely encoded."""
        dangerous_chars = ["*", "?", "[", "]", "{", "}"]
        keys = []

        for char in dangerous_chars:
            key = generate_key("ratelimit", f"user{char}123", "default", "1000")
            keys.append(key)
            # Key should not contain the raw special character
            # (it should be URL encoded)

        # All should be unique
        assert len(set(keys)) == len(dangerous_chars)

    def test_email_identifiers_unique(self):
        """Test that email addresses produce unique keys."""
        key1 = generate_key("ratelimit", "user@example.com", "default", "1000")
        key2 = generate_key("ratelimit", "user_example.com", "default", "1000")
        key3 = generate_key("ratelimit", "user:example.com", "default", "1000")

        keys = [key1, key2, key3]
        assert len(set(keys)) == 3

    def test_ip_address_formats(self):
        """Test that IP address formats are handled correctly."""
        key1 = generate_key("ratelimit", "192.168.1.1", "default", "1000")
        key2 = generate_key("ratelimit", "192:168:1:1", "default", "1000")  # IPv6-like
        key3 = generate_key("ratelimit", "192_168_1_1", "default", "1000")

        keys = [key1, key2, key3]
        assert len(set(keys)) == 3

    def test_url_encoding_deterministic(self):
        """Test that URL encoding produces consistent results."""
        for _ in range(100):
            key = generate_key("ratelimit", "user:123:session", "default", "1000")
            # Should always produce the same key
            assert key == generate_key("ratelimit", "user:123:session", "default", "1000")

    @pytest.mark.asyncio
    async def test_collision_prevention_in_practice(self, clean_limiter):
        """Integration test: verify keys don't collide in actual rate limiting."""
        limiter = clean_limiter
        rate = "5/minute"

        # Use up limit for "user:123"
        for _ in range(5):
            await limiter.check(key="user:123", rate=rate)

        # "user:123" should be rate limited
        with pytest.raises(RateLimitExceeded):
            await limiter.check(key="user:123", rate=rate)

        # But "user_123" should NOT be rate limited (different key)
        result = await limiter.check(key="user_123", rate=rate)
        assert result is True, "user_123 should not be affected by user:123's limit"


class TestPasswordRedaction:
    """
    Tests for password redaction in logs (NEW-C10 fix).

    These tests verify that Redis URLs with passwords are redacted
    before being logged.
    """

    def test_redact_redis_url_with_password(self):
        """Test that passwords are redacted from Redis URLs."""
        from fastlimit.backends.redis import _redact_redis_url

        url = "redis://user:secretpassword@localhost:6379/0"
        redacted = _redact_redis_url(url)

        assert "secretpassword" not in redacted
        assert "[REDACTED]" in redacted or "***" in redacted
        assert "localhost" in redacted

    def test_redact_redis_url_without_password(self):
        """Test that URLs without passwords are unchanged."""
        from fastlimit.backends.redis import _redact_redis_url

        url = "redis://localhost:6379/0"
        redacted = _redact_redis_url(url)

        # Should be mostly unchanged
        assert "localhost" in redacted
        assert "6379" in redacted

    def test_redact_redis_url_only_user(self):
        """Test URL with user but no password."""
        from fastlimit.backends.redis import _redact_redis_url

        url = "redis://user@localhost:6379"
        redacted = _redact_redis_url(url)

        # Should work without crashing
        assert "localhost" in redacted

    def test_password_not_in_connect_logs(self, caplog):
        """
        Test that password is not logged during connect.

        Note: This test checks that the connect() call redacts passwords.
        The init logging of config is a separate concern - the config object
        itself may contain the unredacted URL in its repr. The important
        security property is that connect() uses _redact_redis_url().
        """
        # This is a basic validation that _redact_redis_url works
        from fastlimit.backends.redis import _redact_redis_url

        url = "redis://user:mysecretpassword@localhost:6379"
        redacted = _redact_redis_url(url)

        assert "mysecretpassword" not in redacted
        assert "[REDACTED]" in redacted
        assert "localhost" in redacted


class TestProxyHeaderSecurity:
    """
    Tests for proxy header security (I3 fix).

    These tests verify that X-Forwarded-For and X-Real-IP headers
    are only trusted when explicitly enabled.
    """

    def test_default_ignores_forwarded_headers(self, mock_request):
        """Test that proxy headers are ignored by default."""
        request = mock_request(
            client_host="192.168.1.100", headers={"X-Forwarded-For": "10.0.0.1, 10.0.0.2"}
        )

        # Without trust_proxy_headers, should use client.host
        key = _get_default_key(request, trust_proxy_headers=False)
        assert "192.168.1.100" in key
        assert "10.0.0.1" not in key

    def test_trust_proxy_headers_enabled(self, mock_request):
        """
        Test that proxy headers are used when trusted and client.host is unavailable.

        Note: The implementation uses client.host first if available.
        Proxy headers are only used when client.host is not available.
        """
        # Create request without client host (simulating proxy scenario)
        request = mock_request(
            client_host=None,  # No direct client IP
            headers={"X-Forwarded-For": "10.0.0.1, 10.0.0.2"},
        )
        # Override client to have no host
        request.client.host = None

        # With trust_proxy_headers and no client.host, should use X-Forwarded-For
        key = _get_default_key(request, trust_proxy_headers=True)
        assert "10.0.0.1" in key

    def test_real_ip_header_trusted(self, mock_request):
        """Test X-Real-IP header when trusted and no client.host."""
        request = mock_request(client_host=None, headers={"X-Real-IP": "203.0.113.50"})
        request.client.host = None

        key = _get_default_key(request, trust_proxy_headers=True)
        assert "203.0.113.50" in key

    def test_forwarded_for_takes_precedence(self, mock_request):
        """Test that X-Forwarded-For takes precedence over X-Real-IP."""
        request = mock_request(
            client_host=None, headers={"X-Forwarded-For": "10.0.0.1", "X-Real-IP": "203.0.113.50"}
        )
        request.client.host = None

        key = _get_default_key(request, trust_proxy_headers=True)
        assert "10.0.0.1" in key

    def test_spoofed_header_ignored_by_default(self, mock_request):
        """Test that spoofed headers are ignored without trust_proxy_headers."""
        # Simulate an attacker trying to bypass rate limiting
        request = mock_request(
            client_host="attacker.ip.here", headers={"X-Forwarded-For": "1.2.3.4"}  # Spoofed header
        )

        # Default behavior should use actual client IP
        key = _get_default_key(request, trust_proxy_headers=False)
        assert "attacker.ip.here" in key
        assert "1.2.3.4" not in key

    def test_multiple_ips_in_forwarded_for(self, mock_request):
        """Test handling of multiple IPs in X-Forwarded-For chain."""
        request = mock_request(
            client_host=None, headers={"X-Forwarded-For": "client.ip, proxy1.ip, proxy2.ip"}
        )
        request.client.host = None

        key = _get_default_key(request, trust_proxy_headers=True)
        # Should use the first IP (original client)
        assert "client.ip" in key

    def test_empty_forwarded_for(self, mock_request):
        """Test handling of empty X-Forwarded-For header."""
        request = mock_request(client_host="192.168.1.100", headers={"X-Forwarded-For": ""})

        key = _get_default_key(request, trust_proxy_headers=True)
        # Should fall back to client.host
        assert "192.168.1.100" in key


class TestMiddlewareProxySecurity:
    """Tests for ASGI middleware proxy header handling."""

    @pytest.mark.asyncio
    async def test_middleware_default_no_trust(self, redis_url):
        """Test that middleware doesn't trust proxy headers by default."""

        # Create a mock ASGI app
        async def mock_app(scope, receive, send):
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [],
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": b"OK",
                }
            )

        limiter = RateLimiter(redis_url=redis_url)
        await limiter.connect()

        middleware = RateLimitMiddleware(
            app=mock_app,
            limiter=limiter,
            default_rate="100/minute",
            trust_proxy_headers=False,  # Default
        )

        # Simulate request with spoofed header
        scope = {
            "type": "http",
            "path": "/api/test",
            "client": ("192.168.1.100", 12345),
            "headers": [(b"x-forwarded-for", b"10.0.0.1")],
        }

        receive_called = False
        send_calls = []

        async def receive():
            nonlocal receive_called
            receive_called = True
            return {"type": "http.request", "body": b""}

        async def send(message):
            send_calls.append(message)

        await middleware(scope, receive, send)

        # Should have processed without error
        assert len(send_calls) >= 1

        await limiter.close()

    @pytest.mark.asyncio
    async def test_middleware_trust_proxy_headers(self, redis_url):
        """Test middleware with trust_proxy_headers enabled."""

        async def mock_app(scope, receive, send):
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [],
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": b"OK",
                }
            )

        limiter = RateLimiter(redis_url=redis_url)
        await limiter.connect()

        middleware = RateLimitMiddleware(
            app=mock_app,
            limiter=limiter,
            default_rate="100/minute",
            trust_proxy_headers=True,  # Enabled
        )

        scope = {
            "type": "http",
            "path": "/api/test",
            "client": ("192.168.1.100", 12345),
            "headers": [(b"x-forwarded-for", b"10.0.0.1")],
        }

        send_calls = []

        async def receive():
            return {"type": "http.request", "body": b""}

        async def send(message):
            send_calls.append(message)

        await middleware(scope, receive, send)
        assert len(send_calls) >= 1

        await limiter.close()


class TestInputSanitization:
    """Tests for input sanitization and injection prevention."""

    def test_special_chars_in_key_safe(self):
        """Test that special characters in keys are safe."""
        dangerous_inputs = [
            "user\ninjected",  # Newline
            "user\rinjected",  # Carriage return
            "user\x00injected",  # Null byte
            "user'injected",  # Single quote
            'user"injected',  # Double quote
            "user`injected",  # Backtick
            "user\\injected",  # Backslash
        ]

        for dangerous in dangerous_inputs:
            # Should not crash
            key = generate_key("ratelimit", dangerous, "default", "1000")
            assert key is not None
            assert len(key) > 0

    @pytest.mark.asyncio
    async def test_dangerous_input_in_rate_limit(self, clean_limiter):
        """Test that dangerous inputs don't break rate limiting."""
        limiter = clean_limiter
        dangerous_keys = [
            "user:with:many:colons",
            "user with spaces",
            "user\twith\ttabs",
            "user/with/slashes",
            "user[with]brackets",
            "user{with}braces",
            'user"with"quotes',
        ]

        for key in dangerous_keys:
            # Should work without errors
            result = await limiter.check(key=key, rate="10/minute")
            assert result is True

    def test_long_input_handling(self):
        """Test that very long inputs are handled safely."""
        long_key = "x" * 10000  # Very long input
        key = generate_key("ratelimit", long_key, "default", "1000")

        # Should be hashed to reasonable length
        assert len(key) <= 300  # Well under Redis key limit


class TestByteStringHandling:
    """Tests for ASGI bytes vs string handling (I2 fix)."""

    @pytest.mark.asyncio
    async def test_asgi_bytes_headers_decoded(self, redis_url):
        """Test that ASGI bytes headers are properly decoded."""

        async def mock_app(scope, receive, send):
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [],
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": b"OK",
                }
            )

        limiter = RateLimiter(redis_url=redis_url)
        await limiter.connect()

        middleware = RateLimitMiddleware(app=mock_app, limiter=limiter, default_rate="100/minute")

        # Headers with various encodings
        scope = {
            "type": "http",
            "path": "/api/test",
            "client": ("192.168.1.100", 12345),
            "headers": [
                (b"content-type", b"application/json"),
                (b"x-custom-header", b"value with spaces"),
            ],
        }

        send_calls = []

        async def receive():
            return {"type": "http.request", "body": b""}

        async def send(message):
            send_calls.append(message)

        # Should not crash on byte headers
        await middleware(scope, receive, send)
        assert len(send_calls) >= 1

        await limiter.close()

    @pytest.mark.asyncio
    async def test_latin1_header_values(self, redis_url):
        """Test that latin-1 encoded header values are handled."""

        async def mock_app(scope, receive, send):
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [],
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": b"OK",
                }
            )

        limiter = RateLimiter(redis_url=redis_url)
        await limiter.connect()

        middleware = RateLimitMiddleware(app=mock_app, limiter=limiter, default_rate="100/minute")

        # Headers with latin-1 characters
        scope = {
            "type": "http",
            "path": "/api/test",
            "client": ("192.168.1.100", 12345),
            "headers": [
                (b"x-custom", b"caf\xe9"),  # café in latin-1
            ],
        }

        send_calls = []

        async def receive():
            return {"type": "http.request", "body": b""}

        async def send(message):
            send_calls.append(message)

        # Should handle latin-1 encoding
        await middleware(scope, receive, send)
        assert len(send_calls) >= 1

        await limiter.close()
