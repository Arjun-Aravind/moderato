# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## v0.3.0 (2026-09-18)

### Changed

- **BREAKING:** Renamed the package from `fastlimit` to `moderato` (`pip install moderato`, `import moderato`). The name `fastlimit` was already taken on PyPI by an unrelated package. In musical notation, moderato means "at a moderate pace" — the library enforces your API's tempo.
  - **Prometheus metric names change** with the default namespace: `fastlimit_checks_total` becomes `moderato_checks_total` (likewise for all other `fastlimit_*` metrics). Existing dashboards, alerts, and recording rules keyed on the old names will silently lose data and must be updated: the legacy metric names cannot be kept, because namespaces must be valid Prometheus name prefixes (letters, digits, underscore — `fastlimit` contains a hyphen). Initialize metrics before constructing `RateLimiter` instances when using a custom namespace.
- Corrected package author metadata to `Arjun Aravind <arjunaravind748@gmail.com>`.
- **BREAKING:** `moderato.utils.generate_key()` now takes separate `policy`, `time_window`, and `scope` arguments. This is required for the cross-policy and cross-route isolation fixes below: keys that omit these components would collide across different rate limits and routes. Callers using `generate_key` directly must pass the policy (e.g. `p100x60`); `RateLimiter` users are unaffected.
- The `moderato[fastapi]` extra now installs FastAPI itself (plus Starlette), matching the README's documented install path.
- **BREAKING:** `cost` must be a positive integer. Previously `0` performed a no-op check, booleans and floats were accepted, and non-numeric values raised a raw `TypeError`.

### Added

- **Route and scope isolation**: decorated endpoints are limited per method and route (`GET:/api/users`); pass `scope="name"` to share one bucket across routes. Manual `check()` calls default to the `"global"` scope.
- **Prometheus metrics are wired up**: `RateLimiter(enable_metrics=True)` now records checks, denials, per-algorithm latency, and backend operations. Requires the `moderato[metrics]` extra.
- **PEP 561 support**: the package now ships `py.typed`, so type checkers see moderato's type hints in consuming projects.
- **Optional extras**: `moderato[fastapi]` (Starlette middleware), `moderato[metrics]` (Prometheus), and `moderato[all]`. The core `RateLimiter` now imports and works without Starlette or FastAPI installed.
- **CI wheel smoke test**: every commit builds the wheel, installs it into a clean environment without extras, and verifies both the core import and the full `[all]` surface.

### Fixed

- **Cross-policy state corruption**: Redis keys now include a policy component (`p{limit}x{window}`). Previously, the same identity under two different rate policies with the same window shared one bucket (e.g., an exhausted 5/minute policy silently reduced a 100/minute policy to ~90 requests).
- **Negative-cost limit bypass**: `check(..., cost=-5)` restored capacity instead of consuming it. Costs are now validated as positive integers in Python and defensively inside every Lua script.
- **Invalid rates**: `"0/minute"` is now rejected at parse time (a 0-request limit denied everything but still wrote state).
- **Long identifiers can be reset**: keys are hashed per component, so `reset()` scans still match identifiers of any length. Previously, identifiers long enough to trigger whole-key hashing could not be reset.
- **`reset()` for a specific tenant** now scans rather than reconstructing keys, so it correctly clears state from every policy and window size for that tenant.
- **Removed unused `pydantic-settings` dependency**; `typing_extensions` is now declared directly (it was imported but never declared).
- **Broken doc links**: README/CONTRIBUTING referenced deleted `ALGORITHMS.md` and `ARCHITECTURE.md`.

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
