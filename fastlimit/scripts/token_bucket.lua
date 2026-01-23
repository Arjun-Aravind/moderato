-- Token Bucket Rate Limiting Script
-- Implements token bucket algorithm with atomic operations
--
-- KEYS[1] = rate limit key (e.g., "ratelimit:tenant123:premium:bucket")
-- ARGV[1] = max_tokens (bucket capacity, e.g., 100000 for 100 tokens with 1000x multiplier)
-- ARGV[2] = refill_rate_per_second (tokens per second, integer with 1000x multiplier)
-- ARGV[3] = window_seconds (window duration for TTL calculation)
-- ARGV[4] = current_time_ms (current timestamp in milliseconds)
-- ARGV[5] = cost (tokens to consume, e.g., 1000 for cost=1 with 1000x multiplier)
--
-- Returns: {allowed (1 or 0), remaining, retry_after_ms}
--
-- Token Bucket Algorithm:
-- - Tokens are continuously added at refill_rate
-- - Bucket has maximum capacity of max_tokens
-- - Each request consumes 'cost' tokens
-- - If not enough tokens, request is denied
-- - Uses millisecond precision to support low rates (e.g., 1/hour)

local key = KEYS[1]
local max_tokens = tonumber(ARGV[1])
local refill_rate_per_second = tonumber(ARGV[2])
local window_seconds = tonumber(ARGV[3])
local current_time_ms = tonumber(ARGV[4])
local cost = tonumber(ARGV[5]) or 1000  -- Default to 1000 (cost=1) if not provided

-- Get current bucket state
local bucket = redis.call('HMGET', key, 'tokens', 'last_refill_ms')
local current_tokens = tonumber(bucket[1]) or max_tokens  -- Start with full bucket
local last_refill_ms = tonumber(bucket[2]) or current_time_ms

-- Calculate time elapsed since last refill (in milliseconds)
local time_elapsed_ms = math.max(0, current_time_ms - last_refill_ms)

-- Calculate tokens to add based on elapsed time
-- tokens_to_add = refill_rate_per_second * (time_elapsed_ms / 1000)
-- Use integer math: tokens_to_add = (refill_rate_per_second * time_elapsed_ms) / 1000
local tokens_to_add = 0
if refill_rate_per_second > 0 then
    tokens_to_add = math.floor((refill_rate_per_second * time_elapsed_ms) / 1000)
end

-- Add tokens to bucket, but don't exceed max capacity
local new_tokens = math.min(max_tokens, current_tokens + tokens_to_add)

-- Determine if request is allowed
local allowed = 0
local remaining = 0
local retry_after_ms = 0

if new_tokens >= cost then
    -- Request is allowed - consume tokens
    allowed = 1
    new_tokens = new_tokens - cost
    remaining = new_tokens

    -- Update bucket state with millisecond timestamp
    redis.call('HMSET', key, 'tokens', new_tokens, 'last_refill_ms', current_time_ms)

    -- Set expiry to prevent memory leaks
    -- Use window_seconds * 2 as a safe TTL (bucket expires after inactivity)
    local ttl = window_seconds * 2 + 60  -- Add 60s buffer
    redis.call('EXPIRE', key, ttl)
else
    -- Request is denied - not enough tokens
    allowed = 0
    remaining = 0

    -- Calculate how long until enough tokens are available
    local tokens_needed = cost - new_tokens
    if refill_rate_per_second > 0 then
        -- time_needed_ms = (tokens_needed / refill_rate_per_second) * 1000
        -- Integer math: time_needed_ms = (tokens_needed * 1000) / refill_rate_per_second
        retry_after_ms = math.ceil((tokens_needed * 1000) / refill_rate_per_second)
    else
        -- If refill rate is 0 (shouldn't happen), wait for full window
        retry_after_ms = window_seconds * 1000
    end

    -- Update bucket state with refilled tokens (even though request denied)
    redis.call('HMSET', key, 'tokens', new_tokens, 'last_refill_ms', current_time_ms)

    -- Set expiry
    local ttl = window_seconds * 2 + 60
    redis.call('EXPIRE', key, ttl)
end

-- Return results
-- allowed: 1 if request should proceed, 0 if rate limited
-- remaining: number of tokens remaining in bucket (with multiplier)
-- retry_after_ms: milliseconds until enough tokens available (0 if allowed)
return {allowed, remaining, retry_after_ms}
