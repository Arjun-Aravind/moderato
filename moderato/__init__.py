"""
Moderato - Production-ready rate limiting library for Python
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

A high-performance, Redis-backed rate limiting library with async support.

Basic usage:
    >>> from moderato import RateLimiter
    >>> limiter = RateLimiter(redis_url="redis://localhost:6379")
    >>> await limiter.connect()
    >>> await limiter.check(key="user:123", rate="100/minute")

FastAPI integration:
    >>> from fastapi import FastAPI, Request
    >>> from moderato import RateLimiter
    >>>
    >>> app = FastAPI()
    >>> limiter = RateLimiter()
    >>>
    >>> @app.get("/api/data")
    >>> @limiter.limit("100/minute")
    >>> async def get_data(request: Request):
    >>>     return {"data": "..."}
"""

from .exceptions import BackendError, RateLimitConfigError, RateLimitExceeded
from .limiter import RateLimiter
from .middleware import RateLimitHeadersMiddleware
from .models import CheckResult, RateLimitConfig

# Metrics are optional - only import if prometheus_client is available
try:
    from .metrics import RateLimitMetrics, init_metrics

    _METRICS_AVAILABLE = True
except ImportError:
    _METRICS_AVAILABLE = False
    RateLimitMetrics = None  # type: ignore[misc, assignment]
    init_metrics = None  # type: ignore[assignment]

__version__ = "0.3.0"
__author__ = "Arjun Aravind"
__email__ = "arjunaravind748@gmail.com"

__all__ = [
    "RateLimiter",
    "RateLimitExceeded",
    "RateLimitConfigError",
    "BackendError",
    "RateLimitConfig",
    "CheckResult",
    "RateLimitHeadersMiddleware",
]

# Add metrics to exports if available
if _METRICS_AVAILABLE:
    __all__.extend(["RateLimitMetrics", "init_metrics"])
