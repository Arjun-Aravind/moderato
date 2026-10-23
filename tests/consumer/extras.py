"""Check independently selectable extras in a clean installed environment."""

import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

import moderato


async def main():
    assert Path(moderato.__file__).is_relative_to(Path(sys.prefix))
    extra = sys.argv[1]
    framework = extra in ("fastapi", "all")
    metrics = extra in ("metrics", "all")
    assert bool(importlib.util.find_spec("starlette")) == framework
    assert bool(importlib.util.find_spec("fastapi")) == framework
    assert bool(importlib.util.find_spec("prometheus_client")) == metrics
    assert (moderato.RateLimitHeadersMiddleware is not None) == framework
    assert hasattr(moderato, "RateLimitMetrics") == metrics
    async with moderato.RateLimiter(os.environ["REDIS_URL"], enable_metrics=metrics) as limiter:
        for algorithm in ("fixed_window", "token_bucket", "sliding_window"):
            key = uuid4().hex
            assert await limiter.check(key, "1/day", algorithm=algorithm)
            assert not (await limiter.check_with_info(key, "1/day", algorithm=algorithm)).allowed
            assert await limiter.reset(key)
    print(json.dumps({"status": "pass", "extra": extra, "package": moderato.__file__}))


if __name__ == "__main__":
    asyncio.run(main())
