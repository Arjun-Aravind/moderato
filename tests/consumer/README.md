# Installed-consumer qualification

This is a separate billing API and executable E2E harness, not a pytest suite.
Build Moderato, install the distribution into a fresh environment, copy the
consumer outside the repository, and invoke Python with `-I`. Every consumer
probe verifies that Moderato comes from that interpreter's `site-packages`.
No test imports the repository's Moderato source or patches its Redis scripts.

**Use disposable local Redis only.** Set `REDIS_URL` for primary checks and,
when faults are enabled, explicitly set `FAULT_REDIS_URL` and
`ALLOW_INSTANCE_WIDE_FAULTS=disposable`. Both targets are validated as local,
unauthenticated TCP Redis URLs and recorded with server run IDs and versions.
The relay, observer, connection-kill and script-flush tests all use the fault target.
`CLIENT KILL TYPE normal` disconnects clients across **all databases**, and
`SCRIPT FLUSH` clears the **instance-wide** cache. A separate DB or key prefix
does not isolate these effects. Use a separate disposable instance if other
clients use the primary instance. Never opt in for a shared Redis server.
`outage.py` stops and starts its own named service. The consumer's
account/tier headers are synthetic authenticated identities, not an example of
safe authentication for a public service.

## One-command compatibility matrix in an orb

Prerequisites: `uv`, `poetry`, `curl`, Amp's service supervisor, and disposable
Redis listening on 127.0.0.1:6379. Python interpreters can be installed with
`uv python install 3.10 3.11 3.12 3.13 3.14`.

```sh
poetry build
bash tests/consumer/matrix.sh "$PWD/.amp/in/artifacts/qualification/rerun"
```

The matrix covers Python 3.10–3.14 with resolvable current dependencies, a minimum
compatible integration set on Python 3.10, and Starlette 0.46.2 on Python 3.12.
It installs the wheel into seven fresh environments, verifies dependency consistency, runs a real Uvicorn
server, retains JSON results, import paths, exact dependency versions, Lua
hashes, HTTP/Prometheus observations, and server logs. Environments are retained
in `/tmp` and their path is recorded. Use `PROFILES='minimum transitional'` for
a subset. `STARLETTE_VERSION` deliberately overrides the transitional version
for regression reproduction; 0.46.0/0.46.1 must fail resolution with the fixed
distribution. `matrix.sh` returns nonzero on installation, startup, or probe
failure. Its sampled version combinations are not a Cartesian-product guarantee.

The minimum integration profile uses Pydantic 2.0.2 (FastAPI 0.100.0 excludes
2.0.0/2.0.1) and AnyIO 3.6.2; it is not a claim that old native dependencies build
on Python 3.14. The source CI separately checks the redis-py 5.0.0 floor on all
supported interpreters. See the [support policy](../../README.md#python-support-policy).
When running the source suite with this minimum integration set, install the
repository's HTTPX 0.25.2 pin: Starlette 0.27's `TestClient` is incompatible with
HTTPX 0.28+. The live TCP consumer does not use `TestClient` and can use current
HTTPX. This is a test-client dependency incompatibility, not a skipped assertion.

For exactly repeatable dependency versions, reinstall the versions from a run's
`environment.dependencies`, rather than resolving the latest versions again.
The package version alone is insufficient: different builds can report the same version.
The probe and server automatically hash the local installed wheel and verify its
runtime files against site-packages. Keep the installation wheel available.
The probe requires `QUALIFICATION_PROFILE`, `EXPECTED_APP_PREFIX` and
`EXPECTED_SERVER_ENV` for HTTP runs, and rejects the wrong server profile,
application prefix, environment prefix or wheel hash. The matrix sets these
automatically. `provenance.py` must accompany the copied app and probe.

## Full fault, transport, and soak run

```sh
uv venv /tmp/moderato-consumer --python 3.12
uv pip install --python /tmp/moderato-consumer/bin/python \
  "moderato[all] @ file://$PWD/dist/moderato-0.6.1-py3-none-any.whl" httpx uvicorn
mkdir -p /tmp/moderato-consumer-app
cp tests/consumer/{app,probe,provenance}.py /tmp/moderato-consumer-app/
prefix="qualification-$(cat /proc/sys/kernel/random/uuid)"
mkdir -p "$PWD/.amp/in/artifacts/qualification"
amp orb service start qualification-http --command \
  "env REDIS_URL=redis://127.0.0.1:6379/12 QUALIFICATION_PROFILE=full QUALIFICATION_PREFIX=$prefix /tmp/moderato-consumer/bin/python -I -m uvicorn app:app --app-dir /tmp/moderato-consumer-app --port 8091"
# Only opt in if this entire Redis instance is disposable, including other DBs.
env REDIS_URL=redis://127.0.0.1:6379/12 QUALIFICATION_PREFIX="$prefix-probe" \
  FAULT_REDIS_URL=redis://127.0.0.1:6379/13 ALLOW_INSTANCE_WIDE_FAULTS=disposable \
  QUALIFICATION_PROFILE=full EXPECTED_APP_PREFIX="$prefix" EXPECTED_SERVER_ENV=/tmp/moderato-consumer \
  HTTP_URL=http://127.0.0.1:8091 SOAK_SECONDS=120 \
  RESULT_PATH="$PWD/.amp/in/artifacts/qualification/full.json" \
  /tmp/moderato-consumer/bin/python -I /tmp/moderato-consumer-app/probe.py
amp orb service logs qualification-http -n 2000
amp orb service stop qualification-http
```

Create the output directory before running. Use a fresh application prefix for
each run: the global ASGI middleware quota persists in Redis. Outside an orb,
run the same Uvicorn command in a terminal and stop it when finished; `probe.py`
does not require Amp. It connects to the test server over real TCP HTTP/1.1.

The probe covers weighted admission/denial/permanent denial; invalid costs;
tenant, scope and policy isolation; exceptions and nonthrowing decisions;
usage, selective/all resets; 300 concurrent attempts per algorithm; wall-clock
retry recovery; Redis types/TTL; sync/async decorators, stacked limits, dynamic
mounts, 429/422/503 headers, raw ASGI middleware, fail-open header suppression;
real blackholes and lost replies under both policies for all algorithms;
cancellation, warm-pool exhaustion, connection loss, NOSCRIPT fallback;
streaming-error observations; metrics aggregation and bounded soak.

Fault injection uses a TCP relay on an ephemeral loopback port. `blackhole`
discards commands and replies; `lost-reply` forwards commands but discards Redis
replies. An independent client checks whether quota was charged, then the relay
recovers and verifies the next charge. One-second timeouts are configured for
those tests. Socket timeouts are **not total operation deadlines**: a cold
redis-py connection handshake under the pool lock can queue other callers.

Optional `UNIX_REDIS_URL` and `TLS_REDIS_URL` exercise real transports. Example
disposable Redis TLS setup (requires a TLS-enabled Redis build):

```sh
mkdir -p /tmp/moderato-tls
openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout /tmp/moderato-tls/key.pem -out /tmp/moderato-tls/cert.pem -days 1 \
  -subj '/CN=localhost' -addext 'subjectAltName=DNS:localhost,IP:127.0.0.1'
amp orb service start qualification-redis-transports --command \
  "redis-server --bind 127.0.0.1 --port 0 --tls-port 6391 --tls-cert-file /tmp/moderato-tls/cert.pem --tls-key-file /tmp/moderato-tls/key.pem --tls-ca-cert-file /tmp/moderato-tls/cert.pem --tls-auth-clients no --unixsocket /tmp/moderato-tls/redis.sock --save '' --appendonly no"
# Add to the probe environment:
# UNIX_REDIS_URL=unix:///tmp/moderato-tls/redis.sock
# TLS_REDIS_URL='rediss://localhost:6391?ssl_ca_certs=/tmp/moderato-tls/cert.pem&ssl_cert_reqs=required'
```

## Multiple workers and actual Prometheus scraping

Start Uvicorn with `--workers 2 --port 8093`, a fresh prefix, and a fresh empty
`PROMETHEUS_MULTIPROC_DIR` set **before imports**. The sample app selects
`MultiProcessCollector` when this variable is present. Run the probe with
`HTTP_URL=http://127.0.0.1:8093 FAULTS=0 EXPECT_MULTIPROCESS=1`, with matching
`QUALIFICATION_PROFILE`, `EXPECTED_APP_PREFIX` and `EXPECTED_SERVER_ENV`. The test requires
responses from multiple PIDs and an exact aggregated increment of 40 checks
across ten scrapes. Without multiprocess configuration it records process-local
counter variation; this is an observation, not a claim that aggregation works.
Directory lifecycle belongs to the application, not Moderato.

Run Prometheus using `prometheus.yml` against the single/multiworker apps:

```sh
amp orb service start qualification-prometheus --command \
  'prometheus --config.file=/ABSOLUTE/PATH/tests/consumer/prometheus.yml --storage.tsdb.path=/tmp/moderato-prometheus-data --web.listen-address=127.0.0.1:9091'
curl -fsS http://127.0.0.1:9091/api/v1/targets
curl -fsSG --data-urlencode 'query=moderato_checks_total{job="moderato-consumer"}' \
  http://127.0.0.1:9091/api/v1/query
```

Wait for both targets to be `up`; startup's empty target list is not success.
These loopback URLs are automation inputs, not user-accessible orb portals.

## Outages, rolling versions, and extras

* Start `qualification-outage` as `redis-server --bind 127.0.0.1 --port 6394
  --save '' --appendonly no`, then run `outage.py` with isolated installed Python
  and `REDIS_URL=redis://127.0.0.1:6394/0`. It keeps clients alive across the outage,
  verifies manual failure policy and recovery, and demonstrates quota loss on a
  nonpersistent restart. Entry requires the dedicated service to be running;
  cleanup restores that running state even if the outage checks fail or are
  cancelled, and surfaces a restoration failure rather than silently succeeding.
  This restores availability, not lost data. Stop this service afterwards.
* Build old tags with `git archive TAG | tar -x -C EMPTY_DIRECTORY`, then
  `uv build EMPTY_DIRECTORY`. Install old and current wheels separately. Run
  `REDIS_URL=redis://127.0.0.1:6379/12 python -I tests/consumer/rolling.py
  OLD_PYTHON CURRENT_PYTHON UNIQUE_PREFIX`. The supported comparisons are v0.4.0
  and v0.5.0 to current. v0.4.0 token state is deliberately isolated from current;
  fixed/sliding state and v0.5.0 token state are shared. This is an alternating
  process compatibility test, not an orchestrated rolling deployment.
* Run `tests/package_smoke.py` after a build for base/all wheel and sdist installs.
  For independent extras, create fresh environments with either `[fastapi]` or
  `[metrics]` and run `python -I tests/consumer/extras.py fastapi` (or `metrics`).

## Evidence interpretation

A `pass` on `streaming-observation` means the failure behavior was observed,
not that the stream completed correctly. The same applies to process-local
metrics observations and the intentional token-bucket migration discontinuity.
Preserve failures, then name whether they were library defects, unsupported
combinations, documented deployment requirements, or harness setup mistakes.

This harness does not qualify Redis Cluster/Sentinel, a pure-ASGI rewrite,
replication failover, production deployment automation, long-duration leaks,
every dependency combination, every protocol/server, or deprecated educational
algorithm classes as production APIs. Source-suite coverage supplements the
consumer evidence; it does not replace it.
