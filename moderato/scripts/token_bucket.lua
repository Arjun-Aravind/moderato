-- Token Bucket Rate Limiting Script
-- Implements token bucket algorithm with atomic operations
--
-- KEYS[1] = rate limit key (e.g., "ratelimit:tenant123:premium:bucket")
-- ARGV[1] = max_tokens (bucket capacity, e.g., 100000 for 100 tokens with 1000x multiplier)
-- ARGV[2] = refill_rate_per_second (tokens per second, float with 1000x multiplier)
-- ARGV[3] = window_seconds (window duration for TTL calculation)
-- ARGV[4] = cost (tokens to consume, e.g., 1000 for cost=1 with 1000x multiplier)
--
-- Returns: {allowed (1 or 0), remaining, retry_after_ms, reset_at}
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
local cost_arg = ARGV[4]
local cost = 1000
if cost_arg then
    cost = tonumber(cost_arg)
end

if max_tokens == nil or max_tokens <= 0 or max_tokens > 9007199254000 or max_tokens ~= math.floor(max_tokens) then
    return redis.error_reply("capacity must be a positive integer no greater than 9007199254000")
end
if window_seconds == nil or window_seconds <= 0 or window_seconds > 86400 or window_seconds ~= math.floor(window_seconds) then
    return redis.error_reply("window_seconds must be a positive integer no greater than 86400")
end
if refill_rate_per_second == nil or refill_rate_per_second ~= refill_rate_per_second or refill_rate_per_second < max_tokens / 86400 or refill_rate_per_second > 9007199254000 then
    return redis.error_reply("refill rate must be finite and between capacity/86400 and 9007199254000")
end

-- Defense in depth: request cost must consume capacity
if cost == nil or cost <= 0 or cost == math.huge or cost ~= math.floor(cost) then
    return redis.error_reply("cost must be a positive integer")
end

-- Never expire a custom-rate bucket before it could refill completely.
local ttl = window_seconds * 2 + 60
if refill_rate_per_second ~= max_tokens / window_seconds then
    ttl = math.max(ttl, math.ceil(max_tokens / refill_rate_per_second) * 2 + 60)
end

-- Reading TIME here instead of in the client saves a round trip per check
-- and keeps every instance on the Redis clock.
local time = redis.call('TIME')
local current_time_ms = tonumber(time[1]) * 1000 + math.floor(tonumber(time[2]) / 1000)

-- Get current bucket state
local bucket = redis.call('HMGET', key, 'tokens', 'last_refill_ms', 'refill_units')
local current_tokens = tonumber(bucket[1]) or max_tokens  -- Start with full bucket
local last_refill_ms = tonumber(bucket[2]) or current_time_ms
-- Whole scaled units already credited since last_refill_ms. Keeping a
-- common origin avoids discarding a fractional unit on every check.
-- Existing hashes have no refill_units field and start at zero.
local refill_units = tonumber(bucket[3]) or 0

-- Calculate time elapsed since last refill (in milliseconds)
local time_elapsed_ms = math.max(0, current_time_ms - last_refill_ms)

local accrued = 0
local fractional_credit = 0
local accrued_units
local time_until_refill
if refill_rate_per_second == max_tokens / window_seconds then
    -- Public policies refill exactly one capacity per window. Move past
    -- whole windows without discarding fractional progress, so a busy
    -- bucket's origin and credited-unit counter cannot grow indefinitely.
    local window_ms = window_seconds * 1000
    local whole_windows = math.floor(time_elapsed_ms / window_ms)
    if whole_windows > 0 then
        last_refill_ms = last_refill_ms + whole_windows * window_ms
        refill_units = refill_units - whole_windows * max_tokens
        time_elapsed_ms = time_elapsed_ms % window_ms
    end
    -- Quotient/remainder decomposition keeps each integer product below
    -- 2^53, even at the maximum capacity and a day-long window.
    local units_per_ms = math.floor(max_tokens / window_ms)
    local remainder = max_tokens % window_ms
    local partial_product = remainder * time_elapsed_ms
    accrued_units = units_per_ms * time_elapsed_ms + math.floor(partial_product / window_ms)
    -- Use the same integer credit calculation for deadlines. Floating-point
    -- division followed by ceil can add a spurious millisecond at a boundary.
    -- One window always replenishes the needed units; at most 27 iterations.
    time_until_refill = function(units_needed)
        local low = 0
        local high = window_ms
        while low < high do
            local delay = math.floor((low + high) / 2)
            local elapsed = time_elapsed_ms + delay
            local phase = elapsed % window_ms
            local credit = math.floor(elapsed / window_ms) * max_tokens
                + units_per_ms * phase + math.floor((remainder * phase) / window_ms)
            if credit - accrued_units >= units_needed then
                high = delay
            else
                low = delay + 1
            end
        end
        return low
    end
else
    -- Direct backend callers may supply an independent refill rate.
    accrued = (refill_rate_per_second * time_elapsed_ms) / 1000
    if accrued > 9007199254740991 then
        return redis.error_reply("custom refill accrual exceeds the exact numeric range")
    end
    accrued_units = math.floor(accrued)
    fractional_credit = accrued - accrued_units
    time_until_refill = function(units_needed)
        return math.ceil(((units_needed - fractional_credit) * 1000) / refill_rate_per_second)
    end
end
local tokens_to_add = math.max(0, accrued_units - refill_units)
refill_units = math.max(refill_units, accrued_units)

-- Add tokens to bucket, but don't exceed max capacity
local new_tokens = math.min(max_tokens, current_tokens + tokens_to_add)

-- Saturation discards all idle credit, including fractional credit. Start
-- the next refill period at this check before consuming from a full bucket.
if new_tokens == max_tokens then
    last_refill_ms = current_time_ms
    refill_units = 0
    fractional_credit = 0
    time_elapsed_ms = 0
    accrued_units = 0
end

if cost > max_tokens then
    return {0, new_tokens, -1, 0}
end

-- Determine if request is allowed
local allowed = 0
local remaining = 0
local retry_after_ms = 0

if new_tokens >= cost then
    -- Request is allowed - consume tokens
    allowed = 1
    new_tokens = new_tokens - cost
    remaining = new_tokens

    -- Update bucket state without losing fractional refill progress
    redis.call('HMSET', key, 'tokens', string.format('%.0f', new_tokens),
        'last_refill_ms', string.format('%.0f', last_refill_ms),
        'refill_units', string.format('%.0f', refill_units))

    -- Set expiry to prevent memory leaks after inactivity.
    redis.call('EXPIRE', key, ttl)
else
    -- Request is denied - not enough tokens
    allowed = 0
    remaining = new_tokens

    -- Calculate how long until enough tokens are available
    retry_after_ms = time_until_refill(cost - new_tokens)

    -- Update bucket state with refilled tokens (even though request denied)
    redis.call('HMSET', key, 'tokens', string.format('%.0f', new_tokens),
        'last_refill_ms', string.format('%.0f', last_refill_ms),
        'refill_units', string.format('%.0f', refill_units))

    -- Set expiry
    redis.call('EXPIRE', key, ttl)
end

-- Return results
-- allowed: 1 if request should proceed, 0 if rate limited
-- remaining: number of tokens remaining in bucket (with multiplier)
-- Allowed reset_at describes full refill; a denial describes this cost.
local reset_at
if allowed == 1 then
    local full_refill_after_ms = time_until_refill(max_tokens - new_tokens)
    reset_at = math.ceil((current_time_ms + full_refill_after_ms) / 1000)
else
    -- A denied caller must not be told to come back before Retry-After,
    -- which clients see rounded up to at least one second from now.
    local retry_after_s = math.max(1, math.ceil(retry_after_ms / 1000))
    reset_at = math.ceil((current_time_ms + retry_after_s * 1000) / 1000)
end

-- retry_after_ms: milliseconds until enough tokens available (0 if allowed)
return {allowed, remaining, retry_after_ms, reset_at}
