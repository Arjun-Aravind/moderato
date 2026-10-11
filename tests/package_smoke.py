"""Verify built distributions on Python 3.12, not the source checkout."""

import asyncio
import os
import subprocess
import sys
import tarfile
import tempfile
import venv
import zipfile
from email.parser import BytesParser
from pathlib import Path
from uuid import uuid4

import tomllib


async def installed(version, extras):
    from importlib.metadata import version as installed_version

    import moderato

    assert Path(moderato.__file__).is_relative_to(Path(sys.prefix))
    assert moderato.__version__ == installed_version("moderato") == version
    assert (Path(moderato.__file__).parent / "py.typed").is_file()
    assert (moderato.RateLimitHeadersMiddleware is not None) == extras
    assert hasattr(moderato, "RateLimitMetrics") == extras
    if extras:
        from fastapi import FastAPI
        from prometheus_client import generate_latest

        app = FastAPI()
        app.add_middleware(moderato.RateLimitHeadersMiddleware)
        first = moderato.RateLimiter(enable_metrics=True)
        second = moderato.RateLimiter(enable_metrics=True)
        assert first.metrics is second.metrics
        first.metrics.record_check("fixed_window", True)
        assert b"moderato_checks_total" in generate_latest()
    else:
        for options in ({"enable_metrics": True}, {"fail_open": True}):
            try:
                moderato.RateLimiter(**options)
            except moderato.RateLimitConfigError as exc:
                assert "moderato[metrics]" in str(exc)
            else:
                raise AssertionError("metrics must require the extra")

    limiter = moderato.RateLimiter(redis_url=os.environ["REDIS_URL"])
    await limiter.close()  # Closing before connecting is safe.
    async with limiter:
        await limiter.connect()  # Idempotent.
        assert await limiter.health_check()
        for algorithm in ("fixed_window", "token_bucket", "sliding_window"):
            key = f"package-smoke-{uuid4().hex}"
            try:
                assert await limiter.check(key, "2/day", algorithm=algorithm)
                denied = await limiter.check_with_info(key, "2/day", algorithm=algorithm, cost=2)
                assert not denied.allowed and denied.remaining == 1
                assert denied.retry_after > 0 and denied.reset_at is not None
                assert await limiter.check(key, "2/day", algorithm=algorithm)
                try:
                    await limiter.check(key, "2/day", algorithm=algorithm)
                except moderato.RateLimitExceeded:
                    pass
                else:
                    raise AssertionError(f"{algorithm} failed to enforce quota")
                permanent = await limiter.check_with_info(key, "2/day", algorithm=algorithm, cost=3)
                assert not permanent.allowed
                assert permanent.retry_after is None and permanent.reset_at is None
            finally:
                await limiter.reset(key, algorithm=algorithm)
    await limiter.close()
    await limiter.connect()  # Reconnect after context-manager cleanup.
    assert await limiter.health_check()
    await limiter.close()


def verify():
    root = Path(__file__).resolve().parents[1]
    config = tomllib.loads((root / "pyproject.toml").read_text())["tool"]
    version = config["poetry"]["version"]
    assert config["commitizen"]["version"] == version
    required = {"moderato/py.typed"} | {
        f"moderato/scripts/{name}.lua"
        for name in ("fixed_window", "token_bucket", "sliding_window")
    }
    artifacts = [
        root / "dist" / f"moderato-{version}{suffix}" for suffix in ("-py3-none-any.whl", ".tar.gz")
    ]
    for artifact in artifacts:
        if artifact.suffix == ".whl":
            with zipfile.ZipFile(artifact) as archive:
                assert required <= set(archive.namelist())
                assert any(
                    name.startswith(f"moderato-{version}.dist-info/") and name.endswith("/LICENSE")
                    for name in archive.namelist()
                )
                metadata = archive.read(f"moderato-{version}.dist-info/METADATA")
        else:
            with tarfile.open(artifact) as archive:
                prefix = f"moderato-{version}/"
                assert {prefix + name for name in required} <= set(archive.getnames())
                assert prefix + "LICENSE" in archive.getnames()
                metadata = archive.extractfile(prefix + "PKG-INFO").read()
        assert BytesParser().parsebytes(metadata)["Version"] == version
        for extras in (False, True):
            with tempfile.TemporaryDirectory(prefix="moderato-package-") as directory:
                env = Path(directory) / "venv"
                venv.create(env, with_pip=True, symlinks=True)
                python = str(env / "bin/python")
                requirement = f"moderato{'[all]' if extras else ''} @ {artifact.as_uri()}"
                subprocess.run([python, "-m", "pip", "install", requirement], check=True)
                subprocess.run([python, "-m", "pip", "check"], check=True)
                subprocess.run(
                    [python, "-I", str(Path(__file__).resolve()), version, str(int(extras))],
                    cwd=directory,
                    check=True,
                )
                print(f"{artifact.name}, extras={extras}: OK", flush=True)


if __name__ == "__main__":
    if len(sys.argv) == 3:
        asyncio.run(installed(sys.argv[1], bool(int(sys.argv[2]))))
    else:
        verify()
