"""Koya's standard ICP defaults (owner-approved, 2026-09-24; specs D-58).

The rule for what an objective must contain (D-58):
  * REQUIRED: what kind of company (a company type or an industry). Without it there is nothing to search for,
    so save_icp turns the run into a clarification question instead.
  * DEFAULTED: everything else. When the objective doesn't say, code (not the AI) fills in the value below and
    records it as an assumption, so the same vague objective always gets the same ICP and the user sees exactly
    what was assumed on the ICP tab.

The home page lists these same values under "What we assume when you leave something out".

Separately, Koya's competitors (firms that also place AI automation talent) are ALWAYS excluded, even when the
objective names its own exclusions, unless the objective asks for them (D-89): see apply_competitor_rule.
"""

import re
from dataclasses import dataclass

DEFAULT_LEAD_COUNT = 10
MAX_LEAD_COUNT = 10


@dataclass(frozen=True)
class Default:
    field: str        # ICP field it fills
    label: str        # shown to users
    value: object     # what gets filled in
    why: str          # shown to users and recorded in the assumption
    hard_filter: str | None = None  # also added as a hard filter (so every company is checked against it)


DEFAULTS: list[Default] = [
    Default("geography", "Location", ["United States"],
            "Koya's default outbound market; name any other country or region in your objective",
            hard_filter="Headquartered in the United States"),
    Default("headcount_range", "Company size", "10-100",
            "Koya's buyers are founders and operators of small teams with repetitive operational work",
            hard_filter="10-100 employees"),
    Default("buyer_persona", "Who we'd contact", "Founder, operations lead or agency owner",
            "the people Koya's outbound team reaches, who hire AI automation assistants"),
    Default("business_problem", "Likely problem", "Repetitive operational work (onboarding, support, reporting, "
            "data entry) that could be automated", "the work Koya's AI automation assistants take on"),
    Default("soft_preferences", "Nice-to-haves", ["Recently hiring for operations roles",
                                                  "Uses tools that may connect to automation workflows",
                                                  "Publishes content about scaling operations"],
            "signs a team is growing its operations; they raise the fit score but never rule a company out"),
]


# Not agencies: agency owners are part of Koya's audience (PRD business context). The only fixed exclusion is a
# direct competitor, and it applies whatever else the objective excludes (D-89).
COMPETITOR_EXCLUSION = "Recruiting or staffing firm that places AI or automation talent"
COMPETITOR_WHY = "they compete with Koya for the same clients"
_COMPETITOR_TARGET = re.compile(r"\b(?:recruit\w*|staffing|talent (?:agenc\w*|placement\w*)|headhunt\w*)", re.I)


def wants_competitors(icp: dict) -> bool:
    """True when the objective ASKS for recruiting/staffing firms: the ICP refiner says so (include_competitors),
    or, as a code backstop, the target itself names them (e.g. "recruiting agencies")."""
    if icp.get("include_competitors"):
        return True
    target = " ".join([str(icp.get("target_company_type") or ""), *(str(i) for i in icp.get("industries") or [])])
    return bool(_COMPETITOR_TARGET.search(target))


def apply_competitor_rule(icp: dict) -> tuple[dict, str]:
    """Always exclude Koya's competitors unless the objective asks for them; either way, tell the user (the note
    is recorded as an assumption, shown on the run's ICP tab)."""
    exclusions = list(icp.get("disqualifiers") or [])
    present = COMPETITOR_EXCLUSION.lower() in {str(d).strip().lower() for d in exclusions}
    if wants_competitors(icp):
        exclusions = [d for d in exclusions if str(d).strip().lower() != COMPETITOR_EXCLUSION.lower()]
        note = ("Recruiting and staffing firms are included because the objective asks for them (Koya normally "
                "excludes them as competitors).")
    else:
        if not present:
            exclusions.append(COMPETITOR_EXCLUSION)
        note = (f"Always excluded: {COMPETITOR_EXCLUSION} ({COMPETITOR_WHY}). To include them, say so in the "
                "objective.")
    return {**icp, "disqualifiers": exclusions, "assumptions": list(icp.get("assumptions") or []) + [note]}, note


def _is_empty(value) -> bool:
    if isinstance(value, list):
        return not any(isinstance(v, str) and v.strip() or (v and not isinstance(v, str)) for v in value)
    return not value or (isinstance(value, str) and not value.strip())


def has_company_type(icp: dict) -> bool:
    """The one thing an objective must say (D-58)."""
    return not _is_empty(icp.get("target_company_type")) or any(str(i).strip() for i in icp.get("industries") or [])


def apply_defaults(icp: dict) -> tuple[dict, list[str]]:
    """Fill every empty field that has a Koya default. Returns (icp, assumptions added)."""
    icp = {**icp, "hard_filters": list(icp.get("hard_filters") or []), "assumptions": list(icp.get("assumptions") or [])}
    added: list[str] = []
    for d in DEFAULTS:
        if not _is_empty(icp.get(d.field)):
            continue
        icp[d.field] = list(d.value) if isinstance(d.value, list) else d.value
        shown = ", ".join(d.value) if isinstance(d.value, list) else d.value
        added.append(f"{d.label} not given, so the Koya default was used: {shown} ({d.why}).")
        if d.hard_filter and d.hard_filter.lower() not in {h.lower() for h in icp["hard_filters"]}:
            icp["hard_filters"].append(d.hard_filter)
    icp["assumptions"] += added
    return icp, added


def decide_lead_count(requested: int | None, run_cap: int) -> tuple[int, str | None]:
    """The run's target, from the objective (D-57): its number, else 10; never above the run's cap."""
    cap = max(1, min(int(run_cap), MAX_LEAD_COUNT))
    if requested is None or int(requested) < 1:
        target = min(DEFAULT_LEAD_COUNT, cap)
        note = (f"No number of leads given, so the default of {DEFAULT_LEAD_COUNT} was used"
                + (f" (this run's limit is {cap})." if target < DEFAULT_LEAD_COUNT else "."))
        return target, note
    if int(requested) > cap:
        return cap, f"The objective asked for {int(requested)} leads; this run's limit is {cap}."
    return int(requested), None
