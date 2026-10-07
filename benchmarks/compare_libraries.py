"""
Head-to-head throughput benchmark: moderato vs slowapi vs fastapi-limiter.

All three libraries drive the same minimal FastAPI app shape (one GET route
returning a small JSON body, per-IP keying) through the same in-process httpx
client against the same local Redis. Each library is configured per its own
documented usage, so this compares implementations as-shipped, not
theoretical optima:

- moderato: ``RateLimiter.limit()`` decorator (async redis client + Lua) with
  the documented ``RateLimitExceeded`` exception handler for 429s.
- slowapi: ``Limiter`` decorator with ``storage_uri="redis://..."``. With
  limits 5.x this instantiates a SYNC redis client inside the async
  endpoint; slowapi has no working async Redis storage (the
  ``async+redis://`` scheme requires coredis and then fails slowapi's own
  strategy assertion), so sync-in-async is its only Redis mode.
- fastapi-limiter: ``RateLimiter`` dependency over a pyrate-limiter
  ``RedisBucket``. Its README's ``Limiter(Rate(...))`` is in-memory only; the
  Redis bucket is pyrate-limiter's documented backend, and it uses ONE shared
  bucket per route rather than one per identity. With a single client
  identity the workload is identical either way.

A raw Redis floor (one and two EVALSHA round trips per request) is measured
so library numbers can be read as client-bound vs Redis-bound.

The competitor libraries are installed into an isolated virtualenv
(benchmarks/.venv-compare by default), never as main dependencies. The
harness never flushes the Redis database: every library's keys are namespaced
under a run-specific prefix and only those keys are deleted.

    python benchmarks/compare_libraries.py             # bootstraps venv, runs
    python benchmarks/compare_libraries.py --trials 1  # quick smoke run
"""

import argparse
import asyncio
import json
import math
import os
import platform
import re
import shutil
import statistics
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Pin the versions the comparison runs against; the results JSON records what
# was actually installed so the report can state them verbatim.
PINNED_PACKAGES = [
    "fastapi==0.141.1",
    "httpx==0.28.1",
    "pydantic==2.13.5",
    "anyio==4.15.1",
    "redis==5.3.1",
    "slowapi==0.1.10",
    "fastapi-limiter==0.2.0",
    "pyrate-limiter==4.5.0",
]

LEVELS = [1, 10, 50, 100]
REQUESTS_PER_TRIAL = 1000
TRIALS = 3
LIMITED_PER_MINUTE = 100

DEFAULT_VENV = REPO_ROOT / "benchmarks" / ".venv-compare"

AppBuilder = Callable[[str, int, str], Any]
StateReset = Callable[[], Awaitable[None]]


def venv_python_path(venv_dir: Path) -> Path:
    return venv_dir / "bin" / "python"


def venv_matches_pins(venv_python: Path) -> bool:
    """True when the venv has every pinned package at exactly the pinned version."""
    pins = {p.split("==")[0]: p.split("==")[1] for p in PINNED_PACKAGES}
    script = (
        "import json, importlib.metadata as m; "
        f"pins = {pins!r}; "
        "print(json.dumps({n: m.version(n) for n in pins}))"
    )
    result = subprocess.run([str(venv_python), "-c", script], capture_output=True)
    if result.returncode != 0:
        return False
    try:
        installed = json.loads(result.stdout.decode().strip().splitlines()[-1])
    except (ValueError, IndexError):
        return False
    return installed == pins


def ensure_venv(venv_dir: Path) -> Path:
    """Return a python interpreter inside an isolated venv with competitors.

    Creates the venv on first use (uv if available, otherwise venv+pip) and
    reinstalls the pinned set whenever the existing venv drifts from it, so
    the comparison never runs arbitrary local versions and never installs
    competitors into the main environment.
    """
    venv_python = venv_python_path(venv_dir)
    if venv_python.exists() and venv_matches_pins(venv_python):
        return venv_python

    print(f"Bootstrapping isolated comparison venv at {venv_dir} ...")
    if shutil.which("uv"):
        subprocess.run(
            ["uv", "venv", str(venv_dir), "--python", platform.python_version()],
            check=True,
        )
        installer: list[str] = ["uv", "pip", "install", "--python", str(venv_python)]
    else:
        import venv as venv_module

        venv_module.create(str(venv_dir), with_pip=True)
        installer = [str(venv_python), "-m", "pip", "install"]

    subprocess.run([*installer, *PINNED_PACKAGES], check=True)
    if not venv_matches_pins(venv_python):
        raise RuntimeError("comparison venv does not match the pinned package set")
    return venv_python


# -- app builders (run inside the comparison venv) ---------------------------
#
# Every builder namespaces its keys under ``key_prefix`` so the harness can
# delete exactly what it created, on a possibly shared Redis database.


async def build_moderato_app(redis_url: str, limit_per_minute: int, key_prefix: str):
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse

    from moderato import RateLimitCallbackError, RateLimiter, RateLimitExceeded

    limiter = RateLimiter(redis_url=redis_url, key_prefix=key_prefix)
    app = FastAPI()

    @app.exception_handler(RateLimitExceeded)
    async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
        return JSONResponse(
            status_code=429,
            content={"error": "Rate limit exceeded", "retry_after": exc.retry_after},
            headers={"Retry-After": str(exc.retry_after)},
        )

    @app.exception_handler(RateLimitCallbackError)
    async def callback_error_handler(request: Request, exc: RateLimitCallbackError):
        return JSONResponse(status_code=503, content={"error": "callback failed"})

    @app.get("/")
    @limiter.limit(f"{limit_per_minute}/minute")
    async def index(request: Request):
        return {"msg": "ok"}

    app.state.close_limiter = limiter.close
    return app


async def build_slowapi_app(redis_url: str, limit_per_minute: int, key_prefix: str):
    from fastapi import FastAPI, Request
    from slowapi import Limiter, _rate_limit_exceeded_handler
    from slowapi.errors import RateLimitExceeded as SlowAPIRateLimitExceeded
    from slowapi.util import get_remote_address

    limiter = Limiter(
        key_func=get_remote_address,
        storage_uri=redis_url,
        key_prefix=f"{key_prefix}-",
    )
    app = FastAPI()
    app.state.limiter = limiter
    # slowapi 0.1.10's limits storage wraps a sync redis client; expose its
    # close for harness teardown (sync, so the caller must run it in a thread).
    app.state.close_limiter_sync = limiter._storage.storage.close
    app.add_exception_handler(SlowAPIRateLimitExceeded, _rate_limit_exceeded_handler)

    @app.get("/")
    @limiter.limit(f"{limit_per_minute}/minute")
    async def index(request: Request):
        return {"msg": "ok"}

    return app


async def build_fastapi_limiter_app(redis_url: str, limit_per_minute: int, key_prefix: str):
    import redis.asyncio as redis
    from fastapi import Depends, FastAPI, Request
    from fastapi_limiter.depends import RateLimiter
    from pyrate_limiter import Duration, FixedWindow, Rate, RedisBucket

    client = redis.from_url(redis_url, decode_responses=True)
    bucket = await RedisBucket.init(
        rates=[Rate(limit_per_minute, Duration.MINUTE)],
        redis=client,
        bucket_key=f"{key_prefix}-pyrate",
        algorithm=FixedWindow(),
    )
    limiter = _pyrate_limiter(bucket)
    app = FastAPI()

    @app.get("/", dependencies=[Depends(RateLimiter(limiter=limiter))])
    async def index(request: Request):
        return {"msg": "ok"}

    app.state.close_limiter = client.aclose
    return app


async def build_plain_app(redis_url: str, limit_per_minute: int, key_prefix: str):
    """Same app shape with no rate limiting: the shared baseline overhead."""
    from fastapi import FastAPI, Request

    app = FastAPI()

    @app.get("/")
    async def index(request: Request):
        return {"msg": "ok"}

    return app


def _pyrate_limiter(bucket: Any) -> Any:
    from pyrate_limiter import Limiter

    return Limiter(bucket)


APP_BUILDERS: dict[str, AppBuilder] = {
    "plain-fastapi": build_plain_app,
    "moderato": build_moderato_app,
    "slowapi": build_slowapi_app,
    "fastapi-limiter": build_fastapi_limiter_app,
}


# -- measurement --------------------------------------------------------------


async def delete_owned_keys(redis_client: Any, key_prefix: str) -> int:
    """Delete every key this run owns, identified by its unique prefix.

    Never touches anything else on the (possibly shared) database. The
    prefix ends in ``~``, which run ids cannot contain, so a run id that is
    a prefix of another (``c1`` and ``c1-b``) can never match the other's
    keys.
    """
    deleted = 0
    batch: list[str] = []
    async for key in redis_client.scan_iter(match=f"*{key_prefix}*", count=500):
        batch.append(key)
        if len(batch) >= 500:
            deleted += await redis_client.delete(*batch)
            batch = []
    if batch:
        deleted += await redis_client.delete(*batch)
    return deleted


async def measure_app(
    app: Any,
    *,
    reset_state: StateReset,
    levels: Sequence[int],
    requests_per_trial: int,
    trials: int,
    clock: Optional[Callable[[], Awaitable[tuple[int, int]]]] = None,
) -> dict[str, Any]:
    """Drive one app through the shared in-process client at each concurrency.

    ``reset_state`` runs between levels so each level starts from a fresh
    quota. ``clock`` (the Redis server's time) enables boundary gating: a
    trial straddling a minute boundary would reset a fixed-window quota
    mid-trial and skew the limited path's allow counts, so each trial waits
    for enough runway (sized from the previous trial's measured duration)
    and the run fails loudly if a trial crosses the boundary anyway.
    """
    import httpx

    async def redis_seconds() -> float:
        seconds, micros = await clock()
        return seconds + micros / 1_000_000

    async def wait_for_runway(required: float) -> None:
        while True:
            now = await redis_seconds()
            runway = math.ceil(now / 60) * 60 - now
            if runway >= required:
                return
            await asyncio.sleep(runway + 0.05)

    rows = []
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        # Warm up connection pool, routing, and any lazy script registration.
        for _ in range(20):
            (await client.get("/")).raise_for_status()

        for num_clients in levels:
            await reset_state()
            per_client = requests_per_trial // num_clients
            rates: list[float] = []
            latencies: list[float] = []
            status_counts: dict[int, int] = {}
            last_trial_seconds: Optional[float] = None

            async def worker(
                worker_id: int, count: int = per_client
            ) -> tuple[list[float], dict[int, int]]:
                measured = []
                worker_statuses: dict[int, int] = {}
                for _ in range(count):
                    start = time.perf_counter()
                    response = await client.get("/")
                    measured.append((time.perf_counter() - start) * 1000.0)
                    worker_statuses[response.status_code] = (
                        worker_statuses.get(response.status_code, 0) + 1
                    )
                return measured, worker_statuses

            level_minute: Optional[int] = None
            for _ in range(trials):
                # Require runway sized from the previous trial's measured
                # duration (10 s floor for a level's first trial); a trial
                # that crosses the boundary anyway fails the run loudly
                # instead of publishing skewed allow counts.
                if clock is not None:
                    floor_s = 10.0 if last_trial_seconds is None else last_trial_seconds * 1.5
                    # Cap below a minute: a requirement of >= 60 s of runway
                    # is unsatisfiable and would wait forever. The post-trial
                    # boundary check below still fails the run loudly.
                    await wait_for_runway(min(floor_s + 1.0, 55.0))
                    trial_minute = int(await redis_seconds()) // 60
                    if level_minute is None:
                        level_minute = trial_minute
                    elif trial_minute != level_minute:
                        raise RuntimeError(
                            "trial would start in a new minute; the level's fixed-window "
                            "quota would reset and skew the allow counts"
                        )
                start = time.perf_counter()
                trial_results = await asyncio.gather(*[worker(w) for w in range(num_clients)])
                elapsed = time.perf_counter() - start
                last_trial_seconds = elapsed
                crossed = clock is not None and int(await redis_seconds()) // 60 != level_minute
                if crossed:
                    raise RuntimeError(
                        "trial crossed a minute boundary; a fixed-window quota "
                        "would reset mid-trial and skew the allow counts"
                    )
                rates.append(requests_per_trial / elapsed)
                for chunk, worker_statuses in trial_results:
                    latencies.extend(chunk)
                    for code, count in worker_statuses.items():
                        status_counts[code] = status_counts.get(code, 0) + count

            ordered = sorted(latencies)
            rows.append(
                {
                    "clients": num_clients,
                    "throughput_req_s": _summarize(rates),
                    "latency_p50_ms": _round(_percentile(ordered, 50)),
                    "latency_p99_ms": _round(_percentile(ordered, 99)),
                    "status_counts": status_counts,
                }
            )
            print(
                f"    {num_clients:4} clients: {rows[-1]['throughput_req_s']['mean']:9,.1f}"
                f" ± {rows[-1]['throughput_req_s']['stdev']:9,.1f} req/s"
                f" | p50 {rows[-1]['latency_p50_ms']:7.3f}ms"
                f" | statuses {status_counts}"
            )
    return {"levels": rows}


def _percentile(sorted_values: Sequence[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    rank = math.ceil((pct / 100.0) * len(sorted_values))
    idx = max(0, min(len(sorted_values) - 1, rank - 1))
    return sorted_values[idx]


def _round(value: float, digits: int = 4) -> float:
    return round(value, digits)


def _summarize(values: Sequence[float]) -> dict[str, Any]:
    vals = [float(v) for v in values]
    mean = statistics.mean(vals)
    stdev = statistics.stdev(vals) if len(vals) > 1 else 0.0
    return {
        "trials": len(vals),
        "values": [_round(v) for v in vals],
        "mean": _round(mean),
        "stdev": _round(stdev),
        "cv_pct": _round((stdev / mean) * 100, 2) if mean else 0.0,
        "min": _round(min(vals)),
        "median": _round(statistics.median(vals)),
        "max": _round(max(vals)),
    }


async def measure_redis_floor(redis_client: Any, *, levels: Sequence[int], trials: int) -> dict:
    """Raw Redis cost per decision: one and two EVALSHA round trips."""
    sha = await redis_client.script_load("return 1")

    async def one_roundtrip() -> None:
        await redis_client.evalsha(sha, 0)

    async def two_roundtrips() -> None:
        await redis_client.time()
        await redis_client.evalsha(sha, 0)

    results = {}
    for label, op in (("evalsha_x1", one_roundtrip), ("evalsha_x2", two_roundtrips)):
        rows = []
        for num_clients in levels:
            per_client = REQUESTS_PER_TRIAL // num_clients
            rates: list[float] = []

            async def worker(count: int = per_client, operation: Any = op) -> None:
                for _ in range(count):
                    await operation()

            for _ in range(trials):
                start = time.perf_counter()
                await asyncio.gather(*[worker() for _ in range(num_clients)])
                rates.append(REQUESTS_PER_TRIAL / (time.perf_counter() - start))
            rows.append(_summarize(rates))
        results[label] = {
            "levels": rows,
            "at_1_client": rows[0],
            "at_100_clients": rows[-1],
        }
    return results


# -- orchestration ------------------------------------------------------------


def installed_versions() -> dict[str, str]:
    from importlib.metadata import version

    names = [
        "moderato",
        "fastapi",
        "starlette",
        "httpx",
        "redis",
        "slowapi",
        "limits",
        "fastapi-limiter",
        "pyrate-limiter",
    ]
    versions = {}
    for name in names:
        try:
            versions[name] = version(name)
        except Exception:
            versions[name] = "not installed"
    return versions


async def run_comparison(args: argparse.Namespace) -> dict[str, Any]:
    import redis.asyncio as redis

    from moderato import __version__

    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
    key_prefix = f"bench-compare-{args.run_id}~"
    started_at = datetime.now(timezone.utc)
    redis_client = redis.from_url(redis_url, decode_responses=True)
    await redis_client.ping()

    async def reset_state() -> None:
        await delete_owned_keys(redis_client, key_prefix)

    results: dict[str, Any] = {
        "schema_version": 1,
        "benchmark": "moderato-library-comparison",
        "run_id": args.run_id,
        "started_at_utc": started_at.isoformat(),
        "redis_url_host": urlparse(redis_url).hostname,
        "redis_placement": "localhost (same machine)"
        if urlparse(redis_url).hostname in ("localhost", "127.0.0.1", "::1")
        else (urlparse(redis_url).hostname or "unknown"),
        "environment": {
            "platform": platform.system(),
            "cpu_model": _cpu_model(),
            "cpu_count": _cpu_count(),
            "kernel": platform.release(),
            "python_version": platform.python_version(),
            "redis_version": (await redis_client.info("server")).get("redis_version"),
        },
        "versions": {**installed_versions(), "moderato": __version__},
        "methodology": {
            "transport": "httpx ASGITransport (in-process ASGI, no network)",
            "app_shape": (
                'single GET / returning {"msg": "ok"}, one client identity '
                "127.0.0.1 (the app keys per IP; fastapi-limiter, as wired here, "
                "uses a shared route bucket instead)"
            ),
            "requests_per_trial": REQUESTS_PER_TRIAL,
            "trials": args.trials,
            "levels": LEVELS,
            "redis_state": (
                "every library's keys are namespaced under a run-specific prefix; those "
                "keys are deleted between levels and between (library, scenario) pairs, "
                "so each level starts from a fresh quota and the database is never flushed"
            ),
            "limited_scenario_note": (
                "each level starts from a fresh quota (owned keys are deleted "
                f"before every level), so exactly the first {LIMITED_PER_MINUTE} timed "
                f"requests for each rate-limited library are allowed and the remaining "
                f"{REQUESTS_PER_TRIAL * args.trials - LIMITED_PER_MINUTE:,} are rejected "
                "with 429 (plain FastAPI is uncapped); the 20-request warm-up runs once "
                "before the first "
                "level and its quota consumption is wiped by that level's reset"
            ),
            "note": "compares libraries as-shipped per their documented usage",
        },
        "scenarios": {},
    }

    print("Measuring raw Redis floor ...")
    results["redis_floor"] = await measure_redis_floor(
        redis_client, levels=LEVELS, trials=args.trials
    )

    scenarios = {
        "allowed_1m_per_minute": 1_000_000,
        "limited_100_per_minute": LIMITED_PER_MINUTE,
    }
    try:
        for scenario, limit in scenarios.items():
            for name, builder in APP_BUILDERS.items():
                await reset_state()
                print(f"\nScenario {scenario} | {name}")
                app = await builder(redis_url, limit, key_prefix)
                measured = await measure_app(
                    app,
                    reset_state=reset_state,
                    levels=LEVELS,
                    requests_per_trial=REQUESTS_PER_TRIAL,
                    trials=args.trials,
                    clock=redis_client.time if limit == LIMITED_PER_MINUTE else None,
                )
                results["scenarios"][f"{scenario}/{name}"] = measured
                # Close each app's limiter connections so they do not pile
                # up across the eight (scenario, library) pairs.
                closer = getattr(app.state, "close_limiter", None)
                if closer is not None:
                    await closer()
                sync_closer = getattr(app.state, "close_limiter_sync", None)
                if sync_closer is not None:
                    await asyncio.to_thread(sync_closer)
    finally:
        await reset_state()
        await redis_client.aclose()

    ended_at = datetime.now(timezone.utc)
    results["ended_at_utc"] = ended_at.isoformat()
    results["duration_seconds"] = round((ended_at - started_at).total_seconds(), 2)
    return results


def _cpu_model() -> str:
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.lower().replace(" ", "").startswith("modelname"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or "unknown"


def _cpu_count() -> int:
    try:
        return len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        return os.cpu_count() or 0


async def main_async(args: argparse.Namespace) -> None:
    results = await run_comparison(args)
    output = args.json or Path("benchmarks/results") / f"compare-{args.run_id}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(results, indent=2) + "\n")
    print(f"\nMachine-readable results: {output}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Library comparison benchmark")
    parser.add_argument(
        "--venv",
        type=Path,
        default=DEFAULT_VENV,
        help="Isolated venv for competitor libraries (default: benchmarks/.venv-compare)",
    )
    parser.add_argument("--trials", type=int, default=TRIALS)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args()

    if args.run_id is None:
        args.run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    # The run id becomes part of Redis key prefixes and of the SCAN cleanup
    # patterns; reject anything that could widen cleanup beyond this run.
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", args.run_id):
        parser.error("--run-id must match [A-Za-z0-9._-]{1,64}")
    return args


if __name__ == "__main__":
    parsed = parse_args()
    target_python = ensure_venv(parsed.venv)
    # Compare paths WITHOUT resolving symlinks: both venv pythons may symlink
    # to the same base interpreter, which would wrongly skip the re-exec.
    # Both sides are made absolute so a relative --venv cannot loop forever.
    if os.path.abspath(sys.executable) != os.path.abspath(str(target_python)):
        os.execv(
            str(target_python),
            [str(target_python), str(Path(__file__).resolve()), *sys.argv[1:]],
        )
    asyncio.run(main_async(parsed))
