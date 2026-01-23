"""
Sliding Window rate limiting algorithm implementation.

The sliding window algorithm provides the most accurate rate limiting
by combining the current window with a weighted portion of the previous window.
"""

import logging
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..backends.redis import RedisBackend

from .base import RateLimitAlgorithm, RateLimitResult

logger = logging.getLogger(__name__)


class SlidingWindow(RateLimitAlgorithm):
    """
    Sliding Window algorithm implementation.

    How it works:
    - Maintains counts for current and previous time windows
    - Calculates weighted average based on position in current window
    - Weight = (1 - progress_through_window)
    - Example: 30s into 60s window = 50% from previous + 50% from current

    Mathematical Formula:
        weighted_count = previous_count * (1 - t/T) + current_count
        where:
        - t = time elapsed in current window
        - T = total window duration
        - previous_count = requests in previous window
        - current_count = requests in current window

    Example (100 requests/minute):
        Time: 14:35:30 (30 seconds into minute)
        Previous window (14:34): 80 requests
        Current window (14:35): 40 requests

        Weight for previous = 1 - (30/60) = 0.5
        Weighted count = 80 * 0.5 + 40 = 40 + 40 = 80 requests

        Can accept: 100 - 80 = 20 more requests

    Advantages over Fixed Window:
    - No boundary bursts (smooths across window edges)
    - More accurate distribution
    - Better user experience
    - Fairer rate limiting

    Advantages over Token Bucket:
    - Simpler to understand
    - More predictable
    - Lower memory usage
    - Easier to debug

    Disadvantages:
    - Requires two Redis keys (current + previous)
    - Slightly more complex than Fixed Window
    - Not as smooth as Token Bucket for bursts
    """

    def __init__(self, backend: "RedisBackend") -> None:
        """
        Initialize Sliding Window algorithm.

        Args:
            backend: Redis backend for executing Lua scripts
        """
        self.backend = backend
        logger.debug("Initialized SlidingWindow algorithm")

    async def check(
        self,
        key: str,
        max_requests: int,
        window_seconds: int,
        cost: int = 1000,
    ) -> RateLimitResult:
        """
        Check if a request is allowed under sliding window rate limit.

        Args:
            key: Base identifier for the rate limit (without time suffix)
            max_requests: Maximum requests (with multiplier)
            window_seconds: Time window in seconds
            cost: Number of requests to consume (with multiplier)

        Returns:
            RateLimitResult with allowed status and metadata

        Example:
            For "100/minute":
            - max_requests = 100000 (100 * 1000)
            - window_seconds = 60
            - At 14:35:30:
              - Current window: ratelimit:user:default:14:35
              - Previous window: ratelimit:user:default:14:34
              - Weight: 0.5 (30 seconds into current window)
        """
        # Get current timestamp
        current_time = int(time.time())

        # Calculate current window start time
        window_start = current_time - (current_time % window_seconds)

        # Calculate previous window start time
        previous_window_start = window_start - window_seconds

        # Generate keys for current and previous windows
        # We'll append the window start timestamp to ensure uniqueness
        current_key = f"{key}:{window_start}"
        previous_key = f"{key}:{previous_window_start}"

        # Execute sliding window Lua script
        result = await self.backend.check_sliding_window(
            current_key=current_key,
            previous_key=previous_key,
            max_requests=max_requests,
            window_seconds=window_seconds,
            current_time=current_time,
            cost=cost,
        )

        # Calculate reset timestamp (start of next window)
        reset_at = window_start + window_seconds

        return RateLimitResult(
            allowed=result.allowed,
            remaining=result.remaining,
            retry_after=result.retry_after,
            reset_at=reset_at,
        )

    async def reset(self, key: str) -> bool:
        """
        Reset sliding window for a specific key.

        This removes both current and previous window data.

        Args:
            key: Base identifier for the rate limit

        Returns:
            True if reset was successful
        """
        # For sliding window, we need to reset multiple keys
        # We'll try to delete keys for recent windows
        current_time = int(time.time())

        # Try to delete current and recent windows
        success = False
        for offset in [0, 60, 3600, 86400]:  # Now, minute, hour, day ago
            window_start = current_time - (current_time % offset) if offset > 0 else current_time
            test_key = f"{key}:{window_start}"
            if await self.backend.reset(test_key):
                success = True

            # Also try previous window
            if offset > 0:
                prev_key = f"{key}:{window_start - offset}"
                if await self.backend.reset(prev_key):
                    success = True

        return success

    async def get_usage(self, key: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """
        Get current usage statistics for sliding window.

        Args:
            key: Base identifier for the rate limit
            *args: Expected to contain max_requests and window_seconds
            **kwargs: Not used

        Returns:
            Dictionary with:
            - current: Weighted current usage (without multiplier)
            - limit: Maximum requests (without multiplier)
            - remaining: Requests remaining (without multiplier)
            - current_window: Requests in current window
            - previous_window: Requests in previous window
            - weight: Weight applied to previous window (0.0 to 1.0)
        """
        max_requests: int = args[0] if len(args) > 0 else kwargs.get("max_requests", 0)
        window_seconds: int = args[1] if len(args) > 1 else kwargs.get("window_seconds", 60)
        current_time = int(time.time())
        window_start = current_time - (current_time % window_seconds)
        previous_window_start = window_start - window_seconds

        current_key = f"{key}:{window_start}"
        previous_key = f"{key}:{previous_window_start}"

        # Get counts from Redis
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
        # Note: counts already have 1000x multiplier, weight is 0-1000
        weighted_previous = (previous_count * prev_weight_fp) // 1000
        weighted_count = (current_count + weighted_previous) // 1000  # Divide by 1000 for display

        max_requests_display = max_requests // 1000
        remaining = max(0, max_requests_display - weighted_count)

        return {
            "current": weighted_count,
            "limit": max_requests_display,
            "remaining": remaining,
            "current_window": current_count // 1000,
            "previous_window": previous_count // 1000,
            "weight": prev_weight_fp / 1000,  # Convert fixed-point to float for display
            "window_seconds": window_seconds,
        }


def calculate_sliding_window_count(
    current_count: int,
    previous_count: int,
    window_seconds: int,
    elapsed_seconds: int,
) -> float:
    """
    Calculate weighted count for sliding window.

    This is the core formula used by the sliding window algorithm.

    Args:
        current_count: Requests in current window
        previous_count: Requests in previous window
        window_seconds: Total window duration
        elapsed_seconds: Time elapsed in current window

    Returns:
        Weighted count as float

    Examples:
        >>> # 30 seconds into 60-second window
        >>> calculate_sliding_window_count(
        ...     current_count=40,
        ...     previous_count=80,
        ...     window_seconds=60,
        ...     elapsed_seconds=30
        ... )
        80.0  # 40 + (80 * 0.5)

        >>> # Start of window (0 seconds elapsed)
        >>> calculate_sliding_window_count(
        ...     current_count=10,
        ...     previous_count=90,
        ...     window_seconds=60,
        ...     elapsed_seconds=0
        ... )
        100.0  # 10 + (90 * 1.0)

        >>> # End of window (60 seconds elapsed)
        >>> calculate_sliding_window_count(
        ...     current_count=50,
        ...     previous_count=100,
        ...     window_seconds=60,
        ...     elapsed_seconds=60
        ... )
        50.0  # 50 + (100 * 0.0)
    """
    if window_seconds <= 0:
        raise ValueError("Window seconds must be positive")

    if elapsed_seconds < 0 or elapsed_seconds > window_seconds:
        raise ValueError(f"Elapsed seconds must be between 0 and {window_seconds}")

    # Calculate weight for previous window
    progress = elapsed_seconds / window_seconds
    previous_weight = 1 - progress

    # Apply weighted formula
    weighted_count = current_count + (previous_count * previous_weight)

    return weighted_count
