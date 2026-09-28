# Benchmarks

Everything in this file comes from actual runs executed on the committed
harness. The raw, machine-readable results for each published run are
committed next to it under [`benchmarks/results/`](benchmarks/results/) —
every number below can be traced to a JSON file named in its caption. No
number on this page is a marketing target.

- Harness: [`benchmarks/performance.py`](benchmarks/performance.py) (moderato alone)
- Library comparison: [`benchmarks/compare_libraries.py`](benchmarks/compare_libraries.py)
  (head-to-head, run in an isolated venv)

## How to reproduce

```bash
# Requires Redis on localhost:6379 (or REDIS_URL)
poetry install --with benchmarks
poetry run python benchmarks/performance.py                 # ~50 s
poetry run python benchmarks/compare_libraries.py           # ~90 s, bootstraps its own venv
```

Each run writes `benchmarks/results/<kind>-<run-id>.json`. Re-running with
the same `--run-id` replays the same deterministic Redis key layout; the
harness sweeps its own keys before and after every run.

## Environment

All runs below were executed on the same machine in one session:

| Component | Value |
| --- | --- |
| CPU | Intel(R) Xeon(R) Processor @ 2.60GHz, 2 vCPU |
| RAM | 3.8 GiB |
| Kernel | Linux 6.1.158+ |
| Python | CPython 3.12.14 |
| Redis | 7.0.15, standalone, `localhost` (same machine) |
| moderato | 0.3.0 |

This is a shared sandbox: run-to-run dips of 10–20% on individual
concurrency levels are visible in the raw data. Treat fine-grained rankings
with corresponding caution; the pattern-level conclusions are stable across
runs.

## Methodology

- **Trials and variance.** Every scenario runs multiple trials; each number
  is reported as mean ± sample standard deviation across trials, with the
  raw per-trial values in the JSON.
- **Deterministic run keys.** All Redis keys derive from the run id
  (`ratelimit:bench%3A<run-id>%3A…`), swept before and after each run.
- **Memory is measured, not estimated.** Per-key memory comes from Redis'
  `MEMORY USAGE` over thousands of freshly created keys.
- **Accuracy is compared to analytic expectations.** A burst of 4× the
  limit against a fresh key must land inside the analytically expected
  band (see [Accuracy under load](#accuracy-under-load)).
- **Connection pool disclosure.** The shipped default pool
  (`RateLimitConfig.max_connections = 50`) can reject in-flight checks
  under deep bursts (see [Default pool capacity](#default-pool-capacity)).
  The concurrency sections therefore run with the pool raised to 200 via
  the same public config field, so those tables measure the limiter rather
  than the pool; the default-pool behavior is measured separately and not
  hidden.
- **Latency semantics.** Latency is measured per request around the await,
  so concurrent scenarios report per-request latency including
  client-side queueing.

## Results — moderato alone

Raw data: `benchmarks/results/full-20260928T164500Z-full1.json` (run 1) and
`benchmarks/results/full-20260928T165500Z-full2.json` (run 2). Each run is
a complete, independent execution of the whole suite.

### Throughput (limiter only, no HTTP)

3,000 requests per trial, high limit (`100000/minute`) so nothing is denied.

| Run | Sequential (req/s) | Concurrent (req/s) | Speedup |
| --- | --- | --- | --- |
| 1 | 3,477 ± 138 (CV 4.0%) | 4,295 ± 184 (CV 4.3%) | 1.24× |
| 2 | 3,581 ± 134 (CV 3.7%) | 4,307 ± 142 (CV 3.3%) | 1.20× |

Per-trial values are in the JSON. Sequential and concurrent throughput are
stable across the two runs to within ~3% and ~0.3% respectively.

### Latency

2,000 sequential samples × 3 trials per run, percentiles pooled across
trials:

| Run | p50 | p90 | p95 | p99 | p99.9 | max |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 0.266 ms | 0.333 ms | 0.358 ms | 0.427 ms | 0.591 ms | 1.175 ms |
| 2 | 0.294 ms | 0.344 ms | 0.364 ms | 0.411 ms | 0.543 ms | 1.321 ms |

### Concurrency sweep

100 requests per client, 3 trials per level. Throughput plateaus around
4.4–5.0k req/s from 5 clients onward while per-request latency grows
roughly linearly with concurrency — the single-process client saturates
before Redis does (the [Redis floor](#redis-floor) is several times
higher).

| Clients | Run 1 req/s | Run 1 p50/p99 | Run 2 req/s | Run 2 p50/p99 |
| --- | --- | --- | --- | --- |
| 1 | 3,903 ± 4.7% | 0.25 / 0.38 ms | 3,240 ± 9.8% | 0.31 / 0.42 ms |
| 2 | 4,718 ± 3.6% | 0.41 / 0.65 ms | 4,655 ± 1.1% | 0.42 / 0.60 ms |
| 5 | 4,940 ± 1.2% | 0.99 / 1.38 ms | 4,963 ± 1.8% | 0.99 / 1.24 ms |
| 10 | 5,007 ± 1.2% | 1.95 / 2.75 ms | 4,983 ± 1.4% | 1.97 / 2.57 ms |
| 25 | 4,727 ± 5.6% | 4.91 / 9.25 ms | 4,932 ± 1.0% | 4.97 / 6.67 ms |
| 50 | 4,592 ± 1.2% | 10.7 / 13.4 ms | 4,745 ± 11.1% | 9.90 / 22.9 ms |
| 100 | 4,438 ± 3.6% | 22.0 / 28.3 ms | 4,197 ± 4.8% | 22.6 / 42.7 ms |
| 200 | 4,451 ± 1.2% | 44.5 / 51.3 ms | 4,428 ± 0.8% | 44.5 / 60.8 ms |

### Algorithm comparison

1,000 sequential samples × 3 trials per algorithm. On this workload the
three algorithms cost about the same; all execute one Lua script per
decision.

| Algorithm | Run 1 throughput | Run 1 p50/p99 | Run 2 throughput | Run 2 p50/p99 |
| --- | --- | --- | --- | --- |
| fixed_window | 3,407 ± 328 req/s | 0.29 / 0.58 ms | 3,252 ± 209 req/s | 0.30 / 0.57 ms |
| token_bucket | 3,453 ± 33 req/s | 0.28 / 0.41 ms | 3,205 ± 89 req/s | 0.32 / 0.45 ms |
| sliding_window | 3,458 ± 140 req/s | 0.28 / 0.41 ms | 3,215 ± 70 req/s | 0.31 / 0.45 ms |

### Memory per key

4,000 fresh identities per algorithm at `100/minute`, measured with
`MEMORY USAGE` over every key the run created. Values include the key
name, value encoding, and Redis' per-entry bookkeeping.

| Algorithm | Keys per identity | Bytes per key |
| --- | --- | --- |
| fixed_window | 1.0 | 152.0 ± 0.0 |
| token_bucket | 1.0 | 200.0 ± 0.0 |
| sliding_window | 1.0 | 168.0 ± 0.0 |

Identical across both runs. Token bucket stores the most state per key
(tokens and last-refill timestamp in a small hash).

### Accuracy under load

A burst of 200 concurrent checks (4× the limit) against a fresh key at
`50/second`:

| Algorithm | Allowed (run 1) | Allowed (run 2) | Analytic band | Pass |
| --- | --- | --- | --- | --- |
| fixed_window | 50 | 50 | [50, 50] | ✅ both |
| token_bucket | 51 | 51 | [50, 50 + refill·span + 1] | ✅ both |
| sliding_window | 50 | 50 | [50, 50] | ✅ both |

The token bucket legitimately allows more than 50: it refills continuously
during the burst, so the analytic expectation is `50 + refill_rate ×
burst_span`, and 51 is inside that band. Demanding exactly 50 here — as
this benchmark once did — is a bug in the check, not in the library.
Fixed and sliding windows refill nothing mid-window and are held to exactly
50 while the burst stays inside one window.

### Multi-tenant

100 tenants × 100 requests × 3 trials, tier mix 70/25/5
(free/premium/enterprise): 4,486 ± 92 req/s (run 1), 4,364 ± 46 req/s
(run 2) — consistent with the plain concurrency sweep.

### Default pool capacity

The shipped default pool rejects a Redis borrow once 50 connections are in
use (redis-py's non-blocking asyncio pool raises `Too many connections`).
A dedicated probe (1,000 concurrent checks per level, default pool) did
**not** trip this boundary at 25–200 concurrent in either published run —
but in an earlier development run, the 100-client sweep level did abort
with exactly that error. The boundary is timing-dependent: bursts that
sustain more than ~50 truly in-flight checks can raise
`moderato.exceptions.BackendError` as shipped. Applications expecting deep
concurrency should raise `RateLimitConfig.max_connections`.

## Head-to-head vs slowapi and fastapi-limiter

Raw data: `benchmarks/results/compare-20260928T164500Z.json` (run 1) and
`benchmarks/results/compare-20260928T164701Z.json` (run 2).

**What is identical for everyone:** the same minimal FastAPI app shape
(one `GET /` returning a small JSON body, per-IP keying, client
`127.0.0.1`), the same in-process httpx client (`ASGITransport`, no
network), the same Redis instance, `FLUSHDB` before each (library,
scenario) so every scenario starts empty, 1,000 requests per trial × 3
trials per concurrency level.

**What is per-library (as-shipped, documented usage):**

| Library | Version | Configured as |
| --- | --- | --- |
| moderato | 0.3.0 | `RateLimiter.limit()` decorator + documented `RateLimitExceeded` handler; async redis-py, Lua scripts |
| slowapi | 0.1.10 (limits 5.8.0) | `Limiter(key_func=get_remote_address, storage_uri="redis://…")` + `limiter.limit()` |
| fastapi-limiter | 0.2.0 (pyrate-limiter 4.5.0) | `RateLimiter` dependency over pyrate-limiter's `RedisBucket` (fixed-window algorithm) |
| plain FastAPI | 0.141.1 | Same app shape, no limiter — shared baseline |
| Redis floor | — | 1× and 2× `EVALSHA` per request, no HTTP stack |

This compares implementations as-shipped, not theoretical optima. Two
as-shipped facts worth knowing:

- **slowapi cannot use async Redis as shipped.** Its documented
  `redis://` storage URI instantiates limits' *sync* Redis client inside
  the async endpoint (verified: `limits.storage.redis.RedisStorage`
  wrapping `redis.client.Redis`). limits' async scheme
  (`async+redis://`) needs `coredis`, and with it installed slowapi still
  fails its own strategy assertion — so the sync client is its only
  working Redis mode, and it is what was benchmarked.
- **fastapi-limiter's documented usage is in-memory.** Its README's
  `Limiter(Rate(...))` never touches Redis; to honor the same-Redis
  workload it was wired to pyrate-limiter's documented `RedisBucket`.
  Its documented limiter also uses one shared bucket per route rather
  than one per client identity — with the single-identity workload used
  here, both behave identically.

### Allowed path (limit 1,000,000/minute — nothing denied)

| Clients | plain FastAPI | moderato | slowapi | fastapi-limiter |
| --- | --- | --- | --- | --- |
| **Run 1** | | | | |
| 1 | 3,560 | 1,310 | 1,590 | 1,500 |
| 10 | 3,680 | 1,480 | 1,660 | 1,290 |
| 50 | 3,700 | 1,420 | 1,410 | 1,360 |
| 100 | 3,650 | 1,310 | 1,360 | 1,370 |
| **Run 2** | | | | |
| 1 | 3,770 | 1,080 | 1,350 | 1,220 |
| 10 | 3,160 | 1,450 | 1,560 | 1,210 |
| 50 | 2,970 | 1,220 | 1,640 | 1,260 |
| 100 | 3,380 | 1,290 | 1,720 | 1,340 |

(req/s, mean of 3 trials; CVs and per-trial values are in the JSON. slowapi
shows the highest single-trial noise — CVs up to 17%.)

### Limited path (limit 100/minute, rejections expected)

Same shape; every library correctly allowed exactly 100 requests per
window (80 timed + 20 warm-up) at 1 client, then 429s — with occasional
window-boundary crossings granting an extra 100 (fixed-window behavior,
visible in the `status_counts` of the raw JSON).

| Clients | plain FastAPI | moderato | slowapi | fastapi-limiter |
| --- | --- | --- | --- | --- |
| **Run 1** | | | | |
| 1 | 3,420 | 1,140 | 1,140 | 1,530 |
| 10 | 3,510 | 1,410 | 1,240 | 1,470 |
| 50 | 3,630 | 1,250 | 1,190 | 1,370 |
| 100 | 3,750 | 1,280 | 1,030 | 1,230 |
| **Run 2** | | | | |
| 1 | 3,090 | 1,200 | 980 | 1,530 |
| 10 | 3,400 | 1,190 | 1,020 | 1,510 |
| 50 | 3,650 | 1,210 | 830 | 1,370 |
| 100 | 3,040 | 1,240 | 1,090 | 1,360 |

### Redis floor

Raw Redis operations per request, no HTTP stack, same client machine:

| Pattern | 1 client | 10 clients | 50 clients | 100 clients |
| --- | --- | --- | --- | --- |
| 1× EVALSHA | 9,227 /s | 13,328 /s | 13,430 /s | 13,154 /s |
| 2× EVALSHA (TIME + script) | 4,346 /s | 7,621 /s | 6,429 /s | 6,259 /s |

### Interpretation

- **The HTTP stack dominates every library.** The plain app tops out at
  ~3.0–3.8k req/s in-process on this box; all three limiter libraries land
  in the same 1.0–1.7k band, adding roughly 0.3–0.6 ms per request. None
  of them is the bottleneck a deployment would feel first.
- **Not Redis-bound.** Raw Redis sustains 9–13k single-command ops/s
  here, several times any library's end-to-end number: under HTTP the
  workload is client-bound (event loop + ASGI), and even the limiter-only
  throughput of moderato (3.5–4.5k/s) sits near the 2-round-trip Redis
  floor rather than at a library-imposed ceiling.
- **moderato trails slightly, by design.** Each moderato decision makes
  two sequential Redis round trips (`TIME` for server-authoritative time,
  then the Lua script); slowapi and fastapi-limiter make one. That buys
  windows that stay consistent across application instances without
  trusting client clocks, at a visible ~10–30% throughput cost at low
  concurrency in this comparison. Whether that tradeoff is right depends
  on whether you need distributed time consistency.
- **All three enforce their configured limits.** In the limited scenario
  each library allowed exactly the configured 100 per minute (modulo
  documented fixed-window boundary effects).

### Caveats

- Single identity, single route, no network hop, no TLS, no multi-node
  Redis: this isolates library overhead but says nothing about network
  dominated deployments, where the extra Redis round trip matters less.
- Configurations follow each library's documented usage as shipped; none
  were tuned. The comparison is fair to defaults, not to peak
  configurability.
- Both published runs are committed; rerunning on different hardware will
  produce different absolute numbers. The defensible claims are the
  relationships (client-bound vs Redis-bound, the 2-RTT pattern cost, the
  pool boundary), not the absolute req/s.
