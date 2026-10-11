# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased (planned 0.5.0)

### Behavior changes

- Fixed windows now charge only admitted requests, matching token bucket and sliding window accounting. Rejected requests no longer increment usage or consume the capacity that a smaller request could use. Existing counters inflated by older rejected attempts remain until their window expires or they are explicitly reset.
- Token buckets preserve fractional refill progress across checks, including denied polling and partial consumption. Recovery no longer depends on polling frequency. Full buckets discard idle fractional credit before consumption.
- Token bucket hashes gain `refill_units`, recording whole scaled units credited since their refill origin. `RateLimiter` now uses `:bucket:v2` keys, automatically isolating the new schema from old writers and rollback. Upgrade starts fresh quotas; during rolling overlap, old and new workers enforce independent quotas and combined traffic can exceed a single quota. Rollback resumes legacy quota state if retained. Strict quota continuity requires a coordinated cutover. Direct backend callers must version their supplied keys themselves. Explicit token resets include recognized legacy and v2 keys.
- Denials now report actual remaining capacity, rounded down to whole unit-cost requests, across all algorithms and HTTP headers. A smaller request may still fit. Sliding `get_usage()` rounds remaining capacity down before converting to display units, so displayed usage and remaining need not sum to the limit.
- Sliding window retry hints now follow the whole-second, permille-rounded two-bucket estimate through rollover, rather than assuming all current usage disappears at the next boundary. For costs within capacity, denied token/sliding `reset_at` is a conservative retry timestamp for that cost, not full-quota recovery. Allowed reset metadata is unchanged. Retry hints assume no intervening admissions and retained Redis state; they are not reservations or exact rolling-window guarantees.
- Valid per-request costs above policy capacity are permanent denials across all algorithms: `CheckResult` and `RateLimitExceeded` report `retry_after=None` and `reset_at=None`. HTTP integrations return 422 with no retry/reset headers; temporary denials still return 429. Custom exception handlers must use `exc.status_code` and omit `Retry-After` when `exc.retry_after` is `None`.
- Rates are limited to 1–9,007,199,254 requests per supported period to keep scaled/weighted arithmetic within Lua's exact integer range. Invalid policies now raise `RateLimitConfigError` before Redis access in both checks and usage snapshots, and at decorator creation. `parse_rate()` continues to raise `ValueError`.
- Lua/backend inputs now require integer scaled capacities of 1–9,007,199,254,000 and integer windows of 1–86,400 seconds. Token refill rates must be finite and between capacity/86,400 and 9,007,199,254,000 scaled units/second (full refill within a day). Independent custom refill rates raise `BackendError` if cumulative accrual exceeds the exact numeric range. Backend permanent denials use `retry_after=-1` and `reset_at=None`.
- Backend decision failures remain fail-closed by default: manual checks raise `BackendError`, while both HTTP integrations now return 503 without quota or retry headers instead of an unhandled 500. Confirmed quota denials remain 429/422.
- Opt-in `fail_open=True` bypasses feasible request checks on `BackendError`, with a warning and mandatory metrics. It automatically enables Prometheus collection and requires the metrics extra. Bypassed `CheckResult` values have `allowed=True` and `remaining=None`, `retry_after=None`, `reset_at=None`; integrations omit quota headers. Callers using remaining capacity must handle unknown metadata. Configuration/callback failures, cancellation, and over-capacity costs are never bypassed. Administrative APIs and eager connection/context-manager entry retain their existing failure behavior.

### Added

- Decorator `cost` accepts a static positive integer as well as a per-request callback. Invalid static costs, including costs above policy capacity, raise `RateLimitConfigError` at decorator creation; callbacks are evaluated only per request.
- `moderato_fail_open_total{algorithm}` records bypasses, also exposed as `moderato_checks_total{algorithm,result="bypassed"}` rather than confirmed admissions. Custom metrics namespaces continue to apply.

### Fixed

- Token bucket recovery timestamps and retry delays account for fractional progress and the actual depletion time of a full bucket.
- Rate-limit headers preserve a denial exception's remaining capacity instead of replacing it with zero.
- Permanent denial exceptions discard supplied reset timestamps, and ASGI denial responses omit reset headers when no retry is possible.
- Oversized fixed-window requests repair a missing expiry on existing counters without changing usage, creating cold counters, or extending valid expiries.
- Public token policies rebase whole-window refill progress without losing fractional credit, avoiding unbounded credited-unit counters in continuously busy buckets. Quotient/remainder arithmetic and decimal integer serialization prevent high-capacity refill rounding and scientific-notation usage parsing failures.
- Public token retry/full-refill deadlines use the same integer credit calculation as admission, avoiding a spurious extra millisecond from floating-point division/ceiling. Slow custom backend refill rates now retain state for twice the longer of the configured window or full-refill duration, plus 60 seconds, rather than expiring before full recovery.

## v0.4.0 (2026-10-07)

### Changed

- **BREAKING:** `check_with_info()` now always returns a `CheckResult` (with `allowed=False` on denial) instead of raising `RateLimitExceeded`. Use `check()` when you want the exception; both share a single Redis decision.
- **BREAKING:** Key, tenant, and cost callback exceptions no longer fall back silently to IP/default-tenant/cost=1. They now raise `RateLimitCallbackError` (HTTP 503) before the endpoint runs. `RateLimitHeadersMiddleware` maps it to a 503 JSON response.
- Decorated synchronous endpoints now run in a worker thread via `anyio.to_thread.run_sync` instead of blocking the event loop; `anyio` is a new main dependency.
- `X-RateLimit-Reset` is now derived from Redis/Lua (`reset_at`) instead of the application clock, and is omitted when unknown. `RateLimitExceeded` and `CheckResult` gained `reset_at`. For token bucket, `reset_at` is the time the bucket is fully refilled.
- Removed the unused `inject_rate_limit_headers` helper.
- **BREAKING:** `RedisBackend.check_fixed_window()` now takes a key *prefix* ending in `:` instead of a full key. The window start is derived from Redis time inside the Lua script, which also fixes a race where a window ending between the client clock read and the script call admitted an uncounted request. Callers passing the old positional `window_end` argument positionally are still accepted (it is ignored, but deprecated); `cost` is now keyword-only. `RateLimiter` users are unaffected.
- **BREAKING:** Token bucket and sliding window scripts now read Redis `TIME` themselves, cutting their checks from two Redis round trips (`TIME`, then the script) to one; all three algorithms now make one round trip per check. `RedisBackend.check_token_bucket()` no longer takes `current_time_ms`, and `RedisBackend.check_sliding_window()` takes a single key prefix ending in `:` instead of `current_key`/`previous_key`/`current_time_ms`; the script derives both window keys (`<prefix><window_start>`, the same layout as before). `cost` is keyword-only on both, so old positional calls fail with `TypeError` instead of being misread. On Redis Cluster, the sliding window prefix needs a per-tenant hash tag, as with fixed window. `RateLimiter` users are unaffected.

### Fixed

- Fixed window: a request arriving as a window ended could be admitted uncounted (the counter key was deleted by an already-past `EXPIREAT`). Window selection now happens atomically inside the Lua script from Redis time.
- Token bucket: a denied check's `reset_at` (and `Retry-After`) was floored to the whole second, so it could report up to one second earlier than the actual refill moment. It is now computed from the millisecond check time.
- Flaky `test_concurrent_multi_tenant` now gates on the Redis clock so it cannot straddle a window boundary on any runner.

- `trust_proxy_headers=True` now takes precedence over the direct client address, which is the proxy itself behind a reverse proxy; previously all clients shared the proxy's bucket.
- Flaky `test_sustained_high_load` now uses an hour window so the allowed total is deterministic on any runner.

## v0.3.0 (2026-09-18)

### Changed

- **BREAKING:** Renamed the package from `fastlimit` to `moderato` (`pip install moderato`, `import moderato`). The name `fastlimit` was already taken on PyPI by an unrelated package. In musical notation, moderato means "at a moderate pace" — the library enforces your API's tempo.
  - **Prometheus metric names change** with the default namespace: `fastlimit_checks_total` becomes `moderato_checks_total` (likewise for all other `fastlimit_*` metrics). Existing dashboards, alerts, and recording rules keyed on the old names will silently lose data. To keep the legacy names during migration, call `init_metrics(namespace="fastlimit")` before constructing any metrics-enabled `RateLimiter` (the limiter reuses an already-initialized collector; metric names cannot change after collectors are created).
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
