-- Sliding Window Rate Limiting Script
-- Implements sliding window algorithm with weighted previous window
-- Uses integer-only arithmetic to avoid Lua floating-point inconsistencies
--
-- KEYS[1] = current window key (e.g., "ratelimit:user123:default:sliding:1700000100")
-- KEYS[2] = previous window key (e.g., "ratelimit:user123:default:sliding:1700000040")
-- ARGV[1] = max_requests (e.g., 100000 for 100 requests with 1000x multiplier)
-- ARGV[2] = window_seconds (e.g., 60 for 1 minute window)
-- ARGV[3] = current_timestamp (seconds since epoch)
-- ARGV[4] = cost (tokens to consume, e.g., 1000 for cost=1 with 1000x multiplier)
--
-- Returns: {allowed (1 or 0), remaining, retry_after_ms}
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

local current_key = KEYS[1]
local previous_key = KEYS[2]
local max_requests = tonumber(ARGV[1])
local window_seconds = tonumber(ARGV[2])
local current_timestamp = tonumber(ARGV[3])
local cost = tonumber(ARGV[4]) or 1000  -- Default to 1000 (cost=1) if not provided

-- Get counts from both windows
local current_count = tonumber(redis.call('GET', current_key)) or 0
local previous_count = tonumber(redis.call('GET', previous_key)) or 0

-- Calculate position in current window using integer math
local window_start = current_timestamp - (current_timestamp % window_seconds)
local elapsed_in_window = current_timestamp - window_start

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
    remaining = 0

    -- Calculate time until enough capacity is available
    -- As window progresses, previous_weight decreases, freeing up capacity
    --
    -- Math derivation:
    -- At time t (elapsed seconds into window):
    --   weight(t) = (window_seconds - t) / window_seconds
    --   weighted_count(t) = current_count + previous_count * weight(t)
    --
    -- We need: weighted_count(t) + cost <= max_requests
    -- Solve for t:
    --   current_count + previous_count * (window_seconds - t) / window_seconds <= max_requests - cost
    --   previous_count * (window_seconds - t) <= (max_requests - cost - current_count) * window_seconds
    --   (window_seconds - t) <= (max_requests - cost - current_count) * window_seconds / previous_count
    --   t >= window_seconds - (max_requests - cost - current_count) * window_seconds / previous_count
    --
    -- Using integer math with 1000x scale:
    --   t_needed = window_seconds - ((max_requests - cost - current_count) * window_seconds) / previous_count
    --   wait_time = t_needed - elapsed_in_window

    local tokens_needed = (weighted_count + cost) - max_requests

    if previous_count > 0 then
        -- Calculate exact time until weight decrease frees enough tokens
        -- available_capacity = max_requests - cost - current_count
        local available_capacity = max_requests - cost - current_count

        if available_capacity < 0 then
            -- Current window alone exceeds limit, must wait for next window
            retry_after_ms = remaining_in_window * 1000
        else
            -- Calculate when previous window weight will be low enough
            -- We need: previous_count * weight <= available_capacity
            -- weight = (window_seconds - t) / window_seconds
            -- So: (window_seconds - t) <= available_capacity * window_seconds / previous_count
            -- t >= window_seconds - (available_capacity * window_seconds / previous_count)

            -- Using integer math: multiply by 1000 for precision
            local target_elapsed = window_seconds * 1000 -
                math.floor((available_capacity * window_seconds * 1000) / previous_count)

            -- Wait time is target_elapsed - current_elapsed (in milliseconds)
            local wait_ms = target_elapsed - (elapsed_in_window * 1000)

            if wait_ms > 0 then
                retry_after_ms = wait_ms
            else
                -- Calculation suggests we should already be allowed (edge case)
                -- Use minimum wait time
                retry_after_ms = 1000
            end
        end
    else
        -- No previous window count, but current exceeds limit
        -- Wait until next window starts
        retry_after_ms = remaining_in_window * 1000
    end

    -- Ensure at least 1 second wait and cap at remaining_in_window
    if retry_after_ms < 1000 then
        retry_after_ms = 1000
    end
    if retry_after_ms > remaining_in_window * 1000 then
        retry_after_ms = remaining_in_window * 1000
    end
end

-- Return results
-- allowed: 1 if request should proceed, 0 if rate limited
-- remaining: estimated tokens remaining (with multiplier)
-- retry_after_ms: milliseconds until rate limit might allow request
return {allowed, remaining, retry_after_ms}
