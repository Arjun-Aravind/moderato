# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## v0.3.0 (2026-09-18)

### Changed

- **BREAKING:** Renamed the package from `fastlimit` to `moderato` (`pip install moderato`, `import moderato`). The name `fastlimit` was already taken on PyPI by an unrelated package. In musical notation, moderato means "at a moderate pace" — the library enforces your API's tempo.
  - **Prometheus metric names change** with the default namespace: `fastlimit_checks_total` becomes `moderato_checks_total` (likewise for all other `fastlimit_*` metrics). Existing dashboards, alerts, and recording rules keyed on the old names will silently lose data. To keep the old metric names while you migrate dashboards, pass the namespace explicitly: `init_metrics(namespace="fastlimit")` or `RateLimitMetrics(namespace="fastlimit")`.
- Corrected package author metadata to `Arjun Aravind <arjunaravind748@gmail.com>`.

## [0.1.0] - 2025-01-18

### Added

- Initial release of fastlimit
- Core rate limiting with Redis backend
- Token bucket and sliding window algorithms
- FastAPI integration with `@limiter.limit()` decorator
- Async-first design with full async/await support
- Rate limit headers middleware (`RateLimitHeadersMiddleware`)
- Configurable rate limit patterns (e.g., "100/minute", "1000/hour")
- Custom key extraction for rate limiting
- Comprehensive exception handling (`RateLimitExceeded`, `BackendError`)
- Optional Prometheus metrics integration
- Docker Compose setup for development
- Full test suite with pytest

## v0.2.0 (2026-01-24)

### Feat

- **tooling**: add commitizen for semantic versioning and changelog

### Fix

- **api**: make reset() and get_usage() algorithm-aware
- **sliding-window**: correct inverted get_usage() formula in Python
- **sliding-window**: use integer-only arithmetic and accurate retry_after
- **token-bucket**: use millisecond precision to prevent crash on low rates
- **fixed-window**: use epoch-aligned boundaries and EXPIREAT
- **distributed**: add Redis TIME methods for consistent window boundaries
- **redis**: import exceptions from redis.exceptions module

### Perf

- **api**: add CheckResult and check_with_info() to eliminate double Redis calls

## v0.1.0 (2025-12-06)
