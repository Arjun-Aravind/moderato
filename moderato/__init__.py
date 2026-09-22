"""
Moderato - Async Redis-backed rate limiting for Python
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

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
from .models import CheckResult, RateLimitConfig

# Framework integration (Starlette middleware) is optional - only import
# if starlette is available. Install with: pip install 'moderato[fastapi]'
try:
    from .middleware import RateLimitHeadersMiddleware

    _FRAMEWORK_AVAILABLE = True
except ModuleNotFoundError as exc:
    # Hide only a missing starlette; a defect inside middleware.py or its
    # other dependencies must surface instead of being swallowed.
    if exc.name is None or not (exc.name == "starlette" or exc.name.startswith("starlette.")):
        raise
    _FRAMEWORK_AVAILABLE = False
    RateLimitHeadersMiddleware = None  # type: ignore[misc, assignment]

# Metrics are optional - only import if prometheus_client is available
try:
    from .metrics import RateLimitMetrics as RateLimitMetrics
    from .metrics import init_metrics as init_metrics

    _METRICS_AVAILABLE = True
except ModuleNotFoundError as exc:
    if exc.name is None or not (
        exc.name == "prometheus_client" or exc.name.startswith("prometheus_client.")
    ):
        raise
    _METRICS_AVAILABLE = False

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
]

if _FRAMEWORK_AVAILABLE:
    __all__.append("RateLimitHeadersMiddleware")

# Add metrics to exports if available
if _METRICS_AVAILABLE:
    __all__.extend(["RateLimitMetrics", "init_metrics"])
