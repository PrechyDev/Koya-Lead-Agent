"""Fit score, computed by code from the recorded evidence (decision D-48). Repeatable and itemised.

The researcher (AI) only records evidence decisions: pass/fail/unknown per hard filter, applies/no/unknown
per exclusion, matched/not_matched/unknown per nice-to-have, each with a source. This module turns those into
a number with a written reason for every point. Same recorded evidence -> same score, always.

Bands match the qualification status rules:
  qualified      0.70 - 1.00   every hard filter passes with evidence and no exclusion applies
  needs_review   0.40 - 0.65   something is unknown, nothing fails
  not_qualified  0.00 - 0.30   a hard filter fails or an exclusion applies
"""

from dataclasses import dataclass
from urllib.parse import urlsplit

BASE_ELIGIBLE = 0.70
TWO_SOURCES = 0.05
HEADCOUNT_CONFIRMED = 0.05
PER_SOFT_MATCH = 0.05
MAX_SOFT = 0.10
PENALTY = 0.05
REVIEW_FLOOR, REVIEW_SPAN = 0.40, 0.25
NOT_QUALIFIED_SPAN = 0.30


@dataclass
class Score:
    value: float
    band: str  # qualified | needs_review | not_qualified
    breakdown: list[dict]  # [{"points": +0.05, "reason": "..."}]

    def as_json(self) -> dict:
        return {"score": self.value, "band": self.band, "breakdown": self.breakdown}


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

    def add(points: float, reason: str) -> None:
        breakdown.append({"points": round(points, 2), "reason": reason})

    by_filter = {c.filter.strip().lower(): c for c in hard_checks}
    results = []
    for f in required_filters or [c.filter for c in hard_checks]:
        c = by_filter.get(f.strip().lower())
        if c is None:
            results.append("unknown")
        elif c.result == "pass" and not _evidenced(c):
            results.append("unknown")
        else:
            results.append(c.result)
    passing = results.count("pass")
    share = passing / len(results) if results else 0.0

    by_disq = {d.disqualifier.strip().lower(): d for d in disqualifier_checks}
    disq_results = []
    for d in required_disqualifiers or [d.disqualifier for d in disqualifier_checks]:
        chk = by_disq.get(d.strip().lower())
        if chk is None:
            disq_results.append("unknown")
        elif chk.applies == "no" and not _evidenced(chk):
            disq_results.append("unknown")
        else:
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
        failed = [f for f, r in zip(required_filters or [], results) if r == "fail"]
        applied = [d for d, r in zip(required_disqualifiers or [], disq_results) if r == "yes"]
        add(value, f"{passing} of {len(results)} hard filters pass (0.30 x share)")
        if failed:
            add(0.0, "fails: " + "; ".join(failed))
        if applied:
            add(0.0, "exclusion applies: " + "; ".join(applied))
        return Score(value, "not_qualified", breakdown)

    if "unknown" in results or "unknown" in disq_results:
        value = REVIEW_FLOOR + REVIEW_SPAN * share
        add(REVIEW_FLOOR, "base for 'needs review' (some evidence missing, nothing fails)")
        add(round(REVIEW_SPAN * share, 2), f"{passing} of {len(results)} hard filters pass with evidence")
        unknown = [f for f, r in zip(required_filters or [], results) if r == "unknown"]
        unknown_d = [d for d, r in zip(required_disqualifiers or [], disq_results) if r == "unknown"]
        if unknown or unknown_d:
            add(0.0, "unconfirmed: " + "; ".join(unknown + [f"exclusion '{d}'" for d in unknown_d]))
        for points, reason in penalties:
            value += points
            add(points, reason)
        return Score(round(max(REVIEW_FLOOR, min(value, REVIEW_FLOOR + REVIEW_SPAN)), 2), "needs_review", breakdown)

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
    return Score(round(max(BASE_ELIGIBLE, min(value, 1.0)), 2), "qualified", breakdown)


def cap_for_status(score: Score, final_status: str, reason: str) -> Score:
    """If the final status is lower than the evidence band (e.g. the researcher asked for human review),
    keep the safer status and cap the score to its band, recording why."""
    caps = {"needs_review": REVIEW_FLOOR + REVIEW_SPAN, "not_qualified": NOT_QUALIFIED_SPAN}
    order = {"not_qualified": 0, "needs_review": 1, "qualified": 2}
    if order[final_status] < order[score.band]:
        capped = min(score.value, caps[final_status])
        score.breakdown.append({"points": round(capped - score.value, 2), "reason": reason})
        return Score(round(capped, 2), final_status, score.breakdown)
    return score
