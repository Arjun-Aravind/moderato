"""
Prometheus metrics support for rate limiting observability.

This module provides comprehensive metrics collection for monitoring
rate limiter performance, usage patterns, and system health.
"""

import logging
import time
from collections.abc import Generator
from contextlib import contextmanager
from functools import wraps
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# Check if prometheus_client is available
try:
    from prometheus_client import Counter, Gauge, Histogram

    PROMETHEUS_AVAILABLE = True
except ImportError:
    PROMETHEUS_AVAILABLE = False
    logger.warning(
        "prometheus_client not installed. Metrics will be disabled. "
        "Install with: pip install prometheus-client"
    )


class RateLimitMetrics:
    """
    Prometheus metrics collector for rate limiting.

    This class provides comprehensive metrics for monitoring:
    - Rate limit check operations
    - Rate limit violations
    - Backend performance
    - Error rates
    - System health

    Metrics follow Prometheus naming conventions and best practices.

    Usage:
        from fastlimit import RateLimiter
        from fastlimit.metrics import RateLimitMetrics

        limiter = RateLimiter(
            redis_url="redis://localhost:6379",
            enable_metrics=True
        )

        # Metrics are automatically collected
        # Expose metrics endpoint in your app:

        from fastapi import FastAPI, Response
        from prometheus_client import generate_latest, CONTENT_TYPE_LATEST

        app = FastAPI()

        @app.get("/metrics")
        def metrics():
            return Response(
                content=generate_latest(),
                media_type=CONTENT_TYPE_LATEST
            )
    """

    def __init__(self, namespace: str = "fastlimit", enabled: bool = True):
        """
        Initialize metrics collector.

        Args:
            namespace: Prometheus namespace for metrics
            enabled: Whether metrics collection is enabled
        """
        self.namespace = namespace
        self.enabled = enabled and PROMETHEUS_AVAILABLE

        if not self.enabled:
            if not PROMETHEUS_AVAILABLE:
                logger.info("Metrics disabled: prometheus_client not installed")
            else:
                logger.info("Metrics disabled by configuration")
            return

        # Initialize metrics
        self._init_metrics()
        logger.info(f"Prometheus metrics initialized with namespace '{namespace}'")

    def _init_metrics(self) -> None:
        """Initialize Prometheus metrics."""
        # Rate limit check metrics
        self.checks_total = Counter(
            f"{self.namespace}_checks_total",
            "Total number of rate limit checks performed",
            ["algorithm", "result"],  # result: allowed, denied
        )

        self.checks_duration = Histogram(
            f"{self.namespace}_check_duration_seconds",
            "Time spent performing rate limit checks",
            ["algorithm"],
            buckets=(0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0),
        )

        # Rate limit violations
        self.limit_exceeded_total = Counter(
            f"{self.namespace}_limit_exceeded_total",
            "Total number of times rate limits were exceeded",
            ["algorithm", "tenant_type"],
        )

        # Backend metrics
        self.backend_operations_total = Counter(
            f"{self.namespace}_backend_operations_total",
            "Total number of backend operations",
            ["operation", "status"],  # status: success, error
        )

        self.backend_operation_duration = Histogram(
            f"{self.namespace}_backend_operation_duration_seconds",
            "Time spent on backend operations",
            ["operation"],
            buckets=(0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0),
        )

        # Redis-specific metrics
        self.redis_operations_total = Counter(
            f"{self.namespace}_redis_operations_total",
            "Total number of Redis operations",
            ["command", "status"],
        )

        self.redis_connection_errors = Counter(
            f"{self.namespace}_redis_connection_errors_total",
            "Total number of Redis connection errors",
        )

        self.redis_script_executions = Counter(
            f"{self.namespace}_redis_script_executions_total",
            "Total number of Lua script executions",
            ["script_name", "execution_type"],  # execution_type: evalsha, eval
        )

        # Usage metrics
        self.current_usage = Gauge(
            f"{self.namespace}_current_usage",
            "Current usage count for rate limits",
            ["key", "algorithm"],
        )

        self.limit_value = Gauge(
            f"{self.namespace}_limit_value",
            "Configured rate limit value",
            ["key", "algorithm"],
        )

        # System health metrics
        self.active_connections = Gauge(
            f"{self.namespace}_active_redis_connections",
            "Number of active Redis connections",
        )

        logger.debug(f"Initialized {len(self.__dict__)} Prometheus metrics")

    @contextmanager
    def track_check_duration(self, algorithm: str = "fixed_window") -> Generator[None, None, None]:
        """
        Context manager to track rate limit check duration.

        Args:
            algorithm: Algorithm being used

        Usage:
            with metrics.track_check_duration("fixed_window"):
                await limiter.check(key="user:123", rate="100/minute")
        """
        if not self.enabled:
            yield
            return

        start_time = time.perf_counter()
        try:
            yield
        finally:
            duration = time.perf_counter() - start_time
            self.checks_duration.labels(algorithm=algorithm).observe(duration)

    @contextmanager
    def track_backend_operation(self, operation: str) -> Generator[None, None, None]:
        """
        Context manager to track backend operation duration and status.

        Args:
            operation: Operation name (e.g., "check_fixed_window", "reset")

        Usage:
            with metrics.track_backend_operation("check_fixed_window"):
                result = await backend.check_fixed_window(...)
        """
        if not self.enabled:
            yield
            return

        start_time = time.perf_counter()
        status = "success"
        try:
            yield
        except Exception:
            status = "error"
            raise
        finally:
            duration = time.perf_counter() - start_time
            self.backend_operations_total.labels(operation=operation, status=status).inc()
            self.backend_operation_duration.labels(operation=operation).observe(duration)

    def record_check(self, algorithm: str, allowed: bool) -> None:
        """
        Record a rate limit check.

        Args:
            algorithm: Algorithm used
            allowed: Whether the request was allowed
        """
        if not self.enabled:
            return

        result = "allowed" if allowed else "denied"
        self.checks_total.labels(algorithm=algorithm, result=result).inc()

    def record_limit_exceeded(self, algorithm: str, tenant_type: str = "default") -> None:
        """
        Record a rate limit violation.

        Args:
            algorithm: Algorithm that triggered the violation
            tenant_type: Type of tenant that exceeded the limit
        """
        if not self.enabled:
            return

        self.limit_exceeded_total.labels(algorithm=algorithm, tenant_type=tenant_type).inc()

    def record_redis_operation(self, command: str, success: bool) -> None:
        """
        Record a Redis operation.

        Args:
            command: Redis command executed
            success: Whether the operation succeeded
        """
        if not self.enabled:
            return

        status = "success" if success else "error"
        self.redis_operations_total.labels(command=command, status=status).inc()

    def record_redis_error(self) -> None:
        """Record a Redis connection error."""
        if not self.enabled:
            return

        self.redis_connection_errors.inc()

    def record_script_execution(self, script_name: str, used_cache: bool) -> None:
        """
        Record Lua script execution.

        Args:
            script_name: Name of the script
            used_cache: Whether EVALSHA was used (cached) vs EVAL
        """
        if not self.enabled:
            return

        execution_type = "evalsha" if used_cache else "eval"
        self.redis_script_executions.labels(
            script_name=script_name, execution_type=execution_type
        ).inc()

    def update_usage_gauge(self, key: str, algorithm: str, current: int, limit: int) -> None:
        """
        Update current usage gauges.

        Args:
            key: Rate limit key
            algorithm: Algorithm in use
            current: Current usage count
            limit: Maximum allowed
        """
        if not self.enabled:
            return

        # Truncate key if too long for label
        safe_key = key[:50] if len(key) > 50 else key

        self.current_usage.labels(key=safe_key, algorithm=algorithm).set(current)
        self.limit_value.labels(key=safe_key, algorithm=algorithm).set(limit)

    def set_active_connections(self, count: int) -> None:
        """
        Update active Redis connections count.

        Args:
            count: Number of active connections
        """
        if not self.enabled:
            return

        self.active_connections.set(count)

    def get_metrics_dict(self) -> dict[str, Any]:
        """
        Get current metrics as a dictionary.

        This is useful for debugging or custom monitoring systems.

        Returns:
            Dictionary of metric names and values
        """
        if not self.enabled:
            return {"enabled": False}

        # Note: This is a simplified view
        # For full metrics, use the Prometheus endpoint
        return {
            "enabled": True,
            "prometheus_available": PROMETHEUS_AVAILABLE,
            "namespace": self.namespace,
        }


def metrics_decorator(
    metrics: Optional[RateLimitMetrics], operation: str
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """
    Decorator to automatically track metrics for a function.

    Args:
        metrics: Metrics collector instance
        operation: Operation name

    Usage:
        @metrics_decorator(metrics, "check_fixed_window")
        async def check_fixed_window(self, key, max_requests, window_seconds):
            ...
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        if not metrics or not metrics.enabled:
            return func

        @wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            with metrics.track_backend_operation(operation):
                return await func(*args, **kwargs)

        @wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            with metrics.track_backend_operation(operation):
                return func(*args, **kwargs)

        # Return appropriate wrapper based on function type
        import inspect

        if inspect.iscoroutinefunction(func):
            return async_wrapper
        else:
            return sync_wrapper

    return decorator


# Global metrics instance (can be configured via RateLimiter)
_global_metrics: Optional[RateLimitMetrics] = None


def get_metrics() -> Optional[RateLimitMetrics]:
    """
    Get the global metrics instance.

    Returns:
        Global metrics collector or None if not initialized
    """
    return _global_metrics


def init_metrics(namespace: str = "fastlimit", enabled: bool = True) -> RateLimitMetrics:
    """
    Initialize the global metrics collector.

    Args:
        namespace: Prometheus namespace
        enabled: Whether to enable metrics

    Returns:
        Initialized metrics collector
    """
    global _global_metrics
    _global_metrics = RateLimitMetrics(namespace=namespace, enabled=enabled)
    return _global_metrics
