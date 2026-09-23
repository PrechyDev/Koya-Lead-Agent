"""Server-side qualification rules (specs.md §8.2–8.3; E-15, E-16, E-17, E-31, E-45).

"Qualify from evidence, not guesses" is enforced here, in code, whatever the
agent asks for:
  * every cited source URL must have been fetched/returned in this run;
  * a hard filter only counts as 'pass' with evidence + a source URL;
  * any 'fail'  -> not_qualified;
  * any 'unknown' (or a missing check) -> needs_review, never qualified;
  * qualified needs confidence >= 0.70.
Pre-screening rejects candidates that already fail a hard filter on the
discovery data, before any scrape or Claude call.
"""

from typing import Literal

from pydantic import BaseModel, Field

from app.lib.domain import same_url
from app.lib.objective import normalize_geo, parse_headcount_range

QUALIFIED_MIN_CONFIDENCE = 0.70
Status = Literal["qualified", "not_qualified", "needs_review"]


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


def decide_status(
    requested: Status,
    checks: list[HardFilterCheck],
    confidence: float,
    required_filters: list[str] | None = None,
    disqualifier_checks: list[DisqualifierCheck] | None = None,
    required_disqualifiers: list[str] | None = None,
) -> tuple[Status, list[str]]:
    """Return the status the server will actually store, plus notes explaining any change."""
    notes: list[str] = []
    effective: list[str] = []
    for d in disqualifier_checks or []:
        if d.applies == "yes":
            notes.append(f'disqualifier applies: "{d.disqualifier}"')
            effective.append("fail")
        elif d.applies == "no" and (d.evidence.strip() and d.source_url):
            effective.append("pass")
        else:
            if d.applies == "no":
                notes.append(f'"{d.disqualifier}" was marked not applying without evidence, so it counts as unknown')
            effective.append("unknown")
    if required_disqualifiers:
        covered = {d.disqualifier.strip().lower() for d in disqualifier_checks or []}
        missing = [d for d in required_disqualifiers if d.strip().lower() not in covered]
        if missing:
            notes.append("no check for disqualifier(s): " + "; ".join(missing))
            effective += ["unknown"] * len(missing)
    for c in checks:
        result = c.result
        if result == "pass" and (not c.evidence.strip() or not c.source_url):
            notes.append(f'"{c.filter}" was marked pass without evidence and a source URL, so it counts as unknown')
            result = "unknown"
        effective.append(result)

    if required_filters:
        covered = {c.filter.strip().lower() for c in checks}
        missing = [f for f in required_filters if f.strip().lower() not in covered]
        if missing:
            notes.append("no check for hard filter(s): " + "; ".join(missing))
            effective += ["unknown"] * len(missing)

    if "fail" in effective:
        if requested != "not_qualified":
            notes.append("a hard filter failed, so the lead is not_qualified")
        return "not_qualified", notes
    if requested == "not_qualified":
        return "not_qualified", notes
    if "unknown" in effective:
        if requested == "qualified":
            notes.append("at least one hard filter is unknown, so the lead needs human review instead of qualified")
        return "needs_review", notes
    if requested == "qualified" and confidence < QUALIFIED_MIN_CONFIDENCE:
        notes.append(f"confidence {confidence:.2f} is below {QUALIFIED_MIN_CONFIDENCE:.2f}, so it needs review")
        return "needs_review", notes
    return requested, notes


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

    geos = {normalize_geo(g) for g in (icp.get("geography") or []) if g}
    if geos:
        country = _hq_country(discovery)
        if country is None:
            unknown = True
            reasons.append("HQ country not in discovery data")
        elif country not in geos:
            return "rejected:geography", f"HQ country is {country.upper()}, outside {sorted(geos)}"

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
