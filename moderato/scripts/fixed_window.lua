-- Fixed Window Rate Limiting Script
-- Implements atomic rate limiting using Redis with proper window alignment
--
-- KEYS[1] = rate limit key (e.g., "ratelimit:tenant123:free:1700000100")
-- ARGV[1] = max_requests (e.g., 100000 for 100 requests with 1000x multiplier)
-- ARGV[2] = window_seconds (e.g., 60 for 1 minute window)
-- ARGV[3] = window_end_timestamp (epoch when this window expires)
-- ARGV[4] = cost (e.g., 1000 for cost=1 with 1000x multiplier, default 1000)
--
-- Returns: {allowed (1 or 0), remaining, retry_after_ms, reset_at}

local key = KEYS[1]
local max_requests = tonumber(ARGV[1])
local window_seconds = tonumber(ARGV[2])
local window_end = tonumber(ARGV[3])
local cost_arg = ARGV[4]
local cost = 1000
if cost_arg then
    cost = tonumber(cost_arg)
end

-- Defense in depth: request cost must consume capacity
if cost == nil or cost <= 0 or cost ~= math.floor(cost) then
    return redis.error_reply("cost must be a positive integer")
end

-- Increment counter atomically by cost
local current = redis.call('INCRBY', key, cost)

-- Set expiration using EXPIREAT on first request (when counter equals cost)
-- This ensures the window expires at the correct boundary, not relative to first request
if current == cost then
    redis.call('EXPIREAT', key, window_end)
end

-- Get millisecond TTL so Retry-After can be rounded up accurately.
local ttl = redis.call('PTTL', key)

-- Handle edge cases where key might not have TTL set properly
-- TTL = -1 means key has no expiration (shouldn't happen, but be safe)
-- TTL = -2 means key doesn't exist
-- NOTE: We do NOT reset TTL when ttl=0 (key about to expire) - that's valid behavior
if ttl < 0 then
    -- Ensure expiration is set (in case EXPIREAT failed earlier)
    redis.call('EXPIREAT', key, window_end)
    ttl = redis.call('PTTL', key)
    -- EXPIREAT deletes keys whose time already passed; report an expired
    -- window instead of negative metadata.
    if ttl < 0 then
        ttl = 0
    end
end

-- Calculate if request is allowed
local allowed = 0
local remaining = 0

if current <= max_requests then
    -- Request is allowed
    allowed = 1
    remaining = max_requests - current
else
    -- Request is denied, no remaining capacity
    remaining = 0
end

-- Return results
-- allowed: 1 if request should proceed, 0 if rate limited
-- remaining: number of requests remaining in current window (with multiplier)
-- retry_after_ms: milliseconds until the current window resets
-- reset_at: Redis-authoritative window boundary
return {allowed, remaining, ttl, window_end}
