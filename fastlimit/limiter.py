"""
Main RateLimiter class implementation.
"""

import asyncio
import logging
from types import TracebackType
from typing import Any, Callable, Optional

from .backends.redis import RedisBackend
from .exceptions import RateLimitConfigError, RateLimitExceeded
from .models import CheckResult, RateLimitConfig
from .utils import generate_key, get_time_window, parse_rate

logger = logging.getLogger(__name__)


class RateLimiter:
    """
    Main rate limiter class with async support.

    This class provides the primary interface for rate limiting,
    supporting both decorator-based and manual check approaches.

    Examples:
        Basic usage:
        >>> limiter = RateLimiter(redis_url="redis://localhost:6379")
        >>> await limiter.connect()
        >>> await limiter.check(key="user:123", rate="100/minute")

        With context manager:
        >>> async with RateLimiter() as limiter:
        >>>     await limiter.check(key="api:endpoint", rate="1000/hour")

        As decorator:
        >>> @limiter.limit("100/minute")
        >>> async def my_endpoint(request):
        >>>     return {"status": "ok"}
    """

    def __init__(
        self,
        redis_url: str = "redis://localhost:6379",
        key_prefix: str = "ratelimit",
        default_algorithm: str = "fixed_window",
        enable_metrics: bool = False,
    ):
        """
        Initialize the rate limiter.

        Args:
            redis_url: Redis connection URL
            key_prefix: Prefix for all Redis keys
            default_algorithm: Default algorithm to use
            enable_metrics: Whether to enable metrics collection
        """
        if default_algorithm not in ("fixed_window", "token_bucket", "sliding_window"):
            raise RateLimitConfigError(f"Unknown algorithm: {default_algorithm}")
        self.config = RateLimitConfig(
            redis_url=redis_url,
            key_prefix=key_prefix,
            default_algorithm=default_algorithm,  # type: ignore[arg-type]
            enable_metrics=enable_metrics,
        )
        self.backend = RedisBackend(self.config)
        self._connected = False
        self._lock = asyncio.Lock()  # For thread-safe connection

        logger.debug(f"Initialized RateLimiter with config: {self.config}")

    async def __aenter__(self) -> "RateLimiter":
        """Async context manager entry."""
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[TracebackType],
    ) -> None:
        """Async context manager exit."""
        await self.close()

    async def connect(self) -> None:
        """
        Initialize Redis connection.

        This method is idempotent and thread-safe.

        Raises:
            BackendError: If connection fails
        """
        async with self._lock:
            if not self._connected:
                await self.backend.connect()
                self._connected = True
                logger.info("RateLimiter connected to Redis")

    async def close(self) -> None:
        """
        Close Redis connection gracefully.

        This method is idempotent and thread-safe.
        """
        async with self._lock:
            if self._connected:
                await self.backend.close()
                self._connected = False
                logger.info("RateLimiter disconnected from Redis")

    async def check(
        self,
        key: str,
        rate: str,
        algorithm: Optional[str] = None,
        tenant_type: Optional[str] = None,
        cost: int = 1,
    ) -> bool:
        """
        Check if a request is allowed under the rate limit.

        This is the core method for rate limiting. It checks whether
        a request identified by `key` is allowed under the specified
        rate limit.

        Args:
            key: Unique identifier for the rate limit (e.g., user ID, IP address)
            rate: Rate limit string (e.g., "100/minute", "1000/hour")
            algorithm: Algorithm to use (defaults to config.default_algorithm)
            tenant_type: Tenant type for multi-tenant setups (e.g., "free", "premium")
            cost: Cost of this request (default 1, can be higher for expensive operations)

        Returns:
            True if request is allowed

        Raises:
            RateLimitExceeded: If rate limit is exceeded
            RateLimitConfigError: If configuration is invalid
            BackendError: If backend operation fails

        Examples:
            >>> # Simple check
            >>> await limiter.check(key="user:123", rate="100/minute")
            True

            >>> # Multi-tenant check
            >>> await limiter.check(
            ...     key="api:key:abc123",
            ...     rate="1000/hour",
            ...     tenant_type="premium"
            ... )
            True

            >>> # Higher cost operation
            >>> await limiter.check(
            ...     key="user:123",
            ...     rate="100/minute",
            ...     cost=10  # This request counts as 10 regular requests
            ... )
            True
        """
        # Use check_with_info internally and just return the allowed status
        result = await self.check_with_info(
            key=key,
            rate=rate,
            algorithm=algorithm,
            tenant_type=tenant_type,
            cost=cost,
        )
        return result.allowed

    async def check_with_info(
        self,
        key: str,
        rate: str,
        algorithm: Optional[str] = None,
        tenant_type: Optional[str] = None,
        cost: int = 1,
    ) -> CheckResult:
        """
        Check if a request is allowed and return detailed rate limit info.

        This method is similar to check() but returns a CheckResult with
        full rate limit information instead of just True/raising an exception.
        This is more efficient when you need usage info (e.g., for headers)
        because it avoids a second Redis call.

        Args:
            key: Unique identifier for the rate limit (e.g., user ID, IP address)
            rate: Rate limit string (e.g., "100/minute", "1000/hour")
            algorithm: Algorithm to use (defaults to config.default_algorithm)
            tenant_type: Tenant type for multi-tenant setups (e.g., "free", "premium")
            cost: Cost of this request (default 1, can be higher for expensive operations)

        Returns:
            CheckResult with allowed status and usage information

        Raises:
            RateLimitConfigError: If configuration is invalid
            BackendError: If backend operation fails

        Examples:
            >>> result = await limiter.check_with_info(key="user:123", rate="100/minute")
            >>> if result.allowed:
            ...     print(f"{result.remaining} requests remaining")
            ... else:
            ...     print(f"Rate limited, retry after {result.retry_after}s")
        """
        # Ensure we're connected
        if not self._connected:
            await self.connect()

        # Parse rate limit
        try:
            requests, window_seconds = parse_rate(rate)
        except ValueError as e:
            raise RateLimitConfigError(f"Invalid rate format: {e}") from e

        # Select algorithm
        algorithm = algorithm or self.config.default_algorithm
        if algorithm not in ["fixed_window", "token_bucket", "sliding_window"]:
            raise RateLimitConfigError(f"Unknown algorithm: {algorithm}")

        tenant_type = tenant_type or "default"

        # Use integer math (multiply by 1000 for precision)
        max_requests = requests * 1000
        cost_with_multiplier = cost * 1000

        # Route to appropriate algorithm
        # Use Redis server time for consistency in distributed deployments
        redis_time_seconds, redis_time_us = await self.backend.get_redis_time()

        if algorithm == "fixed_window":
            # Fixed window needs time-based key for window buckets
            current_time = redis_time_seconds
            time_window = get_time_window(window_seconds, current_time)
            window_end = int(time_window) + window_seconds  # When this window expires
            full_key = generate_key(
                self.config.key_prefix,
                key,
                tenant_type,
                time_window,
            )
            result = await self.backend.check_fixed_window(
                full_key, max_requests, window_seconds, window_end, cost_with_multiplier
            )
        elif algorithm == "token_bucket":
            # Token bucket uses persistent key (no time window needed)
            full_key = generate_key(
                self.config.key_prefix,
                key,
                tenant_type,
                "bucket",  # Static suffix instead of time window
            )
            # Use milliseconds for precision with low rates (e.g., 1/hour)
            # refill_rate = max_requests / window_seconds (tokens per second, integer)
            # Lua script will use ms timestamps for sub-second refill precision
            refill_rate_per_second = max_requests // window_seconds
            current_time_ms = redis_time_seconds * 1000 + redis_time_us // 1000
            result = await self.backend.check_token_bucket(
                key=full_key,
                max_tokens=max_requests,
                refill_rate_per_second=refill_rate_per_second,
                window_seconds=window_seconds,
                current_time_ms=current_time_ms,
                cost=cost_with_multiplier,
            )
        elif algorithm == "sliding_window":
            # Sliding window needs base key (windows calculated in algorithm)
            base_key = generate_key(
                self.config.key_prefix,
                key,
                tenant_type,
                "sliding",  # Base suffix for sliding window
            )
            current_time = redis_time_seconds
            window_start = current_time - (current_time % window_seconds)
            previous_window_start = window_start - window_seconds

            result = await self.backend.check_sliding_window(
                current_key=f"{base_key}:{window_start}",
                previous_key=f"{base_key}:{previous_window_start}",
                max_requests=max_requests,
                window_seconds=window_seconds,
                current_time=current_time,
                cost=cost_with_multiplier,
            )
        else:
            raise NotImplementedError(f"Algorithm {algorithm} not yet implemented")

        # Convert from integer math (1000x multiplier)
        remaining_requests = result.remaining // 1000
        retry_after_seconds = max(1, result.retry_after // 1000) if not result.allowed else 0

        # Create CheckResult with all info
        check_result = CheckResult(
            allowed=result.allowed,
            limit=requests,
            remaining=remaining_requests,
            retry_after=retry_after_seconds,
            window_seconds=window_seconds,
        )

        # If not allowed, raise exception (for backward compatibility with check())
        if not result.allowed:
            raise RateLimitExceeded(
                retry_after=retry_after_seconds,
                limit=rate,
                remaining=remaining_requests,
            )

        logger.debug(f"Rate limit check passed for key={key}, " f"remaining={remaining_requests}")

        return check_result

    def limit(
        self,
        rate: str,
        key: Optional[Callable[..., str]] = None,
        tenant_type: Optional[Callable[..., str]] = None,
        algorithm: Optional[str] = None,
        cost: Optional[Callable[..., int]] = None,
        trust_proxy_headers: bool = False,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """
        Create a decorator for rate limiting endpoints.

        This method returns a decorator that can be used to rate limit
        FastAPI or other async endpoints.

        Args:
            rate: Rate limit string (e.g., "100/minute")
            key: Optional function to extract key from request
                 If not provided, uses request.client.host (IP address)
            tenant_type: Optional function to extract tenant type from request
            algorithm: Algorithm to use (defaults to config.default_algorithm)
            cost: Optional function to calculate request cost
            trust_proxy_headers: If True, trust X-Forwarded-For headers for IP.
                               Only enable if behind a trusted reverse proxy.

        Returns:
            Decorator function for rate limiting

        Examples:
            >>> @app.get("/api/data")
            >>> @limiter.limit("100/minute")
            >>> async def get_data(request: Request):
            >>>     return {"data": "..."}

            >>> @app.get("/api/users/{user_id}")
            >>> @limiter.limit(
            ...     "1000/hour",
            ...     key=lambda req: req.path_params.get("user_id")
            ... )
            >>> async def get_user(request: Request, user_id: str):
            >>>     return {"user_id": user_id}

            >>> @app.post("/api/expensive")
            >>> @limiter.limit(
            ...     "100/minute",
            ...     cost=lambda req: 10 if req.headers.get("X-Premium") else 1
            ... )
            >>> async def expensive_operation(request: Request):
            >>>     return {"status": "completed"}
        """
        from .decorators import create_limit_decorator

        return create_limit_decorator(
            limiter=self,
            rate=rate,
            key_func=key,
            tenant_func=tenant_type,
            algorithm=algorithm,
            cost_func=cost,
            trust_proxy_headers=trust_proxy_headers,
        )

    async def reset(
        self, key: str, algorithm: Optional[str] = None, tenant_type: Optional[str] = None
    ) -> bool:
        """
        Reset rate limit for a specific key.

        This method removes all rate limit data for the specified key,
        allowing it to start fresh.

        Args:
            key: Unique identifier for the rate limit
            algorithm: Algorithm used (defaults to config.default_algorithm).
                       Use "all" to reset keys for all algorithms.
            tenant_type: Tenant type (defaults to "default")

        Returns:
            True if reset was successful, False if key didn't exist

        Examples:
            >>> await limiter.reset("user:123")
            True

            >>> await limiter.reset("user:123", algorithm="token_bucket")
            True

            >>> await limiter.reset("user:123", algorithm="all")  # Reset all algorithms
            True
        """
        if not self._connected:
            await self.connect()

        tenant_type = tenant_type or "default"
        algorithm = algorithm or self.config.default_algorithm

        # Use Redis server time for consistent window calculation
        redis_time_seconds, _ = await self.backend.get_redis_time()

        reset_success = False

        if algorithm == "all":
            # Reset all algorithm types
            reset_success |= await self._reset_fixed_window(key, tenant_type, redis_time_seconds)
            reset_success |= await self._reset_token_bucket(key, tenant_type)
            reset_success |= await self._reset_sliding_window(key, tenant_type, redis_time_seconds)
        elif algorithm == "fixed_window":
            reset_success = await self._reset_fixed_window(key, tenant_type, redis_time_seconds)
        elif algorithm == "token_bucket":
            reset_success = await self._reset_token_bucket(key, tenant_type)
        elif algorithm == "sliding_window":
            reset_success = await self._reset_sliding_window(key, tenant_type, redis_time_seconds)
        else:
            raise RateLimitConfigError(f"Unknown algorithm: {algorithm}")

        return reset_success

    async def _reset_fixed_window(self, key: str, tenant_type: str, current_time: int) -> bool:
        """Reset fixed window rate limit keys."""
        reset_success = False

        # Reset all common window sizes (current window for each)
        for window_seconds in [1, 60, 3600, 86400]:  # second, minute, hour, day
            time_window = get_time_window(window_seconds, current_time)
            full_key = generate_key(
                self.config.key_prefix,
                key,
                tenant_type,
                time_window,
            )
            result = await self.backend.reset(full_key)
            if result:
                reset_success = True

        return reset_success

    async def _reset_token_bucket(self, key: str, tenant_type: str) -> bool:
        """Reset token bucket rate limit key."""
        full_key = generate_key(
            self.config.key_prefix,
            key,
            tenant_type,
            "bucket",
        )
        return await self.backend.reset(full_key)

    async def _reset_sliding_window(self, key: str, tenant_type: str, current_time: int) -> bool:
        """Reset sliding window rate limit keys."""
        reset_success = False

        # Reset sliding window keys for all common window sizes
        for window_seconds in [1, 60, 3600, 86400]:  # second, minute, hour, day
            base_key = generate_key(
                self.config.key_prefix,
                key,
                tenant_type,
                "sliding",
            )

            # Calculate current and previous window starts
            window_start = current_time - (current_time % window_seconds)
            previous_window_start = window_start - window_seconds

            # Delete both current and previous window keys
            current_key = f"{base_key}:{window_start}"
            previous_key = f"{base_key}:{previous_window_start}"

            if await self.backend.reset(current_key):
                reset_success = True
            if await self.backend.reset(previous_key):
                reset_success = True

        return reset_success

    async def get_usage(
        self,
        key: str,
        rate: str,
        algorithm: Optional[str] = None,
        tenant_type: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Get current usage statistics for a key.

        Args:
            key: Unique identifier for the rate limit
            rate: Rate limit string to determine window
            algorithm: Algorithm used (defaults to config.default_algorithm)
            tenant_type: Tenant type (defaults to "default")

        Returns:
            Dictionary with usage statistics. Format varies by algorithm:
            - fixed_window: {'current': int, 'limit': int, 'remaining': int, 'ttl': int}
            - token_bucket: {'tokens': int, 'limit': int, 'remaining': int, 'ttl': int}
            - sliding_window: {'current': int, 'limit': int, 'remaining': int,
                               'current_window': int, 'previous_window': int, 'weight': float}

        Examples:
            >>> usage = await limiter.get_usage("user:123", "100/minute")
            >>> print(usage)
            {'current': 42, 'limit': 100, 'remaining': 58, 'ttl': 45}

            >>> usage = await limiter.get_usage("user:123", "100/minute", algorithm="token_bucket")
            >>> print(usage)
            {'tokens': 58, 'limit': 100, 'remaining': 58, 'ttl': 120}
        """
        if not self._connected:
            await self.connect()

        requests, window_seconds = parse_rate(rate)
        tenant_type = tenant_type or "default"
        algorithm = algorithm or self.config.default_algorithm

        # Use Redis server time for consistency
        redis_time_seconds, redis_time_us = await self.backend.get_redis_time()

        if algorithm == "fixed_window":
            return await self._get_fixed_window_usage(
                key, requests, window_seconds, tenant_type, redis_time_seconds
            )
        elif algorithm == "token_bucket":
            return await self._get_token_bucket_usage(
                key, requests, window_seconds, tenant_type, redis_time_seconds, redis_time_us
            )
        elif algorithm == "sliding_window":
            return await self._get_sliding_window_usage(
                key, requests, window_seconds, tenant_type, redis_time_seconds
            )
        else:
            raise RateLimitConfigError(f"Unknown algorithm: {algorithm}")

    async def _get_fixed_window_usage(
        self, key: str, requests: int, window_seconds: int, tenant_type: str, current_time: int
    ) -> dict[str, Any]:
        """Get usage statistics for fixed window algorithm."""
        time_window = get_time_window(window_seconds, current_time)
        full_key = generate_key(
            self.config.key_prefix,
            key,
            tenant_type,
            time_window,
        )

        usage = await self.backend.get_usage(full_key)

        # Convert from integer math (1000x multiplier)
        current_requests = usage["current"] // 1000 if usage["current"] > 0 else 0
        remaining = max(0, requests - current_requests)

        return {
            "current": current_requests,
            "limit": requests,
            "remaining": remaining,
            "ttl": usage["ttl"],
            "window_seconds": window_seconds,
        }

    async def _get_token_bucket_usage(
        self,
        key: str,
        max_requests: int,
        window_seconds: int,
        tenant_type: str,
        redis_time_seconds: int,
        redis_time_us: int,
    ) -> dict[str, Any]:
        """Get usage statistics for token bucket algorithm."""
        full_key = generate_key(
            self.config.key_prefix,
            key,
            tenant_type,
            "bucket",
        )

        usage = await self.backend.get_token_bucket_usage(full_key)

        # Get current tokens (with 1000x multiplier)
        stored_tokens = usage.get("tokens", 0)
        last_refill_ms = usage.get("last_refill_ms", 0)

        # Calculate tokens after refill since last update
        max_tokens = max_requests * 1000
        refill_rate_per_second = max_tokens // window_seconds

        if last_refill_ms > 0:
            current_time_ms = redis_time_seconds * 1000 + redis_time_us // 1000
            time_elapsed_ms = max(0, current_time_ms - last_refill_ms)
            tokens_to_add = (refill_rate_per_second * time_elapsed_ms) // 1000
            current_tokens = min(max_tokens, stored_tokens + tokens_to_add)
        else:
            # Bucket not yet created, would start full
            current_tokens = max_tokens

        # Convert from integer math
        tokens_display = current_tokens // 1000
        remaining = tokens_display  # Tokens are what's remaining

        # Estimate TTL (bucket keys use 2*window + 60s expiry)
        ttl = window_seconds * 2 + 60

        return {
            "tokens": tokens_display,
            "limit": max_requests,
            "remaining": remaining,
            "ttl": ttl,
            "window_seconds": window_seconds,
        }

    async def _get_sliding_window_usage(
        self, key: str, max_requests: int, window_seconds: int, tenant_type: str, current_time: int
    ) -> dict[str, Any]:
        """Get usage statistics for sliding window algorithm."""
        base_key = generate_key(
            self.config.key_prefix,
            key,
            tenant_type,
            "sliding",
        )

        # Calculate window boundaries
        window_start = current_time - (current_time % window_seconds)
        previous_window_start = window_start - window_seconds

        current_key = f"{base_key}:{window_start}"
        previous_key = f"{base_key}:{previous_window_start}"

        # Get counts from both windows
        current_usage = await self.backend.get_usage(current_key)
        previous_usage = await self.backend.get_usage(previous_key)

        current_count = current_usage.get("current", 0)
        previous_count = previous_usage.get("current", 0)

        # Calculate weight using integer math (consistent with Lua script)
        elapsed_in_window = current_time - window_start
        remaining_in_window = window_seconds - elapsed_in_window

        # Use fixed-point weight (0-1000 scale) for consistency with Lua
        prev_weight_fp = (remaining_in_window * 1000) // window_seconds if window_seconds > 0 else 0

        # Calculate weighted count using integer math
        # Formula: weighted = current + (previous * weight)
        weighted_previous = (previous_count * prev_weight_fp) // 1000
        weighted_count = current_count + weighted_previous

        # Convert from 1000x multiplier for display
        weighted_count_display = weighted_count // 1000
        current_window_display = current_count // 1000
        previous_window_display = previous_count // 1000
        remaining = max(0, max_requests - weighted_count_display)

        return {
            "current": weighted_count_display,
            "limit": max_requests,
            "remaining": remaining,
            "current_window": current_window_display,
            "previous_window": previous_window_display,
            "weight": prev_weight_fp / 1000,  # Convert to float for display
            "window_seconds": window_seconds,
            "ttl": remaining_in_window,
        }

    async def health_check(self) -> bool:
        """
        Check if the rate limiter is healthy.

        Returns:
            True if healthy, False otherwise

        Examples:
            >>> if await limiter.health_check():
            >>>     print("Rate limiter is healthy")
        """
        if not self._connected:
            return False

        return await self.backend.health_check()
