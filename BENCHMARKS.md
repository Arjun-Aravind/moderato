# Benchmarks

Everything in this file comes from actual runs executed on the committed
harness. The raw, machine-readable results for each published run are
committed next to it under [`benchmarks/results/`](benchmarks/results/) —
every number below can be traced to a JSON file named in its caption. No
number on this page is a marketing target.

Committed JSON is written with `--slim`: it keeps every summary and drops
the per-sample arrays (4,000 per-key memory samples per algorithm and the
per-client sweep percentiles), which would otherwise make each file about
16x larger. Rerun without `--slim` to regenerate the full samples.

- Harness: [`benchmarks/performance.py`](benchmarks/performance.py) (moderato alone)
- Library comparison: [`benchmarks/compare_libraries.py`](benchmarks/compare_libraries.py)
  (head-to-head, run in an isolated venv)

## How to reproduce

```bash
# Requires Redis on localhost:6379 (or REDIS_URL)
poetry install
poetry run python benchmarks/performance.py --slim     # ~60 s, commit-sized JSON
poetry run python benchmarks/compare_libraries.py      # ~65 s, bootstraps its own venv
```

Each run writes `benchmarks/results/<kind>-<run-id>.json`. Re-running with
the same `--run-id` replays the same deterministic Redis key layout; the
harness sweeps its own keys before and after every run. `--slim` only
controls JSON size; the measurements are identical either way.

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
runs. The benchmark client, Redis, and (in the comparison) the ASGI app all
share two vCPUs.

## Methodology

- **Trials and variance.** Every scenario runs multiple trials; each number
  is reported as mean ± sample standard deviation across trials, with the
  raw per-trial values in the JSON. The two per-key memory tables are the
  exception: their ± is the spread across the measured keys, not across
  trials, and the tables say so.
- **Deterministic run keys.** All Redis keys derive from the run id
  (`ratelimit:bench%3A<run-id>%3A…`), swept before and after each run.
- **Comparison isolation.** In the head-to-head below each library's
  connections are closed after its scenario pair, and every measured
  trial starts with Redis-clock runway — 10 s for a level's first trial,
  then 1.5× the previous trial's measured duration (capped below a
  minute) — and fails the run if it straddles a minute boundary or would
  begin in a later minute than its level's first trial. A real
  fixed-window quota therefore cannot reset within a level and skew the
  allow counts.
- **Memory is measured, not estimated.** Per-key memory comes from Redis'
  `MEMORY USAGE`, over 4,000 freshly created keys for the one-check table
  and over every key still alive for the steady-state table. The one-check
  batch starts only when it fits before the next minute boundary (fixed
  window keys expire at that boundary), and the run fails loudly if a scan
  finds fewer keys than identities.
- **Accuracy is compared to analytic expectations.** A burst of 4× the
  limit against a fresh key must land inside the analytically expected
  band (see [Accuracy under load](#accuracy-under-load)). Each burst starts
  just after a whole second on the Redis clock, and a failed band makes
  `performance.py` exit non-zero.
- **Client-bound vs Redis-bound is measured.** The concurrency sweep records
  the CPU time of the benchmark process and of the Redis server (from
  `INFO cpu`) over each level, so the bottleneck claim below rests on
  observed utilisation rather than inference.
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

Raw data: `benchmarks/results/full-20261007T-lua7.json` (run 1) and
`benchmarks/results/full-20261007T-lua8.json` (run 2). Each run is
a complete, independent execution of the whole suite.

### Throughput (limiter only, no HTTP)

3,000 requests per trial × 5 trials, high limit (`100000/minute`) so nothing
is denied.

| Run | Sequential (req/s) | Concurrent (req/s) | Speedup |
| --- | --- | --- | --- |
| 1 | 5,826 ± 135 (CV 2.3%) | 8,396 ± 516 (CV 6.2%) | 1.44× |
| 2 | 5,947 ± 139 (CV 2.3%) | 8,585 ± 348 (CV 4.0%) | 1.44× |

Per-trial values are in the JSON. Sequential throughput agrees across the
two runs to within ~2%; concurrent throughput agrees to within ~2% (CV up
to ~20% at individual concurrency levels in one run).

### Latency

2,000 sequential samples × 3 trials per run, percentiles pooled across
trials:

| Run | p50 | p90 | p95 | p99 | p99.9 | max |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 0.167 ms | 0.190 ms | 0.198 ms | 0.227 ms | 0.320 ms | 1.358 ms |
| 2 | 0.166 ms | 0.193 ms | 0.204 ms | 0.235 ms | 0.306 ms | 0.967 ms |

### Concurrency sweep

100 requests per client, 3 trials per level. Throughput plateaus around
7,859–9,474 req/s from 5 clients onward while per-request latency grows
roughly linearly with concurrency. The CPU columns show why: the benchmark
process is at ~100% of one core from 2 clients on, while the Redis server
stays at 28–41% of a core. The single-threaded Python client saturates
long before Redis does.

| Clients | Run 1 req/s (CV) | Run 1 p50 / p99 | Run 1 CPU client / Redis | Run 2 req/s (CV) | Run 2 p50 / p99 | Run 2 CPU client / Redis |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 6,181 ± 8.5% | 0.15 / 0.24 ms | 84% / 28% | 5,010 ± 20.4% | 0.19 / 0.39 ms | 82% / 28% |
| 2 | 8,693 ± 1.2% | 0.22 / 0.30 ms | 100% / 36% | 7,411 ± 19.2% | 0.25 / 0.42 ms | 100% / 28% |
| 5 | 8,421 ± 12.3% | 0.55 / 1.02 ms | 100% / 38% | 8,874 ± 6.8% | 0.55 / 0.85 ms | 100% / 37% |
| 10 | 9,267 ± 0.4% | 1.06 / 1.56 ms | 100% / 41% | 9,474 ± 0.5% | 1.04 / 1.23 ms | 100% / 39% |
| 25 | 8,975 ± 5.2% | 2.68 / 4.45 ms | 99% / 38% | 9,178 ± 4.3% | 2.64 / 3.67 ms | 100% / 41% |
| 50 | 8,511 ± 1.4% | 5.87 / 7.39 ms | 100% / 34% | 8,490 ± 2.2% | 5.84 / 8.20 ms | 100% / 35% |
| 100 | 8,325 ± 0.8% | 11.81 / 15.97 ms | 100% / 32% | 8,098 ± 5.7% | 11.99 / 19.10 ms | 100% / 35% |
| 200 | 7,859 ± 0.8% | 24.50 / 40.58 ms | 99% / 32% | 8,108 ± 3.4% | 24.26 / 38.16 ms | 99% / 34% |

(CV is across the 3 trials at that level; CPU is % of one core.)

### Algorithm comparison

1,000 sequential samples × 3 trials per algorithm. All three algorithms
execute one Lua script per decision with server `TIME` inside it, and on
this workload they cost about the same.

| Algorithm | Run 1 throughput | Run 1 p50 / p99 | Run 2 throughput | Run 2 p50 / p99 |
| --- | --- | --- | --- | --- |
| fixed_window | 5,725 ± 238 req/s | 0.17 / 0.27 ms | 6,432 ± 1,031 req/s | 0.16 / 0.25 ms |
| token_bucket | 5,731 ± 113 req/s | 0.17 / 0.26 ms | 5,602 ± 52 req/s | 0.18 / 0.24 ms |
| sliding_window | 5,778 ± 94 req/s | 0.17 / 0.23 ms | 5,521 ± 105 req/s | 0.18 / 0.27 ms |

### Memory per key

Two measurements per algorithm, both from Redis `MEMORY USAGE`; values
include the key name, value encoding, and Redis' per-entry bookkeeping.

**One check per identity** — 4,000 identities at `100/minute`, one check
each, then every key the run created is scanned and measured:

| Algorithm | Keys per identity | Bytes per key | Bytes per identity |
| --- | --- | --- | --- |
| fixed_window | 1.0 | 152.0 ± 0.0 | 152.0 |
| token_bucket | 1.0 | 196.0 ± 6.9 | 196.0 |
| sliding_window | 1.0 | 152.0 ± 0.0 | 152.0 |

**Steady state** — 200 identities at `100/second`, checked twice one
window apart, then every key still alive is measured:

| Algorithm | Keys per identity | Bytes per key | Bytes per identity |
| --- | --- | --- | --- |
| fixed_window | 1.0 | 152.0 ± 0.0 | 152.0 |
| token_bucket | 1.0 | 184.0 ± 0.0 | 184.0 |
| sliding_window | 2.0 | 152.0 ± 0.0 | 304.0 |

Identical across both runs. The ± here is the spread across keys, not
across trials. One key-name artifact to read past: `MEMORY USAGE` rises in
allocator size-class increments as the key name grows, and the token
bucket's dict-encoded hash sits on one of those boundaries. Reproduced
directly: a token-bucket key whose full Redis name is 90 characters
measures 184 B; the same key at 95 characters measures 200 B. The one-check
token-bucket batch produces names straddling that boundary (identity index
length changes the name length), so its mean lands between the classes;
these runs use the run ids `20261007T-lua7/8`, whose steady-state names
fall below the boundary. The capacity-relevant numbers are the
bytes-per-identity columns: between window boundaries a sliding-window
identity holds two keys (304 B in these runs) until its previous-window
key expires, while the other algorithms hold one. A one-check measurement
alone would understate sliding-window steady-state cost by half.

### Accuracy under load

A burst of 200 concurrent checks (4× the limit) against a fresh key at
`50/second`, started just after a whole second on the Redis clock:

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

**Why the burst is aligned, and a library race it exposed.** An
earlier version of this check started the burst at an arbitrary instant.
Across 150 such runs the fixed-window check failed 7 times, always when
the burst straddled a one-second boundary, with 113–200 of 200 requests
admitted against a limit of 50 — more than the two-window band allows. The
cause was in the library, not the benchmark: `check()` read Redis `TIME`
in one round trip and ran the Lua script in a second, so a request whose
`TIME` read landed in window *W* but whose script ran after *W* had ended
sent a `window_end` that was already in the past. `EXPIREAT` then deleted
the counter immediately and the request was admitted without being counted
(reproduced directly against the script: 5 of 5 requests allowed with a
limit of 50, and the key never persisted). That race is fixed as of the
current code: every algorithm's Lua script reads `TIME` and derives its
window state atomically, so no client-side window can expire
mid-decision. The aligned burst is retained because it keeps the accuracy
check deterministic; the boundary race no longer exists to expose.

### Multi-tenant

100 tenants × 100 requests × 3 trials, tier mix 70/25/5
(free/premium/enterprise): 8,002 ± 204 req/s (run 1), 8,211 ± 491 req/s (run 2) —
consistent with the plain concurrency sweep.

### Default pool capacity

The shipped default pool rejects a Redis borrow once no connection is idle
and all 50 are checked out (redis-py's non-blocking asyncio pool raises
`Too many connections`). A dedicated probe splits 1,000 checks across
N workers per level (so exactly N checks are in flight, 25–200) on the
default pool and records both rejections and the observed peak pool usage:

| Concurrent | Run 1 outcomes | Run 1 peak in use | Run 2 outcomes | Run 2 peak in use |
| --- | --- | --- | --- | --- |
| 25 | 1,000 ok | 4 / 50 | 1,000 ok | 4 / 50 |
| 50 | 1,000 ok | 5 / 50 | 1,000 ok | 5 / 50 |
| 75 | 1,000 ok | 10 / 50 | 1,000 ok | 6 / 50 |
| 100 | 1,000 ok | 14 / 50 | 1,000 ok | 7 / 50 |
| 200 | 1,000 ok | 15 / 50 | 1,000 ok | 9 / 50 |

The boundary was not tripped in either published run, and the observed
peak never exceeded 15 of 50 connections even at 200 concurrent:
each localhost check completes in well under a millisecond, so far fewer
than 50 checks are ever mid-round-trip at once. The peak depends on
round-trip time and scheduling, not on the client count, so treat it as
environment-specific; it is read from a redis-py pool internal and is
reported as unavailable on versions that lack it. The rejection path is
real (redis-py raises once every pooled connection is checked out), but how
close a deployment gets to it depends on the Redis round-trip time — a
remote Redis with multi-millisecond RTTs would sustain far deeper overlap.
Applications with high true concurrency and a slow Redis should raise
`RateLimitConfig.max_connections`.

## Head-to-head vs slowapi and fastapi-limiter

Raw data: `benchmarks/results/compare-20261007T-cmp13.json` (run 1) and
`benchmarks/results/compare-20261007T-cmp14.json` (run 2).

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
| slowapi | 0.1.10 (limits 5.8.0) | `Limiter(key_func=get_remote_address, storage_uri="redis://…")` + `limiter.limit()`; default fixed-window strategy |
| fastapi-limiter | 0.2.0 (pyrate-limiter 4.5.0) | `RateLimiter` dependency over pyrate-limiter's `RedisBucket` with `FixedWindow()` |
| plain FastAPI | 0.141.1 | Same app shape, no limiter — shared baseline |
| Redis floor | — | 1× and 2× `EVALSHA` per request, no HTTP stack |

The comparison venv pins these exact versions (plus httpx 0.28.1 and
redis-py 5.3.1); the installed set is recorded in each JSON under
`versions`. This compares implementations as-shipped, not theoretical
optima. As-shipped facts worth knowing, each checked against the installed
packages:

- **slowapi cannot use async Redis as shipped.** Its documented
  `redis://` storage URI instantiates limits' *sync* Redis client inside
  the async endpoint (`limits.storage.redis.RedisStorage` wrapping
  `redis.client.Redis`). limits' async scheme (`async+redis://`) raises a
  `ConfigurationError` without `coredis`; with `coredis` 6.9.0 installed,
  `Limiter(...)` still fails inside slowapi's strategy construction
  (`assert isinstance(storage, Storage)` in `limits/strategies.py`). The
  sync client is therefore its only working Redis mode, and it is what was
  benchmarked.
- **fastapi-limiter's documented usage is in-memory.** Its README's
  `Limiter(Rate(...))` never touches Redis; to honor the same-Redis
  workload it was wired to pyrate-limiter's documented `RedisBucket`.
  A `RedisBucket` is a single bucket per route, not per client identity:
  with a limit of 3 per minute, 3 requests from `10.0.0.1` returned
  200/200/200 and 3 from `10.0.0.2` returned 429/429/429. With the
  single-identity workload used here the behaviour is identical either way,
  but it is not a per-IP limiter as configured. (The harness's
  `methodology.app_shape` metadata describes the app, which keys per IP;
  fastapi-limiter's shared route bucket is the exception disclosed here.)
- **fastapi-limiter's Redis bucket stores one sorted-set member per
  request** (pyrate-limiter's README says so), so its memory grows with
  traffic inside the window, unlike moderato's constant-size counters.
  This benchmark measures speed, not its memory.
- **Redis commands per request** (counted with `INFO commandstats` over 200
  requests, after warm-up): moderato 1 `EVALSHA` for every algorithm (the
  server `TIME` runs inside the Lua script); slowapi 1 `EVALSHA`;
  fastapi-limiter 1 `EVALSHA`. (Commands run inside a script, such as
  `INCRBY`, `PTTL`, and `TIME`, also appear in the counters.)

### Allowed path (limit 1,000,000/minute — nothing denied)

| Clients | plain FastAPI | moderato | slowapi | fastapi-limiter |
| --- | --- | --- | --- | --- |
| **Run 1** | | | | |
| 1 | 3,780 | 1,652 | 1,447 | 1,663 |
| 10 | 3,774 | 1,694 | 1,740 | 1,542 |
| 50 | 3,827 | 1,772 | 1,695 | 1,469 |
| 100 | 3,612 | 1,614 | 1,742 | 1,367 |
| **Run 2** | | | | |
| 1 | 3,876 | 1,751 | 1,703 | 1,585 |
| 10 | 3,933 | 1,836 | 1,841 | 1,189 |
| 50 | 3,832 | 1,738 | 1,782 | 1,474 |
| 100 | 3,790 | 1,716 | 1,724 | 1,501 |

(req/s, mean of 3 trials; CVs and per-trial values are in the JSON. All
four apps show occasional 10–20% single-trial dips on this shared sandbox.)

### Limited path (limit 100/minute, rejections expected)

Same shape; each level resets the quota before measuring. The limit is a
real fixed window of 100 per minute, so a trial straddling a minute
boundary would admit a second batch of 100 and make its allow counts
incomparable across libraries — an earlier run pair caught exactly that.
Each measured trial now starts with Redis-clock runway (10 s for a
level's first trial, then 1.5× the previous trial's duration, capped
below a minute) and fails the run if it crosses the boundary or would
start in a later minute than its level's first trial, so every level allows exactly the
first 100 timed requests and returns 2,900 × 429 (checked programmatically
over all 32 level results per run; visible in the `status_counts` of the
raw JSON). The harness's 20-request warm-up runs once before the first
level and is wiped by that level's reset, so it consumes none of the
measured quota.

| Clients | plain FastAPI | moderato | slowapi | fastapi-limiter |
| --- | --- | --- | --- | --- |
| **Run 1** | | | | |
| 1 | 3,407 | 1,619 | 1,266 | 1,617 |
| 10 | 3,617 | 1,799 | 1,292 | 1,553 |
| 50 | 3,801 | 1,709 | 1,299 | 1,503 |
| 100 | 3,781 | 1,669 | 1,221 | 1,423 |
| **Run 2** | | | | |
| 1 | 3,693 | 1,651 | 1,289 | 1,634 |
| 10 | 3,699 | 1,787 | 1,338 | 1,473 |
| 50 | 3,807 | 1,760 | 1,369 | 1,475 |
| 100 | 3,758 | 1,687 | 1,327 | 1,422 |

### Redis floor

Raw Redis operations per request, no HTTP stack, same client machine:

| Pattern | Run | 1 client | 10 clients | 50 clients | 100 clients |
| --- | --- | --- | --- | --- | --- |
| 1× EVALSHA | 1 | 9,773 | 12,651 | 13,216 | 12,921 |
| 1× EVALSHA | 2 | 9,087 | 13,586 | 13,113 | 12,976 |
| 2× EVALSHA (TIME + script) | 1 | 4,237 | 7,686 | 6,582 | 6,607 |
| 2× EVALSHA (TIME + script) | 2 | 4,666 | 7,717 | 6,630 | 6,535 |

(The 1-client numbers are the noisiest; from 10 clients on, each row is
stable within a few percent.)

### Moderato relative to each library

Percent difference in throughput, moderato versus the library, run 1 / run
2 (negative = moderato slower):

| Path | Against | 1 client | 10 clients | 50 clients | 100 clients |
| --- | --- | --- | --- | --- | --- |
| allowed | vs slowapi | +14% / +3% | -3% / -0% | +5% / -2% | -7% / -0% |
| allowed | vs fastapi-limiter | -1% / +10% | +10% / +54% | +21% / +18% | +18% / +14% |
| limited | vs slowapi | +28% / +28% | +39% / +34% | +31% / +29% | +37% / +27% |
| limited | vs fastapi-limiter | +0% / +1% | +16% / +21% | +14% / +19% | +17% / +19% |

### Interpretation

- **The HTTP stack dominates every library.** The plain app runs at
  ~3,407–3,933 req/s in-process on this box; all three limiter libraries land
  in the same ~1.1–1.8k band, adding a few tenths of a millisecond per
  request. None of them is the bottleneck a deployment would feel first.
- **Client-bound, not Redis-bound.** In the concurrency sweep the benchmark
  process runs at 99–100% of a core while the Redis server stays at
  28–41% of a core — the direct evidence for client-boundedness. The raw
  Redis floor is consistent with that: a single `EVALSHA` sustains 9,087–13,586
  ops/s on this box — above the ~8.0–8.6k req/s the concurrency sweep
  achieves — while the two-call floor (4,237–7,717 ops/s), which no shipped
  algorithm pays anymore, is shown for reference.
- **The ranking depends on the path.** On the allowed path moderato and
  slowapi are inside each other's run-to-run noise at every level (−7% to
  +14% across the two runs) and moderato is ahead of fastapi-limiter at
  every level except 1 client in run 1 (−1% there, +10% to +54%
  elsewhere). On the limited path moderato is ahead of slowapi at every
  level (+27% to +39%) — slowapi's rejection path is slower than its allow
  path in these runs — and ahead of fastapi-limiter at every level
  (+0% to +21%). There is no single "moderato is X% faster" number: the
  margins are path- and level-dependent.
- **One round trip per decision for every algorithm.** Each of moderato's
  Lua scripts reads server `TIME` and derives its window state atomically,
  so every check is a single `EVALSHA` — the same round-trip count as
  slowapi and fastapi-limiter (confirmed with the command counts above).
  Windows stay consistent across application instances without trusting
  client clocks. At 1 client moderato's allowed-path p50 is 0.56–0.59 ms
  against slowapi's 0.55–0.66 ms, and the raw floors attribute only
  ~0.1 ms to the second call (0.214–0.236 ms per two-call decision against
  0.102–0.110 ms per one-call), so the remaining gap is client-side work
  around the script call, which this benchmark does not isolate.
- **All three enforce their configured limits.** In the limited scenario
  every level allowed exactly the configured 100 per minute and 429'd the
  other 2,900 requests, in both runs (each limited trial is boundary-gated
  and must remain in its level's starting minute).

### Caveats

- Single identity, single route, no network hop, no TLS, no multi-node
  Redis: this isolates library overhead but says nothing about network
  dominated deployments, where the single Redis round trip per check
  matters less.
- The client, the ASGI app and Redis share two vCPUs, so the head-to-head
  numbers include contention that a deployment would not have.
- Configurations follow each library's documented usage as shipped; none
  were tuned. The comparison is fair to defaults, not to peak
  configurability.
- Both published runs are committed; rerunning on different hardware will
  produce different absolute numbers. The defensible claims are the
  relationships (client-bound vs Redis-bound, the one-round-trip design
  all algorithms share, the
  pool boundary), not the absolute req/s.
