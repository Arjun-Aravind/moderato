"""Benchmark contracts; Redis tests always launch their own disposable server.

Run with: python -m pytest benchmarks/test_harness.py
"""

import argparse
import json
import socket
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

import pytest
import redis.asyncio as redis
from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Route

from benchmarks.compare_libraries import StatusDistributionError, measure_app
from benchmarks.performance import QUICK_PROFILE, PerformanceBenchmark


@pytest.fixture
def disposable_redis(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    process = subprocess.Popen(
        [
            "redis-server",
            "--bind",
            "127.0.0.1",
            "--port",
            str(port),
            "--save",
            "",
            "--appendonly",
            "no",
            "--dir",
            str(tmp_path),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(100):
            if process.poll() is not None:
                raise RuntimeError("disposable Redis failed to start")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.01)
        else:
            raise RuntimeError("disposable Redis did not become ready")
        yield f"redis://127.0.0.1:{port}"
    finally:
        process.terminate()
        process.wait(timeout=5)


async def test_long_run_cleanup_preserves_other_runs(disposable_redis):
    run_id = "x" * 64
    benchmark = PerformanceBenchmark(disposable_redis, "quick", QUICK_PROFILE, run_id)
    other = PerformanceBenchmark(disposable_redis, "quick", QUICK_PROFILE, run_id[:-1])
    client = redis.from_url(disposable_redis, decode_responses=True)
    await benchmark.setup()
    await other.setup()
    try:
        await client.set("unrelated", "keep")
        for algorithm in ("fixed_window", "token_bucket", "sliding_window"):
            await benchmark.limiter.check(
                key=benchmark.bench_key("algorithm", algorithm, 999),
                rate="100/minute",
                algorithm=algorithm,
            )
        # Even a future longer identity that hashes must remain owned.
        await benchmark.limiter.check(key=benchmark.bench_key("y" * 150), rate="100/minute")
        await other.limiter.check(key=other.bench_key("control"), rate="100/minute")
        assert len([k async for k in client.scan_iter(match=benchmark.redis_key_pattern())]) == 4
        await benchmark.teardown()
        remaining = [k async for k in client.scan_iter()]
        assert len(remaining) == 2
        assert "unrelated" in remaining
        assert any(k.startswith(other.key_prefix + ":") for k in remaining)
    finally:
        await other.teardown()
        await client.aclose()


@pytest.mark.parametrize("status", [503, 429, 302])
async def test_rejects_unexpected_timed_status_after_successful_warmup(status):
    calls = 0

    async def endpoint(request):
        nonlocal calls
        calls += 1
        return Response(status_code=200 if calls <= 20 else status)

    async def reset():
        pass

    with pytest.raises(StatusDistributionError) as caught:
        await measure_app(
            Starlette(routes=[Route("/", endpoint)]),
            reset_state=reset,
            levels=[3],
            requests_per_trial=7,
            trials=2,
        )
    assert caught.value.status_counts == {status: 14}
    assert caught.value.expected_status_counts == {200: 14}


@pytest.mark.parametrize("timed_quota", [2, 3, 4])
async def test_limited_distribution_and_quota_reset(timed_quota):
    calls = -20  # warmup is allowed and erased by reset

    async def endpoint(request):
        nonlocal calls
        calls += 1
        return Response(status_code=200 if calls <= timed_quota else 429)

    async def reset():
        nonlocal calls
        calls = 0

    kwargs = {
        "reset_state": reset,
        "levels": [2, 3],
        "requests_per_trial": 7,
        "trials": 2,
        "allowed_per_level": 3,
    }
    app = Starlette(routes=[Route("/", endpoint)])
    if timed_quota == 3:
        result = await measure_app(app, **kwargs)
        assert [row["status_counts"] for row in result["levels"]] == [{200: 3, 429: 11}] * 2
    else:
        with pytest.raises(StatusDistributionError) as caught:
            await measure_app(app, **kwargs)
        assert caught.value.status_counts == {200: timed_quota, 429: 14 - timed_quota}


async def test_quick_tenant_mix():
    observed = Counter()

    class Limiter:
        async def check(self, *, tenant_type, **kwargs):
            observed[tenant_type] += 1
            return True

    benchmark = PerformanceBenchmark("unused", "quick", QUICK_PROFILE, "tiers")
    benchmark.limiter = Limiter()
    await benchmark.benchmark_multi_tenant()
    assert observed == {"free": 280, "premium": 100, "enterprise": 20}
    assert benchmark.results["multi_tenant"]["tier_counts"] == {
        "free": 14,
        "premium": 5,
        "enterprise": 1,
    }


async def test_scheduling_control_detects_blocking_endpoint():
    async def endpoint(request):
        time.sleep(0.01)
        return Response(status_code=200)

    async def reset():
        pass

    result = await measure_app(
        Starlette(routes=[Route("/", endpoint)]),
        reset_state=reset,
        levels=[1],
        requests_per_trial=4,
        trials=1,
    )
    assert result["levels"][0]["scheduling_delay_ms"]["max"] >= 20


@pytest.mark.parametrize("git_metadata", [True, False])
async def test_failed_run_writes_counts_and_provenance(
    disposable_redis, tmp_path, monkeypatch, git_metadata
):
    from benchmarks import compare_libraries as comparison
    from benchmarks import provenance

    if not git_metadata:
        monkeypatch.setattr(provenance, "ROOT", tmp_path)

    calls = 0

    async def builder(redis_url, limit, prefix):
        async def endpoint(request):
            nonlocal calls
            calls += 1
            return Response(status_code=200 if calls <= 20 else 503)

        return Starlette(routes=[Route("/", endpoint)])

    monkeypatch.setenv("REDIS_URL", disposable_redis)
    monkeypatch.setattr(comparison, "APP_BUILDERS", {"failing": builder})
    monkeypatch.setattr(comparison, "LEVELS", [1])
    monkeypatch.setattr(comparison, "REQUESTS_PER_TRIAL", 4)
    output = tmp_path / "failed.json"
    args = argparse.Namespace(
        run_id="failure", trials=1, reverse_order=False, http_control=False, json=output
    )
    with pytest.raises(StatusDistributionError):
        await comparison.main_async(args)
    payload = json.loads(output.read_text())
    assert payload["failure"]["status_counts"] == {"503": 4}
    assert payload["failure"]["expected_status_counts"] == {"200": 4}
    assert payload["scenarios"] == {}
    sha = payload["provenance"]["source_sha"]
    assert sha is None or len(sha) == 40
    if not git_metadata:
        assert sha is None
    assert "moderato/scripts/fixed_window.lua" in payload["provenance"]["sha256"]
    assert payload["provenance"]["installed_dependencies"]["redis"]


async def test_real_http_control_returns_all_requests():
    pytest.importorskip("uvicorn")
    from benchmarks.compare_libraries import measure_http_control

    async def endpoint(request):
        return Response(status_code=200)

    async def reset():
        pass

    result = await measure_http_control(Starlette(routes=[Route("/", endpoint)]), reset)
    assert result["levels"][0]["status_counts"] == {200: 200}
    assert result["levels"][0]["scheduling_delay_ms"]["samples"] > 0


async def test_native_async_limits_partitions_identities(disposable_redis):
    pytest.importorskip("limits.aio.storage")
    import httpx

    from benchmarks.compare_libraries import build_limits_async_app, close_app, delete_owned_keys

    prefix = "bench-native-contract~"
    app = await build_limits_async_app(disposable_redis, 2, prefix)
    client = redis.from_url(disposable_redis, decode_responses=True)
    try:
        for host in ("10.0.0.1", "10.0.0.2"):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, client=(host, 123)), base_url="http://test"
            ) as http:
                assert [(await http.get("/")).status_code for _ in range(3)] == [200, 200, 429]
        assert await delete_owned_keys(client, prefix) == 2
    finally:
        await close_app(app)
        await client.aclose()


@pytest.mark.parametrize("git_available", [True, False])
def test_provenance_without_checkout_or_git(tmp_path, monkeypatch, git_available):
    from benchmarks import provenance

    monkeypatch.setattr(provenance, "ROOT", tmp_path)
    if not git_available:
        monkeypatch.setenv("PATH", "")
    result = provenance.collect_provenance()
    assert result["source_sha"] is None
    assert result["source_dirty"] is None
    assert "unknown" in result["source_note"]
    assert len(result["sha256"]["moderato/scripts/token_bucket.lua"]) == 64
    assert result["installed_dependencies"]["redis"]


def test_comparison_cli_before_dependency_bootstrap(tmp_path):
    environment = tmp_path / "empty-venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(environment)], check=True)
    result = subprocess.run(
        [
            str(environment / "bin/python"),
            str(Path(__file__).with_name("compare_libraries.py")),
            "--help",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "--venv" in result.stdout
