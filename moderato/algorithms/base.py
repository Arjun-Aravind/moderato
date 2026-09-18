"""
Base class for rate limiting algorithms.
"""

from abc import ABC, abstractmethod
from typing import Any, NamedTuple, Optional


class RateLimitResult(NamedTuple):
    """Result of a rate limit check."""

    allowed: bool  # Whether the request is allowed
    remaining: int  # Number of requests remaining (with multiplier)
    retry_after: int  # Milliseconds until rate limit resets
    reset_at: Optional[int] = None  # Unix timestamp when limit resets


class RateLimitAlgorithm(ABC):
    """Abstract base class for rate limiting algorithms."""

    @abstractmethod
    async def check(self, key: str, max_requests: int, window_seconds: int) -> RateLimitResult:
        """
        Check if a request is allowed under the rate limit.

        Args:
            key: Unique identifier for the rate limit
            max_requests: Maximum allowed requests (with multiplier)
            window_seconds: Time window in seconds

        Returns:
            RateLimitResult with status and metadata
        """
        pass

    @abstractmethod
    async def reset(self, key: str) -> bool:
        """
        Reset the rate limit for a specific key.

        Args:
            key: Unique identifier for the rate limit

        Returns:
            True if reset was successful
        """
        pass

    @abstractmethod
    async def get_usage(self, key: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """
        Get current usage statistics for a key.

        Args:
            key: Unique identifier for the rate limit
            *args: Additional positional arguments (algorithm-specific)
            **kwargs: Additional keyword arguments (algorithm-specific)

        Returns:
            Dictionary with usage statistics
        """
        pass
