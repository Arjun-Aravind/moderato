"""
Rate limit headers middleware for decorated responses.

This module adds rate limit headers when a limiter decorator stores usage
information on the request.
"""

import logging
from typing import Any, Optional

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from .exceptions import RateLimitCallbackError, RateLimitExceeded
from .utils import parse_rate

logger = logging.getLogger(__name__)


class RateLimitHeadersMiddleware(BaseHTTPMiddleware):
    """
    Middleware to automatically add rate limit headers to responses.

    This middleware adds the following headers when rate limit information is available:
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
        from moderato import RateLimiter
        from moderato.middleware import RateLimitHeadersMiddleware

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

    Decorated endpoints receive headers on successful and rate-limited responses.
    """

    def __init__(self, app: ASGIApp) -> None:
        """Initialize the middleware."""
        super().__init__(app)

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

        except RateLimitCallbackError as exc:
            from starlette.responses import JSONResponse

            return JSONResponse(
                status_code=exc.status_code,
                content={"error": "Rate limit callback failed", "callback": exc.callback},
            )
        except RateLimitExceeded as exc:
            # Rate limit was exceeded - add headers with retry info.
            # exc.limit may be a rate string ("100/minute") or a plain number
            # from a manually raised exception; never fail the 429 on parsing.
            try:
                limit_display = str(parse_rate(exc.limit)[0])
            except (ValueError, AttributeError, TypeError):
                limit_display = str(exc.limit)

            headers = self._create_rate_limit_headers(
                limit=limit_display,
                remaining=0,
                reset_at=exc.reset_at,
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
        reset_at = rate_limit_info.get("reset_at")

        # Add standard headers
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        if reset_at is not None:
            response.headers["X-RateLimit-Reset"] = str(reset_at)

        logger.debug(
            f"Added rate limit headers: limit={limit}, remaining={remaining}, " f"reset={reset_at}"
        )

    def _create_rate_limit_headers(
        self,
        limit: str,
        remaining: int,
        reset_at: Optional[int],
        retry_after: Optional[int] = None,
    ) -> dict[str, str]:
        """
        Create rate limit headers dictionary.

        Args:
            limit: Rate limit string (e.g., "100/minute")
            remaining: Requests remaining
            reset_at: Unix timestamp when limit resets, if known
            retry_after: Seconds to wait before retrying (optional)

        Returns:
            Dictionary of headers
        """
        headers = {
            "X-RateLimit-Limit": limit,
            "X-RateLimit-Remaining": str(remaining),
        }

        if reset_at is not None:
            headers["X-RateLimit-Reset"] = str(reset_at)

        if retry_after is not None:
            headers["Retry-After"] = str(retry_after)

        return headers
