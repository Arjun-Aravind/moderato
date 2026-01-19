"""
Decorator implementations for rate limiting.
"""

import functools
import logging
from inspect import iscoroutinefunction
from typing import Any, Callable, Optional, TypeVar

from typing_extensions import ParamSpec

from .exceptions import RateLimitExceeded

logger = logging.getLogger(__name__)

P = ParamSpec("P")
R = TypeVar("R")

KeyFunc = Callable[[Any], str]
TenantFunc = Callable[[Any], str]
CostFunc = Callable[[Any], int]


def create_limit_decorator(
    limiter: Any,
    rate: str,
    key_func: Optional[KeyFunc] = None,
    tenant_func: Optional[TenantFunc] = None,
    algorithm: Optional[str] = None,
    cost_func: Optional[CostFunc] = None,
    trust_proxy_headers: bool = False,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """
    Create a rate limit decorator for async functions.

    This function creates a decorator that can be used to rate limit
    FastAPI endpoints or any async function that receives a request-like
    object as its first argument.

    Args:
        limiter: RateLimiter instance
        rate: Rate limit string (e.g., "100/minute")
        key_func: Optional function to extract rate limit key from request
        tenant_func: Optional function to extract tenant type from request
        algorithm: Algorithm to use for rate limiting
        cost_func: Optional function to calculate request cost

    Returns:
        Decorator function

    Examples:
        >>> limiter = RateLimiter()
        >>> decorator = create_limit_decorator(
        ...     limiter=limiter,
        ...     rate="100/minute",
        ...     key_func=lambda req: req.client.host
        ... )
        >>> @decorator
        >>> async def my_endpoint(request):
        >>>     return {"status": "ok"}
    """

    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        """The actual decorator."""

        if not iscoroutinefunction(func):
            # For sync functions, create an async wrapper
            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                # Extract request from arguments
                request = _extract_request(args, kwargs)

                # Perform rate limit check
                await _check_rate_limit(
                    limiter=limiter,
                    request=request,
                    rate=rate,
                    key_func=key_func,
                    tenant_func=tenant_func,
                    algorithm=algorithm,
                    cost_func=cost_func,
                    trust_proxy_headers=trust_proxy_headers,
                )

                # Call the original function
                # Note: This converts sync to async, which may not be ideal
                return func(*args, **kwargs)

            return async_wrapper  # type: ignore[return-value]
        else:
            # For async functions
            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                # Extract request from arguments
                request = _extract_request(args, kwargs)

                # Perform rate limit check
                await _check_rate_limit(
                    limiter=limiter,
                    request=request,
                    rate=rate,
                    key_func=key_func,
                    tenant_func=tenant_func,
                    algorithm=algorithm,
                    cost_func=cost_func,
                    trust_proxy_headers=trust_proxy_headers,
                )

                # Call the original async function
                return await func(*args, **kwargs)

            return async_wrapper  # type: ignore[return-value]

    return decorator


def _extract_request(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    """
    Extract request object from function arguments.

    FastAPI passes the Request object as the first positional argument
    or as a keyword argument named 'request'.

    Args:
        args: Positional arguments
        kwargs: Keyword arguments

    Returns:
        Request object or None

    Raises:
        ValueError: If request cannot be found
    """
    # Try to get from kwargs first (more explicit)
    if "request" in kwargs:
        return kwargs["request"]

    # Try first positional argument
    if args:
        # Check if it looks like a request object
        # (has client attribute for FastAPI Request)
        first_arg = args[0]
        if hasattr(first_arg, "client") or hasattr(first_arg, "headers"):
            return first_arg

    # If we can't find a request, raise an error
    raise ValueError(
        "Could not extract request object from function arguments. "
        "Ensure the decorated function receives a Request object."
    )


async def _check_rate_limit(
    limiter: Any,
    request: Any,
    rate: str,
    key_func: Optional[KeyFunc],
    tenant_func: Optional[TenantFunc],
    algorithm: Optional[str],
    cost_func: Optional[CostFunc],
    trust_proxy_headers: bool = False,
) -> None:
    """
    Perform rate limit check and handle the result.

    Args:
        limiter: RateLimiter instance
        request: Request object
        rate: Rate limit string
        key_func: Function to extract key
        tenant_func: Function to extract tenant type
        algorithm: Algorithm to use
        cost_func: Function to calculate cost

    Raises:
        RateLimitExceeded: If rate limit is exceeded
    """
    # Extract rate limit key
    if key_func:
        try:
            key = key_func(request)
        except Exception as e:
            logger.error(f"Error extracting key with key_func: {e}")
            key = _get_default_key(request, trust_proxy_headers=trust_proxy_headers)
    else:
        key = _get_default_key(request, trust_proxy_headers=trust_proxy_headers)

    # Extract tenant type
    tenant_type = None
    if tenant_func:
        try:
            tenant_type = tenant_func(request)
        except Exception as e:
            logger.error(f"Error extracting tenant type: {e}")

    # Calculate cost
    cost = 1
    if cost_func:
        try:
            cost = cost_func(request)
        except Exception as e:
            logger.error(f"Error calculating cost: {e}")
            cost = 1

    # Perform rate limit check using check_with_info to get usage in single call
    try:
        result = await limiter.check_with_info(
            key=key,
            rate=rate,
            algorithm=algorithm,
            tenant_type=tenant_type,
            cost=cost,
        )

        # Rate limit check passed - store usage info for headers (no extra Redis call)
        if hasattr(request, "state"):
            request.state.rate_limit_info = {
                "limit": result.limit,
                "remaining": result.remaining,
                "window_seconds": result.window_seconds,
                "ttl": result.retry_after if result.retry_after > 0 else result.window_seconds,
            }

    except RateLimitExceeded as e:
        # Add rate limit headers to the request state
        # This allows middleware to add them to the response
        if hasattr(request, "state"):
            request.state.rate_limit_headers = {
                "X-RateLimit-Limit": rate,
                "X-RateLimit-Remaining": str(e.remaining),
                "X-RateLimit-Reset": str(e.retry_after),
                "Retry-After": str(e.retry_after),
            }

        # Re-raise the exception
        raise


def _get_default_key(request: Any, trust_proxy_headers: bool = False) -> str:
    """
    Get default rate limit key from request.

    Default behavior is to use the client's direct IP address.
    Proxy headers (X-Forwarded-For, X-Real-IP) are only trusted when
    explicitly enabled via trust_proxy_headers=True.

    SECURITY NOTE: Never trust proxy headers in production unless you're
    behind a trusted reverse proxy that sets these headers. An attacker
    can easily spoof these headers to bypass rate limiting.

    Args:
        request: Request object
        trust_proxy_headers: If True, trust X-Forwarded-For and X-Real-IP headers.
                            Only enable if behind a trusted reverse proxy.

    Returns:
        Rate limit key
    """
    # Primary: use direct client IP (most secure)
    # FastAPI/Starlette Request
    if hasattr(request, "client") and request.client and request.client.host:
        return f"ip:{request.client.host}"

    # Secondary: check proxy headers only if explicitly trusted
    if trust_proxy_headers and hasattr(request, "headers"):
        # X-Forwarded-For header (behind proxy)
        forwarded_for = request.headers.get("X-Forwarded-For")
        if forwarded_for:
            # Take the first IP in the chain (original client IP)
            ip = forwarded_for.split(",")[0].strip()
            if ip:
                return f"ip:{ip}"

        # X-Real-IP header (nginx)
        real_ip = request.headers.get("X-Real-IP")
        if real_ip:
            return f"ip:{real_ip}"

    # Fallback to a generic key
    logger.warning("Could not determine client IP, using fallback key")
    return "ip:unknown"


class RateLimitMiddleware:
    """
    ASGI middleware for rate limiting.

    This middleware can be added to FastAPI or other ASGI applications
    to automatically handle rate limiting for all endpoints.

    Examples:
        >>> from fastapi import FastAPI
        >>> from fastlimit import RateLimiter, RateLimitMiddleware
        >>>
        >>> app = FastAPI()
        >>> limiter = RateLimiter()
        >>>
        >>> app.add_middleware(
        ...     RateLimitMiddleware,
        ...     limiter=limiter,
        ...     default_rate="1000/minute"
        ... )
    """

    def __init__(
        self,
        app: Any,
        limiter: Any,
        default_rate: str = "1000/minute",
        exclude_paths: Optional[list[str]] = None,
        trust_proxy_headers: bool = False,
    ):
        """
        Initialize middleware.

        Args:
            app: ASGI application
            limiter: RateLimiter instance
            default_rate: Default rate limit for all endpoints
            exclude_paths: List of paths to exclude from rate limiting
            trust_proxy_headers: If True, trust X-Forwarded-For headers.
                               Only enable if behind a trusted reverse proxy.
        """
        self.app = app
        self.limiter = limiter
        self.default_rate = default_rate
        self.exclude_paths = exclude_paths or []
        self.trust_proxy_headers = trust_proxy_headers

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        """
        ASGI middleware implementation.

        Args:
            scope: ASGI scope
            receive: ASGI receive callable
            send: ASGI send callable
        """
        # Only process HTTP requests
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Check if path is excluded
        path = scope.get("path", "")
        if any(path.startswith(excluded) for excluded in self.exclude_paths):
            await self.app(scope, receive, send)
            return

        # Create a simple request-like object for rate limiting
        class SimpleRequest:
            def __init__(self, scope: dict[str, Any]) -> None:
                self.client = type(
                    "Client", (), {"host": scope.get("client", ("unknown", None))[0]}
                )()
                # ASGI headers are List[Tuple[bytes, bytes]], convert to str keys/values
                raw_headers = scope.get("headers", [])
                self.headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in raw_headers}
                self.path = scope.get("path", "")

        request: Any = SimpleRequest(scope)

        # Check rate limit
        try:
            await self.limiter.check(
                key=_get_default_key(request, trust_proxy_headers=self.trust_proxy_headers),
                rate=self.default_rate,
            )
        except RateLimitExceeded as e:
            # Send 429 response
            await send(
                {
                    "type": "http.response.start",
                    "status": 429,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"retry-after", str(e.retry_after).encode()),
                        (b"x-ratelimit-limit", self.default_rate.encode()),
                        (b"x-ratelimit-remaining", str(e.remaining).encode()),
                    ],
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": f'{{"error": "Rate limit exceeded", "retry_after": {e.retry_after}}}'.encode(),  # noqa: E501
                }
            )
            return

        # Continue with the application
        await self.app(scope, receive, send)
