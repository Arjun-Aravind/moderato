"""Alternate old/current/rollback interpreters against retained Redis state.

Usage: python rolling.py OLD_PYTHON CURRENT_PYTHON PREFIX
Run against disposable Redis only. This checks rolling state compatibility,
not deployment automation, failover, or persistence guarantees.
"""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path


async def worker():
    import moderato

    assert Path(moderato.__file__).is_relative_to(Path(sys.prefix))
    async with moderato.RateLimiter(os.environ["REDIS_URL"], key_prefix=sys.argv[2]) as limiter:
        rows = []
        for algorithm in ("fixed_window", "token_bucket", "sliding_window"):
            result = await limiter.check_with_info(
                "account", "2/day", algorithm=algorithm, cost=int(sys.argv[3])
            )
            rows.append(
                {"algorithm": algorithm, "allowed": result.allowed, "remaining": result.remaining}
            )
        print(
            json.dumps(
                {"version": moderato.__version__, "package": moderato.__file__, "decisions": rows}
            )
        )


def run():
    old, current, prefix = sys.argv[1:]
    rows = []
    for phase, interpreter, cost in (("old", old, 2), ("new", current, 1), ("rollback", old, 1)):
        output = subprocess.check_output(
            [interpreter, "-I", str(Path(__file__).resolve()), "worker", prefix, str(cost)],
            cwd="/tmp",
            text=True,
        )
        rows.append({"phase": phase, **json.loads(output)})
    assert all(r["allowed"] for r in rows[0]["decisions"])
    expected = [False, rows[0]["version"] == "0.4.0", False]
    assert [r["allowed"] for r in rows[1]["decisions"]] == expected
    assert not any(r["allowed"] for r in rows[2]["decisions"])
    print(
        json.dumps(
            {"status": "pass", "phases": rows, "token_bucket_quota_shared": not expected[1]},
            indent=2,
        )
    )


if __name__ == "__main__":
    if sys.argv[1] == "worker":
        asyncio.run(worker())
    else:
        run()
