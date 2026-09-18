"""
Exception classes for Moderato rate limiting library.
"""

from typing import Optional


class RateLimitError(Exception):
    """Base exception for all rate limiting errors."""

    pass


class RateLimitExceeded(RateLimitError):
    """
    Raised when a rate limit is exceeded.

    This exception contains metadata about the rate limit state,
    including when the client can retry and how many requests remain.
    """

    def __init__(
        self,
        retry_after: int,
        limit: str,
        remaining: int = 0,
        message: Optional[str] = None,
    ):
        """
        Initialize RateLimitExceeded exception.

        Args:
            retry_after: Seconds until the rate limit resets
            limit: The rate limit that was exceeded (e.g., "100/minute")
            remaining: Number of requests remaining in the current window
            message: Optional custom error message
        """
        self.retry_after = retry_after
        self.limit = limit
        self.remaining = remaining

        if message is None:
            message = f"Rate limit exceeded ({limit}). Retry after {retry_after} seconds."

        super().__init__(message)


class RateLimitConfigError(RateLimitError):
    """Raised when rate limit configuration is invalid."""

    pass


class BackendError(RateLimitError):
    """Raised when backend operations fail (e.g., Redis connection issues)."""

    pass
