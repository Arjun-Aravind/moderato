"""
Token Bucket rate limiting algorithm implementation.

The token bucket algorithm provides smoother rate limiting compared to
fixed window, with better handling of bursty traffic.
"""

import logging
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..backends.redis import RedisBackend

from .base import RateLimitAlgorithm, RateLimitResult

logger = logging.getLogger(__name__)


class TokenBucket(RateLimitAlgorithm):
    """
    Token Bucket algorithm implementation.

    How it works:
    - A bucket holds tokens up to a maximum capacity
    - Tokens are continuously added at a fixed refill rate
    - Each request consumes one or more tokens
    - If not enough tokens, request is denied
    - Provides smooth rate limiting without boundary bursts

    Example:
        100 requests/minute = 100 max tokens, ~1.67 refill rate/second
        Allows bursts up to 100 requests, then sustained 1.67 req/sec

    Advantages over Fixed Window:
    - No burst at window boundaries
    - Smoother traffic distribution
    - Better for bursty workloads
    - Allows controlled bursts within capacity

    Disadvantages:
    - Slightly more complex
    - Uses more Redis memory (stores tokens + timestamp)
    - Can allow more requests in first window
    """

    def __init__(self, backend: "RedisBackend") -> None:
        """
        Initialize Token Bucket algorithm.

        Args:
            backend: Redis backend for executing Lua scripts
        """
        self.backend = backend
        logger.debug("Initialized TokenBucket algorithm")

    async def check(
        self,
        key: str,
        max_requests: int,
        window_seconds: int,
        cost: int = 1000,
    ) -> RateLimitResult:
        """
        Check if a request is allowed under token bucket rate limit.

        Args:
            key: Unique identifier for the rate limit
            max_requests: Maximum tokens (bucket capacity, with multiplier)
            window_seconds: Time window to spread requests over
            cost: Number of tokens to consume (with multiplier)

        Returns:
            RateLimitResult with allowed status and metadata

        Example:
            For "100/minute":
            - max_requests = 100000 (100 * 1000)
            - window_seconds = 60
            - refill_rate = 100000 / 60 = 1666.67 tokens/sec
            - Bucket starts full (100000 tokens)
            - Refills at 1666.67 tokens/sec
            - Max capacity: 100000 tokens
        """
        # Calculate refill rate (tokens per second as integer)
        # For 100/minute: 100000 / 60 = 1666 tokens/sec
        refill_rate_per_second = max_requests // window_seconds

        # Get current timestamp in milliseconds
        current_time_ms = int(time.time() * 1000)

        # Execute token bucket Lua script
        result = await self.backend.check_token_bucket(
            key=key,
            max_tokens=max_requests,
            refill_rate_per_second=refill_rate_per_second,
            window_seconds=window_seconds,
            current_time_ms=current_time_ms,
            cost=cost,
        )

        # Calculate reset timestamp (when bucket would be full)
        # If tokens remaining, no reset needed
        # If denied, reset_at = current_time + retry_after
        reset_at = None
        if not result.allowed:
            reset_at = (current_time_ms // 1000) + (result.retry_after // 1000)

        return RateLimitResult(
            allowed=result.allowed,
            remaining=result.remaining,
            retry_after=result.retry_after,
            reset_at=reset_at,
        )

    async def reset(self, key: str) -> bool:
        """
        Reset token bucket for a specific key.

        This removes the bucket data, causing the next request
        to start with a full bucket.

        Args:
            key: Unique identifier for the rate limit

        Returns:
            True if reset was successful
        """
        return await self.backend.reset(key)

    async def get_usage(self, key: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """
        Get current usage statistics for a token bucket.

        Args:
            key: Unique identifier for the rate limit
            *args: Expected to contain max_requests as first argument
            **kwargs: Not used

        Returns:
            Dictionary with:
            - current: Current token count (without multiplier)
            - limit: Maximum tokens (without multiplier)
            - remaining: Tokens remaining (without multiplier)
            - last_refill: Unix timestamp of last refill

        Note: For token bucket, "current" means tokens available,
        not requests consumed (inverse of fixed window).
        """
        max_requests: int = args[0] if args else kwargs.get("max_requests", 0)
        usage = await self.backend.get_token_bucket_usage(key)

        # Convert from integer math (divide by 1000)
        current_tokens = usage.get("tokens", max_requests) // 1000
        max_tokens_display = max_requests // 1000

        return {
            "current": current_tokens,
            "limit": max_tokens_display,
            "remaining": current_tokens,
            "last_refill": usage.get("last_refill", 0),
        }


def calculate_refill_rate(requests: int, window_seconds: int) -> float:
    """
    Calculate token refill rate for token bucket.

    Args:
        requests: Number of requests allowed in window
        window_seconds: Time window in seconds

    Returns:
        Refill rate in tokens per second

    Examples:
        >>> calculate_refill_rate(100, 60)  # 100/minute
        1.6666666666666667

        >>> calculate_refill_rate(1000, 3600)  # 1000/hour
        0.2777777777777778

        >>> calculate_refill_rate(10, 1)  # 10/second
        10.0
    """
    if window_seconds <= 0:
        raise ValueError("Window seconds must be positive")

    return requests / window_seconds


def calculate_bucket_capacity(requests: int, burst_factor: float = 1.0) -> int:
    """
    Calculate bucket capacity with optional burst allowance.

    By default, bucket capacity equals the rate limit. You can
    increase capacity to allow larger bursts.

    Args:
        requests: Base number of requests allowed
        burst_factor: Multiplier for bucket capacity (>= 1.0)
                     1.0 = no extra burst (default)
                     2.0 = allow 2x burst
                     0.5 = not recommended (less than rate)

    Returns:
        Bucket capacity in tokens

    Examples:
        >>> calculate_bucket_capacity(100, 1.0)  # Standard
        100

        >>> calculate_bucket_capacity(100, 1.5)  # Allow 50% burst
        150

        >>> calculate_bucket_capacity(100, 2.0)  # Allow 2x burst
        200

    Note: Burst factor is not currently exposed in the main API,
    but is available for advanced use cases.
    """
    if burst_factor < 1.0:
        logger.warning(
            f"Burst factor {burst_factor} < 1.0 may cause issues. " "Consider using >= 1.0"
        )

    return int(requests * burst_factor)
