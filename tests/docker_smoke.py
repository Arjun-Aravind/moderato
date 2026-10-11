"""Exercise the running Docker demos before, during, and after a Redis outage."""

import json
import os
import sys
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4


def request(port, path, headers=None):
    try:
        response = urlopen(
            Request(f"http://127.0.0.1:{port}{path}", headers=headers or {}), timeout=5
        )
    except HTTPError as error:
        response = error
    with response:
        return response.status, json.load(response), response.headers


def verify(phase):
    ports = [os.getenv("APP_PORT", "8000"), os.getenv("TENANT_PORT", "8001")]
    for port in ports:
        status, body, _ = request(port, "/api/status")
        assert status == 200 and body["redis_connected"] == (phase != "outage")
    policies = [(ports[0], "/api/limited", {}), (ports[1], "/api/data", {"X-API-Key": "key-001"})]
    for port, path, headers in policies:
        if phase == "healthy":
            first, _, _ = request(port, path, headers)
            assert first == 200
            # These minute-window demos must eventually deny this burst even
            # if the burst crosses a boundary; exact accounting is tested by pytest.
            results = [request(port, path, headers) for _ in range(25)]
            assert all(status in (200, 429) for status, _, _ in results)
            denials = [headers for status, _, headers in results if status == 429]
            assert denials and all(int(headers["Retry-After"]) > 0 for headers in denials)
        elif phase == "outage":
            status, _, response_headers = request(port, path, headers)
            assert status == 503
            assert not any(
                name.lower().startswith("x-ratelimit") or name.lower() == "retry-after"
                for name in response_headers
            )
    if phase == "recovery":
        assert request(ports[0], f"/api/user/{uuid4().hex}")[0] == 200
        assert request(ports[1], "/api/data", {"X-API-Key": "key-003"})[0] == 200
    print(f"Docker HTTP {phase}: OK")


if __name__ == "__main__":
    phase = sys.argv[1]
    assert phase in ("healthy", "outage", "recovery")
    verify(phase)
