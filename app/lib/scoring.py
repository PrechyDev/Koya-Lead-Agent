"""Fit score, computed by code from the recorded evidence (decision D-48). Repeatable and itemised.

The researcher (AI) only records evidence decisions: pass/fail/unknown per hard filter, applies/no/unknown
per exclusion, matched/not_matched/unknown per nice-to-have, each with a source. This module turns those into
a number with a written reason for every point. Same recorded evidence -> same score, always.

The evidence band IS the qualification rule (specs §8.3; E-15, E-16, E-17, E-31, E-45), in one place:
  qualified      0.70 - 1.00   every ICP hard filter passes with evidence + a source, and no exclusion applies
  needs_review   0.40 - 0.65   something is unknown or unevidenced (or a check is missing), nothing fails
  not_qualified  0.00 - 0.30   a hard filter fails or an exclusion applies
decide() then stores the SAFER of the researcher's requested status and this band (D-80).
"""

from dataclasses import dataclass, field
from urllib.parse import urlsplit

BASE_ELIGIBLE = 0.70
TWO_SOURCES = 0.05
HEADCOUNT_CONFIRMED = 0.05
PER_SOFT_MATCH = 0.05
MAX_SOFT = 0.10
PENALTY = 0.05
REVIEW_FLOOR, REVIEW_SPAN = 0.40, 0.25
NOT_QUALIFIED_SPAN = 0.30


STATUS_ORDER = {"not_qualified": 0, "needs_review": 1, "qualified": 2}  # lower = safer


@dataclass
class Score:
    value: float
    band: str  # qualified | needs_review | not_qualified
    breakdown: list[dict]  # [{"points": +0.05, "reason": "..."}]
    notes: list[str] = field(default_factory=list)  # why a check counted differently than the researcher said


def _evidenced(check, source_field: str = "source_url") -> bool:
    return bool((getattr(check, "evidence", "") or "").strip() and getattr(check, source_field, None))


def _source_kinds(urls: list[str], company_domain: str) -> set[str]:
    kinds = set()
    for url in urls:
        host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
        if not host:
            continue
        if host == company_domain or host.endswith("." + company_domain):
            kinds.add("company website")
        elif host.endswith("linkedin.com"):
            kinds.add("LinkedIn")
        else:
            kinds.add(host)
    return kinds


def compute_fit_score(*, hard_checks: list, disqualifier_checks: list, soft_checks: list,
                      required_filters: list[str], required_disqualifiers: list[str], discovery: dict,
                      company_domain: str, source_urls: list[str]) -> Score:
    breakdown: list[dict] = []
    notes: list[str] = []

    def add(points: float, reason: str) -> None:
        breakdown.append({"points": round(points, 2), "reason": reason})

    by_filter = {c.filter.strip().lower(): c for c in hard_checks}
    filter_names = list(required_filters or [c.filter for c in hard_checks])
    results = []
    for f in filter_names:
        c = by_filter.get(f.strip().lower())
        if c is None:
            notes.append(f'no check for hard filter "{f}", so it counts as unknown')
            results.append("unknown")
        elif c.result == "pass" and not _evidenced(c):
            notes.append(f'"{f}" was marked pass without evidence and a source URL, so it counts as unknown')
            results.append("unknown")
        else:
            results.append(c.result)
    passing = results.count("pass")
    share = passing / len(results) if results else 0.0

    by_disq = {d.disqualifier.strip().lower(): d for d in disqualifier_checks}
    disq_names = list(required_disqualifiers or [d.disqualifier for d in disqualifier_checks])
    disq_results = []
    for d in disq_names:
        chk = by_disq.get(d.strip().lower())
        if chk is None:
            notes.append(f'no check for exclusion "{d}", so it counts as unknown')
            disq_results.append("unknown")
        elif chk.applies == "no" and not _evidenced(chk):
            notes.append(f'"{d}" was marked not applying without evidence, so it counts as unknown')
            disq_results.append("unknown")
        else:
            if chk.applies == "yes":
                notes.append(f'exclusion applies: "{d}"')
            disq_results.append(chk.applies)

    # Penalties that code can see for itself (no judgment involved).
    penalties: list[tuple[float, str]] = []
    band_info = discovery.get("employee_count_range") or {}
    b_low, b_high = band_info.get("start"), band_info.get("end")
    members = discovery.get("employee_count_linkedin")
    if isinstance(members, int) and b_low and members * 5 < b_low:
        penalties.append((-PENALTY, f"only {members} people on LinkedIn vs a stated {b_low}-{b_high or '+'}"))
    if discovery.get("injection_flags"):
        penalties.append((-PENALTY, "the website contained text aimed at AI tools"))

    if "fail" in results or "yes" in disq_results:
        value = round(NOT_QUALIFIED_SPAN * share, 2)
        failed = [f for f, r in zip(filter_names, results, strict=True) if r == "fail"]
        applied = [d for d, r in zip(disq_names, disq_results, strict=True) if r == "yes"]
        add(value, f"{passing} of {len(results)} hard filters pass (0.30 x share)")
        if failed:
            add(0.0, "fails: " + "; ".join(failed))
        if applied:
            add(0.0, "exclusion applies: " + "; ".join(applied))
        return Score(value, "not_qualified", breakdown, notes)

    if "unknown" in results or "unknown" in disq_results:
        value = REVIEW_FLOOR + REVIEW_SPAN * share
        add(REVIEW_FLOOR, "base for 'needs review' (some evidence missing, nothing fails)")
        add(round(REVIEW_SPAN * share, 2), f"{passing} of {len(results)} hard filters pass with evidence")
        unknown = [f for f, r in zip(filter_names, results, strict=True) if r == "unknown"]
        unknown_d = [d for d, r in zip(disq_names, disq_results, strict=True) if r == "unknown"]
        if unknown or unknown_d:
            add(0.0, "unconfirmed: " + "; ".join(unknown + [f"exclusion '{d}'" for d in unknown_d]))
        for points, reason in penalties:
            value += points
            add(points, reason)
        return Score(round(max(REVIEW_FLOOR, min(value, REVIEW_FLOOR + REVIEW_SPAN)), 2), "needs_review", breakdown,
                     notes)

    value = BASE_ELIGIBLE
    add(BASE_ELIGIBLE, f"all {len(results)} hard filters pass with evidence and no exclusion applies")
    kinds = _source_kinds(list(source_urls) + [c.source_url for c in hard_checks if c.source_url], company_domain)
    if len(kinds) >= 2:
        value += TWO_SOURCES
        add(TWO_SOURCES, "evidence from 2+ independent sources (" + ", ".join(sorted(kinds)) + ")")
    if isinstance(members, int) and b_low and b_high and b_low / 2 <= members <= b_high:
        value += HEADCOUNT_CONFIRMED
        add(HEADCOUNT_CONFIRMED, f"headcount confirmed twice: stated {b_low}-{b_high}, {members} on LinkedIn")
    matched = [s for s in soft_checks if s.result == "matched" and _evidenced(s)]
    soft_points = min(MAX_SOFT, PER_SOFT_MATCH * len(matched))
    if soft_points:
        value += soft_points
        add(round(soft_points, 2), "nice-to-haves matched: " + "; ".join(s.preference for s in matched))
    for points, reason in penalties:
        value += points
        add(points, reason)
    return Score(round(max(BASE_ELIGIBLE, min(value, 1.0)), 2), "qualified", breakdown, notes)


def decide(requested: str, score: Score) -> tuple[str, list[str], Score]:
    """The status the server stores: the SAFER of what the researcher asked for and what the evidence shows.
    Returns (status, notes for the researcher and the lead's concerns, the score capped to that status)."""
    notes = list(score.notes)
    if STATUS_ORDER[score.band] < STATUS_ORDER[requested]:  # the evidence doesn't support the request
        notes.append({"not_qualified": "a hard filter failed or an exclusion applies, so the lead is not_qualified",
                      "needs_review": "something is unconfirmed, so the lead needs human review instead of "
                                      "qualified"}[score.band])
        return score.band, notes, score
    if STATUS_ORDER[requested] < STATUS_ORDER[score.band]:  # the researcher chose the safer status: keep it
        caps = {"needs_review": REVIEW_FLOOR + REVIEW_SPAN, "not_qualified": NOT_QUALIFIED_SPAN}
        capped = min(score.value, caps[requested])
        breakdown = score.breakdown + [{"points": round(capped - score.value, 2),
                                        "reason": f"researcher chose '{requested}' (the safer status wins)"}]
        return requested, notes, Score(round(capped, 2), requested, breakdown, score.notes)
    return requested, notes, score
