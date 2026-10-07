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

Raw data: `benchmarks/results/full-20261001T-full1.json` (run 1) and
`benchmarks/results/full-20261001T-full2.json` (run 2). Each run is
a complete, independent execution of the whole suite.

### Throughput (limiter only, no HTTP)

3,000 requests per trial × 5 trials, high limit (`100000/minute`) so nothing
is denied.

| Run | Sequential (req/s) | Concurrent (req/s) | Speedup |
| --- | --- | --- | --- |
| 1 | 3,540 ± 81 (CV 2.3%) | 4,273 ± 382 (CV 8.9%) | 1.21× |
| 2 | 3,510 ± 94 (CV 2.7%) | 4,225 ± 152 (CV 3.6%) | 1.20× |

Per-trial values are in the JSON. Sequential throughput agrees across the
two runs to within ~1%; concurrent throughput is noisier (CV up to 9% in one
run) and agrees to within ~1%.

### Latency

2,000 sequential samples × 3 trials per run, percentiles pooled across
trials:

| Run | p50 | p90 | p95 | p99 | p99.9 | max |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 0.287 ms | 0.349 ms | 0.373 ms | 0.438 ms | 0.843 ms | 1.956 ms |
| 2 | 0.274 ms | 0.336 ms | 0.353 ms | 0.402 ms | 0.563 ms | 1.306 ms |

### Concurrency sweep

100 requests per client, 3 trials per level. Throughput plateaus around
4,040–5,108 req/s from 5 clients onward while per-request latency grows
roughly linearly with concurrency. The CPU columns show why: the benchmark
process is at ~100% of one core from 2 clients on, while the Redis server
stays at 27–37% of a core. The single-threaded Python client saturates
long before Redis does.

| Clients | Run 1 req/s (CV) | Run 1 p50 / p99 | Run 1 CPU client / Redis | Run 2 req/s (CV) | Run 2 p50 / p99 | Run 2 CPU client / Redis |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 3,672 ± 12.3% | 0.26 / 0.39 ms | 87% / 24% | 3,162 ± 5.2% | 0.31 / 0.57 ms | 85% / 27% |
| 2 | 4,894 ± 0.5% | 0.40 / 0.50 ms | 100% / 32% | 4,503 ± 3.6% | 0.42 / 0.68 ms | 100% / 32% |
| 5 | 5,017 ± 0.8% | 0.98 / 1.34 ms | 100% / 32% | 4,854 ± 1.9% | 0.99 / 1.50 ms | 98% / 35% |
| 10 | 5,000 ± 1.9% | 1.95 / 2.80 ms | 100% / 33% | 4,540 ± 10.2% | 2.02 / 3.42 ms | 100% / 34% |
| 25 | 5,108 ± 0.9% | 4.85 / 6.05 ms | 99% / 36% | 4,662 ± 4.3% | 5.11 / 7.68 ms | 100% / 37% |
| 50 | 4,419 ± 2.6% | 10.99 / 15.48 ms | 99% / 30% | 4,310 ± 6.1% | 11.12 / 16.74 ms | 99% / 28% |
| 100 | 4,040 ± 1.6% | 23.52 / 35.43 ms | 100% / 27% | 4,148 ± 1.6% | 23.27 / 36.34 ms | 99% / 28% |
| 200 | 4,196 ± 1.6% | 45.93 / 66.37 ms | 99% / 29% | 4,237 ± 1.4% | 46.63 / 58.49 ms | 100% / 28% |

(CV is across the 3 trials at that level; CPU is % of one core.)

### Algorithm comparison

1,000 sequential samples × 3 trials per algorithm. On this workload the
three algorithms cost about the same; all execute one Lua script per
decision.

| Algorithm | Run 1 throughput | Run 1 p50 / p99 | Run 2 throughput | Run 2 p50 / p99 |
| --- | --- | --- | --- | --- |
| fixed_window | 3,484 ± 127 req/s | 0.28 / 0.43 ms | 3,371 ± 276 req/s | 0.29 / 0.48 ms |
| token_bucket | 3,245 ± 166 req/s | 0.30 / 0.49 ms | 3,246 ± 154 req/s | 0.30 / 0.63 ms |
| sliding_window | 3,315 ± 39 req/s | 0.30 / 0.46 ms | 3,403 ± 45 req/s | 0.28 / 0.44 ms |

### Memory per key

Two measurements per algorithm, both from Redis `MEMORY USAGE`; values
include the key name, value encoding, and Redis' per-entry bookkeeping.

**One check per identity** — 4,000 identities at `100/minute`, one check
each, then every key the run created is scanned and measured:

| Algorithm | Keys per identity | Bytes per key | Bytes per identity |
| --- | --- | --- | --- |
| fixed_window | 1.0 | 152.0 ± 0.0 | 152.0 |
| token_bucket | 1.0 | 199.6 ± 2.5 | 199.6 |
| sliding_window | 1.0 | 152.0 ± 0.0 | 152.0 |

**Steady state** — 200 identities at `100/second`, checked twice one
window apart, then every key still alive is measured:

| Algorithm | Keys per identity | Bytes per key | Bytes per identity |
| --- | --- | --- | --- |
| fixed_window | 1.0 | 152.0 ± 0.0 | 152.0 |
| token_bucket | 1.0 | 184.0 ± 0.0 | 184.0 |
| sliding_window | 2.0 | 152.0 ± 0.0 | 304.0 |

Identical across both runs. The ± here is the spread across keys, not
across trials. One key-name artifact to read past: the token bucket's hash
is allocated in 184 B for key names of up to 92 characters and 200 B for
93 or more (observed directly: 100 keys at 184 B, 3,900 at 200 B, split
exactly on key length). Identity numbers 0–99 produce the shorter names in
the one-check table, and the steady-state pass uses a shorter policy
component (`p100x1` vs `p100x60`), which puts all of its keys at 184 B. The
capacity-relevant numbers are the bytes-per-identity columns: between
window boundaries a sliding-window identity holds two keys (~304 B) until
its previous-window key expires, while the other algorithms hold one. A
one-check measurement alone would understate sliding-window steady-state
cost by half.

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

**Why the burst is aligned, and a library finding it exposed.** An
earlier version of this check started the burst at an arbitrary instant.
Across 150 such runs the fixed-window check failed 7 times, always when
the burst straddled a one-second boundary, with 113–200 of 200 requests
admitted against a limit of 50 — more than the two-window band allows. The
cause is in the library, not the benchmark: `check()` reads Redis `TIME` in
one round trip and runs the Lua script in a second, so a request whose
`TIME` read lands in window *W* but whose script runs after *W* has ended
sends a `window_end` that is already in the past. `EXPIREAT` then deletes
the counter immediately and the request is admitted without being counted
(reproduced directly against the script: 5 of 5 requests allowed with a
limit of 50, and the key never persists). With 300 aligned runs after the
change there were no failures and no straddling bursts. The aligned burst
is what this benchmark measures; the boundary race itself is outside this
PR's scope (no library runtime changes) and is reported to the owner.

### Multi-tenant

100 tenants × 100 requests × 3 trials, tier mix 70/25/5
(free/premium/enterprise): 4,339 ± 70 req/s (run 1), 4,350 ± 148 req/s (run 2) —
consistent with the plain concurrency sweep.

### Default pool capacity

The shipped default pool rejects a Redis borrow once no connection is idle
and all 50 are checked out (redis-py's non-blocking asyncio pool raises
`Too many connections`). A dedicated probe splits 1,000 checks across
N workers per level (so exactly N checks are in flight, 25–200) on the
default pool and records both rejections and the observed peak pool usage:

| Concurrent | Run 1 outcomes | Run 1 peak in use | Run 2 outcomes | Run 2 peak in use |
| --- | --- | --- | --- | --- |
| 25 | 1,000 ok | 6 / 50 | 1,000 ok | 5 / 50 |
| 50 | 1,000 ok | 7 / 50 | 1,000 ok | 7 / 50 |
| 75 | 1,000 ok | 12 / 50 | 1,000 ok | 12 / 50 |
| 100 | 1,000 ok | 15 / 50 | 1,000 ok | 14 / 50 |
| 200 | 1,000 ok | 17 / 50 | 1,000 ok | 15 / 50 |

The boundary was not tripped in either published run, and the observed
peak never exceeded 17 of 50 connections even at 200 concurrent:
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

Raw data: `benchmarks/results/compare-20261001T-cmp1.json` (run 1) and
`benchmarks/results/compare-20261001T-cmp2.json` (run 2).

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
  but it is not a per-IP limiter as configured.
- **fastapi-limiter's Redis bucket stores one sorted-set member per
  request** (pyrate-limiter's README says so), so its memory grows with
  traffic inside the window, unlike moderato's constant-size counters.
  This benchmark measures speed, not its memory.
- **Redis commands per request** (counted with `INFO commandstats` over 200
  requests, after warm-up): moderato 1 `TIME` + 1 `EVALSHA`; slowapi 1
  `EVALSHA`; fastapi-limiter 1 `EVALSHA`. (Commands run inside a script,
  such as `INCRBY` and `PTTL`, also appear in the counters.)

### Allowed path (limit 1,000,000/minute — nothing denied)

| Clients | plain FastAPI | moderato | slowapi | fastapi-limiter |
| --- | --- | --- | --- | --- |
| **Run 1** | | | | |
| 1 | 3,566 | 1,288 | 1,745 | 1,503 |
| 10 | 3,461 | 1,448 | 1,633 | 1,358 |
| 50 | 3,598 | 1,350 | 1,624 | 1,250 |
| 100 | 3,468 | 1,363 | 1,618 | 1,325 |
| **Run 2** | | | | |
| 1 | 3,356 | 1,244 | 1,692 | 1,596 |
| 10 | 3,566 | 1,418 | 1,645 | 1,426 |
| 50 | 3,406 | 1,384 | 1,661 | 1,350 |
| 100 | 3,242 | 1,322 | 1,665 | 1,353 |

(req/s, mean of 3 trials; CVs and per-trial values are in the JSON. All
four apps show occasional 10–20% single-trial dips on this shared sandbox.)

### Limited path (limit 100/minute, rejections expected)

Same shape; each level resets the quota before measuring, so every library
allowed exactly its first 100 timed requests at every concurrency level
and returned 2,900 × 429 in both runs (checked programmatically over all 32
level results per run; visible in the `status_counts` of the raw JSON). The
harness's 20-request warm-up runs once before the first level and is wiped
by that level's reset, so it consumes none of the measured quota.

| Clients | plain FastAPI | moderato | slowapi | fastapi-limiter |
| --- | --- | --- | --- | --- |
| **Run 1** | | | | |
| 1 | 3,343 | 1,253 | 1,171 | 1,496 |
| 10 | 3,446 | 1,394 | 1,310 | 1,316 |
| 50 | 2,686 | 1,308 | 1,229 | 1,298 |
| 100 | 3,164 | 1,299 | 1,190 | 1,295 |
| **Run 2** | | | | |
| 1 | 3,411 | 1,249 | 1,190 | 1,504 |
| 10 | 3,532 | 1,428 | 1,254 | 1,398 |
| 50 | 3,474 | 1,375 | 1,168 | 1,267 |
| 100 | 3,448 | 1,326 | 1,066 | 1,378 |

### Redis floor

Raw Redis operations per request, no HTTP stack, same client machine:

| Pattern | Run | 1 client | 10 clients | 50 clients | 100 clients |
| --- | --- | --- | --- | --- | --- |
| 1× EVALSHA | 1 | 9,414 | 13,122 | 13,689 | 12,837 |
| 1× EVALSHA | 2 | 9,291 | 13,333 | 13,432 | 12,735 |
| 2× EVALSHA (TIME + script) | 1 | 4,414 | 7,495 | 6,181 | 6,202 |
| 2× EVALSHA (TIME + script) | 2 | 4,303 | 7,457 | 6,222 | 5,983 |

(The 1-client numbers are the noisiest; from 10 clients on, each row is
stable within a few percent.)

### Moderato relative to each library

Percent difference in throughput, moderato versus the library, run 1 / run
2 (negative = moderato slower):

| Path | Against | 1 client | 10 clients | 50 clients | 100 clients |
| --- | --- | --- | --- | --- | --- |
| allowed | vs slowapi | -26% / -27% | -11% / -14% | -17% / -17% | -16% / -21% |
| allowed | vs fastapi-limiter | -14% / -22% | +7% / -1% | +8% / +3% | +3% / -2% |
| limited | vs slowapi | +7% / +5% | +6% / +14% | +6% / +18% | +9% / +24% |
| limited | vs fastapi-limiter | -16% / -17% | +6% / +2% | +1% / +8% | +0% / -4% |

### Interpretation

- **The HTTP stack dominates every library.** The plain app runs at
  ~2,686–3,598 req/s in-process on this box; all three limiter libraries land
  in the same ~1.1–1.7k band, adding a few tenths of a millisecond per
  request. None of them is the bottleneck a deployment would feel first.
- **Client-bound, not Redis-bound.** In the concurrency sweep the benchmark
  process runs at 98–100% of a core while the Redis server stays at
  27–37% of a core. The raw Redis floor is 9,291–13,689 ops/s for a single
  `EVALSHA` and 4,303–7,495 ops/s for the two-round-trip pattern moderato uses
  — both above the limiter-only path's ~3.5–5.1k req/s. Redis has headroom;
  the single-threaded Python client is the ceiling.
- **The ranking depends on the path.** On the allowed path moderato is
  behind slowapi by roughly 11–27% and level with or slightly ahead of
  fastapi-limiter from 10 clients up (−2% to +8%), but about 14–27% behind
  both at 1 client. On the limited path moderato is ahead of slowapi at
  every level (+5% to +24%) — slowapi's rejection path is slower than its
  allow path in these runs — and behind fastapi-limiter by about 16–17% at 1
  client, within about ±8% from 10 clients up. There is no single
  "moderato trails by X%" number: the gap is largest at 1 client and
  against slowapi's allow path.
- **Part of the gap is the extra round trip, by design.** moderato makes
  two sequential Redis round trips per decision (`TIME` for
  server-authoritative time, then the Lua script); slowapi and
  fastapi-limiter make one (confirmed with the command counts above). That
  buys windows that stay consistent across application instances without
  trusting client clocks. At 1 client moderato's p50 is 0.76–0.77 ms
  against slowapi's 0.54–0.56 ms on the allowed path; the raw floor
  attributes only ~0.12 ms to the extra round trip (at 1 client, 0.227 ms
  per two-call decision against 0.106 ms per one-call decision), so the
  rest of the ~0.2 ms gap is client-side work around the second call, which
  this benchmark does not isolate.
  Whether the tradeoff is right depends on whether you need distributed time
  consistency.
- **All three enforce their configured limits.** In the limited scenario
  each library allowed exactly the configured 100 per minute and 429'd
  the other 2,900 requests at every level in both runs.

### Caveats

- Single identity, single route, no network hop, no TLS, no multi-node
  Redis: this isolates library overhead but says nothing about network
  dominated deployments, where the extra Redis round trip matters less.
- The client, the ASGI app and Redis share two vCPUs, so the head-to-head
  numbers include contention that a deployment would not have.
- Configurations follow each library's documented usage as shipped; none
  were tuned. The comparison is fair to defaults, not to peak
  configurability.
- Both published runs are committed; rerunning on different hardware will
  produce different absolute numbers. The defensible claims are the
  relationships (client-bound vs Redis-bound, the 2-RTT pattern cost, the
  pool boundary), not the absolute req/s.
