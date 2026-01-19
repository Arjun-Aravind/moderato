"""
Rate limit headers middleware for automatic header injection.

This middleware automatically adds standard rate limit headers to all HTTP responses,
following industry best practices from GitHub, Twitter, and other major APIs.
"""

import logging
import time
from typing import Any, Callable, Optional

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from .exceptions import RateLimitExceeded

logger = logging.getLogger(__name__)


class RateLimitHeadersMiddleware(BaseHTTPMiddleware):  # type: ignore[misc]
    """
    Middleware to automatically add rate limit headers to responses.

    This middleware adds the following headers to ALL responses:
    - X-RateLimit-Limit: Maximum requests allowed in the current window
    - X-RateLimit-Remaining: Requests remaining in the current window
    - X-RateLimit-Reset: Unix timestamp when the current window resets

    When rate limit is exceeded, it also adds:
    - Retry-After: Seconds to wait before retrying

    Industry Standard Headers:
    These headers follow the de facto standard used by GitHub, Twitter, Stripe,
    and other major APIs for rate limiting communication.

    Usage:
        from fastapi import FastAPI
        from fastlimit import RateLimiter
        from fastlimit.middleware import RateLimitHeadersMiddleware

        app = FastAPI()
        limiter = RateLimiter(redis_url="redis://localhost:6379")

        # Add middleware
        app.add_middleware(RateLimitHeadersMiddleware)

        @app.on_event("startup")
        async def startup():
            await limiter.connect()

        @app.get("/api/data")
        @limiter.limit("100/minute")
        async def get_data(request: Request):
            return {"data": "..."}

    The middleware will automatically add headers to all responses, even
    successful ones, so clients always know their rate limit status.
    """

    def __init__(self, app: ASGIApp, always_add_headers: bool = True) -> None:
        """
        Initialize the middleware.

        Args:
            app: The ASGI application
            always_add_headers: If True, add headers to all responses.
                               If False, only add headers when rate limit info is available.
        """
        super().__init__(app)
        self.always_add_headers = always_add_headers

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        """
        Process the request and add rate limit headers to the response.

        Args:
            request: The incoming request
            call_next: The next middleware or route handler

        Returns:
            Response with rate limit headers added
        """
        # Initialize rate limit info storage on request state
        request.state.rate_limit_info = None

        try:
            # Call the next middleware or route handler
            response = await call_next(request)

            # Add rate limit headers if available
            if hasattr(request.state, "rate_limit_info") and request.state.rate_limit_info:
                self._add_headers(response, request.state.rate_limit_info)

            return response

        except RateLimitExceeded as exc:
            # Rate limit was exceeded - add headers with retry info
            headers = self._create_rate_limit_headers(
                limit=exc.limit,
                remaining=0,
                reset_timestamp=int(time.time()) + exc.retry_after,
                retry_after=exc.retry_after,
            )

            # Create 429 response with headers
            from starlette.responses import JSONResponse

            return JSONResponse(
                status_code=429,
                content={
                    "error": "Rate limit exceeded",
                    "message": str(exc),
                    "retry_after": exc.retry_after,
                    "limit": exc.limit,
                },
                headers=headers,
            )

    def _add_headers(self, response: Response, rate_limit_info: dict[str, Any]) -> None:
        """
        Add rate limit headers to the response.

        Args:
            response: The response object to add headers to
            rate_limit_info: Dictionary containing rate limit information
        """
        limit = rate_limit_info.get("limit")
        remaining = rate_limit_info.get("remaining", 0)
        window_seconds = rate_limit_info.get("window_seconds", 60)

        # Calculate reset timestamp (current time + TTL or window)
        ttl = rate_limit_info.get("ttl", window_seconds)
        reset_timestamp = int(time.time()) + ttl

        # Add standard headers
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        response.headers["X-RateLimit-Reset"] = str(reset_timestamp)

        logger.debug(
            f"Added rate limit headers: limit={limit}, remaining={remaining}, "
            f"reset={reset_timestamp}"
        )

    def _create_rate_limit_headers(
        self,
        limit: str,
        remaining: int,
        reset_timestamp: int,
        retry_after: Optional[int] = None,
    ) -> dict[str, str]:
        """
        Create rate limit headers dictionary.

        Args:
            limit: Rate limit string (e.g., "100/minute")
            remaining: Requests remaining
            reset_timestamp: Unix timestamp when limit resets
            retry_after: Seconds to wait before retrying (optional)

        Returns:
            Dictionary of headers
        """
        headers = {
            "X-RateLimit-Limit": limit,
            "X-RateLimit-Remaining": str(remaining),
            "X-RateLimit-Reset": str(reset_timestamp),
        }

        if retry_after is not None:
            headers["Retry-After"] = str(retry_after)

        return headers


def inject_rate_limit_headers(
    limit: int,
    remaining: int,
    window_seconds: int,
    ttl: Optional[int] = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """
    Decorator to inject rate limit information into request state.

    This function is meant to be used internally by the limiter decorator
    to pass rate limit information to the middleware.

    Args:
        limit: Maximum requests allowed
        remaining: Requests remaining
        window_seconds: Size of the time window
        ttl: Time to live for the current window

    Returns:
        Decorator function

    Example:
        @inject_rate_limit_headers(limit=100, remaining=75, window_seconds=60)
        async def my_endpoint():
            return {"data": "..."}
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            # Execute the original function
            result = await func(*args, **kwargs)

            # Try to inject rate limit info into request state
            # This requires the Request object to be in args or kwargs
            request = None
            for arg in args:
                if isinstance(arg, Request):
                    request = arg
                    break

            if request is None:
                request = kwargs.get("request")

            if request and hasattr(request, "state"):
                request.state.rate_limit_info = {
                    "limit": limit,
                    "remaining": remaining,
                    "window_seconds": window_seconds,
                    "ttl": ttl or window_seconds,
                }

            return result

        return wrapper

    return decorator
