from decimal import Decimal

import pytest

from app.config import DEV_LIMITS, FULL_LIMITS, limits_for_run
from app.lib.budget import BudgetExceeded, assert_can_spend, cost_from_usage
from app.lib.limits import next_discovery_batch
from app.lib.objective import (
    country_code,
    icp_signature,
    normalize_headcount,
    objective_hash,
    parse_headcount_range,
)
from app.lib.outreach_checks import check_outreach, measure
from app.lib.qualification_rules import HardFilterCheck, invalid_sources, prescreen
from app.lib.scoring import compute_fit_score, decide

SRC = "https://acme.io/"
ICP = {"geography": ["United States"], "headcount_range": "10-100", "industries": ["B2B SaaS"],
       "target_company_type": "B2B SaaS"}


# --- limits (E-06) -----------------------------------------------------------
def test_discovery_batches_first_pool_then_topups_then_stop():
    limits = FULL_LIMITS.to_dict()
    assert next_discovery_batch(limits, {}) == 12
    assert next_discovery_batch(limits, {"discovery_calls": 1, "candidates_found": 12}) == 5
    assert next_discovery_batch(limits, {"discovery_calls": 2, "candidates_found": 17}) == 3
    assert next_discovery_batch(limits, {"discovery_calls": 3, "candidates_found": 20}) == 0
    assert next_discovery_batch(limits, {"discovery_calls": 1, "candidates_found": 20}) == 0


def test_first_search_scales_with_the_target():
    """D-88: the first search fetches 1.5x the leads wanted, never more than the preset pool of 12."""
    full = FULL_LIMITS.to_dict()
    assert [next_discovery_batch({**full, "target_qualified": t}, {}) for t in (1, 3, 5, 8, 10)] == [2, 5, 8, 12, 12]
    assert next_discovery_batch({**full, "target_qualified": 3}, {"discovery_calls": 1, "candidates_found": 5}) == 5


def test_target_is_clamped_to_preset():
    assert limits_for_run(25, dev=False).target_qualified == 10
    assert limits_for_run(0, dev=False).target_qualified == 1
    assert limits_for_run(10, dev=True).target_qualified == DEV_LIMITS.target_qualified


# --- objective fingerprints (E-36, E-37) ----------------------------------------
def test_objective_hash_ignores_case_spacing_punctuation():
    assert objective_hash("Find 10 US B2B SaaS companies!") == objective_hash("  find 10 us b2b saas   companies ")
    assert objective_hash("Find 10 US SaaS") != objective_hash("Find 10 UK SaaS")


def test_icp_signature_matches_same_intent_different_words():
    a = {"geography": ["USA"], "headcount_range": "10 to 100 employees", "industries": ["B2B SaaS"],
         "target_company_type": "SaaS"}
    b = {"geography": ["United States of America"], "headcount_range": "10–100",
         "industries": ["b2b saas"], "target_company_type": "B2B software"}
    assert icp_signature(a) == icp_signature(b)
    assert icp_signature(a) != icp_signature({**a, "geography": ["Canada"]})


def test_headcount_parsing():
    assert normalize_headcount("10–100 employees") == "10-100"
    assert parse_headcount_range("1,000+") == (1000, None)
    assert parse_headcount_range(None) == (None, None)


# --- budget (E-29) ---------------------------------------------------------------
def test_cost_from_usage_uses_price_table_and_batch_discount():
    assert cost_from_usage("claude-sonnet-5", 1_000_000, 100_000) == Decimal("3.00000")
    assert cost_from_usage("claude-haiku-4-5", 1_000_000, 0, batch=True) == Decimal("0.50000")
    assert cost_from_usage("claude-haiku-4-5-20251001", 0, 1_000_000) == Decimal("5.00000")


def test_budget_guard_refuses_before_spending():
    assert_can_spend(Decimal("4.00"), 1.25, 6)
    with pytest.raises(BudgetExceeded) as err:
        assert_can_spend(Decimal("5.10"), 1.25, 6)
    assert err.value.remaining == Decimal("0.90")
    assert "raise the budget on the Spend page" in str(err.value)


# --- qualification rules (E-15, E-16, E-17) -------------------------------------
def _pass(f):
    return HardFilterCheck(filter=f, result="pass", evidence="stated on site", source_url=SRC)


def _decide(checks, requested="qualified", required=None, disq=(), required_disq=()):
    score = compute_fit_score(hard_checks=checks, disqualifier_checks=list(disq), soft_checks=[],
                              required_filters=list(required or [c.filter for c in checks]),
                              required_disqualifiers=list(required_disq), discovery={}, company_domain="acme.io",
                              source_urls=[SRC])
    return decide(requested, score)


def test_qualified_requires_every_filter_to_pass_with_evidence():
    status, _, score = _decide([_pass("US"), _pass("B2B SaaS"), _pass("10-100 employees")])
    assert status == "qualified" and score.value >= 0.70


def test_unknown_filter_downgrades_to_needs_review():
    checks = [_pass("US"), HardFilterCheck(filter="10-100 employees", result="unknown")]
    status, notes, _ = _decide(checks)
    assert status == "needs_review" and "human review" in notes[-1]


def test_pass_without_evidence_counts_as_unknown():
    status, notes, _ = _decide([HardFilterCheck(filter="US", result="pass", evidence="", source_url=None)])
    assert status == "needs_review" and "without evidence" in notes[0]


def test_any_fail_forces_not_qualified():
    checks = [_pass("US"), HardFilterCheck(filter="B2B", result="fail", evidence="consumer app", source_url=SRC)]
    assert _decide(checks)[0] == "not_qualified"


def test_missing_required_filter_check_is_unknown():
    status, notes, _ = _decide([_pass("US")], required=["US", "10-100 employees"])
    assert status == "needs_review" and "no check" in notes[0]


def test_the_researchers_safer_choice_wins_and_caps_the_score():
    status, _, score = _decide([_pass("US")], requested="needs_review")
    assert status == "needs_review" and score.value <= 0.65 and "safer status wins" in score.breakdown[-1]["reason"]


def test_invalid_sources_flags_urls_never_fetched():
    allowed = ["https://acme.io/", "https://www.linkedin.com/company/acme/"]
    assert invalid_sources(["http://www.acme.io", "https://acme.io/secret-page"], allowed) == ["https://acme.io/secret-page"]


# --- pre-screen (E-31, E-15, E-45) --------------------------------------------------
def _disc(country="US", start=11, end=50, members=20):
    return {"hq": {"country_code": country}, "employee_count_range": {"start": start, "end": end},
            "employee_count_linkedin": members}


def test_prescreen_pass_reject_unknown():
    assert prescreen(_disc(), ICP)[0] == "passed"
    assert prescreen(_disc(country="GB"), ICP)[0] == "rejected:geography"
    assert prescreen(_disc(start=201, end=500, members=300), ICP)[0] == "rejected:headcount"
    assert prescreen(_disc(start=51, end=200, members=60), ICP)[0] == "unknown"      # straddles 100
    assert prescreen(_disc(start=51, end=200, members=150), ICP)[0] == "rejected:headcount"
    assert prescreen({"hq": None}, ICP)[0] == "unknown"


def test_prescreen_geography_handles_any_country_and_regions():
    kenya = {**ICP, "geography": ["Kenya"]}
    assert prescreen(_disc(country="KE"), kenya)[0] == "passed"       # was wrongly rejected (not in the old table)
    assert prescreen(_disc(country="NG"), kenya)[0] == "rejected:geography"
    assert prescreen(_disc(country="US"), {**ICP, "geography": ["USA"]})[0] == "passed"
    result, reason = prescreen(_disc(country="DE"), {**ICP, "geography": ["Europe"]})
    assert result == "unknown" and "Europe" in reason                  # a region is never a free rejection
    assert prescreen(_disc(country="GB"), {**ICP, "geography": ["UK", "Europe"]})[0] == "passed"


def test_country_codes():
    got = [country_code(g) for g in ["United States", "USA", "U.S.", "uk", "England", "Kenya", "DE", "Europe", ""]]
    assert got == ["us", "us", "us", "gb", "gb", "ke", "de", None, None]
    assert country_code("America") is None  # ambiguous on purpose


def test_prescreen_notes_large_headcount_disagreement():
    result, reason = prescreen(_disc(start=11, end=50, members=1), ICP)
    assert result == "passed" and "verify headcount" in reason


# --- outreach checks (E-24) -----------------------------------------------------------
def _good_steps():
    return [
        {"step": 1, "subject": "Onboarding for dental clinics", "body": "Hi {{first_name}}, your site says Acme onboards 40 new clinics a month. Is setup still manual? {{sender_name}}", "personalization_note": "onboarding volume", "evidence_ref": SRC},
        {"step": 2, "subject": "Support ticket triage", "body": "Hi {{first_name}}, with a 3-person support team, routing tickets by clinic tier could be automated. {{sender_name}}", "personalization_note": "team size", "evidence_ref": SRC},
        {"step": 3, "subject": "Wrong timing?", "body": "Hi {{first_name}}, if this isn't a priority right now, just say so. — {{sender_name}}", "personalization_note": "close", "evidence_ref": SRC},
    ]


def test_good_outreach_passes():
    assert check_outreach(_good_steps(), "Hi {{first_name}}, saw Acme is scaling clinic onboarding. Open to a chat about automating it?", [SRC]) == []


def test_bad_outreach_is_caught():
    steps = _good_steps()[:2]
    steps[0]["body"] = "Hi Jane, I saw your website and loved what you are building! Email me at me@x.com " + "word " * 130
    steps[1]["subject"] = "x" * 80
    steps[1]["evidence_ref"] = "https://elsewhere.com"
    steps[1]["body"] += " See https://koya.ai {{company_name}}"
    problems = check_outreach(steps, "y" * 301, [SRC])
    text = "\n".join(problems)
    for expected in ["exactly 3", "words", "email address", "banned phrase", "subject is 80", "evidence_ref",
                     "URL", "unknown placeholder", "{{first_name}}", "LinkedIn message is 301"]:
        assert expected in text, expected


def test_template_structure_is_enforced():
    steps = _good_steps()
    steps[0]["body"] = steps[0]["body"].replace("?", ".")          # email 1 must ask a question
    steps[1]["subject"] = steps[0]["subject"]                       # subjects must differ
    steps[2]["body"] = "Hi {{first_name}}, " + "short " * 85 + "{{sender_name}}"  # final email must be brief
    steps[1]["body"] = steps[1]["body"].replace("{{sender_name}}", "")        # every email signed
    text = "\n".join(check_outreach(steps, "Hi {{first_name}}, a question?", [SRC]))
    for expected in ["low-pressure question", "its own subject", "email 3 is 88 words", "email 2 must be signed"]:
        assert expected in text, expected
    reordered = [_good_steps()[1], _good_steps()[0], _good_steps()[2]]
    assert any("numbered step 1, 2, 3" in p for p in check_outreach(reordered, "hi?", [SRC]))


def test_measure_gives_exact_counts():
    counts = measure(_good_steps(), "x" * 316)
    assert counts["linkedin_chars"] == 316 and counts["emails"][0] == {"step": 1, "subject_chars": 29, "body_words": 17}


def test_blank_env_values_fall_back_to_defaults(monkeypatch):
    from app.config import Settings
    monkeypatch.setenv("APIFY_ACTOR_ID", "")
    monkeypatch.setenv("MODEL_RESEARCHER", "  ")
    s = Settings(_env_file=None)
    assert s.apify_actor_id == "harvestapi/linkedin-company-search"
    assert s.model_researcher == "claude-opus-5-5"  # a blank value falls back to the A/B default (D-77)
    assert "ANTHROPIC_API_KEY" in Settings(_env_file=None, anthropic_api_key="").missing_run_config()


# --- ICP defaults + lead count (D-57, D-58) ---------------------------------------------------------------
def test_defaults_fill_only_what_is_missing_and_say_so():
    from app.lib.icp_defaults import apply_defaults
    icp, added = apply_defaults({"target_company_type": "Marketing agency", "geography": ["United Kingdom"],
                                 "hard_filters": ["Headquartered in the United Kingdom"], "assumptions": []})
    assert icp["geography"] == ["United Kingdom"]                      # the user's value is kept
    assert icp["headcount_range"] == "10-100" and "10-100 employees" in icp["hard_filters"]
    assert "Headquartered in the United States" not in icp["hard_filters"]
    assert len(added) == 4 and all("Koya default" in a for a in added) and icp["assumptions"] == added


def test_same_vague_objective_gets_the_same_defaults_every_time():
    from app.lib.icp_defaults import apply_defaults
    assert apply_defaults({"target_company_type": "B2B SaaS"}) == apply_defaults({"target_company_type": "B2B SaaS"})


def test_company_type_is_the_one_required_detail():
    from app.lib.icp_defaults import has_company_type
    assert has_company_type({"target_company_type": "B2B SaaS"}) and has_company_type({"industries": ["Dental clinics"]})
    assert not has_company_type({"target_company_type": " ", "industries": []})


@pytest.mark.parametrize("requested, cap, expected", [(None, 10, 10), (5, 10, 5), (25, 10, 10), (None, 2, 2), (0, 10, 10)])
def test_lead_count_comes_from_the_objective(requested, cap, expected):
    from app.lib.icp_defaults import decide_lead_count
    target, note = decide_lead_count(requested, cap)
    assert target == expected and ((note is None) == (requested is not None and 1 <= requested <= cap))
