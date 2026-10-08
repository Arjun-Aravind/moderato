"""Numeric-domain and permanent-denial contract regressions."""

import importlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, Request
from redis.exceptions import ResponseError

from moderato import CheckResult, RateLimitConfigError, RateLimiter, RateLimitExceeded
from moderato.decorators import RateLimitMiddleware
from moderato.middleware import RateLimitHeadersMiddleware
from moderato.utils import parse_rate


def test_rate_exactness_boundary():
    # Scaled capacity times the permille weight must stay below 2**53.
    assert parse_rate("9007199254/day") == (9007199254, 86400)
    with pytest.raises(ValueError):
        parse_rate("9007199255/day")


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["check", "check_with_info", "get_usage"])
async def test_unsafe_rate_rejected_before_connection(method, monkeypatch):
    limiter = RateLimiter()

    async def unexpected_connect():
        pytest.fail("Invalid policy must not touch Redis")

    monkeypatch.setattr(limiter, "connect", unexpected_connect)
    with pytest.raises(RateLimitConfigError):
        await getattr(limiter, method)(key="unsafe", rate="100000000000001/hour")


@pytest.mark.parametrize("cost", [0, -1, True, 1.5, "1", 11])
def test_static_cost_rejected_at_decoration(cost):
    with pytest.raises(RateLimitConfigError):
        RateLimiter().limit("10/minute", cost=cost)


@pytest.mark.parametrize("rate", ["invalid", "9007199255/day", None])
def test_invalid_decorator_policy_fails_at_creation(rate):
    with pytest.raises(RateLimitConfigError):
        RateLimiter().limit(rate)


@pytest.mark.asyncio
@pytest.mark.parametrize("algorithm", ["fixed_window", "token_bucket", "sliding_window"])
@pytest.mark.parametrize("cost", [11, 10**400])
async def test_oversized_request_is_permanent_without_consumption(frozen_limiter, algorithm, cost):
    kwargs = {"key": uuid4().hex, "rate": "10/day", "algorithm": algorithm}
    cold = await frozen_limiter.check_with_info(**kwargs, cost=cost)
    assert not cold.allowed and cold.remaining == 10
    assert cold.retry_after is None and cold.reset_at is None
    assert not [
        key
        async for key in frozen_limiter.backend.iter_keys(f"{frozen_limiter.config.key_prefix}:*")
    ]
    assert (await frozen_limiter.check_with_info(**kwargs, cost=8)).remaining == 2
    denied = await frozen_limiter.check_with_info(**kwargs, cost=cost)
    assert not denied.allowed
    assert denied.remaining == 2
    assert denied.retry_after is None
    assert denied.reset_at is None
    with pytest.raises(RateLimitExceeded) as caught:
        await frozen_limiter.check(**kwargs, cost=cost)
    assert caught.value.status_code == 422
    assert caught.value.retry_after is None
    assert caught.value.reset_at is None
    assert (await frozen_limiter.check_with_info(**kwargs, cost=2)).allowed


@pytest.mark.asyncio
@pytest.mark.parametrize("algorithm", ["fixed_window", "token_bucket", "sliding_window"])
async def test_dynamic_oversized_cost_is_422_then_smaller_cost_runs(frozen_limiter, algorithm):
    app = FastAPI()
    app.add_middleware(RateLimitHeadersMiddleware)
    calls = []

    @app.get("/dynamic")
    @frozen_limiter.limit("10/day", algorithm=algorithm, cost=lambda req: int(req.headers["cost"]))
    async def dynamic(request: Request):
        calls.append("dynamic")
        return {"ok": True}

    @app.get("/static")
    @frozen_limiter.limit("10/day", algorithm=algorithm, cost=8)
    async def static(request: Request):
        calls.append("static")
        return {"ok": True}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/dynamic", headers={"cost": "11"})
        assert response.status_code == 422
        assert "retry-after" not in response.headers
        assert "x-ratelimit-reset" not in response.headers
        assert response.json()["retry_after"] is None
        assert calls == []
        assert (await client.get("/dynamic", headers={"cost": "10"})).status_code == 200
        assert (await client.get("/dynamic", headers={"cost": "1"})).status_code == 429
        assert (await client.get("/static")).status_code == 200
        assert (await client.get("/static")).status_code == 429
    assert calls == ["dynamic", "static"]


@pytest.mark.asyncio
@pytest.mark.parametrize("algorithm", ["fixed_window", "token_bucket", "sliding_window"])
async def test_maximum_policy_usage_and_exact_capacity(frozen_limiter, algorithm):
    maximum = 9007199254
    kwargs = {"key": uuid4().hex, "rate": f"{maximum}/day", "algorithm": algorithm}
    first = await frozen_limiter.check_with_info(**kwargs)
    assert first.allowed and first.remaining == maximum - 1
    assert (await frozen_limiter.get_usage(**kwargs))["remaining"] == maximum - 1
    assert (await frozen_limiter.check_with_info(**kwargs, cost=maximum - 1)).allowed
    temporary = await frozen_limiter.check_with_info(**kwargs)
    assert not temporary.allowed and temporary.remaining == 0
    assert temporary.retry_after > 0
    assert (await frozen_limiter.get_usage(**kwargs))["remaining"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("algorithm", ["fixed_window", "token_bucket", "sliding_window"])
@pytest.mark.parametrize("capacity,window", [(0, 60), (9007199254001, 60), (1000, 86401)])
async def test_lua_rejects_unsafe_domain_before_mutation(redis_client, algorithm, capacity, window):
    source = (Path(__file__).parents[1] / f"moderato/scripts/{algorithm}.lua").read_text()
    prefix = f"bounds-guard:{uuid4().hex}:"
    args = (
        [capacity, capacity / window, window, 1000]
        if algorithm == "token_bucket"
        else [capacity, window, 1000]
    )
    with pytest.raises(ResponseError):
        await redis_client.eval(source, 1, prefix, *args)
    assert not [key async for key in redis_client.scan_iter(match=f"{prefix}*")]


@pytest.mark.asyncio
@pytest.mark.parametrize("refill", [0, -1, 5e-324, float("nan"), float("inf"), 9007199254001])
async def test_lua_rejects_invalid_refill_rate(redis_client, refill):
    source = (Path(__file__).parents[1] / "moderato/scripts/token_bucket.lua").read_text()
    key = f"bounds-refill:{uuid4().hex}"
    with pytest.raises(ResponseError, match="refill rate"):
        await redis_client.eval(source, 1, key, 5000, refill, 60, 1000)
    assert not await redis_client.exists(key)


@pytest.mark.asyncio
async def test_custom_refill_ttl_covers_full_recovery(redis_client):
    source = (Path(__file__).parents[1] / "moderato/scripts/token_bucket.lua").read_text()
    key = f"bounds-custom-ttl:{uuid4().hex}"
    assert (await redis_client.eval(source, 1, key, 5000, 10, 60, 1000))[0]
    # Capacity/rate = 500s, so TTL must be 2*500+60, not 2*60+60.
    assert 1059 <= await redis_client.ttl(key) <= 1060


@pytest.mark.asyncio
@pytest.mark.parametrize("module", ["fastapi_app", "multi_tenant"])
@pytest.mark.parametrize("retry,status", [(None, 422), (7, 429)])
async def test_example_handlers_support_permanent_and_temporary_denials(module, retry, status):
    example = importlib.import_module(f"examples.{module}")
    response = await example.rate_limit_handler(None, RateLimitExceeded(retry, "10/day"))
    assert response.status_code == status
    if retry is None:
        assert "retry-after" not in response.headers
        assert "x-ratelimit-reset" not in response.headers
    else:
        assert response.headers["retry-after"] == "7"


@pytest.mark.asyncio
@pytest.mark.parametrize("raises", [False, True])
async def test_asgi_middleware_permanent_denial(raises):
    check = AsyncMock(
        side_effect=RateLimitExceeded(None, "10/day") if raises else None,
        return_value=CheckResult(False, 10, 10, None, 86400),
    )
    app = FastAPI()
    app.add_middleware(
        RateLimitMiddleware, limiter=SimpleNamespace(check_with_info=check), default_rate="10/day"
    )

    @app.get("/")
    async def endpoint():
        pytest.fail("Permanent denial must not execute the endpoint")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/")
    assert response.status_code == 422
    assert response.json()["retry_after"] is None
    assert "retry-after" not in response.headers
    assert "x-ratelimit-reset" not in response.headers


@pytest.mark.asyncio
async def test_long_lived_token_refill_origin_is_bounded(redis_client):
    source = (Path(__file__).parents[1] / "moderato/scripts/token_bucket.lua").read_text()
    key = f"bounds:{uuid4().hex}"
    capacity = 9007199254000
    origin = 2_000_000_000_000
    previous_elapsed = 1000 * 60000
    await redis_client.hset(
        key,
        mapping={
            "tokens": capacity // 2,
            "last_refill_ms": origin,
            "refill_units": capacity * 1000,
        },
    )
    # This represents a continuously busy bucket: repeated refills have
    # been consumed without ever reaching full capacity and resetting origin.
    now = origin + previous_elapsed + 1
    script = source.replace("redis.call('TIME')", f"{{'{now // 1000}', '{now % 1000 * 1000}'}}")
    result = await redis_client.eval(script, 1, key, capacity, capacity / 60, 60, 1000)
    expected = capacity // 2 + capacity // 60000 - 1000
    assert result[:2] == [1, expected]
    fields = await redis_client.hgetall(key)
    assert int(fields["refill_units"]) < capacity
    assert int(fields["tokens"]) == expected
    assert int(fields["last_refill_ms"]) == origin + previous_elapsed
