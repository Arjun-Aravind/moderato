-- Sliding Window Rate Limiting Script
-- Implements sliding window algorithm with weighted previous window
-- Uses integer-only arithmetic to avoid Lua floating-point inconsistencies
--
-- KEYS[1] = window key prefix ending in ':' (e.g., "ratelimit:user123:default:sliding:")
-- NOTE: the current and previous window keys (prefix .. window_start) are
-- derived inside the script, so Redis Cluster deployments need a per-tenant
-- hash tag in the prefix (e.g. "ratelimit:{user123}:default:sliding:").
-- ARGV[1] = max_requests (e.g., 100000 for 100 requests with 1000x multiplier)
-- ARGV[2] = window_seconds (e.g., 60 for 1 minute window)
-- ARGV[3] = cost (tokens to consume, e.g., 1000 for cost=1 with 1000x multiplier)
--
-- Returns: {allowed (1 or 0), remaining, retry_after_ms, reset_at}
--
-- Sliding Window Algorithm:
-- - Combines current window with weighted portion of previous window
-- - Weight = percentage of window remaining (not elapsed)
-- - Example: 30 seconds into 60-second window = 50% weight from previous
-- - Formula: weighted_count = current_count + (prev_count * weight)
--
-- Integer Math Strategy:
-- - Use fixed-point weight: weight_fp = ((window_seconds - elapsed) * 1000) / window_seconds
-- - This gives a value 0-1000 representing 0.000 to 1.000
-- - weighted_count = current_count + (previous_count * weight_fp) / 1000

local key_prefix = KEYS[1]
local max_requests = tonumber(ARGV[1])
local window_seconds = tonumber(ARGV[2])
local cost_arg = ARGV[3]
local cost = 1000
if cost_arg then
    cost = tonumber(cost_arg)
end

if window_seconds == nil or window_seconds <= 0 or window_seconds ~= math.floor(window_seconds) then
    return redis.error_reply("window_seconds must be a positive integer")
end

-- Defense in depth: request cost must consume capacity
if cost == nil or cost <= 0 or cost ~= math.floor(cost) then
    return redis.error_reply("cost must be a positive integer")
end

-- Select the windows from Redis TIME in this atomic script. A client-side
-- TIME call costs an extra round trip and can pick a window that has
-- already ended by the time the script runs.
local time = redis.call('TIME')
local current_timestamp = tonumber(time[1])
local current_timestamp_ms = current_timestamp * 1000 + math.floor(tonumber(time[2]) / 1000)

-- Calculate position in current window using integer math
local window_start = current_timestamp - (current_timestamp % window_seconds)
local elapsed_in_window = current_timestamp - window_start
local current_key = key_prefix .. window_start
local previous_key = key_prefix .. (window_start - window_seconds)

-- Get counts from both windows
local current_count = tonumber(redis.call('GET', current_key)) or 0
local previous_count = tonumber(redis.call('GET', previous_key)) or 0

-- Calculate weight for previous window using fixed-point arithmetic (0-1000 scale)
-- Weight decreases as we progress through current window
-- At start of window (elapsed=0): weight = 1000 (100%)
-- At end of window (elapsed=window_seconds): weight = 0 (0%)
local remaining_in_window = window_seconds - elapsed_in_window
local prev_weight_fp = 0
if window_seconds > 0 then
    prev_weight_fp = math.floor((remaining_in_window * 1000) / window_seconds)
end

-- Calculate weighted count using integer math
-- weighted_count = current_count + (previous_count * weight) / 1000
local weighted_previous = math.floor((previous_count * prev_weight_fp) / 1000)
local weighted_count = current_count + weighted_previous

-- Check if request is allowed (before adding cost)
local allowed = 0
local remaining = 0
local retry_after_ms = 0

if (weighted_count + cost) <= max_requests then
    -- Request is allowed - increment current window
    allowed = 1

    -- Increment current window counter
    current_count = redis.call('INCRBY', current_key, cost)

    -- Set TTL on current window (2x window to keep previous)
    redis.call('EXPIRE', current_key, window_seconds * 2)

    -- Recalculate weighted count after increment (integer math)
    weighted_count = current_count + weighted_previous
    remaining = math.max(0, max_requests - weighted_count)
else
    -- Request is denied
    allowed = 0
    remaining = math.max(0, max_requests - weighted_count)

    if cost <= max_requests then
        -- Without intervening admissions the estimate is monotone. Search
        -- whole Redis seconds using the same permille rounding as admission.
        -- At rollover current becomes previous; it does not disappear.
        -- By the second boundary both sampled counts have aged out.
        local low = current_timestamp + 1
        local high = window_start + window_seconds * 2
        while low < high do
            local candidate = math.floor((low + high) / 2)
            local candidate_count
            if candidate < window_start + window_seconds then
                local weight = math.floor(
                    ((window_start + window_seconds - candidate) * 1000) / window_seconds)
                candidate_count = current_count + math.floor((previous_count * weight) / 1000)
            else
                local weight = math.floor(
                    ((window_start + window_seconds * 2 - candidate) * 1000) / window_seconds)
                candidate_count = math.floor((current_count * weight) / 1000)
            end
            if candidate_count + cost <= max_requests then
                high = candidate
            else
                low = candidate + 1
            end
        end
        retry_after_ms = low * 1000 - current_timestamp_ms
    else
        -- Permanent oversized-cost denial is handled in the bounds change.
        retry_after_ms = remaining_in_window * 1000
    end
end

-- Return results
-- allowed: 1 if request should proceed, 0 if rate limited
-- remaining: estimated tokens remaining (with multiplier)
local reset_at
if allowed == 1 then
    reset_at = window_start + window_seconds
else
    local retry_after_s = math.max(1, math.ceil(retry_after_ms / 1000))
    reset_at = math.ceil((current_timestamp_ms + retry_after_s * 1000) / 1000)
end

-- retry_after_ms: milliseconds until this cost fits the estimate, assuming
-- no intervening traffic and retained state (for costs within capacity).
return {allowed, remaining, retry_after_ms, reset_at}
