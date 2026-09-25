"""Continuing a run that stopped early, and settling size bands (D-100). Pure functions, no I/O."""

from decimal import Decimal

from app.config import DRAFT_COST_PER_LEAD_USD, RUN_CAP_MARGIN, run_cap_usd
from app.lib.objective import parse_headcount_range

RESUMABLE = ("paused", "failed", "completed_partial", "cancelled")


def work_left(run: dict, leads: list[dict]) -> dict:
    """What a stopped run still has to do: companies found but not researched, qualified leads without drafts,
    and whether it may still search for more companies."""
    usage, limits = run.get("usage") or {}, run.get("limits") or {}
    qualified = [lead for lead in leads if lead.get("qualification_status") == "qualified"]
    return {
        "pending": [lead["company_domain"] for lead in leads if lead.get("qualification_status") == "pending"],
        "undrafted": [lead["company_domain"] for lead in qualified if lead.get("outreach_status") != "drafted"],
        "still_needed": max(0, int(limits.get("target_qualified") or 0) - len(qualified)),
        "can_search": (int(usage.get("candidates_found", 0)) < int(limits.get("max_candidates") or 0)
                       and int(usage.get("discovery_calls", 0)) < int(limits.get("max_discovery_calls") or 0)),
    }


def continue_plan(run: dict, leads: list[dict]) -> dict | None:
    """The Continue / Retry button for a stopped run, or None when there's nothing sensible to continue."""
    if run.get("status") not in RESUMABLE or not run.get("icp"):
        return None
    left = work_left(run, leads)
    n_pending, n_undrafted = len(left["pending"]), len(left["undrafted"])
    if n_pending:
        what = f"research {n_pending} remaining compan{'y' if n_pending == 1 else 'ies'}"
    elif n_undrafted:
        what = f"write drafts for {n_undrafted} lead{'' if n_undrafted == 1 else 's'}"
    elif left["still_needed"] and left["can_search"]:
        what = "search for more companies"
    else:
        return None
    verb = "Retry" if run.get("status") == "failed" else "Continue"
    return {**left, "label": f"{verb}: {what}"}


def continue_cap(run: dict, left: dict) -> float:
    """The run's cap for continuing (D-100): keep what's left of it if that covers the remaining work, otherwise
    size a new cap for it on top of what was already spent. The client never sees these numbers."""
    limits = run.get("limits") or {}
    spent = Decimal(str(run.get("cost_usd") or 0))
    cap = Decimal(str(limits.get("max_budget_usd") or 0))
    needed = (Decimal(str(run_cap_usd(left["still_needed"], int(limits.get("max_candidates") or 0))))
              + len(left["undrafted"]) * DRAFT_COST_PER_LEAD_USD * RUN_CAP_MARGIN)
    return float(cap if cap - spent >= needed else (spent + needed).quantize(Decimal("0.01")))


def settle_headcount(checks: list[dict], headcount_range: str | None, band: dict | None, source_url: str | None
                     ) -> list[dict]:
    """A size filter the researcher left "unknown" passes when LinkedIn's size band overlaps the range (owner,
    D-100): LinkedIn only gives bands (11-50, 51-200), so "20-80 employees" can rarely be confirmed exactly, and
    outreach sizing is approximate. A band fully outside the range is already rejected by the pre-screen."""
    low, high = parse_headcount_range(headcount_range)
    b_low, b_high = (band or {}).get("start"), (band or {}).get("end")
    if low is None or b_low is None:
        return checks
    overlaps = (high is None or b_low <= high) and (b_high is None or b_high >= low)
    out = []
    for check in checks:
        is_size = parse_headcount_range(check.get("filter")) == (low, high) or "employee" in str(check.get("filter")).lower()
        if overlaps and is_size and check.get("result") == "unknown":
            band_text = f"{b_low}-{b_high}" if b_high is not None else f"{b_low}+"
            check = {**check, "result": "pass", "source_url": check.get("source_url") or source_url,
                     "evidence": (f"{check.get('evidence') or ''} LinkedIn size band {band_text} overlaps "
                                  f"{low}-{high if high is not None else '+'}.").strip()}
        out.append(check)
    return out
