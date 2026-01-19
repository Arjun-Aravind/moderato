"""
Unit tests for utility functions.

These tests do not require Redis and focus on pure Python functionality.
They validate key generation, rate parsing, window alignment, and key hashing.
"""

import time

import pytest

from fastlimit.utils import (
    _url_encode_key_component,
    calculate_cost,
    generate_key,
    get_time_window,
    hash_key,
    parse_rate,
)


class TestParseRate:
    """Test suite for parse_rate() function."""

    def test_valid_rates_singular(self):
        """Test parsing valid rate strings with singular period."""
        assert parse_rate("10/second") == (10, 1)
        assert parse_rate("100/minute") == (100, 60)
        assert parse_rate("1000/hour") == (1000, 3600)
        assert parse_rate("10000/day") == (10000, 86400)

    def test_valid_rates_plural(self):
        """Test parsing valid rate strings with plural period."""
        assert parse_rate("5/seconds") == (5, 1)
        assert parse_rate("100/minutes") == (100, 60)
        assert parse_rate("500/hours") == (500, 3600)
        assert parse_rate("1000/days") == (1000, 86400)

    def test_case_insensitivity(self):
        """Test that rate parsing is case insensitive."""
        assert parse_rate("100/MINUTE") == (100, 60)
        assert parse_rate("100/Minute") == (100, 60)
        assert parse_rate("100/SECOND") == (100, 1)

    def test_whitespace_handling(self):
        """Test that whitespace is handled correctly."""
        assert parse_rate("  100/minute  ") == (100, 60)
        assert parse_rate("100/minute") == (100, 60)

    def test_large_numbers(self):
        """Test parsing very large rate values."""
        assert parse_rate("1000000/hour") == (1000000, 3600)
        assert parse_rate("999999999/day") == (999999999, 86400)

    def test_invalid_zero_rate(self):
        """Test that zero rate is parsed but should be handled by caller."""
        # parse_rate accepts 0, validation happens elsewhere
        assert parse_rate("0/minute") == (0, 60)

    def test_invalid_format_missing_slash(self):
        """Test invalid format without slash."""
        with pytest.raises(ValueError) as exc_info:
            parse_rate("100minute")
        assert "Invalid rate string" in str(exc_info.value)

    def test_invalid_format_missing_period(self):
        """Test invalid format with missing period."""
        with pytest.raises(ValueError) as exc_info:
            parse_rate("100/")
        assert "Invalid rate string" in str(exc_info.value)

    def test_invalid_format_missing_number(self):
        """Test invalid format with missing number."""
        with pytest.raises(ValueError) as exc_info:
            parse_rate("/minute")
        assert "Invalid rate string" in str(exc_info.value)

    def test_invalid_period(self):
        """Test invalid period type."""
        with pytest.raises(ValueError) as exc_info:
            parse_rate("100/week")
        assert "Invalid rate string" in str(exc_info.value)

    def test_invalid_negative_rate(self):
        """Test that negative rate raises an error."""
        with pytest.raises(ValueError) as exc_info:
            parse_rate("-1/minute")
        assert "Invalid rate string" in str(exc_info.value)

    def test_invalid_non_numeric(self):
        """Test that non-numeric rate raises an error."""
        with pytest.raises(ValueError) as exc_info:
            parse_rate("abc/minute")
        assert "Invalid rate string" in str(exc_info.value)


class TestUrlEncodeKeyComponent:
    """Test suite for _url_encode_key_component() function."""

    def test_simple_string(self):
        """Test that simple strings are unchanged."""
        assert _url_encode_key_component("simple") == "simple"
        assert _url_encode_key_component("user123") == "user123"

    def test_colon_encoded(self):
        """Test that colons are encoded (prevents key delimiter collision)."""
        assert _url_encode_key_component("user:123") == "user%3A123"
        assert _url_encode_key_component("a:b:c") == "a%3Ab%3Ac"

    def test_space_encoded(self):
        """Test that spaces are encoded."""
        assert _url_encode_key_component("user 123") == "user%20123"

    def test_special_chars_encoded(self):
        """Test that special Redis pattern chars are encoded."""
        assert _url_encode_key_component("user[1]") == "user%5B1%5D"
        assert _url_encode_key_component("user{1}") == "user%7B1%7D"
        assert _url_encode_key_component("user*") == "user%2A"
        assert _url_encode_key_component("user?") == "user%3F"

    def test_safe_chars_unchanged(self):
        """Test that safe characters are not encoded."""
        assert _url_encode_key_component("user-name") == "user-name"
        assert _url_encode_key_component("user_name") == "user_name"
        assert _url_encode_key_component("user.name") == "user.name"
        assert _url_encode_key_component("user~name") == "user~name"

    def test_unicode_handling(self):
        """Test that unicode characters are encoded."""
        encoded = _url_encode_key_component("用户123")
        assert "用户" not in encoded  # Unicode should be encoded
        assert "123" in encoded  # ASCII digits unchanged


class TestGenerateKey:
    """Test suite for generate_key() function."""

    def test_basic_key_generation(self):
        """Test basic key generation."""
        key = generate_key("ratelimit", "user123", "default", "1700000100")
        assert key.startswith("ratelimit:")
        assert "user123" in key
        assert "default" in key
        assert "1700000100" in key

    def test_no_key_collision_colon_vs_underscore(self):
        """Test that 'user:123' and 'user_123' produce different keys (NEW-C9 fix)."""
        key1 = generate_key("ratelimit", "user:123", "default", "1700000100")
        key2 = generate_key("ratelimit", "user_123", "default", "1700000100")
        assert key1 != key2, "Keys with ':' and '_' should not collide"

    def test_no_key_collision_special_chars(self):
        """Test that different special characters produce different keys."""
        key1 = generate_key("ratelimit", "a:b", "default", "1000")
        key2 = generate_key("ratelimit", "a_b", "default", "1000")
        key3 = generate_key("ratelimit", "a-b", "default", "1000")
        key4 = generate_key("ratelimit", "a.b", "default", "1000")

        # All should be different
        keys = [key1, key2, key3, key4]
        assert len(set(keys)) == 4, "All keys should be unique"

    def test_url_encoding_applied(self):
        """Test that URL encoding is applied to identifier."""
        key = generate_key("ratelimit", "user:123:session", "premium", "1000")
        # Colon should be encoded as %3A
        assert "%3A" in key or "user%3A123%3Asession" in key

    def test_special_characters_in_tenant(self):
        """Test special characters in tenant type."""
        key = generate_key("ratelimit", "user", "tier:1", "1000")
        # Should not crash and should encode the colon
        assert key is not None
        assert len(key) > 0

    def test_unicode_identifier(self):
        """Test unicode characters in identifier."""
        key = generate_key("ratelimit", "用户123", "default", "1000")
        assert key is not None
        assert "ratelimit" in key

    def test_email_identifier(self):
        """Test email address as identifier."""
        key = generate_key("ratelimit", "user@example.com", "default", "1000")
        assert key is not None
        # @ should be encoded
        assert "%40" in key

    def test_path_identifier(self):
        """Test path-like identifier."""
        key = generate_key("ratelimit", "/api/v1/users", "default", "1000")
        assert key is not None
        # Slashes should be encoded
        assert "%2F" in key

    def test_long_key_is_hashed(self):
        """Test that very long keys are hashed."""
        long_id = "x" * 500
        key = generate_key("ratelimit", long_id, "default", "1000")
        # Key should be shorter than the original would be
        assert len(key) < len(long_id) + 50


class TestGetTimeWindow:
    """Test suite for get_time_window() function."""

    def test_epoch_alignment_second(self):
        """Test epoch alignment for 1-second window."""
        # Any time in the same second should give same window
        window1 = get_time_window(1, 1700000100)
        window2 = get_time_window(1, 1700000100)
        assert window1 == window2

    def test_epoch_alignment_minute(self):
        """Test epoch alignment for 60-second (1 minute) window."""
        # All times within same minute should produce same window
        window1 = get_time_window(60, 1700000100)  # Some time in minute
        window2 = get_time_window(60, 1700000142)  # Same minute
        window3 = get_time_window(60, 1700000159)  # End of same minute
        assert window1 == window2 == window3

        # Next minute should be different
        window_next = get_time_window(60, 1700000160)
        assert window1 != window_next

    def test_epoch_alignment_hour(self):
        """Test epoch alignment for 3600-second (1 hour) window."""
        # 1699999200 is a hour boundary (divisible by 3600)
        # Anything from 1699999200 to 1700002799 is in the same hour window
        window1 = get_time_window(3600, 1699999200)  # At hour boundary
        window2 = get_time_window(3600, 1699999500)  # 5 min into hour
        window3 = get_time_window(3600, 1700002799)  # Just before hour ends
        assert window1 == window2 == window3

        # Next hour should be different
        window_next = get_time_window(3600, 1700002800)
        assert window1 != window_next

    def test_epoch_alignment_day(self):
        """Test epoch alignment for 86400-second (1 day) window."""
        # 1699920000 is a day boundary (divisible by 86400)
        # Same day window: 1699920000 to 1700006399
        window1 = get_time_window(86400, 1699920000)  # At day boundary
        window2 = get_time_window(86400, 1699950000)  # Same day
        assert window1 == window2

        # Different day
        window_diff = get_time_window(86400, 1700006400)  # Next day
        assert window1 != window_diff

    def test_boundary_at_window_start(self):
        """Test timestamp exactly at window start."""
        # 1700000100 is divisible by 100 (for testing)
        window = get_time_window(100, 1700000100)
        assert window == "1700000100"

    def test_boundary_at_window_end(self):
        """Test timestamp just before window end."""
        window = get_time_window(100, 1700000199)
        assert window == "1700000100"

    def test_uses_current_time_if_none(self):
        """Test that current time is used if timestamp is None."""
        window = get_time_window(60)
        current = int(time.time())
        expected_start = current - (current % 60)
        assert int(window) <= expected_start + 60  # Within current window

    def test_window_key_is_string(self):
        """Test that return value is a string."""
        window = get_time_window(60, 1700000100)
        assert isinstance(window, str)


class TestHashKey:
    """Test suite for hash_key() function."""

    def test_short_key_unchanged(self):
        """Test that short keys are not hashed."""
        short_key = "ratelimit:user123:default:2024"
        assert hash_key(short_key) == short_key

    def test_key_at_max_length_unchanged(self):
        """Test that key exactly at max length is unchanged."""
        key = "x" * 200
        assert hash_key(key, max_length=200) == key

    def test_long_key_is_hashed(self):
        """Test that long keys are hashed."""
        long_key = "ratelimit:" + "x" * 500
        hashed = hash_key(long_key)
        assert len(hashed) < len(long_key)
        assert len(hashed) <= 200  # Default max_length

    def test_hash_is_deterministic(self):
        """Test that hashing produces same result for same input."""
        long_key = "ratelimit:" + "x" * 500
        hashed1 = hash_key(long_key)
        hashed2 = hash_key(long_key)
        assert hashed1 == hashed2

    def test_different_keys_produce_different_hashes(self):
        """Test that different keys produce different hashes."""
        key1 = "ratelimit:" + "x" * 500
        key2 = "ratelimit:" + "y" * 500
        hashed1 = hash_key(key1)
        hashed2 = hash_key(key2)
        assert hashed1 != hashed2

    def test_prefix_preserved_in_hash(self):
        """Test that some prefix is preserved for debugging."""
        long_key = "ratelimit:user123:" + "x" * 500
        hashed = hash_key(long_key)
        # The hashed key should contain some of the original prefix
        assert "ratelimit" in hashed or "_" in hashed

    def test_custom_max_length(self):
        """Test custom max_length parameter."""
        long_key = "ratelimit:" + "x" * 200
        hashed = hash_key(long_key, max_length=100)
        assert len(hashed) <= 100


class TestCalculateCost:
    """Test suite for calculate_cost() function."""

    def test_basic_calculation(self):
        """Test basic cost calculation."""
        # 100 per minute = 100/60 = ~1.67 per second
        cost = calculate_cost(100, 60)
        assert abs(cost - 1.6666666666666667) < 0.001

    def test_per_second_rate(self):
        """Test per-second rate calculation."""
        cost = calculate_cost(10, 1)
        assert cost == 10.0

    def test_per_hour_rate(self):
        """Test per-hour rate calculation."""
        cost = calculate_cost(3600, 3600)
        assert cost == 1.0

    def test_per_day_rate(self):
        """Test per-day rate calculation."""
        cost = calculate_cost(86400, 86400)
        assert cost == 1.0

    def test_zero_window_raises_error(self):
        """Test that zero window raises error."""
        with pytest.raises(ValueError) as exc_info:
            calculate_cost(100, 0)
        assert "positive" in str(exc_info.value).lower()

    def test_negative_window_raises_error(self):
        """Test that negative window raises error."""
        with pytest.raises(ValueError) as exc_info:
            calculate_cost(100, -1)
        assert "positive" in str(exc_info.value).lower()


class TestKeyCollisionPrevention:
    """
    Integration tests for key collision prevention (NEW-C9 fix).

    These tests verify that the URL encoding approach prevents
    collisions that could occur with simple character replacement.
    """

    def test_comprehensive_collision_prevention(self):
        """Test that many similar-looking keys don't collide."""
        test_cases = [
            ("user:123", "user_123"),
            ("user:123", "user-123"),
            ("user:123", "user.123"),
            ("api/users", "api_users"),
            ("api/users", "api:users"),
            ("key with space", "key_with_space"),
            ("[user]", "{user}"),
            ("user@host", "user_host"),
        ]

        for id1, id2 in test_cases:
            key1 = generate_key("ratelimit", id1, "default", "1000")
            key2 = generate_key("ratelimit", id2, "default", "1000")
            assert key1 != key2, f"Keys for '{id1}' and '{id2}' should not collide"

    def test_tenant_collision_prevention(self):
        """Test that tenant types with similar characters don't collide."""
        key1 = generate_key("ratelimit", "user", "tier:1", "1000")
        key2 = generate_key("ratelimit", "user", "tier_1", "1000")
        assert key1 != key2, "Tenant types with ':' and '_' should not collide"
