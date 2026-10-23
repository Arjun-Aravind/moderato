"""Restart a disposable, nonpersistent Redis while consumer clients stay alive.

Requires amp's local service supervisor. Never target shared Redis.
"""

import asyncio
import json
import os
import time

from moderato import BackendError, RateLimiter


async def service(action):
    command = ["amp", "orb", "service", action, "qualification-outage"]
    if action == "start":
        command.extend(
            ["--command", "redis-server --bind 127.0.0.1 --port 6394 --save '' --appendonly no"]
        )
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await process.communicate()
    assert process.returncode == 0, output.decode()


async def main():
    url = os.environ["REDIS_URL"]
    assert url == "redis://127.0.0.1:6394/0", "Dedicated disposable service only"
    closed = RateLimiter(url, key_prefix="qualification-outage")
    opened = RateLimiter(url, key_prefix="qualification-outage", fail_open=True)
    observations = []
    needs_restore = False
    try:
        for limiter in (closed, opened):
            limiter.config.connection_timeout = 1
            limiter.config.socket_timeout = 1
            await limiter.connect()
        for algorithm in ("fixed_window", "token_bucket", "sliding_window"):
            assert await closed.check(algorithm, "1/day", algorithm=algorithm)
        # The entry contract requires a running service. Mark before awaiting
        # stop so even cancellation or an uncertain supervisor result restores it.
        needs_restore = True
        await service("stop")
        for algorithm in ("fixed_window", "token_bucket", "sliding_window"):
            started = time.monotonic()
            try:
                await closed.check(algorithm, "1/day", algorithm=algorithm)
            except BackendError:
                pass
            else:
                raise AssertionError("fail closed admitted during outage")
            bypass = await opened.check_with_info(algorithm, "1/day", algorithm=algorithm)
            assert bypass.allowed and bypass.remaining is None
            for operation in (
                opened.get_usage(algorithm, "1/day", algorithm=algorithm),
                opened.reset(algorithm),
            ):
                try:
                    await operation
                except BackendError:
                    pass
                else:
                    raise AssertionError("administrative API bypassed backend failure")
            observations.append(
                {"algorithm": algorithm, "outage_seconds": time.monotonic() - started}
            )
        await service("start")
        for _ in range(50):
            if await closed.health_check():
                break
            await asyncio.sleep(0.1)
        assert await closed.health_check()
        needs_restore = False
        for algorithm in ("fixed_window", "token_bucket", "sliding_window"):
            # This server has no persistence. Restart loses quota, even fail-closed.
            assert await closed.check(algorithm, "1/day", algorithm=algorithm)
            assert not (
                await opened.check_with_info(algorithm, "1/day", algorithm=algorithm)
            ).allowed
        print(
            json.dumps(
                {
                    "status": "pass",
                    "observations": observations,
                    "restart_lost_quota": True,
                    "clients_recovered": True,
                },
                indent=2,
            )
        )
    finally:
        try:
            if needs_restore:
                await service("start")
                for _ in range(50):
                    if await closed.health_check():
                        break
                    await asyncio.sleep(0.1)
                assert await closed.health_check(), "Failed to restore entry service state"
        finally:
            await asyncio.gather(closed.close(), opened.close())


if __name__ == "__main__":
    asyncio.run(main())
