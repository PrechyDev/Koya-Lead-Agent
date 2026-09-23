from decimal import Decimal

import pytest

from app.config import DEV_LIMITS, FULL_LIMITS, limits_for_run
from app.lib.budget import BudgetExceeded, assert_can_spend, cost_from_usage
from app.lib.limits import next_discovery_batch, qualified_slots_left
from app.lib.objective import icp_signature, normalize_headcount, objective_hash, parse_headcount_range
from app.lib.outreach_checks import check_outreach
from app.lib.qualification_rules import HardFilterCheck, decide_status, invalid_sources, prescreen

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


def test_target_is_clamped_to_preset():
    assert limits_for_run(25, dev=False).target_qualified == 10
    assert limits_for_run(0, dev=False).target_qualified == 1
    assert limits_for_run(10, dev=True).target_qualified == DEV_LIMITS.target_qualified
    assert qualified_slots_left({"target_qualified": 10}, {"qualified": 7}) == 3


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
    assert "raise CLAUDE_BUDGET_TOTAL_USD" in str(err.value)


# --- qualification rules (E-15, E-16, E-17) -------------------------------------
def _pass(f):
    return HardFilterCheck(filter=f, result="pass", evidence="stated on site", source_url=SRC)


def test_qualified_requires_all_pass_and_confidence():
    checks = [_pass("US"), _pass("B2B SaaS"), _pass("10-100 employees")]
    assert decide_status("qualified", checks, 0.8)[0] == "qualified"
    status, notes = decide_status("qualified", checks, 0.6)
    assert status == "needs_review" and "confidence" in notes[0]


def test_unknown_filter_downgrades_to_needs_review():
    checks = [_pass("US"), HardFilterCheck(filter="10-100 employees", result="unknown")]
    assert decide_status("qualified", checks, 0.9)[0] == "needs_review"


def test_pass_without_evidence_counts_as_unknown():
    checks = [HardFilterCheck(filter="US", result="pass", evidence="", source_url=None)]
    status, notes = decide_status("qualified", checks, 0.9)
    assert status == "needs_review" and "without evidence" in notes[0]


def test_any_fail_forces_not_qualified():
    checks = [_pass("US"), HardFilterCheck(filter="B2B", result="fail", evidence="consumer app", source_url=SRC)]
    assert decide_status("qualified", checks, 0.95)[0] == "not_qualified"


def test_missing_required_filter_check_is_unknown():
    status, notes = decide_status("qualified", [_pass("US")], 0.9, required_filters=["US", "10-100 employees"])
    assert status == "needs_review" and "no check" in notes[0]


def test_invalid_sources_flags_urls_never_fetched():
    allowed = ["https://acme.io/", "https://www.linkedin.com/company/acme/"]
    assert invalid_sources(["http://www.acme.io", "https://acme.io/secret-page"], allowed) == ["https://acme.io/secret-page"]


# --- pre-screen (E-31, E-15, E-45) --------------------------------------------------
def _disc(country="US", start=11, end=50, members=20):
    return {"locations": [{"headquarter": True, "parsed": {"countryCode": country}}],
            "employeeCountRange": {"start": start, "end": end}, "employeeCount": members}


def test_prescreen_pass_reject_unknown():
    assert prescreen(_disc(), ICP)[0] == "passed"
    assert prescreen(_disc(country="GB"), ICP)[0] == "rejected:geography"
    assert prescreen(_disc(start=201, end=500, members=300), ICP)[0] == "rejected:headcount"
    assert prescreen(_disc(start=51, end=200, members=60), ICP)[0] == "unknown"      # straddles 100
    assert prescreen(_disc(start=51, end=200, members=150), ICP)[0] == "rejected:headcount"
    assert prescreen({"locations": []}, ICP)[0] == "unknown"


def test_prescreen_notes_large_headcount_disagreement():
    result, reason = prescreen(_disc(start=11, end=50, members=1), ICP)
    assert result == "passed" and "verify headcount" in reason


# --- outreach checks (E-24) -----------------------------------------------------------
def _good_steps():
    return [
        {"subject": "Onboarding for dental clinics", "body": "Hi {{first_name}}, your site says Acme onboards 40 new clinics a month. Is setup still manual?", "personalization_note": "onboarding volume", "evidence_ref": SRC},
        {"subject": "Support ticket triage", "body": "Hi {{first_name}}, with a 3-person support team, routing tickets by clinic tier could be automated.", "personalization_note": "team size", "evidence_ref": SRC},
        {"subject": "Wrong timing?", "body": "Hi {{first_name}}, if this isn't a priority right now, just say so. — {{sender_name}}", "personalization_note": "close", "evidence_ref": SRC},
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


def test_blank_env_values_fall_back_to_defaults(monkeypatch):
    from app.config import Settings
    monkeypatch.setenv("APIFY_ACTOR_ID", "")
    monkeypatch.setenv("MODEL_RESEARCHER", "  ")
    s = Settings(_env_file=None)
    assert s.apify_actor_id == "harvestapi/linkedin-company-search"
    assert s.model_researcher == "claude-sonnet-5"
    assert "ANTHROPIC_API_KEY" in Settings(_env_file=None, anthropic_api_key="").missing_run_config()
