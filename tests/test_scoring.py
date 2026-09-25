"""The fit score is computed by code (D-48): repeatable, itemised, banded like the status rules."""

from app.lib.qualification_rules import DisqualifierCheck, HardFilterCheck, SoftPreferenceCheck
from app.lib.scoring import compute_fit_score, decide

SITE, LI = "https://www.expeditecommerce.com/", "https://www.linkedin.com/company/expeditecommerce/"
FILTERS = ["Headquartered in the United States", "Sells software to businesses (B2B SaaS)", "10-100 employees"]
DISQ = ["Agency or consultancy"]


def _hard(results=("pass", "pass", "pass")):
    src = [LI, SITE, LI]
    return [HardFilterCheck(filter=f, result=r, evidence="stated", source_url=s) for f, r, s in zip(FILTERS, results, src, strict=True)]


def _disq(applies="no"):
    return [DisqualifierCheck(disqualifier=DISQ[0], applies=applies, evidence="sells its own product", source_url=SITE)]


def score(hard=None, disq=None, soft=(), discovery=None, sources=(SITE, LI)):
    return compute_fit_score(hard_checks=hard if hard is not None else _hard(), disqualifier_checks=disq if disq is not None else _disq(),
                             soft_checks=list(soft), required_filters=FILTERS, required_disqualifiers=DISQ,
                             discovery=discovery if discovery is not None else
                             {"employee_count_range": {"start": 11, "end": 50}, "employee_count_linkedin": 38},
                             company_domain="expeditecommerce.com", source_urls=list(sources))


def test_expedite_commerce_example():
    s = score()
    assert (s.value, s.band) == (0.80, "qualified")
    reasons = [b["reason"] for b in s.breakdown]
    assert any("2+ independent sources" in r for r in reasons) and any("headcount confirmed" in r for r in reasons)


def test_newowl_example_headcount_not_confirmed():
    s = score(discovery={"employee_count_range": {"start": 11, "end": 50}, "employee_count_linkedin": 4})
    assert (s.value, s.band) == (0.75, "qualified")


def test_same_evidence_same_score_every_time():
    assert len({score().value for _ in range(50)}) == 1


def test_single_source_and_no_headcount_confirmation_is_the_base():
    s = score(sources=(SITE,), hard=[HardFilterCheck(filter=f, result="pass", evidence="x", source_url=SITE) for f in FILTERS],
              discovery={})
    assert s.value == 0.70


def test_soft_matches_add_up_to_010_and_penalties_subtract():
    soft = [SoftPreferenceCheck(preference=p, result="matched", evidence="careers page", source_url=SITE)
            for p in ("hiring ops", "uses HubSpot", "writes about scaling")]
    assert score(soft=soft).value == 0.90  # 0.80 + max 0.10
    unevidenced = [SoftPreferenceCheck(preference="hiring ops", result="matched", evidence="", source_url=None)]
    assert score(soft=unevidenced).value == 0.80  # a match without evidence earns nothing
    s = score(discovery={"employee_count_range": {"start": 51, "end": 200}, "employee_count_linkedin": 3,
                         "injection_flags": ["ignore_instructions"]})
    assert s.value == 0.70 and s.band == "qualified"  # 0.75 - 0.10, floored at the band's bottom


def test_unknown_goes_to_review_band():
    s = score(hard=_hard(("pass", "pass", "unknown")))
    assert s.band == "needs_review" and s.value == round(0.40 + 0.25 * 2 / 3, 2)
    assert score(disq=[]).band == "needs_review"  # exclusion not checked
    assert score(hard=_hard()[:2]).band == "needs_review"  # a hard filter not checked


def test_fail_or_exclusion_goes_to_not_qualified_band():
    s = score(hard=_hard(("pass", "fail", "pass")))
    assert s.band == "not_qualified" and s.value == 0.2
    assert score(disq=_disq("yes")).band == "not_qualified"


def test_pass_without_evidence_counts_as_unknown():
    weak = _hard()
    weak[0] = HardFilterCheck(filter=FILTERS[0], result="pass", evidence="", source_url=None)
    assert score(hard=weak).band == "needs_review"


def test_safer_status_wins_and_the_cap_is_explained():
    status, _, s = decide("needs_review", score())
    assert (status, s.value, s.band) == ("needs_review", 0.65, "needs_review") and "researcher chose" in s.breakdown[-1]["reason"]
    assert decide("qualified", score())[2].value == 0.80  # never raised
