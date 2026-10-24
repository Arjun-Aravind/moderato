"""Run with an isolated interpreter (-I), never with source-tree imports.

Writes independent cases and observations to JSON even if a case fails.
Fault injection uses a real TCP relay, not mocked Redis methods.
"""

import asyncio
import hashlib
import importlib.metadata
import json
import os
import platform
import runpy
import sys
import time
import traceback
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import redis.asyncio as redis
from prometheus_client import generate_latest

import moderato
from moderato import BackendError, RateLimitConfigError, RateLimiter, RateLimitExceeded

ALGORITHMS = ("fixed_window", "token_bucket", "sliding_window")
URL = os.environ["REDIS_URL"]
PREFIX = os.environ["QUALIFICATION_PREFIX"]
RESULTS = []
FAULT_URL = os.environ.get("FAULT_REDIS_URL")
FAULT_TARGET = urlsplit(FAULT_URL or "")


async def targets():
    urls = {"primary": URL}
    if os.environ.get("FAULTS", "1") == "1":
        assert FAULT_URL, "Set FAULT_REDIS_URL explicitly"
        assert (
            os.environ.get("ALLOW_INSTANCE_WIDE_FAULTS") == "disposable"
        ), "CLIENT KILL and SCRIPT FLUSH affect the entire fault Redis instance, not just its DB"
        urls["fault"] = FAULT_URL
    observations = {}
    for name, url in urls.items():
        target = urlsplit(url)
        assert target.scheme == "redis" and target.hostname in ("127.0.0.1", "localhost", "::1")
        assert not (target.username or target.password or target.query or target.fragment)
        assert target.path.startswith("/") and target.path[1:].isdigit()
        assert target.port and 0 < target.port < 65536
        client = redis.from_url(url)
        try:
            assert await client.ping()
            info = await client.info("server")
            observations[name] = {
                "url": url,
                "run_id": info["run_id"],
                "version": info["redis_version"],
            }
        finally:
            await (getattr(client, "aclose", None) or client.close)()
    return observations


async def case(name, operation):
    started = time.monotonic()
    try:
        detail = await operation()
        row = {"case": name, "status": "pass", "detail": detail}
    except Exception:
        row = {"case": name, "status": "fail", "error": traceback.format_exc()}
    row["seconds"] = time.monotonic() - started
    RESULTS.append(row)
    print(json.dumps(row), flush=True)


async def manual(algorithm):
    async with RateLimiter(URL, key_prefix=PREFIX, enable_metrics=True) as limiter:
        key = uuid4().hex
        options = {"algorithm": algorithm, "tenant_type": "paid", "scope": "exports"}
        result = await limiter.check_with_info(key, "7/day", cost=3, **options)
        assert result.allowed and result.remaining == 4
        result = await limiter.check_with_info(key, "7/day", cost=5, **options)
        assert not result.allowed and result.remaining == 4 and result.retry_after > 0
        assert await limiter.check(key, "7/day", cost=4, **options)
        try:
            await limiter.check(key, "7/day", **options)
        except RateLimitExceeded as exc:
            assert exc.status_code == 429 and exc.reset_at
        else:
            raise AssertionError("quota not enforced")
        usage = await limiter.get_usage(key, "7/day", **options)
        assert usage["remaining"] == 0
        permanent = await limiter.check_with_info(key, "7/day", cost=8, **options)
        assert not permanent.allowed and permanent.retry_after is None
        for cost in (0, -1, True, 1.5):
            try:
                await limiter.check(key, "7/day", cost=cost, **options)
            except RateLimitConfigError:
                pass
            else:
                raise AssertionError(f"invalid cost accepted: {cost}")
        assert await limiter.check(key, "7/day", algorithm=algorithm, tenant_type="free")
        assert await limiter.check(key, "8/day", **options)
        assert await limiter.reset(key, algorithm=algorithm, tenant_type="paid")
        assert (await limiter.get_usage(key, "7/day", **options))["remaining"] == 7
        assert (await limiter.get_usage(key, "7/day", algorithm=algorithm, tenant_type="free"))[
            "remaining"
        ] == 6
        assert await limiter.reset(key)
        assert not await limiter.reset(key)
        return usage


async def concurrency(algorithm):
    async with RateLimiter(URL, key_prefix=PREFIX) as limiter:
        key = uuid4().hex
        gate = asyncio.Semaphore(20)

        async def check():
            async with gate:
                return await limiter.check_with_info(key, "73/day", algorithm=algorithm)

        results = await asyncio.gather(*(check() for _ in range(300)))
        assert sum(r.allowed for r in results) == 73
        assert (await limiter.get_usage(key, "73/day", algorithm=algorithm))["remaining"] == 0
        return {"attempts": 300, "allowed": 73}


class Relay:
    def __init__(self):
        self.mode = "forward"
        self.writers = set()
        self.tasks = set()
        self.forwarded = 0

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        upstream_reader, upstream = await asyncio.open_connection(
            FAULT_TARGET.hostname, FAULT_TARGET.port
        )
        self.writers.update((writer, upstream))

        async def pump(source, target, replies):
            while data := await source.read(65536):
                if self.mode == "blackhole" or (replies and self.mode == "lost-reply"):
                    continue
                target.write(data)
                await target.drain()
                if not replies:
                    self.forwarded += 1

        try:
            await asyncio.gather(pump(reader, upstream, False), pump(upstream_reader, writer, True))
        finally:
            for connection in (writer, upstream):
                connection.close()
                self.writers.discard(connection)
            self.tasks.discard(task)

    async def close(self):
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def faults(mode, fail_open, algorithm):
    relay = Relay()
    server = await asyncio.start_server(relay.handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    limiter = RateLimiter(
        f"redis://127.0.0.1:{port}{FAULT_TARGET.path}",
        key_prefix=PREFIX,
        fail_open=fail_open,
        enable_metrics=True,
    )
    observer = RateLimiter(FAULT_URL, key_prefix=PREFIX)
    limiter.config.socket_timeout = 1
    limiter.config.connection_timeout = 1
    key = uuid4().hex
    try:
        await limiter.connect()
        relay.mode = mode
        started = time.monotonic()
        try:
            result = await limiter.check_with_info(key, "7/day", algorithm=algorithm)
        except BackendError:
            assert not fail_open
        else:
            assert fail_open and result.allowed and result.remaining is None
        elapsed = time.monotonic() - started
        assert 0.8 < elapsed < 4, elapsed
        usage = await observer.get_usage(key, "7/day", algorithm=algorithm)
        assert usage["remaining"] == (6 if mode == "lost-reply" else 7), usage
        relay.mode = "forward"
        recovered = await limiter.check_with_info(key, "7/day", algorithm=algorithm)
        assert recovered.remaining == (5 if mode == "lost-reply" else 6)
        return {
            "elapsed": elapsed,
            "remaining_after_failure": usage["remaining"],
            "remaining_after_recovery": recovered.remaining,
        }
    finally:
        await limiter.close()
        await observer.close()
        server.close()
        await relay.close()
        await server.wait_closed()


async def pool_pressure():
    observations = []
    for fail_open in (False, True):
        relay = Relay()
        server = await asyncio.start_server(relay.handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        limiter = RateLimiter(
            f"redis://127.0.0.1:{port}{FAULT_TARGET.path}", key_prefix=PREFIX, fail_open=fail_open
        )
        limiter.config.max_connections = 2
        limiter.config.socket_timeout = 1
        limiter.config.connection_timeout = 1
        pending = []
        try:
            await limiter.connect()
            # Establish both connections before withholding replies. Otherwise
            # redis-py's pool lock can serialize callers behind a new handshake.
            warm = await asyncio.gather(
                *(limiter.check_with_info(uuid4().hex, "7/day") for _ in range(2))
            )
            assert all(result.allowed for result in warm)
            relay.mode = "blackhole"
            pending = [
                asyncio.create_task(limiter.check_with_info(uuid4().hex, "7/day")) for _ in range(2)
            ]
            await asyncio.sleep(0.1)
            started = time.monotonic()
            try:
                result = await limiter.check_with_info(uuid4().hex, "7/day")
            except BackendError:
                assert not fail_open
            else:
                assert fail_open and result.allowed and result.remaining is None
            elapsed = time.monotonic() - started
            assert elapsed < 0.5
            observations.append({"fail_open": fail_open, "saturation_seconds": elapsed})
        finally:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            await limiter.close()
            server.close()
            await relay.close()
            await server.wait_closed()
    return observations


async def cancellation():
    relay = Relay()
    server = await asyncio.start_server(relay.handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    limiter = RateLimiter(
        f"redis://127.0.0.1:{port}{FAULT_TARGET.path}", key_prefix=PREFIX, fail_open=True
    )
    try:
        await limiter.connect()
        relay.mode = "blackhole"
        task = asyncio.create_task(limiter.check("cancel", "7/day"))
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("cancellation swallowed")
        relay.mode = "forward"
        assert await limiter.check("cancel-recovered", "7/day")
        return "CancelledError propagated under fail-open; subsequent check recovered"
    finally:
        await limiter.close()
        server.close()
        await relay.close()
        await server.wait_closed()


async def connection_loss():
    async with RateLimiter(FAULT_URL, key_prefix=PREFIX) as limiter:
        await limiter.check("disconnect", "7/day")
        admin = redis.from_url(FAULT_URL)
        try:
            killed = await admin.client_kill_filter(_type="normal", skipme=True)
            assert killed >= 1
            first_error = False
            try:
                await limiter.check("disconnect", "7/day")
            except BackendError:
                # A killed socket may be noticed during the command, not checkout.
                # Do not require automatic replay of an ambiguous operation.
                first_error = True
            before = await limiter.get_usage("disconnect", "7/day")
            assert await limiter.check("disconnect", "7/day")
            after = await limiter.get_usage("disconnect", "7/day")
            assert after["remaining"] == before["remaining"] - 1
            await admin.script_flush()
            assert await limiter.check("disconnect", "7/day")
            return {
                "connections_killed": killed,
                "first_check_backend_error": first_error,
                "noscript_recovered": True,
            }
        finally:
            await (getattr(admin, "aclose", None) or admin.close)()


async def manual_metrics():
    from prometheus_client import REGISTRY

    from moderato.metrics import RateLimitMetrics, metrics_decorator

    metrics = RateLimitMetrics(namespace="qualification_manual")
    metrics.update_usage_gauge("bounded", "fixed_window", 3, 7)
    metrics.set_active_connections(2)
    with metrics.track_check_duration("fixed_window"):
        pass

    @metrics_decorator(metrics, "sync")
    def sync():
        return 17

    @metrics_decorator(metrics, "async")
    async def async_call():
        raise ValueError("expected error")

    assert sync() == 17
    try:
        await async_call()
    except ValueError:
        pass
    else:
        raise AssertionError("metrics wrapper swallowed exception")
    labels = {"key": "bounded", "algorithm": "fixed_window"}
    assert REGISTRY.get_sample_value("qualification_manual_current_usage", labels) == 3
    assert REGISTRY.get_sample_value("qualification_manual_limit_value", labels) == 7
    assert REGISTRY.get_sample_value("qualification_manual_active_redis_connections") == 2
    assert (
        REGISTRY.get_sample_value(
            "qualification_manual_backend_operations_total",
            {"operation": "sync", "status": "success"},
        )
        == 1
    )
    assert (
        REGISTRY.get_sample_value(
            "qualification_manual_backend_operations_total",
            {"operation": "async", "status": "error"},
        )
        == 1
    )
    assert metrics.get_metrics_dict()["enabled"]
    disabled = RateLimitMetrics(enabled=False)
    assert disabled.get_metrics_dict() == {"enabled": False}
    return {"usage": 3, "limit": 7, "active_connections": 2, "sync_successes": 1, "async_errors": 1}


async def transport(url):
    async with RateLimiter(url, key_prefix=PREFIX) as limiter:
        for algorithm in ALGORITHMS:
            assert await limiter.check(uuid4().hex, "7/day", algorithm=algorithm)
        return {"healthy": await limiter.health_check()}


async def timed_recovery(algorithm):
    async with RateLimiter(URL, key_prefix=PREFIX) as limiter:
        key = uuid4().hex
        admin = redis.from_url(URL)
        try:
            _, microseconds = await admin.time()
            await asyncio.sleep(1 - microseconds / 1_000_000 + 0.05)
        finally:
            await (getattr(admin, "aclose", None) or admin.close)()
        assert await limiter.check(key, "1/second", algorithm=algorithm)
        denied = await limiter.check_with_info(key, "1/second", algorithm=algorithm)
        assert not denied.allowed and 0 < denied.retry_after <= 2
        await asyncio.sleep(denied.retry_after + 0.05)
        assert await limiter.check(key, "1/second", algorithm=algorithm)
        return {"retry_after": denied.retry_after, "reset_at": denied.reset_at}


async def redis_state():
    admin = redis.from_url(URL, decode_responses=True)
    try:
        keys = [key async for key in admin.scan_iter(match=f"{PREFIX}:*")]
        ttls = [await admin.ttl(key) for key in keys]
        # Zero is a live final second; -2 is expiry between SCAN and TTL.
        assert keys and all(ttl != -1 for ttl in ttls), ttls
        return {
            "key_count": len(keys),
            "min_ttl": min(ttls),
            "max_ttl": max(ttls),
            "types": sorted({await admin.type(key) for key in keys}),
            "redis_version": (await admin.info("server"))["redis_version"],
        }
    finally:
        await (getattr(admin, "aclose", None) or admin.close)()


async def streaming(base):
    # Transport completion is deliberately not equated with application completion.
    async with httpx.AsyncClient(base_url=base) as client:
        try:
            response = await client.get("/stream")
        except httpx.RemoteProtocolError as exc:
            return {"transport": "incomplete", "error": str(exc)}
        assert response.status_code == 200 and response.content == b"partial\n"
        return {
            "transport": "complete",
            "status": 200,
            "application_complete": False,
            "body": response.text,
        }


async def http(base):
    observations = {}
    async with httpx.AsyncClient(base_url=base, timeout=5) as client:
        health = (await client.get("/health")).json()
        assert "site-packages/moderato" in health["package"]
        local = runpy.run_path(str(Path(__file__).with_name("provenance.py")))["installed"]()
        assert health["application_prefix"] == os.environ["EXPECTED_APP_PREFIX"], health
        assert health["environment_prefix"] == str(
            Path(os.environ["EXPECTED_SERVER_ENV"]).resolve()
        )
        assert health["profile"] == local["profile"], health
        assert health["wheel_sha256"] == local["wheel_sha256"], health
        assert Path(health["package"]).is_relative_to(Path(health["environment_prefix"]))
        for algorithm in ALGORITHMS:
            account = uuid4().hex
            headers = {"x-account": account, "x-cost": "3"}
            response = await client.get(f"/billing/{algorithm}/inv-1", headers=headers)
            assert response.status_code == 200, response.text
            assert response.headers["x-ratelimit-remaining"] == "4"
            headers["x-cost"] = "5"
            response = await client.get(f"/billing/{algorithm}/inv-2", headers=headers)
            assert response.status_code == 429 and int(response.headers["retry-after"]) > 0
            assert response.headers["x-ratelimit-remaining"] == "4"
            headers["x-cost"] = "8"
            response = await client.get(f"/billing/{algorithm}/inv-3", headers=headers)
            assert response.status_code == 422 and "retry-after" not in response.headers
            headers.update({"x-cost": "7", "x-tier": "paid"})
            assert (
                await client.get(f"/billing/{algorithm}/inv-4", headers=headers)
            ).status_code == 200
            observations[algorithm] = [200, 429, 422, 200]
        headers = {"x-account": uuid4().hex}
        mounted = [
            await client.get(path, headers=headers)
            for path in ("/shops/a/items/1", "/shops/b/items/2", "/shops/c/items/3")
        ]
        assert [r.status_code for r in mounted] == [200, 200, 429]
        for path, expected in (("/stack", [200, 200, 429, 429]), ("/sync", [200, 429])):
            headers = {"x-account": uuid4().hex}
            actual = [(await client.get(path, headers=headers)).status_code for _ in expected]
            assert actual == expected, (path, actual)
        for path, status in (("/open", 200), ("/closed", 503)):
            response = await client.get(path, headers={"x-account": uuid4().hex})
            assert response.status_code == status
            assert not any(
                h.startswith("x-ratelimit") or h == "retry-after" for h in response.headers
            )
        raw = [(await client.get("/raw/ok")).status_code for _ in range(3)]
        assert raw == [200, 200, 429], raw
        scrape = await client.get("/metrics")
        assert scrape.status_code == 200
        assert 'moderato_checks_total{algorithm="fixed_window",result="allowed"}' in scrape.text
        assert "moderato_fail_open_total" in scrape.text
        Path(os.environ["RESULT_PATH"] + ".prom").write_text(scrape.text)
        observations["health"] = health
        return observations


async def http_concurrency(base):
    gate = asyncio.Semaphore(16)
    observations = {}
    async with httpx.AsyncClient(base_url=base, timeout=10) as client:
        for algorithm in ALGORITHMS:
            account = uuid4().hex

            async def request(n, algorithm=algorithm, account=account):
                async with gate:
                    return await client.get(
                        f"/billing/{algorithm}/{n}", headers={"x-account": account}
                    )

            responses = await asyncio.gather(*(request(n) for n in range(80)))
            statuses = [r.status_code for r in responses]
            assert statuses.count(200) == 7 and statuses.count(429) == 73, statuses
            pids = {r.json()["pid"] for r in responses if r.status_code == 200}
            observations[algorithm] = {"allowed": 7, "denied": 73, "admitting_pids": sorted(pids)}
    return observations


async def metrics_aggregation(base):
    from prometheus_client.parser import text_string_to_metric_families

    async def snapshot():
        async with httpx.AsyncClient(base_url=base) as client:
            response = await client.get("/metrics")
            assert response.status_code == 200
            return sum(
                sample.value
                for family in text_string_to_metric_families(response.text)
                for sample in family.samples
                if sample.name == "moderato_checks_total"
            )

    before = await snapshot()
    pids = set()
    for _ in range(40):
        async with httpx.AsyncClient(base_url=base) as client:
            response = await client.get(
                "/billing/fixed_window/metric", headers={"x-account": uuid4().hex}
            )
            assert response.status_code == 200
            pids.add(response.json()["pid"])
    after = [await snapshot() for _ in range(10)]
    if os.environ.get("EXPECT_MULTIPROCESS") == "1":
        assert len(pids) > 1, pids
        assert all(value == before + 40 for value in after), (before, after)
    return {
        "worker_pids": sorted(pids),
        "before": before,
        "after": after,
        "expected_delta": 40,
        "aggregated": all(value == before + 40 for value in after),
    }


async def soak(seconds):
    latencies = []
    count = 0
    async with RateLimiter(URL, key_prefix=PREFIX) as limiter:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            algorithm = ALGORITHMS[count % 3]
            key = f"soak-{count}"
            started = time.monotonic()
            results = [
                await limiter.check_with_info(key, "2/day", algorithm=algorithm) for _ in range(3)
            ]
            assert [r.allowed for r in results] == [True, True, False]
            assert await limiter.reset(key)
            latencies.append(time.monotonic() - started)
            count += 1
    latencies.sort()
    return {
        "cycles": count,
        "checks": count * 3,
        "seconds": seconds,
        "cycle_p50_ms": latencies[len(latencies) // 2] * 1000,
        "cycle_p99_ms": latencies[int(len(latencies) * 0.99)] * 1000,
    }


async def main():
    target_manifest = await targets()  # Validate before any destructive operation.
    provenance = runpy.run_path(str(Path(__file__).with_name("provenance.py")))["installed"]()
    assert Path(moderato.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
    assert not any(
        Path(p or ".").resolve() == Path(__file__).resolve().parents[2] for p in sys.path
    )
    for algorithm in ALGORITHMS:
        await case(f"manual/{algorithm}", lambda a=algorithm: manual(a))
        await case(f"concurrency/{algorithm}", lambda a=algorithm: concurrency(a))
        await case(f"timed-recovery/{algorithm}", lambda a=algorithm: timed_recovery(a))
    await case("redis-state", redis_state)
    await case("manual-metrics", manual_metrics)
    if os.environ.get("FAULTS", "1") == "1":
        for algorithm in ALGORITHMS:
            for mode in ("blackhole", "lost-reply"):
                for opened in (False, True):
                    await case(
                        f"fault/{algorithm}/{mode}/fail_open={opened}",
                        lambda m=mode, o=opened, a=algorithm: faults(m, o, a),
                    )
        await case("cancellation", cancellation)
        await case("pool-pressure", pool_pressure)
        await case("connection-loss-noscript", connection_loss)
    for variable in ("UNIX_REDIS_URL", "TLS_REDIS_URL"):
        if os.environ.get(variable):
            await case(variable, lambda v=variable: transport(os.environ[v]))
    if os.environ.get("HTTP_URL"):
        await case("http-contract", lambda: http(os.environ["HTTP_URL"]))
        await case("http-concurrency", lambda: http_concurrency(os.environ["HTTP_URL"]))
        await case("streaming-observation", lambda: streaming(os.environ["HTTP_URL"]))
        await case("metrics-aggregation", lambda: metrics_aggregation(os.environ["HTTP_URL"]))
    await case("bounded-soak", lambda: soak(float(os.environ.get("SOAK_SECONDS", "5"))))
    manifest = {
        "provenance": provenance,
        "redis_targets": target_manifest,
        "python": sys.version,
        "platform": platform.platform(),
        "package": moderato.__file__,
        "sys_path": sys.path,
        "dependencies": {d.metadata["Name"]: d.version for d in importlib.metadata.distributions()},
        "scripts": {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in Path(moderato.__file__).parent.glob("scripts/*.lua")
        },
    }
    Path(os.environ["RESULT_PATH"]).write_text(
        json.dumps({"environment": manifest, "results": RESULTS}, indent=2)
    )
    Path(os.environ["RESULT_PATH"] + ".manual.prom").write_bytes(generate_latest())
    return int(any(row["status"] == "fail" for row in RESULTS))


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
