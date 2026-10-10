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
        retry_after: Optional[int],
        limit: str,
        remaining: int = 0,
        message: Optional[str] = None,
        *,
        reset_at: Optional[int] = None,
    ):
        """
        Initialize RateLimitExceeded exception.

        Args:
            retry_after: Seconds until recovery, or None if cost exceeds capacity
            limit: The rate limit that was exceeded (e.g., "100/minute")
            remaining: Number of requests remaining in the current window
            message: Optional custom error message
            reset_at: Unix timestamp when a request may be retried, when known
        """
        self.retry_after = retry_after
        self.limit = limit
        self.remaining = remaining
        self.reset_at = None if retry_after is None else reset_at
        self.status_code = 422 if retry_after is None else 429

        if message is None:
            if retry_after is None:
                message = (
                    f"Request cost exceeds rate limit capacity ({limit}); retrying cannot help."
                )
            else:
                message = f"Rate limit exceeded ({limit}). Retry after {retry_after} seconds."

        super().__init__(message)


class RateLimitConfigError(RateLimitError):
    """Raised when rate limit configuration is invalid."""

    pass


class RateLimitCallbackError(RateLimitError):
    """Raised when a decorator's key, tenant, or cost callback fails.

    Decorated requests are rejected before a rate-limit decision is made so
    an identity or cost policy cannot be bypassed by a callback failure.
    FastAPI applications should map this exception to HTTP 503.
    """

    status_code = 503

    def __init__(self, callback: str):
        self.callback = callback
        super().__init__(f"Rate limit {callback} callback failed")


class BackendError(RateLimitError):
    """Raised when backend operations fail (e.g., Redis connection issues)."""

    pass
