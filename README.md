# Moderato

*In musical notation, **moderato** means "at a moderate pace." Moderato enforces your API's tempo.*

[![Python Version](https://img.shields.io/badge/python-3.9--3.13-blue)](https://www.python.org)
[![PyPI](https://img.shields.io/pypi/v/moderato)](https://pypi.org/project/moderato/)
[![CI](https://github.com/Arjun-Aravind/moderato/actions/workflows/ci.yml/badge.svg)](https://github.com/Arjun-Aravind/moderato/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Redis](https://img.shields.io/badge/redis-7%2B-red)](https://redis.io)
[![Code Style](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)

A Redis-backed rate limiting library for async Python applications.

[Features](#features) | [Quick Start](#quick-start) | [Algorithms](#algorithms) | [Documentation](#documentation) | [Examples](#examples)

---

## What is Moderato?

Moderato is a Redis-backed rate limiting library for async Python applications. It provides three algorithms, FastAPI integration, optional Prometheus metrics, cost-based limits, and tenant isolation.

**Use cases:**
- FastAPI applications requiring rate limiting
- Multi-tenant SaaS platforms with tier-based limits
- APIs that need Redis-backed limits across multiple application instances
- Services that need Prometheus counters and latency histograms
- Applications requiring atomic rate limit decisions

---

## Features

### Core Capabilities
- **Three Algorithms** - Fixed Window, Token Bucket & Sliding Window
- **Async-first design** - Built for FastAPI and modern async Python
- **Atomic decisions** - Redis Lua scripts keep checks and updates in one operation
- **Redis server time** - Consistent windows across application instances
- **Integer precision** - Uses integer math (x1000 multiplier) for accuracy

### Integrations and controls
- **Rate limit headers** - Standard headers on decorated FastAPI responses
- **Prometheus Metrics** - Optional counters and latency histograms
- **Multi-tenant support** - Isolated limits for different users/tiers/organizations
- **Decorator-based API** - Clean, declarative rate limiting
- **Cost-based limiting** - Weight expensive operations appropriately
- **Flexible configuration** - Customizable key extraction, algorithms, and costs

---

## Quick Start

### Installation

```bash
pip install moderato

# With FastAPI/Starlette middleware integration
pip install 'moderato[fastapi]'

# With metrics support (optional)
pip install 'moderato[metrics]'

# Everything (optional)
pip install 'moderato[all]'
```

### Basic Example

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from moderato import RateLimiter, RateLimitHeadersMiddleware

limiter = RateLimiter(redis_url="redis://localhost:6379")

@asynccontextmanager
async def lifespan(app: FastAPI):
    await limiter.connect()
    yield
    await limiter.close()

app = FastAPI(lifespan=lifespan)
app.add_middleware(RateLimitHeadersMiddleware)

@app.get("/api/users")
@limiter.limit("100/minute")
async def get_users(request: Request):
    return {"users": ["Alice", "Bob"]}
```

This gives you:
- Rate limiting (100 requests/minute per IP)
- Rate limit headers on this decorated endpoint
- Proper 429 responses when exceeded
- Redis-backed, distributed-ready

---

## Algorithms

Moderato provides three tested algorithms. Choose based on the traffic behavior you want:

All three algorithms charge only **admitted requests**. For example, under a
`10/minute` limit, after using 8 units, a cost-3 request is rejected without
consuming quota; a subsequent cost-2 request can still be admitted. Admission
charges the quota even if the protected application operation later fails.

This is a planned **0.5.0 behavior change** for fixed windows, which previously
charged rejected attempts. Existing counters retain their recorded usage until
expiry or an explicit reset; upgrading does not undo earlier charges.

### Fixed Window (Default)

**Best for:** Simple rate limiting, strict per-window limits, lower memory usage

```python
@limiter.limit("100/minute", algorithm="fixed_window")
async def endpoint(request: Request):
    return {"data": "..."}
```

**How it works:**
- Time divided into fixed windows (e.g., 14:35:00 - 14:36:00)
- Counter increments by cost only when the request fits the remaining quota
- Resets when window expires

**Pros:** Simple, low memory, strict limits  
**Cons:** Possible boundary bursts (can get 2x at window edge)

### Token Bucket

**Best for:** Smoothing bursts while allowing capacity to refill continuously

```python
@limiter.limit("100/minute", algorithm="token_bucket")
async def endpoint(request: Request):
    return {"data": "..."}
```

**How it works:**
- Bucket holds tokens (capacity = 100)
- Tokens refill continuously (~1.67/second for 100/minute)
- Each admitted request consumes tokens; denied polling preserves refill progress
- Full buckets discard excess idle credit, including fractional credit

**Pros:** Continuous refill and configurable burst capacity
**Cons:** State uses a Redis hash and allows bursts up to bucket capacity

For the planned 0.5.0 upgrade, `RateLimiter` uses token keys ending in
`:bucket:v2` rather than `:bucket`. This isolates the new `refill_units` hash
schema from old writers during rolling deployments and rollback. New buckets
start full; old quotas are not migrated and their keys expire normally.
During overlap, old and new workers enforce independent quotas, so their
combined traffic can exceed a single quota. Versioning prevents state corruption,
not uninterrupted quota continuity. Rollback resumes the old quota if its key
still exists, otherwise it starts fresh. Applications requiring strict continuity
must coordinate the cutover. Direct backend callers supply their own keys and
must version those keys themselves. An explicit token `reset()` removes both
recognized legacy and v2 keys for the selected identity and tenant.

### Sliding Window

**Best for:** Approximating a rolling window without storing every request timestamp

```python
@limiter.limit("100/minute", algorithm="sliding_window")
async def endpoint(request: Request):
    return {"data": "..."}
```

**How it works:**
- Combines current window with weighted portion of previous window
- Provides smooth transition between windows
- Approximates a rolling count with two fixed counters
- Previous-window weight decreases at whole Redis seconds, rounded down to
  thousandths; weighted usage is rounded down in scaled (1/1000-unit) capacity
- This assumes previous traffic was spread across its bucket. Clustered traffic
  can be under- or overestimated; this is not an exact rolling-window guarantee

**Pros:** Smooths fixed-window boundaries with constant Redis storage
**Cons:** It is an approximation rather than an exact request log

### Algorithm Comparison

| Feature | Fixed Window | Token Bucket | Sliding Window |
|---------|--------------|--------------|----------------|
| Simplicity | High | Medium | Medium |
| Boundary behavior | Can admit 2x across a boundary | No aligned window reset; bursts up to capacity | Weighted transition; approximate, not a strict rolling quota |
| Redis data | String counter | Hash with tokens, refill timestamp, and credited refill units | Two string counters |
| Traffic behavior | Resets at boundaries | Continuous refill | Weighted window transition |
| Accuracy model | Exact fixed window | Exact token bucket state | Approximate rolling window |

**Recommendation:** Choose fixed window for simple quotas, token bucket for controlled bursts, and sliding window counter when fixed-window boundary bursts are undesirable.

---

## Documentation

### Configuration

```python
from moderato import RateLimiter

limiter = RateLimiter(
    redis_url="redis://localhost:6379",      # Redis connection URL
    key_prefix="myapp:ratelimit",            # Prefix for Redis keys
    default_algorithm="token_bucket",        # Algorithm: "fixed_window", "token_bucket", or "sliding_window"
    enable_metrics=False,                    # Enable Prometheus metrics
)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `redis_url` | str | `"redis://localhost:6379"` | Redis connection string |
| `key_prefix` | str | `"ratelimit"` | Prefix for all Redis keys |
| `default_algorithm` | str | `"fixed_window"` | Default algorithm to use |
| `enable_metrics` | bool | `False` | Enable Prometheus metrics collection |

### Rate Limit Formats

```python
"10/second"   # 10 requests per second
"100/minute"  # 100 requests per minute
"1000/hour"   # 1000 requests per hour
"10000/day"   # 10000 requests per day
```

### Decorator API

#### Basic IP-based limiting

```python
@app.get("/api/data")
@limiter.limit("100/minute")
async def get_data(request: Request):
    return {"data": "..."}
```

#### Custom key extraction

```python
@app.get("/api/user/{user_id}")
@limiter.limit(
    "1000/hour",
    key=lambda req: f"user:{req.path_params.get('user_id')}"
)
async def get_user_data(request: Request, user_id: str):
    return {"user_id": user_id}
```

#### Choose algorithm

```python
@app.get("/api/smooth")
@limiter.limit("100/minute", algorithm="token_bucket")
async def smooth_endpoint(request: Request):
    return {"data": "..."}
```

#### Share a limit across routes

Decorated endpoints use separate method-and-route scopes by default. Set `scope` to share one bucket:

```python
@limiter.limit("100/minute", scope="search-api")
async def search(request: Request):
    ...
```

Manual `check()` calls use the `"global"` scope unless you pass one explicitly.

### Automatic Headers

For endpoints using `@limiter.limit(...)`, add the middleware to inject rate limit headers:

```python
from moderato import RateLimitHeadersMiddleware

app.add_middleware(RateLimitHeadersMiddleware)
```

**Headers added to decorated responses:**
- `X-RateLimit-Limit`: Maximum requests allowed
- `X-RateLimit-Remaining`: Whole unit-cost requests that fit the remaining capacity,
  rounded down (estimated for sliding windows), including on a denial
- `X-RateLimit-Reset`: Unix timestamp returned by the Redis-backed decision

**Additional headers on 429 responses:**
- `Retry-After`: Seconds to wait before retrying

`Retry-After` is rounded up from the Lua decision's millisecond delay. Reset
timestamps come from Redis-backed metadata rather than the application clock.
On an allowed fixed/sliding check, reset is the current bucket's end; on an
allowed token check, it is full-refill time. On a denial whose cost fits the
policy capacity, fixed reset is the window boundary; token/sliding reset is a
conservative timestamp for retrying **that cost**, not for recovering the whole
quota. Sliding retries follow the same quantized two-bucket estimate as admission,
including rollover, and may extend beyond the next boundary.

For example, after using 8 of 10 units, a cost-3 request is denied with 2
remaining, and a cost-2 request can still succeed. `CheckResult`, exceptions,
and headers use the decision's capacity; `get_usage()` is a later, non-atomic
snapshot, not a reservation. Retry hints assume no intervening traffic and
retained Redis state; other callers may consume the predicted capacity.

### Prometheus Metrics

Install `moderato[metrics]`, then enable collection:

```python
limiter = RateLimiter(
    redis_url="redis://localhost:6379",
    enable_metrics=True  # Enable Prometheus metrics
)
```

**Metrics collected:**
- `moderato_checks_total` - Total rate limit checks
- `moderato_check_duration_seconds` - Check latency histogram
- `moderato_limit_exceeded_total` - Rate limit violations
- `moderato_backend_operations_total` - Redis operations

**Expose metrics endpoint:**
```python
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST

@app.get("/metrics")
async def metrics():
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
```

### Multi-Tenant Setup

```python
# Define tier-specific limits
TIER_LIMITS = {
    "free": "100/hour",
    "premium": "1000/hour",
    "enterprise": "10000/hour",
}

@app.get("/api/data")
async def get_data(request: Request):
    api_key = request.headers.get("X-API-Key", "anonymous")
    tier = get_user_tier(api_key)
    await limiter.check(
        key=api_key,
        rate=TIER_LIMITS[tier],
        tenant_type=tier,
        scope="GET:/api/data",
    )
    return {"data": "..."}
```

### Cost-Based Limiting

```python
@app.post("/api/ml/inference")
@limiter.limit(
    "100/minute",
    cost=10  # This endpoint counts as 10 regular requests
)
async def ml_inference(request: Request):
    return {"prediction": "..."}
```

For planned 0.5.0, `cost` accepts a static integer or a request callback.
Costs must be positive integers (not booleans or floats). A static cost above
the policy capacity raises `RateLimitConfigError` when creating the decorator,
before Redis is contacted. Callbacks still run per request: a valid positive
cost above capacity is a **permanent denial**, not a configuration failure.
HTTP integrations return **422**, omit `Retry-After` and `X-RateLimit-Reset`,
and return `retry_after: null` in JSON. Smaller requests can still use the
uncharged quota. Temporary quota exhaustion remains **429** with retry hints.
The default Moderato 422 body uses `error`; FastAPI's request-validation 422 body uses `detail`.

The public rate range is **1 through 9,007,199,254** requests per second,
minute, hour, or day. The ceiling keeps scaled capacity × permille weight
below Lua's largest exactly representable integer. Unsafe rates raise
`RateLimitConfigError` before Redis is contacted by `check()`,
`check_with_info()`, `get_usage()`, or decorator creation; `parse_rate()` itself
raises `ValueError`. This is an arithmetic limit, not a throughput promise.

Direct Redis backend callers use scaled capacity (maximum 9,007,199,254,000)
and integer windows of 1–86,400 seconds. Token refill rates must be finite and
between capacity/86,400 and 9,007,199,254,000 scaled units/second, so full refill
cannot take longer than a day. Lua rejects
invalid inputs before key mutation. Low-level results use `retry_after=-1`
and `reset_at=None` for permanent denials; public `CheckResult` and
`RateLimitExceeded` use `retry_after=None` and `reset_at=None` instead.
Public token policies refill one capacity per window with millisecond,
1/1000-unit precision; credited refill units are rebased at whole windows.
Independent custom backend refill rates retain floating-point arithmetic;
out-of-range cumulative accrual raises `BackendError` rather than storing
unsafe state. Token state is written as decimal integer strings, not scientific
notation. Custom-rate bucket TTLs cover twice the longer of the configured
window or full-refill duration, plus a 60-second buffer; slow custom rates no
longer get a fresh quota through premature idle expiry.

### Error Handling

```python
from moderato import BackendError, RateLimitCallbackError, RateLimitExceeded
from starlette.responses import JSONResponse

@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse(
        status_code=exc.status_code,  # 429 for temporary, 422 for permanent denial
        content={
            "error": "Rate limit exceeded",
            "retry_after": exc.retry_after,
            "limit": exc.limit,
        },
        headers={"Retry-After": str(exc.retry_after)} if exc.retry_after is not None else {},
    )

@app.exception_handler(BackendError)
async def rate_limit_backend_handler(request: Request, exc: BackendError):
    return JSONResponse(
        status_code=503,
        content={"error": "Rate limit backend unavailable"},
    )

@app.exception_handler(RateLimitCallbackError)
async def rate_limit_callback_handler(request: Request, exc: RateLimitCallbackError):
    return JSONResponse(
        status_code=503,
        content={"error": "Rate limit callback failed", "callback": exc.callback},
    )
```

Key, tenant, and cost callbacks fail closed: their exception raises
`RateLimitCallbackError` before the endpoint runs. With
`RateLimitHeadersMiddleware`, this becomes the same 503 response automatically.

### Backend Failure Policy (planned 0.5.0)

**Fail closed is the default:** `RateLimiter(fail_open=False)`. If Redis cannot
provide a decision, manual `check()` and `check_with_info()` calls raise
`BackendError`. `RateLimitHeadersMiddleware` and `RateLimitMiddleware` return
**503** with `{"error": "Rate limit backend unavailable"}`, without quota or
retry headers. This is not a confirmed quota denial: those remain 429, or
422 when the request cost exceeds capacity.
Both middleware also translate escaping Moderato `BackendError` exceptions
from downstream manual checks, usage snapshots, or resets into 503 responses
before a response starts. These operations still raise normally outside HTTP;
unrelated exception types are not translated. Once ASGI response headers have
been sent, an error is propagated rather than starting a second response.

For availability-first applications, explicitly opt into fail-open:

```python
# Install: pip install 'moderato[metrics]' (or 'moderato[all]' for FastAPI).
limiter = RateLimiter(redis_url="redis://localhost:6379", fail_open=True)
```

When a feasible check raises `BackendError`, fail-open allows it, logs a
warning, and increments `moderato_fail_open_total{algorithm="..."}` and
`moderato_checks_total{algorithm="...",result="bypassed"}`. Bypasses are not
counted as confirmed `allowed` decisions. Custom metric namespaces are honored.
Fail-open automatically enables metrics, even if `enable_metrics=False`, and
requires the metrics extra. Disabling its collector prevents bypasses rather
than letting uncounted requests through.

`check()` returns `True` for a bypass; `check_with_info()` returns
`allowed=True` with `remaining=None`, `retry_after=None`, and `reset_at=None`.
The configured `limit` and `window_seconds` are still known. HTTP integrations
omit quota headers on successful responses if any stacked decorator bypassed
its policy, regardless of decorator order. Check `remaining is not None` before
using it as a quota measurement; unknown does not mean zero or a fresh quota.

Fail-open never hides invalid configuration, callback failures, or cancellation,
and never admits costs above capacity. An oversized request during a backend
outage still raises `BackendError` (503 in HTTP), rather than being bypassed.
Once Redis recovers, normal admissions and denials resume automatically.
A failed Redis command might already have updated quota before its reply was
lost; bypasses are not replayed or retroactively charged.

The setting applies only to request checks. Explicit `connect()`, context-manager
entry, `get_usage()`, and `reset()` still surface backend failures. If an application
must start during an outage, do not require a successful eager `connect()` in
its startup hook; checks can connect lazily under the configured failure policy.

### Manual Checking

```python
# Enforcing check: raises RateLimitExceeded when denied.
try:
    await limiter.check(key="user:123", rate="100/minute")
    # Request allowed
except RateLimitExceeded as e:
    # Cost defaults to 1, so this denial is temporary.
    print(f"Retry after {e.retry_after} seconds")

# Decision check: always returns CheckResult, including a denied decision.
decision = await limiter.check_with_info(key="user:123", rate="100/minute")
if not decision.allowed:
    print(f"Retry after {decision.retry_after} seconds at {decision.reset_at}")

# An oversized per-request cost is permanently denied without consuming quota.
decision = await limiter.check_with_info(key="user:123", rate="100/minute", cost=101)
assert not decision.allowed
assert decision.retry_after is None and decision.reset_at is None
print("Permanent denial: reduce the cost or change the policy; waiting cannot help")
# check(..., cost=101) instead raises RateLimitExceeded with the same None metadata.

# Get usage statistics
usage = await limiter.get_usage(key="user:123", rate="100/minute")
print(f"Current: {usage['current']}, Remaining: {usage['remaining']}")

# Reset rate limit
await limiter.reset(key="user:123")
```

---

## Performance

Numbers on this page's benchmarks are measured, not claimed: the harness commits its raw results, reports variance across repeated trials, and documents the exact environment. See [BENCHMARKS.md](BENCHMARKS.md) for methodology, per-run data, and a head-to-head comparison against slowapi and fastapi-limiter.

Run the benchmark suite against a local Redis instance yourself:

```bash
docker-compose -f docker-compose.dev.yml up -d
poetry install
poetry run python benchmarks/performance.py --quick
```

The quick run exercises the same sections with reduced sizes; drop `--quick` for the full-size suite (~60 s here, about five times the quick run). Results depend on Redis placement, network latency, hardware, Python version, and concurrency, so publish those details with any result.

**Measured on a 2-vCPU sandbox with Redis 7 on localhost (CPython 3.12):** ~5.8–5.9k sequential and ~8.4–8.6k concurrent checks/s for the limiter alone (concurrency sweep run with the pool raised to 250 via the public config field; sequential p99 < 0.5 ms; under high concurrency the sweep's p99 rises into the tens of milliseconds), ~1.6–1.8k req/s end-to-end behind a FastAPI app — the same band as slowapi and fastapi-limiter under an identical ASGI workload (on the allowed path moderato and slowapi are inside each other's run-to-run noise and moderato is ahead of fastapi-limiter; on the limited path moderato is ahead of slowapi by 27–39% and of fastapi-limiter by up to 21%; see BENCHMARKS.md). The same app without a limiter does ~3.4–3.9k req/s, so per-request limiter work (including the Redis round trip) accounts for the drop. The limiter-only path is client-bound, not Redis-bound: during the concurrency sweep the single-threaded Python client sat at ~100% of a core while the Redis server used ~28–41%, and every moderato check invokes one Lua script call that derives server `TIME` internally — a raw-Redis floor of ~9.1–13.6k ops/s on this box. Environment-specific; see [BENCHMARKS.md](BENCHMARKS.md) for the full data and caveats.

**Implemented optimizations:**
- Cached Lua scripts with `EVALSHA` and `EVAL` fallback
- Redis connection pooling
- One atomic script call per rate limit decision, with server `TIME` derived inside the script
- Bounded, component-level hashing for long identifiers and scopes

---

## Examples

See the [examples/](examples/) directory:
- [fastapi_app.py](examples/fastapi_app.py) - Complete FastAPI demo
- [multi_tenant.py](examples/multi_tenant.py) - Multi-tenant SaaS setup
- [algorithms_demo.py](examples/algorithms_demo.py) - Algorithm comparison

### Running Examples

```bash
# Start Redis
docker-compose -f docker-compose.dev.yml up -d

# FastAPI demo
poetry run uvicorn examples.fastapi_app:app --reload

# Multi-tenant demo
poetry run uvicorn examples.multi_tenant:app --reload --port 8001

# Algorithm comparison
poetry run python examples/algorithms_demo.py
```

---

## Architecture

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   FastAPI   │────▶│ RateLimiter │────▶│    Redis    │
│     App     │     │  (Python)   │     │   (Lua)     │
└─────────────┘     └─────────────┘     └─────────────┘
                            │
                    ┌───────┴───────┐
                    │               │
            ┌───────▼─────┐ ┌───────▼─────┐
            │   Fixed     │ │   Token     │
            │   Window    │ │   Bucket    │
            └─────────────┘ └─────────────┘
```

For internals, read the algorithm implementations in `moderato/algorithms/`
and the atomic Lua scripts in `moderato/scripts/` — both are heavily tested
(`tests/`) and intentionally compact.

---

## Development

### Prerequisites

- Python 3.9–3.13
- Redis 7.0+
- Poetry

### Setup

```bash
git clone https://github.com/Arjun-Aravind/moderato.git
cd moderato

poetry install
docker-compose -f docker-compose.dev.yml up -d
poetry run pytest
```

### Commands

```bash
make test          # Run tests
make test-cov      # Run tests with coverage
make lint          # Run linting
make format        # Format code
make demo          # Run algorithm demo
```

---

## Testing

The test suite covers:

- All three algorithms (Fixed Window, Token Bucket, Sliding Window)
- Concurrent requests and race conditions
- Multi-tenant isolation
- Cost-based rate limiting
- Headers middleware
- Edge cases and error handling

```bash
# Run all tests
make test

# Run specific test file
poetry run pytest tests/test_token_bucket.py -v

# Run with coverage
make test-cov
```

---

## Roadmap

- [x] Fixed Window algorithm
- [x] Token Bucket algorithm
- [x] Sliding Window algorithm
- [x] Automatic rate limit headers
- [x] Prometheus metrics
- [x] Multi-tenant support
- [x] Cost-based rate limiting
- [ ] Circuit breaker pattern
- [ ] Redis Cluster support
- [ ] Web dashboard
- [ ] Django integration

---

## Contributing

Contributions are welcome! Please see [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

---

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
