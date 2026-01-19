"""
Rate limiting algorithms module.

DEPRECATION NOTICE:
These algorithm classes are provided for reference and educational purposes.
The main RateLimiter class uses the Redis backend directly and does not use
these classes. For production use, always use the RateLimiter class:

    from fastlimit import RateLimiter

    limiter = RateLimiter(redis_url="redis://localhost:6379")
    await limiter.check(key="user:123", rate="100/minute", algorithm="sliding_window")

The algorithm parameter in RateLimiter.check() accepts:
- "fixed_window" (default) - Simple fixed time windows
- "token_bucket" - Token bucket with smooth refill
- "sliding_window" - Weighted sliding window (most accurate)
"""

import warnings
from typing import Any

from .base import RateLimitAlgorithm


def _deprecated_import(name: str) -> None:
    warnings.warn(
        f"{name} algorithm class is deprecated and may be removed in a future version. "
        f"Use RateLimiter.check(algorithm='{name.lower()}') instead.",
        DeprecationWarning,
        stacklevel=3,
    )


def __getattr__(name: str) -> Any:
    if name == "TokenBucket":
        _deprecated_import("TokenBucket")
        from .token_bucket import TokenBucket

        return TokenBucket
    elif name == "SlidingWindow":
        _deprecated_import("SlidingWindow")
        from .sliding_window import SlidingWindow

        return SlidingWindow
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["RateLimitAlgorithm", "TokenBucket", "SlidingWindow"]
