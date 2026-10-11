import asyncio
import uuid

import httpx
import pytest
from fastapi import FastAPI, Request
from prometheus_client import REGISTRY

import moderato.metrics as metrics_module
from moderato import BackendError, RateLimitConfigError, RateLimiter, RateLimitExceeded
from moderato.decorators import RateLimitMiddleware
from moderato.metrics import init_metrics
from moderato.middleware import RateLimitHeadersMiddleware


def _sample(name, labels):
    return REGISTRY.get_sample_value(name, labels) or 0


def _unregister(metrics):
    for collector in vars(metrics).values():
        if hasattr(collector, "collect"):
            REGISTRY.unregister(collector)
    if metrics_module.get_metrics() is metrics:
        metrics_module._global_metrics = None


@pytest.fixture(autouse=True)
def isolate_metrics():
    original = metrics_module._global_metrics
    metrics_module._global_metrics = None
    yield
    metrics = metrics_module.get_metrics()
    if metrics is not None:
        _unregister(metrics)
    metrics_module._global_metrics = original


def test_metrics_initialization_is_idempotent():
    metrics = init_metrics(namespace=f"test_{uuid.uuid4().hex}")
    assert init_metrics(namespace=metrics.namespace) is metrics


@pytest.mark.parametrize("options", [{"enable_metrics": True}, {"fail_open": True}])
def test_limiter_reuses_initialized_namespace(options):
    metrics = init_metrics(namespace=f"test_{uuid.uuid4().hex}")
    limiter = RateLimiter(**options)
    assert limiter.metrics is metrics


def test_fail_open_automatically_enables_metrics():
    limiter = RateLimiter(fail_open=True)
    assert limiter.config.fail_open and limiter.config.enable_metrics
    assert limiter.metrics.enabled
    assert not RateLimiter().config.fail_open


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RuntimeError, asyncio.CancelledError])
async def test_fail_open_does_not_swallow_nonbackend_failures(error, monkeypatch):
    limiter = RateLimiter(fail_open=True)

    async def fail():
        raise error("not a backend failure")

    monkeypatch.setattr(limiter, "connect", fail)
    with pytest.raises(error):
        await limiter.check("client", "10/day")
    assert _sample("moderato_fail_open_total", {"algorithm": "fixed_window"}) == 0


@pytest.mark.asyncio
async def test_fail_open_cannot_bypass_with_disabled_metrics(monkeypatch):
    limiter = RateLimiter(fail_open=True)
    limiter.metrics.enabled = False

    async def unavailable():
        raise BackendError("backend unavailable")

    monkeypatch.setattr(limiter, "connect", unavailable)
    with pytest.raises(RateLimitConfigError, match="requires enabled metrics"):
        await limiter.check("client", "10/day")


@pytest.mark.asyncio
@pytest.mark.parametrize("callback", ["key", "tenant_type", "cost"])
async def test_fail_open_does_not_bypass_callback_errors(callback):
    limiter = RateLimiter(fail_open=True)
    app = FastAPI()
    app.add_middleware(RateLimitHeadersMiddleware)

    def fail(request):
        raise BackendError("callback failure is not a Redis decision failure")

    @app.get("/")
    @limiter.limit("10/day", **{callback: fail})
    async def endpoint(request: Request):
        pytest.fail("Callback failure must not execute the endpoint")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/")
    assert response.status_code == 503
    assert response.json()["error"] == "Rate limit callback failed"
    assert _sample("moderato_fail_open_total", {"algorithm": "fixed_window"}) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["connect", "get_usage", "reset", "context"])
async def test_fail_open_does_not_fabricate_administrative_success(operation, monkeypatch):
    limiter = RateLimiter(fail_open=True)

    async def unavailable():
        raise BackendError("backend unavailable")

    monkeypatch.setattr(limiter.backend, "connect", unavailable)
    with pytest.raises(BackendError):
        if operation == "context":
            async with limiter:
                pytest.fail("Context entry must require a connection")
        elif operation == "get_usage":
            await limiter.get_usage("client", "10/day")
        elif operation == "reset":
            await limiter.reset("client")
        else:
            await limiter.connect()
    assert _sample("moderato_fail_open_total", {"algorithm": "fixed_window"}) == 0


@pytest.mark.asyncio
async def test_fail_open_does_not_replay_a_lost_decision_reply(frozen_limiter, monkeypatch):
    limiter = frozen_limiter
    limiter.config.fail_open = True
    limiter.metrics = init_metrics()
    original = limiter.backend.check_fixed_window

    async def lost_reply(*args, **kwargs):
        await original(*args, **kwargs)
        raise BackendError("decision applied but reply lost")

    kwargs = {"key": uuid.uuid4().hex, "rate": "2/day"}
    with monkeypatch.context() as patch:
        patch.setattr(limiter.backend, "check_fixed_window", lost_reply)
        decision = await limiter.check_with_info(**kwargs)
    assert decision.allowed and decision.remaining is None
    recovered = await limiter.check_with_info(**kwargs)
    assert recovered.allowed and recovered.remaining == 0
    assert not (await limiter.check_with_info(**kwargs)).allowed
    assert _sample("moderato_fail_open_total", {"algorithm": "fixed_window"}) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("algorithm", ["fixed_window", "token_bucket", "sliding_window"])
@pytest.mark.parametrize("failure_stage", ["connect", "decision"])
@pytest.mark.parametrize("fail_open", [False, True])
async def test_failure_policy_and_recovery(
    frozen_limiter, algorithm, failure_stage, fail_open, monkeypatch, caplog
):
    limiter = frozen_limiter
    limiter.config.fail_open = fail_open
    limiter.metrics = init_metrics()
    kwargs = {"key": uuid.uuid4().hex, "rate": "10/day", "algorithm": algorithm}

    async def unavailable(*args, **kwargs):
        raise BackendError("redis://user:secret@host:6379 failed")

    if failure_stage == "connect":
        await limiter.close()
    with monkeypatch.context() as patch:
        patch.setattr(
            limiter.backend,
            "connect" if failure_stage == "connect" else f"check_{algorithm}",
            unavailable,
        )
        if fail_open:
            decision = await limiter.check_with_info(**kwargs)
            assert decision.allowed and decision.limit == 10
            assert decision.remaining is None and decision.retry_after is None
            assert decision.reset_at is None
            assert await limiter.check(**kwargs)
        else:
            with pytest.raises(BackendError):
                await limiter.check_with_info(**kwargs)
            with pytest.raises(BackendError):
                await limiter.check(**kwargs)
        # Fail-open is not permission to admit an impossible cost or hide bad input.
        with pytest.raises(BackendError):
            await limiter.check(**kwargs, cost=11)
        with pytest.raises(RateLimitConfigError):
            await limiter.check(key="invalid", rate="bad")

    labels = {"algorithm": algorithm}
    assert _sample("moderato_fail_open_total", labels) == (2 if fail_open else 0)
    assert _sample("moderato_checks_total", {**labels, "result": "allowed"}) == 0
    warnings = [record for record in caplog.records if "check bypassed" in record.message]
    assert len(warnings) == (2 if fail_open else 0)
    assert all(
        record.levelname == "WARNING" and "secret" not in record.message for record in warnings
    )

    admitted = await limiter.check_with_info(**kwargs, cost=8)
    assert admitted.allowed and admitted.remaining == 2
    denied = await limiter.check_with_info(**kwargs, cost=3)
    assert not denied.allowed and denied.remaining == 2 and denied.retry_after > 0
    assert await limiter.check(**kwargs, cost=2)
    assert _sample("moderato_fail_open_total", labels) == (2 if fail_open else 0)
    assert _sample("moderato_checks_total", {**labels, "result": "allowed"}) == 2
    assert _sample("moderato_checks_total", {**labels, "result": "denied"}) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("integration", ["decorator", "asgi"])
@pytest.mark.parametrize("algorithm", ["fixed_window", "token_bucket", "sliding_window"])
async def test_fail_open_http_has_no_quota_headers_and_recovers(
    frozen_limiter, integration, algorithm, monkeypatch
):
    limiter = frozen_limiter
    limiter.config.fail_open = True
    limiter.config.default_algorithm = algorithm
    limiter.metrics = init_metrics()
    app = FastAPI()
    calls = []
    if integration == "decorator":
        app.add_middleware(RateLimitHeadersMiddleware)
    else:
        app.add_middleware(RateLimitMiddleware, limiter=limiter, default_rate="1/day")

    async def endpoint(request: Request):
        calls.append("called")
        return {"ok": True}

    if integration == "decorator":
        endpoint = limiter.limit("1/day")(endpoint)
    app.get("/")(endpoint)

    async def unavailable(*args, **kwargs):
        raise BackendError("backend unavailable")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        with monkeypatch.context() as patch:
            patch.setattr(limiter.backend, f"check_{algorithm}", unavailable)
            response = await client.get("/")
        assert response.status_code == 200 and response.json() == {"ok": True}
        assert "retry-after" not in response.headers
        assert not any(name.startswith("x-ratelimit-") for name in response.headers)
        assert (await client.get("/")).status_code == 200
        response = await client.get("/")
        assert response.status_code == 429 and int(response.headers["retry-after"]) > 0
    assert calls == ["called", "called"]
    assert _sample("moderato_fail_open_total", {"algorithm": algorithm}) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("bypass_first", [False, True])
async def test_stacked_bypass_omits_quota_headers(frozen_limiter, bypass_first, monkeypatch):
    limiter = frozen_limiter
    limiter.config.fail_open = True
    limiter.metrics = init_metrics()
    original = limiter.backend.check_fixed_window

    async def selective_failure(key, max_requests, window_seconds, **kwargs):
        if max_requests == 3000:
            raise BackendError("one policy is unavailable")
        return await original(key, max_requests, window_seconds, **kwargs)

    monkeypatch.setattr(limiter.backend, "check_fixed_window", selective_failure)
    app = FastAPI()
    app.add_middleware(RateLimitHeadersMiddleware)
    outer, inner = ("3/day", "2/day") if bypass_first else ("2/day", "3/day")

    @app.get("/")
    @limiter.limit(outer)
    @limiter.limit(inner)
    async def endpoint(request: Request):
        return {"ok": True}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/")
    assert response.status_code == 200 and response.json() == {"ok": True}
    assert not any(name.startswith("x-ratelimit-") for name in response.headers)
    assert _sample("moderato_fail_open_total", {"algorithm": "fixed_window"}) == 1
    assert _sample("moderato_checks_total", {"algorithm": "fixed_window", "result": "allowed"}) == 1


@pytest.mark.asyncio
async def test_limiter_records_checks_and_backend_operations(redis_url):
    limiter = RateLimiter(
        redis_url=redis_url,
        key_prefix=f"metrics:{uuid.uuid4().hex}",
        enable_metrics=True,
    )
    labels = {"algorithm": "fixed_window"}
    allowed_before = _sample("moderato_checks_total", {**labels, "result": "allowed"})
    denied_before = _sample("moderato_checks_total", {**labels, "result": "denied"})
    backend_before = _sample(
        "moderato_backend_operations_total",
        {"operation": "check_fixed_window", "status": "success"},
    )
    exceeded_before = _sample(
        "moderato_limit_exceeded_total",
        {"algorithm": "fixed_window", "tenant_type": "default"},
    )

    try:
        await limiter.check("client", "1/hour")
        with pytest.raises(RateLimitExceeded):
            await limiter.check("client", "1/hour")
    finally:
        await limiter.close()

    assert _sample("moderato_checks_total", {**labels, "result": "allowed"}) == allowed_before + 1
    assert _sample("moderato_checks_total", {**labels, "result": "denied"}) == denied_before + 1
    assert (
        _sample(
            "moderato_backend_operations_total",
            {"operation": "check_fixed_window", "status": "success"},
        )
        == backend_before + 2
    )
    assert (
        _sample(
            "moderato_limit_exceeded_total",
            {"algorithm": "fixed_window", "tenant_type": "default"},
        )
        == exceeded_before + 1
    )


@pytest.mark.asyncio
async def test_limiter_records_backend_errors(clean_limiter, monkeypatch):
    limiter = clean_limiter
    limiter.metrics = init_metrics()
    before = _sample(
        "moderato_backend_operations_total",
        {"operation": "check_fixed_window", "status": "error"},
    )

    async def fail(*args, **kwargs):
        raise BackendError("backend failed")

    monkeypatch.setattr(limiter.backend, "check_fixed_window", fail)
    with pytest.raises(BackendError):
        await limiter.check("client", "1/minute")

    assert (
        _sample(
            "moderato_backend_operations_total",
            {"operation": "check_fixed_window", "status": "error"},
        )
        == before + 1
    )
