import uuid

import pytest
from prometheus_client import REGISTRY

import moderato.metrics as metrics_module
from moderato import BackendError, RateLimiter, RateLimitExceeded
from moderato.metrics import init_metrics


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


def test_limiter_reuses_initialized_namespace():
    metrics = init_metrics(namespace=f"test_{uuid.uuid4().hex}")
    limiter = RateLimiter(enable_metrics=True)
    assert limiter.metrics is metrics


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
