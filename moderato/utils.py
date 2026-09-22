"""
Utility functions for rate limiting operations.
"""

import hashlib
import re
from typing import Optional

# Keys longer than this are replaced by a truncated prefix plus a SHA-256
# digest (see hash_key). The preserved prefix leaves room for the 64-char
# hex digest and a 1-char separator.
HASH_KEY_MAX_LENGTH = 200
HASH_KEY_PRESERVED_LEN = HASH_KEY_MAX_LENGTH - 64 - 1
KEY_COMPONENT_MAX_LENGTH = 100


def parse_rate(rate_string: str) -> tuple[int, int]:
    """
    Parse rate string into requests and window seconds.

    Supports formats like:
    - "100/second" - 100 requests per second
    - "1000/minute" - 1000 requests per minute
    - "10000/hour" - 10000 requests per hour
    - "100000/day" - 100000 requests per day

    Args:
        rate_string: Rate limit string in format "number/period"

    Returns:
        Tuple of (requests, window_seconds)

    Raises:
        ValueError: If rate string is invalid

    Examples:
        >>> parse_rate("100/minute")
        (100, 60)
        >>> parse_rate("1000/hour")
        (1000, 3600)
    """
    # Normalize input
    rate_string = rate_string.strip().lower()

    # Pattern to match rate format
    pattern = r"^(\d+)/(second|seconds|minute|minutes|hour|hours|day|days)$"
    match = re.match(pattern, rate_string)

    if not match:
        raise ValueError(
            f"Invalid rate string: '{rate_string}'. "
            f"Expected format: 'number/period' (e.g., '100/minute')"
        )

    requests = int(match.group(1))
    period = match.group(2)

    if requests < 1:
        raise ValueError(
            f"Invalid rate string: '{rate_string}'. " f"The request limit must be at least 1."
        )

    # Normalize period to singular form
    period = period.rstrip("s")

    # Convert period to seconds
    period_seconds = {
        "second": 1,
        "minute": 60,
        "hour": 3600,
        "day": 86400,
    }

    if period not in period_seconds:
        raise ValueError(f"Invalid period: {period}")

    return requests, period_seconds[period]


def scan_literal_may_miss_hashed_keys(scan_literal_len: int) -> bool:
    """Whether a SCAN/MATCH pattern with this literal head can miss hashed keys.

    ``hash_key`` replaces keys longer than ``HASH_KEY_MAX_LENGTH`` with their
    first ``HASH_KEY_PRESERVED_LEN`` characters plus a SHA-256 digest. A
    pattern only matches such a hashed key if the caller-controlled literal
    part (everything before the wildcard) fits within the preserved prefix.

    Args:
        scan_literal_len: Length of the pattern's literal part, i.e. the
            number of leading key characters that must survive hashing
            (``prefix:identifier[:tenant]:`` including separators).

    Returns:
        True if keys hashed by ``hash_key`` may no longer match the pattern.
    """
    return scan_literal_len > HASH_KEY_PRESERVED_LEN


def generate_key(
    prefix: str,
    identifier: str,
    tenant_type: str,
    policy: str,
    time_window: str,
    scope: str = "global",
) -> str:
    """
    Generate Redis key for rate limiting.

    Creates a hierarchical key structure for efficient Redis operations
    and clear organization of rate limit data.

    Uses URL-safe encoding to prevent key collisions while keeping keys readable.
    This is important because simple character replacement (e.g., : -> _) can
    cause different identifiers to map to the same key.

    Args:
        prefix: Key prefix (e.g., "ratelimit")
        identifier: Unique identifier (e.g., IP address, user ID)
        tenant_type: Tenant type/tier (e.g., "free", "premium", "enterprise")
        policy: Policy component identifying the limit itself
            (e.g., "p100x60" for 100 requests per 60 seconds). Isolates
            buckets for different policies so the same identity under
            different rates never shares state.
        time_window: Time window identifier (e.g., "1700000100")
        scope: Namespace for independently limited resources

    Returns:
        Formatted Redis key

    Examples:
        >>> generate_key("ratelimit", "192.168.1.1", "free", "p100x60", "1700000100")
        'ratelimit:192.168.1.1:free:global:p100x60:1700000100'

        >>> generate_key("ratelimit", "user:123", "premium", "p50x60", "1700000100")
        'ratelimit:user%3A123:premium:global:p50x60:1700000100'
    """
    safe_id = normalize_key_component(identifier)
    safe_tenant = normalize_key_component(tenant_type)
    safe_scope = normalize_key_component(scope)
    return f"{prefix}:{safe_id}:{safe_tenant}:{safe_scope}:{policy}:{time_window}"


def normalize_key_component(value: str) -> str:
    """Encode a Redis key component, hashing it when it is unusually long."""
    encoded = _url_encode_key_component(value)
    if len(encoded) <= KEY_COMPONENT_MAX_LENGTH:
        return encoded
    return f"sha256={hashlib.sha256(encoded.encode()).hexdigest()}"


def _url_encode_key_component(value: str) -> str:
    """
    URL-encode a key component to prevent Redis key issues and collisions.

    Only encodes characters that would cause issues:
    - Colon (:) - used as key delimiter
    - Space ( ) - causes parsing issues
    - Special Redis pattern chars (* ? [ ] { })

    Args:
        value: The string to encode

    Returns:
        URL-safe encoded string

    Examples:
        >>> _url_encode_key_component("user:123")
        'user%3A123'

        >>> _url_encode_key_component("normal_key")
        'normal_key'
    """
    from urllib.parse import quote

    # Encode only problematic characters, keep alphanumeric and common safe chars
    # safe='...' means these characters will NOT be encoded
    return quote(value, safe="-_.~")


def get_time_window(window_seconds: int, timestamp: Optional[int] = None) -> str:
    """
    Generate time window key based on window size using epoch-aligned boundaries.

    Uses epoch-based alignment to ensure consistent window boundaries:
    - window_start = timestamp - (timestamp % window_seconds)

    This prevents the "boundary burst" problem where requests at the end
    of one window and start of another can exceed the intended rate.

    Args:
        window_seconds: Size of the time window in seconds
        timestamp: Unix timestamp (defaults to current time)

    Returns:
        Window start timestamp as string (for use in Redis key)

    Examples:
        >>> # For a 60-second window at timestamp 1700000142 (14:35:42)
        >>> get_time_window(60, 1700000142)
        '1700000100'  # Aligned to 14:35:00

        >>> # For a 3600-second (1 hour) window at timestamp 1700000142
        >>> get_time_window(3600, 1700000142)
        '1699999200'  # Aligned to 14:00:00
    """
    import time

    if timestamp is None:
        timestamp = int(time.time())

    # Align to window boundary using epoch
    window_start = timestamp - (timestamp % window_seconds)
    return str(window_start)


def hash_key(key: str, max_length: int = HASH_KEY_MAX_LENGTH) -> str:
    """
    Hash a key if it's too long for Redis.

    Redis keys can be up to 512MB, but very long keys impact performance.
    This function hashes keys that exceed a reasonable length.

    Args:
        key: The original key
        max_length: Maximum allowed key length before hashing

    Returns:
        Original key or hashed version if too long

    Examples:
        >>> short_key = "ratelimit:user123:free:2024"
        >>> hash_key(short_key) == short_key
        True

        >>> long_key = "ratelimit:" + "x" * 500
        >>> len(hash_key(long_key)) < len(long_key)
        True
    """
    if len(key) <= max_length:
        return key

    # Use SHA256 for consistent hashing
    key_hash = hashlib.sha256(key.encode()).hexdigest()

    # Preserve some prefix for debugging
    prefix_len = max_length - len(key_hash) - 1
    if prefix_len > 0:
        return f"{key[:prefix_len]}_{key_hash}"

    return key_hash


def calculate_cost(requests: int, window_seconds: int) -> float:
    """
    Calculate the "cost" or rate of requests.

    Useful for comparing different rate limits or calculating
    effective rates.

    Args:
        requests: Number of requests allowed
        window_seconds: Time window in seconds

    Returns:
        Requests per second rate

    Examples:
        >>> calculate_cost(100, 60)  # 100 per minute
        1.6666666666666667  # ~1.67 requests per second

        >>> calculate_cost(1000, 3600)  # 1000 per hour
        0.2777777777777778  # ~0.28 requests per second
    """
    if window_seconds <= 0:
        raise ValueError("Window seconds must be positive")

    return requests / window_seconds
