"""Run-limit arithmetic (specs.md §7.1). The agent never chooses these numbers."""


def next_discovery_batch(limits: dict, usage: dict) -> int:
    """How many companies the next Apify call may fetch: first pool, then small top-ups, never past the total."""
    calls = int(usage.get("discovery_calls", 0))
    if calls >= int(limits["max_discovery_calls"]):
        return 0
    remaining = int(limits["max_candidates"]) - int(usage.get("candidates_found", 0))
    size = int(limits["first_pool"]) if calls == 0 else int(limits["topup_size"])
    return max(0, min(size, remaining))

