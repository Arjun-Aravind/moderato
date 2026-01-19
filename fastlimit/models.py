"""
Pydantic models for configuration and data structures.
"""

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


@dataclass
class CheckResult:
    """
    Result of a rate limit check.

    This dataclass contains all information about the rate limit check,
    including whether the request was allowed and usage statistics.

    Attributes:
        allowed: Whether the request was allowed
        limit: Maximum requests allowed in the window
        remaining: Requests remaining in the current window
        retry_after: Seconds until the rate limit resets (0 if allowed)
        window_seconds: Size of the rate limit window in seconds

    Example:
        >>> result = await limiter.check_with_info(key="user:123", rate="100/minute")
        >>> if result.allowed:
        >>>     print(f"Request allowed, {result.remaining} remaining")
    """

    allowed: bool
    limit: int
    remaining: int
    retry_after: int  # seconds (0 if allowed)
    window_seconds: int


class RateLimitConfig(BaseModel):
    """Configuration model for the rate limiter."""

    redis_url: str = Field(
        default="redis://localhost:6379",
        description="Redis connection URL (redis://[user:password@]host[:port][/db])",
    )
    key_prefix: str = Field(
        default="ratelimit",
        description="Prefix for all rate limit keys in Redis",
    )
    default_algorithm: Literal["fixed_window", "token_bucket", "sliding_window"] = Field(
        default="fixed_window",
        description="Default rate limiting algorithm to use",
    )
    enable_metrics: bool = Field(
        default=False,
        description="Enable Prometheus metrics collection (future feature)",
    )
    connection_timeout: int = Field(
        default=5,
        description="Redis connection timeout in seconds",
    )
    socket_timeout: int = Field(
        default=5,
        description="Redis socket timeout in seconds",
    )
    max_connections: int = Field(
        default=50,
        description="Maximum number of Redis connections in the pool",
    )

    @field_validator("default_algorithm")
    @classmethod
    def validate_algorithm(cls, v: str) -> str:
        """Validate that the algorithm is supported."""
        valid_algorithms = ["fixed_window", "token_bucket", "sliding_window"]
        if v not in valid_algorithms:
            raise ValueError(f"Algorithm must be one of {valid_algorithms}")
        return v

    @field_validator("redis_url")
    @classmethod
    def validate_redis_url(cls, v: str) -> str:
        """Basic validation of Redis URL format."""
        if not v.startswith(("redis://", "rediss://", "unix://")):
            raise ValueError("Redis URL must start with redis://, rediss://, or unix://")
        return v

    @field_validator("connection_timeout", "socket_timeout", "max_connections")
    @classmethod
    def validate_positive_int(cls, v: int) -> int:
        """Validate that timeout and connection values are positive."""
        if v <= 0:
            raise ValueError("Value must be positive")
        return v

    model_config = ConfigDict(
        validate_assignment=True,
        frozen=False,
    )
