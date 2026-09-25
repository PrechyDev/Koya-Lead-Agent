"""Run-limit arithmetic (specs.md §7.1). The agent never chooses these numbers."""

import math

FIRST_POOL_PER_LEAD = 1.5  # owner, 2026-09-25 (D-88): the first search fetches 1.5x the leads wanted


def next_discovery_batch(limits: dict, usage: dict) -> int:
    """How many companies the next Apify call may fetch: first pool, then small top-ups, never past the total."""
    calls = int(usage.get("discovery_calls", 0))
    if calls >= int(limits["max_discovery_calls"]):
        return 0
    remaining = int(limits["max_candidates"]) - int(usage.get("candidates_found", 0))
    if calls == 0:  # e.g. 3 leads -> 5 candidates, 10 leads -> 12 (the preset pool is the ceiling)
        wanted = math.ceil(FIRST_POOL_PER_LEAD * int(limits.get("target_qualified") or 0))
        size = min(int(limits["first_pool"]), max(1, wanted)) if wanted else int(limits["first_pool"])
    else:
        size = int(limits["topup_size"])
    return max(0, min(size, remaining))

