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
  (`RateLimitConfig.max_connections = 50`) can reject borrows when every
  pooled connection is in use (see [Default pool capacity](#default-pool-capacity)).
  The concurrency sections therefore run with the pool raised to 250 via
  the same public config field, so those tables measure the limiter rather
  than the pool; the default-pool behavior is measured separately and not
  hidden.
- **Latency semantics.** Latency is measured per request around the await,
  so concurrent scenarios report per-request latency including
  client-side queueing.

## Results — moderato alone

Raw data: `benchmarks/results/full-20260928T-full1.json` (run 1) and
`benchmarks/results/full-20260928T-full2.json` (run 2). Each run is
a complete, independent execution of the whole suite.

### Throughput (limiter only, no HTTP)

3,000 requests per trial, high limit (`100000/minute`) so nothing is denied.

| Run | Sequential (req/s) | Concurrent (req/s) | Speedup |
| --- | --- | --- | --- |
| 1 | 3,507 ± 231 (CV 6.6%) | 4,239 ± 254 (CV 6.0%) | 1.21× |
| 2 | 3,681 ± 94 (CV 2.6%) | 4,360 ± 161 (CV 3.7%) | 1.18× |

Per-trial values are in the JSON. Sequential and concurrent throughput
agree across the two runs to within ~5% and ~3% respectively; single-trial
CVs run 2.6–6.6%.

### Latency

2,000 sequential samples × 3 trials per run, percentiles pooled across
trials:

| Run | p50 | p90 | p95 | p99 | p99.9 | max |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 0.284 ms | 0.344 ms | 0.369 ms | 0.442 ms | 0.626 ms | 1.319 ms |
| 2 | 0.262 ms | 0.321 ms | 0.340 ms | 0.398 ms | 0.566 ms | 1.520 ms |

### Concurrency sweep

100 requests per client, 3 trials per level. Throughput plateaus around
4.4–5.0k req/s from 5 clients onward while per-request latency grows
roughly linearly with concurrency — the single-process client saturates
before Redis does (the [Redis floor](#redis-floor) is several times
higher).

| Clients | Run 1 req/s | Run 1 p50/p99 | Run 2 req/s | Run 2 p50/p99 |
| --- | --- | --- | --- | --- |
| 1 | 3,778 ± 9.6% | 0.25 / 0.36 ms | 3,791 ± 9.7% | 0.25 / 0.36 ms |
| 2 | 4,836 ± 1.0% | 0.40 / 0.55 ms | 4,783 ± 3.0% | 0.40 / 0.61 ms |
| 5 | 5,055 ± 1.2% | 0.97 / 1.31 ms | 5,064 ± 1.3% | 0.96 / 1.43 ms |
| 10 | 5,095 ± 1.8% | 1.91 / 2.69 ms | 5,184 ± 1.4% | 1.91 / 2.43 ms |
| 25 | 4,754 ± 7.1% | 5.22 / 7.39 ms | 5,221 ± 0.7% | 4.75 / 6.12 ms |
| 50 | 4,558 ± 0.2% | 10.8 / 13.2 ms | 4,500 ± 3.1% | 10.8 / 15.9 ms |
| 100 | 4,283 ± 2.1% | 22.8 / 34.9 ms | 4,433 ± 2.0% | 22.1 / 33.2 ms |
| 200 | 4,332 ± 3.9% | 44.8 / 66.8 ms | 4,386 ± 1.3% | 45.2 / 55.9 ms |

### Algorithm comparison

1,000 sequential samples × 3 trials per algorithm. On this workload the
three algorithms cost about the same; all execute one Lua script per
decision.

| Algorithm | Run 1 throughput | Run 1 p50/p99 | Run 2 throughput | Run 2 p50/p99 |
| --- | --- | --- | --- | --- |
| fixed_window | 3,322 ± 279 req/s | 0.30 / 0.45 ms | 3,847 ± 45 req/s | 0.25 / 0.36 ms |
| token_bucket | 3,434 ± 36 req/s | 0.28 / 0.40 ms | 3,587 ± 20 req/s | 0.27 / 0.37 ms |
| sliding_window | 3,457 ± 33 req/s | 0.28 / 0.42 ms | 3,603 ± 86 req/s | 0.27 / 0.37 ms |

### Memory per key

4,000 fresh identities per algorithm at `100/minute`, measured with
`MEMORY USAGE` over every key the run created. Values include the key
name, value encoding, and Redis' per-entry bookkeeping.

| Algorithm | Keys per identity | Bytes per key |
| --- | --- | --- |
| fixed_window | 1.0 | 152.0 ± 0.0 |
| token_bucket | 1.0 | 199.6 ± 2.5 |
| sliding_window | 1.0 | 152.0 ± 0.0 |

Identical across both runs (the token bucket's ± 2.5 bytes is Redis
allocator rounding on the small hash it stores). Token bucket stores the
most state per key (tokens and last-refill timestamp in a small hash).

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
(free/premium/enterprise): 4,514 ± 19 req/s (run 1), 4,111 ± 197 req/s
(run 2) — consistent with the plain concurrency sweep.

### Default pool capacity

The shipped default pool rejects a Redis borrow once no connection is idle
and all 50 are checked out (redis-py's non-blocking asyncio pool raises
`Too many connections`). A dedicated probe splits 1,000 checks across
N workers per level (so exactly N checks are in flight, 25–200) on the
default pool and records both rejections and the observed peak pool usage:

| Concurrent | Run 1 outcomes | Run 1 peak in use | Run 2 outcomes | Run 2 peak in use |
| --- | --- | --- | --- | --- |
| 25 | 1,000 ok | 7 / 50 | 1,000 ok | 4 / 50 |
| 50 | 1,000 ok | 9 / 50 | 1,000 ok | 7 / 50 |
| 75 | 1,000 ok | 10 / 50 | 1,000 ok | 8 / 50 |
| 100 | 1,000 ok | 11 / 50 | 1,000 ok | 11 / 50 |
| 200 | 1,000 ok | 14 / 50 | 1,000 ok | 12 / 50 |

The boundary was not tripped in either published run, and the observed
peak stays at 14 of 50 connections even at 200 concurrent: each localhost
check completes in well under a millisecond, so far fewer than 50 checks
are ever mid-round-trip at once. The rejection path is real
(redis-py raises once every pooled connection is checked out) but how
close a deployment gets to it depends on the Redis round-trip time, not
on the client count — a remote Redis with multi-millisecond RTTs would
sustain far deeper overlap. Applications with high true concurrency and a
slow Redis should raise `RateLimitConfig.max_connections`.

## Head-to-head vs slowapi and fastapi-limiter

Raw data: `benchmarks/results/compare-20260928T-c1.json` (run 1) and
`benchmarks/results/compare-20260928T-c2.json` (run 2).

**What is identical for everyone:** the same minimal FastAPI app shape
(one `GET /` returning a small JSON body, per-IP keying, client
`127.0.0.1`), the same in-process httpx client (`ASGITransport`, no
network), the same Redis instance, keys namespaced under a run-specific
prefix with only those keys deleted between levels and between (library,
scenario) pairs — the database is never flushed, and every limited-scenario
level starts from an empty quota — plus 1,000 requests per trial × 3
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
| 1 | 3,652 | 1,284 | 1,587 | 1,566 |
| 10 | 3,884 | 1,462 | 1,675 | 1,465 |
| 50 | 3,783 | 1,421 | 1,716 | 1,338 |
| 100 | 3,726 | 1,396 | 1,668 | 1,222 |
| **Run 2** | | | | |
| 1 | 3,773 | 1,239 | 1,718 | 1,667 |
| 10 | 3,856 | 1,522 | 1,767 | 1,506 |
| 50 | 3,832 | 1,414 | 1,827 | 1,428 |
| 100 | 3,175 | 1,377 | 1,824 | 1,364 |

(req/s, mean of 3 trials; CVs and per-trial values are in the JSON. All
four apps show occasional 10–20% single-trial dips on this shared sandbox.)

### Limited path (limit 100/minute, rejections expected)

Same shape; every library allowed exactly 100 requests per window (80
timed + 20 warm-up) and returned 2,900 × 429 at every concurrency level in
both runs — the per-level quota reset makes these counts fully
deterministic (visible in the `status_counts` of the raw JSON).

| Clients | plain FastAPI | moderato | slowapi | fastapi-limiter |
| --- | --- | --- | --- | --- |
| **Run 1** | | | | |
| 1 | 3,179 | 1,255 | 1,229 | 1,515 |
| 10 | 3,583 | 1,415 | 1,252 | 1,469 |
| 50 | 3,596 | 1,277 | 1,162 | 1,487 |
| 100 | 3,679 | 1,317 | 1,155 | 1,383 |
| **Run 2** | | | | |
| 1 | 3,349 | 1,139 | 1,211 | 1,543 |
| 10 | 3,562 | 1,414 | 1,372 | 1,422 |
| 50 | 3,546 | 1,416 | 1,309 | 1,426 |
| 100 | 3,524 | 1,354 | 1,255 | 1,353 |

### Redis floor

Raw Redis operations per request, no HTTP stack, same client machine:

| Pattern | 1 client | 10 clients | 50 clients | 100 clients |
| --- | --- | --- | --- | --- |
| 1× EVALSHA | 9,665 /s | 13,143 /s | 13,151 /s | 13,031 /s |
| 2× EVALSHA (TIME + script) | 4,488 /s | 7,548 /s | 4,854 /s | 5,811 /s |

(Run 2 agrees to within ~5% on the 1× row and ~10% on the noisier 2× row.)

### Interpretation

- **The HTTP stack dominates every library.** The plain app tops out at
  ~3.2–3.9k req/s in-process on this box; all three limiter libraries land
  in the same 1.1–1.8k band, adding roughly 0.3–0.6 ms per request. None
  of them is the bottleneck a deployment would feel first.
- **Not Redis-bound.** Raw Redis sustains 9.6–13.6k single-command ops/s
  here, several times any library's end-to-end number: under HTTP the
  workload is client-bound (event loop + ASGI), and even the limiter-only
  throughput of moderato (3.5–4.4k/s) sits near the 2-round-trip Redis
  floor rather than at a library-imposed ceiling.
- **moderato trails slightly, by design.** Each moderato decision makes
  two sequential Redis round trips (`TIME` for server-authoritative time,
  then the Lua script); slowapi and fastapi-limiter make one. That buys
  windows that stay consistent across application instances without
  trusting client clocks, at a visible ~10–25% throughput cost at low
  concurrency in this comparison (moderato p50 0.78 ms vs slowapi
  0.53–0.58 ms — the extra ~0.2 ms is one localhost round trip). Whether
  that tradeoff is right depends on whether you need distributed time
  consistency.
- **All three enforce their configured limits.** In the limited scenario
  each library allowed exactly the configured 100 per minute and 429'd
  the other 2,900 requests at every level in both runs.

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
