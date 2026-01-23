"""
Pytest configuration and fixtures for FastLimit tests.
"""

import asyncio
import logging
import os

# Add parent directory to path for imports
import sys
from collections.abc import AsyncGenerator, Generator
from pathlib import Path

import pytest
import redis.asyncio as redis

sys.path.insert(0, str(Path(__file__).parent.parent))

from fastlimit import RateLimiter  # noqa: E402

# Configure logging for tests
logging.basicConfig(level=logging.DEBUG)


@pytest.fixture(scope="session")
def event_loop() -> Generator:
    """Create event loop for async tests."""
    policy = asyncio.get_event_loop_policy()
    loop = policy.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="session")
def redis_url() -> str:
    """Get Redis URL from environment or use default."""
    return os.getenv("REDIS_URL", "redis://localhost:6379")


@pytest.fixture
async def redis_client(redis_url: str) -> AsyncGenerator[redis.Redis, None]:
    """
    Create Redis client for tests.

    Cleans the database before each test to ensure isolation.
    """
    client = redis.from_url(redis_url, decode_responses=True)

    # Test connection
    try:
        await client.ping()
    except Exception as e:
        pytest.skip(f"Redis not available: {e}")

    # Clean database before test
    await client.flushdb()

    yield client

    # Clean up after test
    await client.flushdb()
    await client.close()


@pytest.fixture
async def rate_limiter(redis_url: str) -> AsyncGenerator[RateLimiter, None]:
    """
    Create RateLimiter instance for tests.

    Automatically connects and disconnects.
    """
    limiter = RateLimiter(
        redis_url=redis_url, key_prefix="test:ratelimit"  # Use test prefix to avoid conflicts
    )

    await limiter.connect()
    yield limiter
    await limiter.close()


@pytest.fixture
def mock_request():
    """
    Create mock request object that mimics FastAPI Request.
    """

    class MockClient:
        def __init__(self, host: str = "192.168.1.100"):
            self.host = host

    class MockRequest:
        def __init__(
            self,
            client_host: str = "192.168.1.100",
            headers: dict = None,
            path: str = "/api/test",
            path_params: dict = None,
        ):
            self.client = MockClient(client_host)
            self.headers = headers or {}
            self.path = path
            self.path_params = path_params or {}
            self.state = type("State", (), {})()  # Simple object for state storage

    return MockRequest


@pytest.fixture
def make_request(mock_request):
    """
    Factory fixture to create custom mock requests.
    """

    def _make_request(**kwargs):
        return mock_request(**kwargs)

    return _make_request


@pytest.fixture
async def clean_limiter(redis_url: str) -> AsyncGenerator[RateLimiter, None]:
    """
    Create a fresh RateLimiter with a unique prefix for each test.

    This ensures complete isolation between tests.
    """
    import uuid

    # Generate unique prefix for this test
    test_id = str(uuid.uuid4())[:8]

    limiter = RateLimiter(redis_url=redis_url, key_prefix=f"test:{test_id}:ratelimit")

    await limiter.connect()
    yield limiter
    await limiter.close()


@pytest.fixture
def rate_limits():
    """Common rate limit configurations for testing."""
    return {
        "strict": "5/second",
        "normal": "100/minute",
        "relaxed": "1000/hour",
        "daily": "10000/day",
    }


@pytest.fixture
async def redis_monitor(redis_client):
    """
    Monitor Redis commands for debugging.

    Usage:
        async with redis_monitor as monitor:
            # Perform operations
            pass
        commands = monitor.get_commands()
    """

    class RedisMonitor:
        def __init__(self, client):
            self.client = client
            self.commands = []
            self.monitoring = False

        async def __aenter__(self):
            # Note: Redis monitoring requires a separate connection
            # This is a simplified version for testing
            self.monitoring = True
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            self.monitoring = False

        def get_commands(self):
            return self.commands

    return RedisMonitor(redis_client)


@pytest.fixture
def assert_rate_limited():
    """
    Helper to assert that a rate limit exception was raised correctly.
    """

    def _assert_rate_limited(exc_info, expected_limit: str = None):
        from fastlimit import RateLimitExceeded

        assert exc_info.type == RateLimitExceeded
        assert exc_info.value.retry_after > 0

        if expected_limit:
            assert exc_info.value.limit == expected_limit

        return exc_info.value

    return _assert_rate_limited


@pytest.fixture
def benchmark(request):
    """
    Simple benchmarking fixture for performance tests.
    """
    import time

    class Benchmark:
        def __init__(self):
            self.start_time = None
            self.end_time = None
            self.iterations = 0

        def start(self):
            self.start_time = time.perf_counter()
            self.iterations = 0

        def increment(self):
            self.iterations += 1

        def stop(self):
            self.end_time = time.perf_counter()

        @property
        def elapsed(self):
            if self.start_time and self.end_time:
                return self.end_time - self.start_time
            return 0

        @property
        def rate(self):
            if self.elapsed > 0:
                return self.iterations / self.elapsed
            return 0

        def report(self):
            print("\nBenchmark Results:")
            print(f"  Iterations: {self.iterations}")
            print(f"  Time: {self.elapsed:.3f} seconds")
            print(f"  Rate: {self.rate:.1f} ops/sec")

    benchmark = Benchmark()
    yield benchmark

    # Report if benchmark was used
    if benchmark.start_time:
        benchmark.report()


@pytest.fixture
async def clean_limiter_token_bucket(redis_url: str) -> AsyncGenerator[RateLimiter, None]:
    """
    Create a fresh RateLimiter configured for token bucket algorithm.
    """
    import uuid

    test_id = str(uuid.uuid4())[:8]

    limiter = RateLimiter(
        redis_url=redis_url,
        key_prefix=f"test:{test_id}:ratelimit",
        default_algorithm="token_bucket",
    )

    await limiter.connect()
    yield limiter
    await limiter.close()


@pytest.fixture
async def clean_limiter_sliding_window(redis_url: str) -> AsyncGenerator[RateLimiter, None]:
    """
    Create a fresh RateLimiter configured for sliding window algorithm.
    """
    import uuid

    test_id = str(uuid.uuid4())[:8]

    limiter = RateLimiter(
        redis_url=redis_url,
        key_prefix=f"test:{test_id}:ratelimit",
        default_algorithm="sliding_window",
    )

    await limiter.connect()
    yield limiter
    await limiter.close()


@pytest.fixture
def redis_time_mock():
    """
    Fixture for mocking Redis time in tests.

    Returns a mock controller that can set and advance time.
    """

    class RedisTimeMock:
        def __init__(self):
            self.current_time = 1700000000  # Fixed starting point
            self.current_us = 0

        def get_time(self):
            return (self.current_time, self.current_us)

        def advance(self, seconds: int = 0, microseconds: int = 0):
            self.current_us += microseconds
            if self.current_us >= 1000000:
                self.current_time += self.current_us // 1000000
                self.current_us = self.current_us % 1000000
            self.current_time += seconds

        def set_time(self, seconds: int, microseconds: int = 0):
            self.current_time = seconds
            self.current_us = microseconds

    return RedisTimeMock()
