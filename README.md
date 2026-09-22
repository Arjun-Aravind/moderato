# Moderato

*In musical notation, **moderato** means "at a moderate pace." Moderato enforces your API's tempo.*

[![Python Version](https://img.shields.io/badge/python-3.9--3.13-blue)](https://www.python.org)
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

### Fixed Window (Default)

**Best for:** Simple rate limiting, strict per-window limits, lower memory usage

```python
@limiter.limit("100/minute", algorithm="fixed_window")
async def endpoint(request: Request):
    return {"data": "..."}
```

**How it works:**
- Time divided into fixed windows (e.g., 14:35:00 - 14:36:00)
- Counter increments per request
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
- Each request consumes tokens

**Pros:** Continuous refill and configurable burst capacity
**Cons:** State uses a Redis hash and allows bursts up to bucket capacity

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

**Pros:** Smooths fixed-window boundaries with constant Redis storage
**Cons:** It is an approximation rather than an exact request log

### Algorithm Comparison

| Feature | Fixed Window | Token Bucket | Sliding Window |
|---------|--------------|--------------|----------------|
| Simplicity | High | Medium | Medium |
| Boundary Bursts | Possible (2x) | None | None |
| Redis data | String counter | Hash with tokens and timestamp | Two string counters |
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
- `X-RateLimit-Remaining`: Requests remaining in current window
- `X-RateLimit-Reset`: Unix timestamp when the limit resets

**Additional headers on 429 responses:**
- `Retry-After`: Seconds to wait before retrying

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
    cost=lambda req: 10  # This endpoint counts as 10 regular requests
)
async def ml_inference(request: Request):
    return {"prediction": "..."}
```

### Error Handling

```python
from moderato import RateLimitExceeded

@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse(
        status_code=429,
        content={
            "error": "Rate limit exceeded",
            "retry_after": exc.retry_after,
            "limit": exc.limit,
        },
        headers={"Retry-After": str(exc.retry_after)},
    )
```

### Manual Checking

```python
# Direct rate limit check
try:
    await limiter.check(key="user:123", rate="100/minute")
    # Request allowed
except RateLimitExceeded as e:
    # Rate limited
    print(f"Retry after {e.retry_after} seconds")

# Get usage statistics
usage = await limiter.get_usage(key="user:123", rate="100/minute")
print(f"Current: {usage['current']}, Remaining: {usage['remaining']}")

# Reset rate limit
await limiter.reset(key="user:123")
```

---

## Performance

Run the benchmark suite against a local Redis instance rather than relying on hardware-independent throughput claims:

```bash
docker-compose -f docker-compose.dev.yml up -d
poetry install --with benchmarks
poetry run python benchmarks/performance.py --quick
```

The quick run reports throughput, latency percentiles, algorithm comparisons, and rate-limit accuracy. Run without `--quick` to include concurrent-client, Redis memory, and multi-tenant benchmarks. Results depend on Redis placement, network latency, hardware, Python version, and concurrency, so publish those details with any result.

**Implemented optimizations:**
- Cached Lua scripts with `EVALSHA` and `EVAL` fallback
- Redis connection pooling
- One atomic script call per rate limit decision
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
