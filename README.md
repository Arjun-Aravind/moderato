# Moderato

*In musical notation, **moderato** means "at a moderate pace." Moderato enforces your API's tempo.*

[![Python Version](https://img.shields.io/badge/python-3.9%2B-blue)](https://www.python.org)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Redis](https://img.shields.io/badge/redis-7%2B-red)](https://redis.io)
[![Code Style](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)

A high-performance, Redis-backed rate limiting library for Python applications.

[Features](#features) | [Quick Start](#quick-start) | [Algorithms](#algorithms) | [Documentation](#documentation) | [Examples](#examples)

---

## What is Moderato?

Moderato is a rate limiting library designed for modern Python applications. It provides multiple algorithms, automatic header injection, comprehensive metrics, and multi-tenant support out of the box.

**Use cases:**
- FastAPI applications requiring rate limiting
- Multi-tenant SaaS platforms with tier-based limits
- APIs needing production monitoring and observability
- High-throughput services (10K+ req/s)
- Applications requiring strict rate limit guarantees

---

## Features

### Core Capabilities
- **Three Algorithms** - Fixed Window, Token Bucket & Sliding Window
- **Async-first design** - Built for FastAPI and modern async Python
- **High performance** - <2ms p99 latency, 10K+ requests/second
- **Zero race conditions** - Atomic operations via Redis Lua scripts
- **Integer precision** - Uses integer math (x1000 multiplier) for accuracy

### Production Features
- **Automatic Headers** - Industry-standard rate limit headers on all responses
- **Prometheus Metrics** - Comprehensive observability out of the box
- **Multi-tenant support** - Isolated limits for different users/tiers/organizations
- **Decorator-based API** - Clean, declarative rate limiting
- **Cost-based limiting** - Weight expensive operations appropriately
- **Flexible configuration** - Customizable key extraction, algorithms, and costs

---

## Quick Start

### Installation

```bash
pip install moderato

# With metrics support (optional)
pip install 'moderato[metrics]'
```

### Basic Example

```python
from fastapi import FastAPI, Request
from moderato import RateLimiter, RateLimitHeadersMiddleware

app = FastAPI()
limiter = RateLimiter(redis_url="redis://localhost:6379")

# Add automatic header injection (optional but recommended)
app.add_middleware(RateLimitHeadersMiddleware)

@app.on_event("startup")
async def startup():
    await limiter.connect()

@app.get("/api/users")
@limiter.limit("100/minute")  # 100 requests per minute per IP
async def get_users(request: Request):
    return {"users": ["Alice", "Bob"]}
```

This gives you:
- Rate limiting (100 requests/minute per IP)
- Automatic headers (X-RateLimit-Limit, X-RateLimit-Remaining, etc.)
- Proper 429 responses when exceeded
- Redis-backed, distributed-ready

---

## Algorithms

Moderato provides three production-tested algorithms. Choose based on your needs:

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

**Best for:** Smooth rate limiting, bursty traffic, better user experience

```python
@limiter.limit("100/minute", algorithm="token_bucket")
async def endpoint(request: Request):
    return {"data": "..."}
```

**How it works:**
- Bucket holds tokens (capacity = 100)
- Tokens refill continuously (~1.67/second for 100/minute)
- Each request consumes tokens

**Pros:** Smooth traffic, no boundary bursts, better UX  
**Cons:** Slightly more memory, more complex

### Sliding Window

**Best for:** Maximum accuracy, strict SLA requirements, preventing all burst scenarios

```python
@limiter.limit("100/minute", algorithm="sliding_window")
async def endpoint(request: Request):
    return {"data": "..."}
```

**How it works:**
- Combines current window with weighted portion of previous window
- Provides smooth transition between windows
- Most accurate rate limiting

**Pros:** Most accurate, no gaming possible, fair distribution  
**Cons:** Slightly higher memory and CPU usage

### Algorithm Comparison

| Feature | Fixed Window | Token Bucket | Sliding Window |
|---------|--------------|--------------|----------------|
| Simplicity | High | Medium | Medium |
| Boundary Bursts | Possible (2x) | None | None |
| Memory Usage | Low (~100 bytes) | Medium (~150 bytes) | Medium (~200 bytes) |
| Traffic Smoothness | Choppy | Smooth | Smooth |
| Accuracy | Good | Good | Best |

**Recommendation:** Start with Token Bucket for better UX, use Fixed Window for strict enforcement, use Sliding Window for maximum accuracy.

See [ALGORITHMS.md](ALGORITHMS.md) for detailed algorithm comparison.

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

### Automatic Headers

Add the middleware to automatically inject rate limit headers:

```python
from moderato import RateLimitHeadersMiddleware

app.add_middleware(RateLimitHeadersMiddleware)
```

**Headers added to all responses:**
- `X-RateLimit-Limit`: Maximum requests allowed
- `X-RateLimit-Remaining`: Requests remaining in current window
- `X-RateLimit-Reset`: Unix timestamp when the limit resets

**Additional headers on 429 responses:**
- `Retry-After`: Seconds to wait before retrying

### Prometheus Metrics

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
@limiter.limit(
    rate="100/hour",  # Base rate (overridden by tenant_type logic)
    key=lambda req: req.headers.get("X-API-Key"),
    tenant_type=lambda req: get_user_tier(req.headers.get("X-API-Key"))
)
async def get_data(request: Request):
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

| Metric | Fixed Window | Token Bucket | Sliding Window |
|--------|--------------|--------------|----------------|
| Latency (p50) | 0.8ms | 1.0ms | 1.2ms |
| Latency (p95) | 1.5ms | 1.8ms | 2.2ms |
| Latency (p99) | 2.0ms | 2.2ms | 2.8ms |
| Throughput | 15,000+ req/s | 12,000+ req/s | 10,000+ req/s |
| Memory per key | ~100 bytes | ~150 bytes | ~200 bytes |

**Optimizations:**
- Lua scripts cached (EVALSHA vs EVAL)
- Connection pooling (max 50 connections)
- Integer-only math (no float conversions)
- Efficient key hashing for long keys

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

See [ARCHITECTURE.md](ARCHITECTURE.md) for detailed internals.

---

## Development

### Prerequisites

- Python 3.9+
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

Moderato has 60+ comprehensive tests covering:

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
