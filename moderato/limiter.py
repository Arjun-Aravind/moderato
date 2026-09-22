"""
Main RateLimiter class implementation.
"""

import asyncio
import logging
import re
import time
from collections.abc import Awaitable
from types import TracebackType
from typing import Any, Callable, Optional, TypeVar

from .backends.redis import RedisBackend, _redact_redis_url
from .exceptions import RateLimitConfigError, RateLimitExceeded
from .models import CheckResult, RateLimitConfig
from .utils import (
    generate_key,
    get_time_window,
    normalize_key_component,
    parse_rate,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")


def _escape_glob(value: str) -> str:
    """Escape Redis glob metacharacters so a MATCH pattern matches literally."""
    for ch in ("\\", "*", "?", "["):
        value = value.replace(ch, f"\\{ch}")
    return value


def _policy_component(requests: int, window_seconds: int) -> str:
    """Policy component for Redis keys.

    Isolates buckets per rate limit (e.g. ``p100x60``) so the same
    identity under two different policies never shares state.
    """
    return f"p{requests}x{window_seconds}"


def _suffix_matches_algorithm(suffix: str, algorithm: str, has_tenant_prefix: bool = False) -> bool:
    """Classify a limiter key from the components after its identifier."""
    parts = suffix.split(":")
    policy_re = r"^p\d+x\d+$"
    if algorithm == "token_bucket":
        return (
            len(parts) >= 2 and re.match(policy_re, parts[-2]) is not None and parts[-1] == "bucket"
        )
    if algorithm == "sliding_window":
        return (
            len(parts) >= 3
            and re.match(policy_re, parts[-3]) is not None
            and parts[-2] == "sliding"
            and parts[-1].isdigit()
        )
    if algorithm == "fixed_window":
        return (
            len(parts) >= 2 and re.match(policy_re, parts[-2]) is not None and parts[-1].isdigit()
        )
    return True


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
        self._lock = asyncio.Lock()
        self.metrics: Optional[Any] = None
        if enable_metrics:
            try:
                from .metrics import init_metrics
            except ModuleNotFoundError as exc:
                if exc.name is None or not (
                    exc.name == "prometheus_client" or exc.name.startswith("prometheus_client.")
                ):
                    raise
                raise RateLimitConfigError(
                    "Prometheus metrics require the 'metrics' extra: "
                    "pip install 'moderato[metrics]'"
                ) from exc
            self.metrics = init_metrics()

        redacted_config = self.config.model_copy(
            update={"redis_url": _redact_redis_url(self.config.redis_url)}
        )
        logger.debug(f"Initialized RateLimiter with config: {redacted_config}")

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

    async def _run_backend_operation(self, operation: str, awaitable: Awaitable[T]) -> T:
        if self.metrics is None:
            return await awaitable

        started = time.perf_counter()
        try:
            result = await awaitable
        except Exception:
            self.metrics.record_backend_operation(
                operation, success=False, duration=time.perf_counter() - started
            )
            raise
        self.metrics.record_backend_operation(
            operation, success=True, duration=time.perf_counter() - started
        )
        return result

    async def check(
        self,
        key: str,
        rate: str,
        algorithm: Optional[str] = None,
        tenant_type: Optional[str] = None,
        cost: int = 1,
        scope: str = "global",
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
            scope: Namespace for independently limited resources (default "global")

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
            scope=scope,
        )
        return result.allowed

    async def check_with_info(
        self,
        key: str,
        rate: str,
        algorithm: Optional[str] = None,
        tenant_type: Optional[str] = None,
        cost: int = 1,
        scope: str = "global",
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
            scope: Namespace for independently limited resources (default "global")

        Returns:
            CheckResult with usage information when the request is allowed

        Raises:
            RateLimitExceeded: If rate limit is exceeded
            RateLimitConfigError: If configuration is invalid
            BackendError: If backend operation fails

        Examples:
            >>> result = await limiter.check_with_info(key="user:123", rate="100/minute")
            >>> print(f"{result.remaining} requests remaining")
        """
        # Validate the request configuration before touching the backend so
        # bad input raises RateLimitConfigError even without a Redis server.
        try:
            requests, window_seconds = parse_rate(rate)
        except ValueError as e:
            raise RateLimitConfigError(f"Invalid rate format: {e}") from e

        if not isinstance(cost, int) or isinstance(cost, bool) or cost < 1:
            raise RateLimitConfigError("cost must be a positive integer")

        # Select algorithm
        algorithm = algorithm or self.config.default_algorithm
        if algorithm not in ["fixed_window", "token_bucket", "sliding_window"]:
            raise RateLimitConfigError(f"Unknown algorithm: {algorithm}")

        check_started = time.perf_counter()

        # Ensure we're connected
        if not self._connected:
            await self.connect()

        tenant_type = tenant_type or "default"
        policy = _policy_component(requests, window_seconds)

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
                policy,
                time_window,
                scope=scope,
            )
            result = await self._run_backend_operation(
                "check_fixed_window",
                self.backend.check_fixed_window(
                    full_key, max_requests, window_seconds, window_end, cost_with_multiplier
                ),
            )
        elif algorithm == "token_bucket":
            # Token bucket uses persistent key (no time window needed)
            full_key = generate_key(
                self.config.key_prefix,
                key,
                tenant_type,
                policy,
                "bucket",
                scope=scope,
            )
            # Use milliseconds for precision with low rates (e.g., 1/hour)
            # refill_rate = max_requests / window_seconds (tokens per second).
            # Keep as float: integer division would truncate low rates like
            # 1/hour (1000 // 3600 == 0) to a bucket that never refills.
            refill_rate_per_second = max_requests / window_seconds
            current_time_ms = redis_time_seconds * 1000 + redis_time_us // 1000
            result = await self._run_backend_operation(
                "check_token_bucket",
                self.backend.check_token_bucket(
                    key=full_key,
                    max_tokens=max_requests,
                    refill_rate_per_second=refill_rate_per_second,
                    window_seconds=window_seconds,
                    current_time_ms=current_time_ms,
                    cost=cost_with_multiplier,
                ),
            )
        elif algorithm == "sliding_window":
            # Sliding window needs base key (windows calculated in algorithm)
            base_key = generate_key(
                self.config.key_prefix,
                key,
                tenant_type,
                policy,
                "sliding",
                scope=scope,
            )
            current_time = redis_time_seconds
            window_start = current_time - (current_time % window_seconds)
            previous_window_start = window_start - window_seconds

            result = await self._run_backend_operation(
                "check_sliding_window",
                self.backend.check_sliding_window(
                    current_key=f"{base_key}:{window_start}",
                    previous_key=f"{base_key}:{previous_window_start}",
                    max_requests=max_requests,
                    window_seconds=window_seconds,
                    current_time=current_time,
                    cost=cost_with_multiplier,
                ),
            )
        else:
            raise NotImplementedError(f"Algorithm {algorithm} not yet implemented")

        remaining_requests = result.remaining // 1000
        retry_after_seconds = (
            max(1, (result.retry_after + 999) // 1000) if not result.allowed else 0
        )

        if self.metrics is not None:
            self.metrics.observe_check_duration(algorithm, time.perf_counter() - check_started)
            self.metrics.record_check(algorithm, result.allowed)
            if not result.allowed:
                self.metrics.record_limit_exceeded(algorithm, tenant_type)

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
        scope: Optional[str] = None,
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
            scope: Shared bucket name. By default, each route and method is isolated.
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
            scope=scope,
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
            tenant_type: Tenant type to reset (e.g., "free", "premium").
                         Defaults to None which resets ALL tenant types for
                         this key. Pass "default" explicitly to reset only
                         the default tenant.

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

        # No tenant_type means reset every tenant type for this key.
        # A single SCAN + delete covers all algorithms and windows at once.
        if tenant_type is None:
            return await self._reset_all_tenants(key, algorithm)

        tenant_type = tenant_type or "default"
        algorithm = algorithm or self.config.default_algorithm

        # Keys include a policy component (p{limit}x{window}), so the exact
        # set of keys cannot be reconstructed without knowing every policy
        # ever applied - scan for the tenant's keys and filter instead.
        if algorithm == "all":
            return await self._reset_matching(key, tenant_type, None)
        if algorithm not in ("fixed_window", "token_bucket", "sliding_window"):
            raise RateLimitConfigError(f"Unknown algorithm: {algorithm}")
        return await self._reset_matching(key, tenant_type, algorithm)

    async def _reset_all_tenants(self, key: str, algorithm: Optional[str] = None) -> bool:
        """Reset rate limit keys for every tenant type of this key.

        Scans for keys shaped ``{prefix}:{encoded_key}:*`` and optionally
        filters the matches by algorithm.
        """
        if not self._connected:
            await self.connect()

        if algorithm is not None and algorithm not in (
            "all",
            "fixed_window",
            "token_bucket",
            "sliding_window",
        ):
            raise RateLimitConfigError(f"Unknown algorithm: {algorithm}")

        encoded_key = normalize_key_component(key)
        pattern = f"{_escape_glob(self.config.key_prefix)}:{encoded_key}:*"
        suffix_offset = len(self.config.key_prefix) + 1 + len(encoded_key) + 1

        check_algorithm = algorithm if algorithm not in (None, "all") else None
        return await self._delete_matching(
            pattern, suffix_offset, check_algorithm, has_tenant_prefix=True
        )

    async def _reset_matching(self, key: str, tenant_type: str, algorithm: Optional[str]) -> bool:
        """Reset rate limit keys for one tenant type of this key.

        Scans for keys shaped ``{prefix}:{encoded_key}:{tenant}:*`` which
        covers every policy, algorithm, and time window for the tenant in
        one pass. If an ``algorithm`` is given, only keys for that algorithm
        are deleted.
        """
        encoded_key = normalize_key_component(key)
        encoded_tenant = normalize_key_component(tenant_type)
        pattern = f"{_escape_glob(self.config.key_prefix)}:{encoded_key}:{encoded_tenant}:*"
        # Everything after "{prefix}:{encoded_key}:{tenant}:" is the suffix
        suffix_offset = (
            len(self.config.key_prefix) + 1 + len(encoded_key) + 1 + len(encoded_tenant) + 1
        )
        return await self._delete_matching(pattern, suffix_offset, algorithm)

    async def _delete_matching(
        self,
        pattern: str,
        suffix_offset: int,
        algorithm: Optional[str],
        has_tenant_prefix: bool = False,
    ) -> bool:
        """Delete keys matching ``pattern`` whose suffix classifies as ``algorithm``."""
        deleted_any = False
        batch: list[str] = []
        async for full_key in self.backend.iter_keys(pattern):
            if algorithm is not None and not _suffix_matches_algorithm(
                full_key[suffix_offset:], algorithm, has_tenant_prefix
            ):
                continue
            batch.append(full_key)
            if len(batch) >= 100:
                if await self.backend.delete_many(batch):
                    deleted_any = True
                batch = []
        if batch and await self.backend.delete_many(batch):
            deleted_any = True
        return deleted_any

    async def get_usage(
        self,
        key: str,
        rate: str,
        algorithm: Optional[str] = None,
        tenant_type: Optional[str] = None,
        scope: str = "global",
    ) -> dict[str, Any]:
        """
        Get current usage statistics for a key.

        Args:
            key: Unique identifier for the rate limit
            rate: Rate limit string to determine window
            algorithm: Algorithm used (defaults to config.default_algorithm)
            tenant_type: Tenant type (defaults to "default")
            scope: Namespace used for the rate limit (defaults to "global")

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
                key, requests, window_seconds, tenant_type, redis_time_seconds, scope
            )
        elif algorithm == "token_bucket":
            return await self._get_token_bucket_usage(
                key,
                requests,
                window_seconds,
                tenant_type,
                redis_time_seconds,
                redis_time_us,
                scope,
            )
        elif algorithm == "sliding_window":
            return await self._get_sliding_window_usage(
                key, requests, window_seconds, tenant_type, redis_time_seconds, scope
            )
        else:
            raise RateLimitConfigError(f"Unknown algorithm: {algorithm}")

    async def _get_fixed_window_usage(
        self,
        key: str,
        requests: int,
        window_seconds: int,
        tenant_type: str,
        current_time: int,
        scope: str,
    ) -> dict[str, Any]:
        """Get usage statistics for fixed window algorithm."""
        time_window = get_time_window(window_seconds, current_time)
        full_key = generate_key(
            self.config.key_prefix,
            key,
            tenant_type,
            _policy_component(requests, window_seconds),
            time_window,
            scope=scope,
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
        scope: str,
    ) -> dict[str, Any]:
        """Get usage statistics for token bucket algorithm."""
        full_key = generate_key(
            self.config.key_prefix,
            key,
            tenant_type,
            _policy_component(max_requests, window_seconds),
            "bucket",
            scope=scope,
        )

        usage = await self.backend.get_token_bucket_usage(full_key)

        # Get current tokens (with 1000x multiplier)
        stored_tokens = usage.get("tokens", 0)
        last_refill_ms = usage.get("last_refill_ms", 0)

        # Calculate tokens after refill since last update
        max_tokens = max_requests * 1000
        refill_rate_per_second = max_tokens / window_seconds

        if last_refill_ms > 0:
            current_time_ms = redis_time_seconds * 1000 + redis_time_us // 1000
            time_elapsed_ms = max(0, current_time_ms - last_refill_ms)
            tokens_to_add = (refill_rate_per_second * time_elapsed_ms) // 1000
            current_tokens = min(max_tokens, stored_tokens + tokens_to_add)
        else:
            # Bucket not yet created, would start full
            current_tokens = max_tokens

        # Convert from integer math (float refill rate can leak floats here)
        tokens_display = int(current_tokens // 1000)
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
        self,
        key: str,
        max_requests: int,
        window_seconds: int,
        tenant_type: str,
        current_time: int,
        scope: str,
    ) -> dict[str, Any]:
        """Get usage statistics for sliding window algorithm."""
        base_key = generate_key(
            self.config.key_prefix,
            key,
            tenant_type,
            _policy_component(max_requests, window_seconds),
            "sliding",
            scope=scope,
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
