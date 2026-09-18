"""
Multi-tenant rate limiting example with different tiers.

This example shows how to implement SaaS-style tiered rate limiting
with different limits for different customer tiers.
"""

import os
import sys
from datetime import datetime
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

sys.path.insert(0, str(Path(__file__).parent.parent))

from fastlimit import RateLimiter, RateLimitExceeded  # noqa: E402

app = FastAPI(
    title="Multi-Tenant API",
    description="Example of multi-tenant rate limiting with tiers",
    version="1.0.0",
)

redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
limiter = RateLimiter(redis_url=redis_url)

# Simulated tenant database
TENANT_DATABASE = {
    "tenant-001": {"name": "Startup Inc", "tier": "free", "api_key": "key-001"},
    "tenant-002": {"name": "Growth Corp", "tier": "premium", "api_key": "key-002"},
    "tenant-003": {"name": "Enterprise Ltd", "tier": "enterprise", "api_key": "key-003"},
    "tenant-004": {"name": "Basic Co", "tier": "free", "api_key": "key-004"},
    "tenant-005": {"name": "Premium Plus", "tier": "premium", "api_key": "key-005"},
}

# API key to tenant mapping
API_KEY_MAP = {v["api_key"]: k for k, v in TENANT_DATABASE.items()}

# Tier-based rate limits
TIER_LIMITS = {
    "free": {
        "data": "10/minute",
        "analytics": "5/minute",
        "export": "1/hour",
    },
    "premium": {
        "data": "100/minute",
        "analytics": "50/minute",
        "export": "10/hour",
    },
    "enterprise": {
        "data": "1000/minute",
        "analytics": "500/minute",
        "export": "100/hour",
    },
}


def get_tenant_info(api_key: str) -> tuple:
    """Get tenant ID and tier from API key."""
    tenant_id = API_KEY_MAP.get(api_key)
    if not tenant_id:
        raise HTTPException(status_code=401, detail="Invalid API key")

    tenant = TENANT_DATABASE[tenant_id]
    return tenant_id, tenant["tier"], tenant["name"]


@app.on_event("startup")
async def startup_event():
    """Initialize rate limiter on startup."""
    await limiter.connect()
    print("Connected to Redis")
    print(f"Loaded {len(TENANT_DATABASE)} tenants")


@app.on_event("shutdown")
async def shutdown_event():
    """Clean up on shutdown."""
    await limiter.close()


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    """Handle rate limit exceeded with tenant context."""
    return JSONResponse(
        status_code=429,
        content={
            "error": "Rate limit exceeded",
            "message": f"Your tier's limit ({exc.limit}) has been exceeded",
            "retry_after": exc.retry_after,
            "upgrade_url": "https://example.com/pricing",
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
    """API documentation."""
    return {
        "message": "Multi-Tenant API",
        "authentication": "Pass API key in X-API-Key header",
        "tiers": {
            "free": TIER_LIMITS["free"],
            "premium": TIER_LIMITS["premium"],
            "enterprise": TIER_LIMITS["enterprise"],
        },
        "demo_keys": {
            "free": "key-001",
            "premium": "key-002",
            "enterprise": "key-003",
        },
    }


@app.get("/api/data")
async def get_data(request: Request, x_api_key: str = Header(None)):
    """Get data endpoint with tier-based rate limiting."""
    if not x_api_key:
        raise HTTPException(status_code=401, detail="API key required")

    tenant_id, tier, tenant_name = get_tenant_info(x_api_key)
    limit = TIER_LIMITS[tier]["data"]

    await limiter.check(key=tenant_id, rate=limit, tenant_type=tier)

    return {
        "tenant": tenant_name,
        "tier": tier,
        "data": [
            {"id": 1, "value": "Sample data 1"},
            {"id": 2, "value": "Sample data 2"},
            {"id": 3, "value": "Sample data 3"},
        ],
        "rate_limit": limit,
        "timestamp": datetime.utcnow().isoformat(),
    }


@app.get("/api/analytics")
async def get_analytics(request: Request, x_api_key: str = Header(None)):
    """Analytics endpoint with tier-based rate limiting."""
    if not x_api_key:
        raise HTTPException(status_code=401, detail="API key required")

    tenant_id, tier, tenant_name = get_tenant_info(x_api_key)
    limit = TIER_LIMITS[tier]["analytics"]

    await limiter.check(key=tenant_id, rate=limit, tenant_type=f"{tier}:analytics")

    if tier == "enterprise":
        analytics_data = {
            "detailed_metrics": True,
            "real_time": True,
            "historical_data": "unlimited",
            "custom_reports": True,
        }
    elif tier == "premium":
        analytics_data = {
            "detailed_metrics": True,
            "real_time": False,
            "historical_data": "90 days",
            "custom_reports": False,
        }
    else:
        analytics_data = {
            "detailed_metrics": False,
            "real_time": False,
            "historical_data": "7 days",
            "custom_reports": False,
        }

    return {
        "tenant": tenant_name,
        "tier": tier,
        "analytics": analytics_data,
        "rate_limit": limit,
        "timestamp": datetime.utcnow().isoformat(),
    }


@app.post("/api/export")
async def export_data(request: Request, x_api_key: str = Header(None)):
    """Export endpoint with strict tier-based rate limiting."""
    if not x_api_key:
        raise HTTPException(status_code=401, detail="API key required")

    tenant_id, tier, tenant_name = get_tenant_info(x_api_key)
    limit = TIER_LIMITS[tier]["export"]

    await limiter.check(key=tenant_id, rate=limit, tenant_type=f"{tier}:export")

    return {
        "tenant": tenant_name,
        "tier": tier,
        "export_id": f"export-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}",
        "status": "initiated",
        "rate_limit": limit,
        "message": f"Export initiated. {tier.capitalize()} tier allows {limit}",
        "timestamp": datetime.utcnow().isoformat(),
    }


@app.get("/api/usage")
async def check_usage(x_api_key: str = Header(None)):
    """Check current rate limit usage for the tenant."""
    if not x_api_key:
        raise HTTPException(status_code=401, detail="API key required")

    tenant_id, tier, tenant_name = get_tenant_info(x_api_key)

    usage_data = {}

    for endpoint, limit in TIER_LIMITS[tier].items():
        if endpoint == "export":
            tenant_type = f"{tier}:export"
        elif endpoint == "analytics":
            tenant_type = f"{tier}:analytics"
        else:
            tenant_type = tier

        try:
            usage = await limiter.get_usage(key=tenant_id, rate=limit, tenant_type=tenant_type)
            usage_data[endpoint] = {
                "current": usage["current"],
                "limit": usage["limit"],
                "remaining": usage["remaining"],
                "resets_in": usage["ttl"],
            }
        except Exception:
            usage_data[endpoint] = {
                "current": 0,
                "limit": int(limit.split("/")[0]),
                "remaining": int(limit.split("/")[0]),
                "resets_in": 0,
            }

    return {
        "tenant": tenant_name,
        "tier": tier,
        "usage": usage_data,
        "timestamp": datetime.utcnow().isoformat(),
    }


@app.post("/api/upgrade")
async def upgrade_tier(
    new_tier: str, x_api_key: str = Header(None), x_admin_key: str = Header(None)
):
    """Simulate tier upgrade (admin only)."""
    if not x_api_key:
        raise HTTPException(status_code=401, detail="API key required")

    if x_admin_key != "admin-secret":
        raise HTTPException(status_code=403, detail="Admin key required")

    if new_tier not in TIER_LIMITS:
        raise HTTPException(status_code=400, detail="Invalid tier")

    tenant_id, current_tier, tenant_name = get_tenant_info(x_api_key)

    TENANT_DATABASE[tenant_id]["tier"] = new_tier

    await limiter.reset(key=tenant_id)

    return {
        "tenant": tenant_name,
        "previous_tier": current_tier,
        "new_tier": new_tier,
        "new_limits": TIER_LIMITS[new_tier],
        "message": f"Tenant upgraded from {current_tier} to {new_tier}",
        "timestamp": datetime.utcnow().isoformat(),
    }


@app.get("/api/tenants")
async def list_tenants(x_admin_key: str = Header(None)):
    """List all tenants (admin only)."""
    if x_admin_key != "admin-secret":
        raise HTTPException(status_code=403, detail="Admin key required")

    return {
        "tenants": [
            {
                "id": tid,
                "name": tdata["name"],
                "tier": tdata["tier"],
                "api_key": tdata["api_key"],
                "limits": TIER_LIMITS[tdata["tier"]],
            }
            for tid, tdata in TENANT_DATABASE.items()
        ],
        "total": len(TENANT_DATABASE),
        "timestamp": datetime.utcnow().isoformat(),
    }


if __name__ == "__main__":
    uvicorn.run(
        "examples.multi_tenant:app",
        host="0.0.0.0",
        port=8001,
        reload=True,
        log_level="info",
    )
