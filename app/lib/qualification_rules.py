"""Server-side qualification rules (specs.md §8.2–8.3; E-15, E-16, E-17, E-31, E-45).

"Qualify from evidence, not guesses" is enforced in code, whatever the agent asks for:
  * the check models below are what the researcher must fill in (evidence + source per check);
  * every cited source URL must have been fetched in this run (invalid_sources);
  * the status itself comes from the evidence band in app/lib/scoring.py (compute_fit_score + decide, D-80):
    any fail -> not_qualified; anything unknown/unevidenced/missing -> needs_review; all pass -> qualified.
Pre-screening rejects candidates that already fail a hard filter on the
discovery data, before any scrape or Claude call.
"""

from typing import Literal

from pydantic import BaseModel, Field

from app.lib.domain import same_url
from app.lib.objective import country_code, parse_headcount_range


class HardFilterCheck(BaseModel):
    filter: str = Field(min_length=1)
    result: Literal["pass", "fail", "unknown"]
    evidence: str = ""
    source_url: str | None = None


class DisqualifierCheck(BaseModel):
    """'Does this disqualifier apply?' yes = disqualified; no needs evidence; unknown = can't confirm."""
    disqualifier: str = Field(min_length=1)
    applies: Literal["yes", "no", "unknown"]
    evidence: str = ""
    source_url: str | None = None


class SoftPreferenceCheck(BaseModel):
    """Soft preferences improve fit and confidence; they never change the qualification status."""
    preference: str = Field(min_length=1)
    result: Literal["matched", "not_matched", "unknown"]
    evidence: str = ""
    source_url: str | None = None


def invalid_sources(source_urls: list[str], allowed_urls: list[str]) -> list[str]:
    return [u for u in source_urls if not any(same_url(u, a) for a in allowed_urls)]


# ---------------------------------------------------------------------------
# Pre-screen on discovery data (no scrape, no Claude).
# ---------------------------------------------------------------------------
def _hq_country(discovery: dict) -> str | None:
    code = ((discovery.get("hq") or {}).get("country_code") or "").lower()
    return code or None


def prescreen(discovery: dict, icp: dict) -> tuple[str, str]:
    """('passed' | 'rejected:<filter>' | 'unknown', reason)."""
    reasons: list[str] = []
    unknown = False

    wanted = [g for g in (icp.get("geography") or []) if g and str(g).strip()]
    codes = {c for c in (country_code(g) for g in wanted) if c}
    regions = [g for g in wanted if country_code(g) is None]  # "Europe", "North America": not checkable here
    if wanted:
        country = _hq_country(discovery)
        if country is None:
            unknown = True
            reasons.append("HQ country not in discovery data")
        elif country in codes:
            pass
        elif regions:
            # Never reject on a region we can't map to countries; the researcher checks it from evidence.
            unknown = True
            reasons.append(f"HQ country is {country.upper()}; can't confirm it is in {', '.join(regions)} "
                           "from discovery data")
        else:
            return "rejected:geography", f"HQ country is {country.upper()}, outside {sorted(codes)}"

    low, high = parse_headcount_range(icp.get("headcount_range"))
    if low is not None or high is not None:
        band = discovery.get("employee_count_range") or {}
        b_low, b_high = band.get("start"), band.get("end")
        members = discovery.get("employee_count_linkedin")
        if b_low is not None and high is not None and b_low > high:
            return "rejected:headcount", f"stated size {b_low}-{b_high or '+'} is above {high}"
        if b_high is not None and low is not None and b_high < low:
            return "rejected:headcount", f"stated size {b_low}-{b_high} is below {low}"
        # LinkedIn member count is a lower bound on real headcount.
        if isinstance(members, int) and high is not None and members > high:
            return "rejected:headcount", f"{members} employees on LinkedIn, above {high}"
        if b_low is None and b_high is None:
            unknown = True
            reasons.append("no stated company size")
        elif (low is not None and b_low is not None and b_low < low) or (
            high is not None and (b_high is None or b_high > high)
        ):
            unknown = True
            reasons.append(f"stated size band {b_low}-{b_high or '+'} straddles {low}-{high}")
        if isinstance(members, int) and b_low is not None and members * 5 < b_low:
            reasons.append(
                f"LinkedIn shows only {members} employees against a stated {b_low}-{b_high}; verify headcount"
            )

    if unknown:
        return "unknown", "; ".join(reasons)
    return "passed", "; ".join(reasons) or "matches geography and size on discovery data"
