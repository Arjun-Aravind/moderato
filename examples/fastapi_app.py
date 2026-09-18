"""
Example FastAPI application demonstrating Moderato rate limiting.

Run with:
    uvicorn examples.fastapi_app:app --reload --port 8000
"""

import os
import sys
from datetime import datetime
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

sys.path.insert(0, str(Path(__file__).parent.parent))

from moderato import RateLimiter, RateLimitExceeded  # noqa: E402

app = FastAPI(
    title="Moderato Demo API",
    description="Demonstration of rate limiting with Moderato",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
limiter = RateLimiter(redis_url=redis_url)


@app.on_event("startup")
async def startup_event():
    """Initialize rate limiter on startup."""
    await limiter.connect()
    print(f"Connected to Redis at {redis_url}")


@app.on_event("shutdown")
async def shutdown_event():
    """Clean up on shutdown."""
    await limiter.close()
    print("Disconnected from Redis")


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    """Global handler for rate limit exceeded exceptions."""
    return JSONResponse(
        status_code=429,
        content={
            "error": "Rate limit exceeded",
            "message": str(exc),
            "retry_after": exc.retry_after,
        },
        headers={
            "X-RateLimit-Limit": exc.limit,
            "X-RateLimit-Remaining": str(exc.remaining),
            "X-RateLimit-Reset": str(exc.retry_after),
            "Retry-After": str(exc.retry_after),
        },
    )


@app.get("/")
async def root():
    """Root endpoint - not rate limited."""
    return {
        "message": "Welcome to Moderato Demo API",
        "docs": "/docs",
        "endpoints": [
            "/api/public",
            "/api/limited",
            "/api/strict",
            "/api/user/{user_id}",
            "/api/tenant",
            "/api/expensive",
        ],
    }


@app.get("/api/public")
async def public_endpoint():
    """Public endpoint without rate limiting."""
    return {
        "message": "This endpoint is not rate limited",
        "timestamp": datetime.utcnow().isoformat(),
    }


@app.get("/api/limited")
@limiter.limit("10/minute")
async def limited_endpoint(request: Request):
    """
    Basic rate-limited endpoint.

    Limit: 10 requests per minute per IP address.
    """
    return {
        "message": "This endpoint is rate limited",
        "limit": "10 requests per minute",
        "your_ip": request.client.host,
        "timestamp": datetime.utcnow().isoformat(),
    }


@app.get("/api/strict")
@limiter.limit("3/second")
async def strict_endpoint(request: Request):
    """
    Strictly rate-limited endpoint.

    Limit: 3 requests per second per IP address.
    """
    return {
        "message": "This endpoint has strict rate limiting",
        "limit": "3 requests per second",
        "your_ip": request.client.host,
        "timestamp": datetime.utcnow().isoformat(),
    }


@app.get("/api/user/{user_id}")
@limiter.limit("100/hour", key=lambda req: f"user:{req.path_params.get('user_id')}")
async def user_endpoint(request: Request, user_id: str):
    """
    Per-user rate limiting.

    Limit: 100 requests per hour per user ID.
    """
    return {
        "message": f"User-specific endpoint for {user_id}",
        "user_id": user_id,
        "limit": "100 requests per hour per user",
        "timestamp": datetime.utcnow().isoformat(),
    }


@app.get("/api/tenant")
@limiter.limit(
    rate="50/minute",
    key=lambda req: req.headers.get("X-Tenant-ID", "default"),
    tenant_type=lambda req: req.headers.get("X-Tenant-Tier", "free"),
)
async def tenant_endpoint(request: Request):
    """
    Multi-tenant rate limiting.

    Headers:
    - X-Tenant-ID: Tenant identifier
    - X-Tenant-Tier: Tenant tier (free/premium/enterprise)

    Limit: 50 requests per minute per tenant.
    """
    tenant_id = request.headers.get("X-Tenant-ID", "default")
    tenant_tier = request.headers.get("X-Tenant-Tier", "free")

    return {
        "message": "Multi-tenant endpoint",
        "tenant_id": tenant_id,
        "tenant_tier": tenant_tier,
        "limit": "50 requests per minute",
        "timestamp": datetime.utcnow().isoformat(),
    }


@app.post("/api/expensive")
@limiter.limit(
    rate="20/minute", cost=lambda req: 5 if req.headers.get("X-Priority") == "high" else 1
)
async def expensive_operation(request: Request):
    """
    Endpoint with variable cost based on priority.

    Headers:
    - X-Priority: Request priority (high = 5x cost, normal = 1x cost)

    Limit: 20 requests per minute (high priority counts as 5 requests).
    """
    priority = request.headers.get("X-Priority", "normal")
    cost = 5 if priority == "high" else 1

    return {
        "message": "Expensive operation completed",
        "priority": priority,
        "cost": cost,
        "limit": "20 requests per minute",
        "note": f"This request counted as {cost} regular request(s)",
        "timestamp": datetime.utcnow().isoformat(),
    }


@app.get("/api/status")
async def status_endpoint():
    """Check API and rate limiter status."""
    health = await limiter.health_check()

    return {
        "api_status": "healthy",
        "rate_limiter_status": "healthy" if health else "unhealthy",
        "redis_connected": health,
        "timestamp": datetime.utcnow().isoformat(),
    }


@app.get("/api/usage/{user_id}")
async def usage_endpoint(user_id: str):
    """Check rate limit usage for a specific user."""
    try:
        usage = await limiter.get_usage(key=f"user:{user_id}", rate="100/hour")

        return {
            "user_id": user_id,
            "current_requests": usage["current"],
            "limit": usage["limit"],
            "remaining": usage["remaining"],
            "resets_in": usage["ttl"],
            "window_seconds": usage["window_seconds"],
        }
    except Exception as e:
        return {
            "user_id": user_id,
            "current_requests": 0,
            "limit": 100,
            "remaining": 100,
            "resets_in": 0,
            "error": str(e),
        }


@app.post("/api/reset/{user_id}")
async def reset_endpoint(user_id: str, request: Request):
    """Reset rate limit for a specific user (admin only)."""
    admin_key = request.headers.get("X-Admin-Key")
    if admin_key != "secret-admin-key":
        raise HTTPException(status_code=403, detail="Unauthorized")

    result = await limiter.reset(key=f"user:{user_id}")

    return {
        "message": f"Rate limit reset for user {user_id}",
        "success": result,
        "timestamp": datetime.utcnow().isoformat(),
    }


if __name__ == "__main__":
    uvicorn.run(
        "examples.fastapi_app:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info",
    )
