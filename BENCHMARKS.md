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

Raw data: `benchmarks/results/full-20261007T-full3.json` (run 1) and
`benchmarks/results/full-20261007T-full4.json` (run 2). Each run is
a complete, independent execution of the whole suite.

### Throughput (limiter only, no HTTP)

3,000 requests per trial × 5 trials, high limit (`100000/minute`) so nothing
is denied.

| Run | Sequential (req/s) | Concurrent (req/s) | Speedup |
| --- | --- | --- | --- |
| 1 | 3,415 ± 132 (CV 3.9%) | 4,411 ± 71 (CV 1.6%) | 1.29× |
| 2 | 3,631 ± 205 (CV 5.7%) | 4,466 ± 105 (CV 2.4%) | 1.23× |

Per-trial values are in the JSON. Sequential throughput agrees across the
two runs to within ~6% (this sandbox is shared and noisier on the
sequential path); concurrent throughput agrees to within ~2% (CV up to
17% at individual concurrency levels in one run).

### Latency

2,000 sequential samples × 3 trials per run, percentiles pooled across
trials:

| Run | p50 | p90 | p95 | p99 | p99.9 | max |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 0.291 ms | 0.352 ms | 0.380 ms | 0.450 ms | 0.741 ms | 1.273 ms |
| 2 | 0.260 ms | 0.320 ms | 0.332 ms | 0.367 ms | 0.456 ms | 1.184 ms |

### Concurrency sweep

100 requests per client, 3 trials per level. Throughput plateaus around
4,453–5,226 req/s from 5 clients onward while per-request latency grows
roughly linearly with concurrency. The CPU columns show why: the benchmark
process is at ~100% of one core from 2 clients on, while the Redis server
stays at 28–35% of a core. The single-threaded Python client saturates
long before Redis does.

| Clients | Run 1 req/s (CV) | Run 1 p50 / p99 | Run 1 CPU client / Redis | Run 2 req/s (CV) | Run 2 p50 / p99 | Run 2 CPU client / Redis |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 3,931 ± 16.5% | 0.24 / 0.38 ms | 82% / 24% | 3,759 ± 10.7% | 0.26 / 0.38 ms | 87% / 25% |
| 2 | 4,719 ± 0.9% | 0.41 / 0.52 ms | 100% / 33% | 4,853 ± 0.3% | 0.40 / 0.50 ms | 100% / 30% |
| 5 | 4,854 ± 2.5% | 1.01 / 1.39 ms | 100% / 34% | 5,022 ± 3.3% | 0.97 / 1.41 ms | 100% / 33% |
| 10 | 4,872 ± 5.6% | 1.99 / 3.04 ms | 100% / 34% | 5,152 ± 0.6% | 1.92 / 2.48 ms | 100% / 32% |
| 25 | 4,890 ± 6.8% | 4.93 / 7.30 ms | 100% / 34% | 5,226 ± 0.7% | 4.75 / 5.92 ms | 100% / 35% |
| 50 | 4,483 ± 1.6% | 11.01 / 13.49 ms | 100% / 29% | 4,607 ± 0.8% | 10.75 / 12.82 ms | 100% / 28% |
| 100 | 4,453 ± 3.6% | 22.05 / 27.25 ms | 100% / 30% | 4,605 ± 0.8% | 21.45 / 25.79 ms | 99% / 28% |
| 200 | 4,566 ± 1.0% | 43.18 / 55.13 ms | 100% / 28% | 4,624 ± 0.8% | 42.89 / 53.10 ms | 100% / 28% |

(CV is across the 3 trials at that level; CPU is % of one core.)

### Algorithm comparison

1,000 sequential samples × 3 trials per algorithm. On this workload the
three algorithms cost about the same; all execute one Lua script per
decision.

| Algorithm | Run 1 throughput | Run 1 p50 / p99 | Run 2 throughput | Run 2 p50 / p99 |
| --- | --- | --- | --- | --- |
| fixed_window | 3,686 ± 174 req/s | 0.26 / 0.37 ms | 3,748 ± 155 req/s | 0.26 / 0.37 ms |
| token_bucket | 3,490 ± 34 req/s | 0.28 / 0.37 ms | 3,587 ± 23 req/s | 0.27 / 0.38 ms |
| sliding_window | 3,552 ± 32 req/s | 0.27 / 0.37 ms | 3,476 ± 219 req/s | 0.28 / 0.43 ms |

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
(free/premium/enterprise): 4,447 ± 175 req/s (run 1), 4,667 ± 23 req/s (run 2) —
consistent with the plain concurrency sweep.

### Default pool capacity

The shipped default pool rejects a Redis borrow once no connection is idle
and all 50 are checked out (redis-py's non-blocking asyncio pool raises
`Too many connections`). A dedicated probe splits 1,000 checks across
N workers per level (so exactly N checks are in flight, 25–200) on the
default pool and records both rejections and the observed peak pool usage:

| Concurrent | Run 1 outcomes | Run 1 peak in use | Run 2 outcomes | Run 2 peak in use |
| --- | --- | --- | --- | --- |
| 25 | 1,000 ok | 8 / 50 | 1,000 ok | 6 / 50 |
| 50 | 1,000 ok | 12 / 50 | 1,000 ok | 7 / 50 |
| 75 | 1,000 ok | 13 / 50 | 1,000 ok | 8 / 50 |
| 100 | 1,000 ok | 14 / 50 | 1,000 ok | 9 / 50 |
| 200 | 1,000 ok | 17 / 50 | 1,000 ok | 11 / 50 |

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

Raw data: `benchmarks/results/compare-20261007T-cmp7.json` (run 1) and
`benchmarks/results/compare-20261007T-cmp8.json` (run 2).

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
| 1 | 3,853 | 1,239 | 1,750 | 1,492 |
| 10 | 3,559 | 1,459 | 1,798 | 1,413 |
| 50 | 3,664 | 1,386 | 1,678 | 1,311 |
| 100 | 3,544 | 1,332 | 1,718 | 1,332 |
| **Run 2** | | | | |
| 1 | 3,398 | 1,227 | 1,637 | 1,433 |
| 10 | 3,344 | 1,374 | 1,709 | 1,406 |
| 50 | 3,394 | 1,339 | 1,645 | 1,315 |
| 100 | 3,266 | 1,319 | 1,608 | 1,351 |

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
| 1 | 3,384 | 1,228 | 1,107 | 1,562 |
| 10 | 3,632 | 1,378 | 1,222 | 1,521 |
| 50 | 3,598 | 1,331 | 1,139 | 1,424 |
| 100 | 3,468 | 1,328 | 1,134 | 1,363 |
| **Run 2** | | | | |
| 1 | 3,540 | 1,238 | 1,177 | 1,501 |
| 10 | 3,617 | 1,380 | 1,268 | 1,476 |
| 50 | 3,558 | 1,285 | 1,125 | 1,350 |
| 100 | 3,586 | 1,260 | 1,122 | 1,329 |

### Redis floor

Raw Redis operations per request, no HTTP stack, same client machine:

| Pattern | Run | 1 client | 10 clients | 50 clients | 100 clients |
| --- | --- | --- | --- | --- | --- |
| 1× EVALSHA | 1 | 8,964 | 12,349 | 12,739 | 12,427 |
| 1× EVALSHA | 2 | 9,404 | 13,874 | 11,925 | 12,566 |
| 2× EVALSHA (TIME + script) | 1 | 4,165 | 7,198 | 6,100 | 6,105 |
| 2× EVALSHA (TIME + script) | 2 | 3,586 | 6,507 | 5,241 | 6,097 |

(The 1-client numbers are the noisiest; from 10 clients on, each row is
stable within a few percent.)

### Moderato relative to each library

Percent difference in throughput, moderato versus the library, run 1 / run
2 (negative = moderato slower):

| Path | Against | 1 client | 10 clients | 50 clients | 100 clients |
| --- | --- | --- | --- | --- | --- |
| allowed | vs slowapi | -29% / -25% | -19% / -20% | -17% / -19% | -22% / -18% |
| allowed | vs fastapi-limiter | -17% / -14% | +3% / -2% | +6% / +2% | -0% / -2% |
| limited | vs slowapi | +11% / +5% | +13% / +9% | +17% / +14% | +17% / +12% |
| limited | vs fastapi-limiter | -21% / -18% | -9% / -6% | -6% / -5% | -3% / -5% |

### Interpretation

- **The HTTP stack dominates every library.** The plain app runs at
  ~3,266–3,853 req/s in-process on this box; all three limiter libraries land
  in the same ~1.1–1.8k band, adding a few tenths of a millisecond per
  request. None of them is the bottleneck a deployment would feel first.
- **Client-bound, not Redis-bound.** In the concurrency sweep the benchmark
  process runs at 99–100% of a core while the Redis server stays at
  28–35% of a core — the direct evidence for client-boundedness. The raw
  Redis floor is consistent with that: 8,964–13,874 ops/s for a single `EVALSHA`
  sits well above the limiter-only path's ~3.4–4.5k req/s, while the
  two-round-trip floor (3,586–7,198 ops/s) straddles the concurrent end of that
  range — Redis headroom is comfortable at low concurrency but is nearly
  spent once the client saturates at high concurrency.
- **The ranking depends on the path.** On the allowed path moderato is
  behind slowapi by roughly 17–29% and within −17% to +6% of
  fastapi-limiter with no consistent ordering from 10 clients up
  (14–29% behind both at 1 client). On the limited path moderato is ahead
  of slowapi at every level (+5% to +17%) — slowapi's rejection path is
  slower than its allow path in these runs — and 3–21% behind
  fastapi-limiter, 18–21% behind at 1 client. There is no single
  "moderato trails by X%" number: the gap is largest at 1 client and
  against slowapi's allow path.
- **Part of the gap is the extra round trip, by design.** moderato makes
  two sequential Redis round trips per decision (`TIME` for
  server-authoritative time, then the Lua script); slowapi and
  fastapi-limiter make one (confirmed with the command counts above). That
  buys windows that stay consistent across application instances without
  trusting client clocks. At 1 client moderato's p50 is 0.77–0.78 ms
  against slowapi's 0.54–0.58 ms on the allowed path; the raw floor
  attributes only ~0.1 ms to the extra round trip (at 1 client,
  0.240–0.279 ms per two-call decision against 0.106–0.112 ms per one-call
  decision), so the rest of the ~0.2 ms gap is client-side work around the
  second call, which this benchmark does not isolate.
  Whether the tradeoff is right depends on whether you need distributed time
  consistency.
- **All three enforce their configured limits.** In the limited scenario
  every level allowed exactly the configured 100 per minute and 429'd the
  other 2,900 requests, in both runs (each limited trial is boundary-gated
  and must remain in its level's starting minute).

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
