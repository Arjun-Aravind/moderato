"""A separate billing API consuming only the installed Moderato distribution."""

import os
import runpy
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest, multiprocess
from starlette.responses import Response, StreamingResponse

from moderato import BackendError, RateLimiter, RateLimitHeadersMiddleware
from moderato.decorators import RateLimitMiddleware

PROVENANCE = runpy.run_path(str(Path(__file__).with_name("provenance.py")))["installed"]()

limiter = RateLimiter(
    redis_url=os.environ["REDIS_URL"],
    key_prefix=os.environ["QUALIFICATION_PREFIX"],
    enable_metrics=True,
)
limiter.config.socket_timeout = 1
limiter.config.connection_timeout = 1
open_limiter = RateLimiter(redis_url="redis://127.0.0.1:1", fail_open=True)
closed_limiter = RateLimiter(redis_url="redis://127.0.0.1:1")


@asynccontextmanager
async def lifespan(app):
    yield
    for instance in (limiter, open_limiter, closed_limiter):
        await instance.close()


app = FastAPI(lifespan=lifespan)
app.add_middleware(RateLimitHeadersMiddleware)


def identity(request):
    return request.headers["x-account"]


def tier(request):
    return request.headers.get("x-tier", "free")


@app.get("/health")
async def health():
    import moderato

    return {
        "pid": os.getpid(),
        "package": moderato.__file__,
        "version": moderato.__version__,
        **PROVENANCE,
    }


@app.get("/metrics")
async def metrics():
    registry = None
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
    body = generate_latest(registry) if registry else generate_latest()
    return Response(body, media_type=CONTENT_TYPE_LATEST)


def register_algorithm(algorithm):
    @app.get(f"/billing/{algorithm}/{{invoice}}")
    @limiter.limit(
        "7/day",
        algorithm=algorithm,
        key=identity,
        tenant_type=tier,
        cost=lambda request: int(request.headers.get("x-cost", "1")),
    )
    async def invoice(request: Request, invoice: str):
        return {"invoice": invoice, "pid": os.getpid()}


for name in ("fixed_window", "token_bucket", "sliding_window"):
    register_algorithm(name)


@app.get("/stack")
@limiter.limit("3/day", key=identity, scope="outer")
@limiter.limit("2/day", key=identity, scope="inner")
async def stack(request: Request):
    return {"ok": True}


@app.get("/sync")
@limiter.limit("1/day", key=identity)
def sync(request: Request):
    return {"ok": True}


@app.get("/open")
@open_limiter.limit("1/day", key=identity)
async def opened(request: Request):
    return {"ok": True}


@app.get("/closed")
@closed_limiter.limit("1/day", key=identity)
async def closed(request: Request):
    return {"ok": True}


@app.get("/stream")
async def stream():
    async def body():
        yield b"partial\n"
        raise BackendError("injected stream failure before completion marker")

    return StreamingResponse(body())


child = FastAPI()
child.add_middleware(RateLimitHeadersMiddleware)


@child.get("/items/{item}")
@limiter.limit("2/day", key=identity)
async def item(request: Request, item: str):
    return {"item": item}


app.mount("/shops/{shop}", child)
raw = FastAPI()


@raw.get("/ok")
async def raw_ok():
    return {"ok": True}


raw.add_middleware(RateLimitMiddleware, limiter=limiter, default_rate="2/day")
app.mount("/raw", raw)
