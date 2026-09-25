"""Failure handling, owner alerts, input gate, scope check and disguised-email redaction (no spend)."""

import json
import uuid
from decimal import Decimal
from types import SimpleNamespace

import httpx
import psycopg
import pytest
import respx

from app import alerts, auth, db
from app.agent import tools as T
from app.agent.context import RunContext
from app.agent.logging import logged_call
from app.config import DEV_LIMITS
from app.failures import CATALOGUE, ServiceFailure, classify_apify, classify_claude_error
from app.lib.objective import objective_problem
from app.lib.sanitize import redact
from app.services import apify as apify_svc
from app.services import health
from app.services.grounding import GroundingResult


# --- catalogue & classification ------------------------------------------------------
def test_every_failure_has_plain_client_text_without_jargon():
    for kind in CATALOGUE.values():
        if not kind.client:
            continue  # budget_warning is admin-only
        for jargon in ("APIFY_", "ANTHROPIC_", "FIRECRAWL_", "HTTP", "401", "402", "404", "Traceback", "env"):
            assert jargon not in kind.client, (kind.code, jargon)
        assert kind.admin  # the admin always gets a fix


@pytest.mark.parametrize("status,text,code", [
    (400, "Your credit balance is too low to access the Anthropic API", "anthropic_no_credit"),
    (401, "invalid x-api-key", "anthropic_auth"),
    (429, "rate_limit_error", "anthropic_rate_limited"),
    (529, "overloaded_error", "anthropic_unavailable"),
])
def test_classify_claude_errors(status, text, code):
    assert classify_claude_error(status, text) == code


@pytest.mark.parametrize("status,text,code", [
    (401, "user-or-token-not-found", "apify_auth"),
    (404, "record-not-found: Actor with this name was not found", "apify_actor_not_found"),
    (402, "not enough usage", "apify_no_credit"),
    (403, "platform-feature-disabled: monthly usage limit exceeded", "apify_no_credit"),
    (503, "service unavailable", "apify_unavailable"),
    (400, "invalid-input: Field input.locations is not valid", "apify_bad_input"),
    (403, "insufficient-permissions: token can't run this actor", "apify_auth"),
])
def test_classify_apify_errors(status, text, code):
    assert classify_apify(status, text) == code


# --- free input gate (E-50) -------------------------------------------------------------
@pytest.mark.parametrize("text", ["......,,,,,,,huovivpfefopfo", "find leads", "asdf qwrt zxcvb plkj",
                                  "Find SaaS firms Lagos",  # 4 words: needs 5
                                  "Find B2B SaaS companies",
                                  "zzzzzzzzzzz find companies now", "12345 678 910 1112", "  "])
def test_junk_is_stopped_before_any_ai_call(text):
    assert objective_problem(text) is not None


@pytest.mark.parametrize("text", ["Find US B2B SaaS companies with 10 to 100 employees",
                                  "Find companies that might need AI automation help",
                                  "Trouvez des entreprises SaaS aux Etats-Unis",
                                  "can I buy icecream in ife?"])  # a real sentence: the scope check decides next
def test_real_sentences_pass_the_free_gate(text):
    assert objective_problem(text) is None


# --- disguised emails (E-11) --------------------------------------------------------------
@pytest.mark.parametrize("text", ["jane [at] acme [dot] io", "jane(at)acme(dot)co(dot)uk", "jane at acme dot io",
                                  "JANE {at} ACME {dot} IO", "reach jane.doe [ at ] acme-corp [ dot ] com today"])
def test_disguised_emails_are_redacted(text):
    out = redact(text)[0]
    assert "[email redacted]" in out and "acme" not in out.lower().split("[email redacted]")[0][-5:]


def test_ordinary_prose_is_left_alone():
    for text in ["Look at our work", "meet us at acme", "we launched at SaaStr", "Founded in 2015 at MIT"]:
        assert redact(text)[0] == text


# --- preflight (E-50) ----------------------------------------------------------------------
@respx.mock
def test_preflight_catches_wrong_actor_and_no_firecrawl_credit(monkeypatch):
    health.clear_cache()
    monkeypatch.setattr(health, "check_anthropic", lambda: None)
    respx.get(url__regex=r"https://api\.apify\.com/v2/acts/.*").mock(
        return_value=httpx.Response(404, json={"error": {"type": "record-not-found"}}))
    respx.get("https://api.firecrawl.dev/v2/team/credit-usage").mock(
        return_value=httpx.Response(200, json={"data": {"remainingCredits": 2}}))
    failures = health.preflight(DEV_LIMITS.to_dict())
    assert {f.code for f in failures} == {"apify_actor_not_found", "firecrawl_no_credit"}
    health.clear_cache()


@respx.mock
def test_preflight_catches_apify_out_of_monthly_usage(monkeypatch):
    health.clear_cache()
    monkeypatch.setattr(health, "check_anthropic", lambda: None)
    respx.get(url__regex=r"https://api\.apify\.com/v2/acts/.*").mock(return_value=httpx.Response(200, json={}))
    respx.get("https://api.apify.com/v2/users/me/limits").mock(return_value=httpx.Response(200, json={"data": {
        "limits": {"maxMonthlyUsageUsd": 5}, "current": {"monthlyUsageUsd": 4.99}}}))
    respx.get("https://api.firecrawl.dev/v2/team/credit-usage").mock(
        return_value=httpx.Response(200, json={"data": {"remainingCredits": 500}}))
    assert [f.code for f in health.preflight(DEV_LIMITS.to_dict())] == ["apify_no_credit"]
    health.clear_cache()


def test_preflight_catches_empty_anthropic_account(monkeypatch):
    import anthropic

    class Boom:
        def __init__(self, **kw):
            self.messages = self

        def create(self, **kw):
            request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
            raise anthropic.BadRequestError("Your credit balance is too low", response=httpx.Response(
                400, request=request), body=None)
    monkeypatch.setattr(health.anthropic, "Anthropic", Boom)
    assert health.check_anthropic().code == "anthropic_no_credit"


# --- alerts: in-app + webhook, deduped -------------------------------------------------------


@pytest.mark.db
@respx.mock
def test_alert_is_recorded_deduped_and_sent(app_dsn, admin_dsn, monkeypatch):
    monkeypatch.setattr(alerts, "get_settings", lambda: SimpleNamespace(
        alert_webhook_url="https://n8n.example/webhook/koya", alert_webhook_secret="s3cret",
        app_base_url="https://app.example", claude_budget_total_usd=Decimal("6")))
    hook = respx.post("https://n8n.example/webhook/koya").mock(return_value=httpx.Response(200))
    # A test-only code, so the clean-up below can never delete (or bump) a real alert.
    code = "test_only_alert"
    real = CATALOGUE["apify_actor_not_found"]
    monkeypatch.setitem(alerts.CATALOGUE, code, type(real)(code, real.service, real.severity,
                                                           real.client, real.admin))
    with psycopg.connect(admin_dsn, prepare_threshold=None, autocommit=True) as conn:
        conn.execute("delete from lead_agent.system_events where code = %s", (code,))
    try:
        alerts.raise_alert(code, "actor x/y missing")
        alerts.raise_alert(code, "actor x/y missing again")
        import time
        deadline = time.monotonic() + 10  # the webhook is sent on a background thread: wait for it, don't guess
        while hook.call_count < 1 and time.monotonic() < deadline:
            time.sleep(0.1)
        rows = db.fetch_all("select * from lead_agent.system_events where code = %s", (code,))
        assert len(rows) == 1 and rows[0]["occurrences"] == 2
        assert hook.call_count == 1
        body = json.loads(hook.calls[0].request.content)
        assert body["code"] == code and hook.calls[0].request.headers["X-Alert-Secret"] == "s3cret"
        assert alerts.open_issue_count() >= 1
    finally:
        with psycopg.connect(admin_dsn, prepare_threshold=None, autocommit=True) as conn:
            conn.execute("delete from lead_agent.system_events where code = %s", (code,))


# --- tools: fatal failures stop the run; checker outages don't burn rewrites -----------------
@pytest.fixture
def run_ctx(app_dsn, admin_dsn):
    run, _ = db.create_run(idempotency_key=f"test-{uuid.uuid4()}", objective="hardening test objective",
                           objective_hash="h" + uuid.uuid4().hex, limits=DEV_LIMITS.to_dict(), run_kind="dev")
    ctx = RunContext.load(str(run["id"]))
    yield ctx
    with psycopg.connect(admin_dsn, prepare_threshold=None, autocommit=True) as conn:
        for table in ("tool_calls", "leads", "system_events"):
            conn.execute(f"delete from lead_agent.{table} where run_id = %s", (ctx.run_id,))
        conn.execute("delete from lead_agent.spend_ledger where ref_id = %s", (ctx.run_id,))
        conn.execute("delete from lead_agent.runs where id = %s", (ctx.run_id,))


async def call(ctx, name, args):
    result = await logged_call(ctx, name, args, T.build_handlers(ctx)[name], T.TOOL_SPECS[name][2])
    return json.loads(result["content"][0]["text"]), result.get("is_error", False)


ICP = {"target_company_type": "B2B SaaS", "industries": ["B2B SaaS"], "geography": ["United States"],
       "headcount_range": "10-100", "hard_filters": ["US", "B2B SaaS", "10-100 employees"], "soft_preferences": [],
       "disqualifiers": [],
       "discovery_query_plan": ["clinic scheduling software"], "assumptions": [], "user_constraints_preserved": []}


@pytest.mark.db
async def test_out_of_scope_request_cannot_be_searched(run_ctx):
    data, _ = await call(run_ctx, "save_icp", {"purpose": "x", "request_type": "question", "icp": ICP,
                                               "is_searchable": True})
    run = db.get_run(run_ctx.run_id)
    assert run["request_type"] == "question" and run["icp_signature"] is None
    assert "can't answer general questions" in run["clarification_question"]


@pytest.mark.db
async def test_apify_out_of_credit_stops_the_run(run_ctx, monkeypatch):
    await call(run_ctx, "save_icp", {"purpose": "x", "request_type": "lead_search", "icp": ICP, "is_searchable": True})

    async def no_credit(**kw):
        raise apify_svc.DiscoveryError("apify_no_credit", "Apify API error (402)", failure_code="apify_no_credit")
    monkeypatch.setattr(T.apify_svc, "find_companies", no_credit)
    data, err = await call(run_ctx, "discover_companies", {"purpose": "x", "search_query": "saas"})
    assert err and run_ctx.fatal is not None and run_ctx.fatal.code == "apify_no_credit"
    call_row = db.list_tool_calls(run_ctx.run_id)[-1]
    assert "402" in (call_row["error_message"] or "")  # technical detail kept for admins


@pytest.mark.db
async def test_fact_checker_outage_does_not_use_up_rewrites(run_ctx, monkeypatch):
    lead = db.insert_lead_if_new(run_ctx.run_id, company_name="Acme", company_domain="acme-hardtest.com",
                                 linkedin_url=None, discovery_data={}, prescreen_result="passed", prescreen_reason="",
                                 qualification_status="qualified", fetched_urls=["https://acme-hardtest.com/"])
    db.update_lead(str(lead["id"]), source_urls=["https://acme-hardtest.com/"], source_summary="Acme sells SaaS.")

    async def down(*a, **k):
        return GroundingResult(False, None, [], Decimal("0"), "claude-haiku-4-5", 0, 0, "fact-checker unavailable (529)",
                               failure_code="anthropic_unavailable")
    monkeypatch.setattr(T, "check_grounding", down)
    steps = [{"step": i, "subject": f"Clinic onboarding {i}",
              "body": "Hi {{first_name}}, Acme sells SaaS. Quick question? {{sender_name}}",
              "personalization_note": "n", "evidence_ref": "https://acme-hardtest.com/"} for i in (1, 2, 3)]
    args = {"purpose": "o", "domain": "acme-hardtest.com", "emails": steps,
            "linkedin_message": "Hi {{first_name}}, quick question?"}
    for _ in range(2):
        data, err = await call(run_ctx, "save_outreach", args)
        assert err and "not counted" in data["error"]
    assert db.get_lead(str(lead["id"]))["outreach_attempts"] == 0
    assert run_ctx.fatal is None
    await call(run_ctx, "save_outreach", args)
    assert run_ctx.fatal is not None  # 3 outages in a row -> stop the run


# --- web: gate + preflight answer in plain language; admins get System issues ----------------
@pytest.fixture
def client_as(monkeypatch, app_dsn):
    from fastapi.testclient import TestClient

    from app.main import app

    def make(member):
        monkeypatch.setattr(auth, "resolve_session",
                            lambda request: ({"sub": member.user_id}, None) if member else (None, None))
        monkeypatch.setattr(auth, "load_member", lambda uid: member if member and uid == member.user_id else None)
        return TestClient(app, follow_redirects=False)
    return make


ADMIN = auth.Member(user_id=str(uuid.uuid4()), email="a@example.invalid", full_name="Admin", role="admin",
                    is_owner=False)
MEMBER = auth.Member(user_id=str(uuid.uuid4()), email="m@example.invalid", full_name="Member", role="member",
                     is_owner=False)
DEV = auth.Member(user_id=str(uuid.uuid4()), email="d@example.invalid", full_name="Dev", role="admin", is_owner=False,
                  is_developer=True)


@pytest.mark.db
def test_gibberish_is_refused_for_free(client_as, monkeypatch):
    called = []
    monkeypatch.setattr(health, "preflight", lambda limits: called.append(1) or [])
    r = client_as(ADMIN).post("/runs", data={"objective": "......,,,,,,,huovivpfefopfo", "idempotency_key":
                                             str(uuid.uuid4()), "csrf_token": auth.csrf_token_for(ADMIN.user_id)},
                              headers={"HX-Request": "true"})
    assert r.status_code == 400 and "symbols" in r.text and called == []


@pytest.mark.db
def test_service_down_gives_plain_message_not_jargon(client_as, monkeypatch):
    import app.web.routes as routes
    monkeypatch.setattr(routes.health, "preflight",
                        lambda limits: [ServiceFailure("apify_no_credit", "HTTP 402 usage limit")])
    monkeypatch.setattr(routes.alerts, "raise_alert", lambda *a, **k: None)
    monkeypatch.setattr(routes.db, "active_run", lambda: None)
    r = client_as(MEMBER).post("/runs", data={"objective": "Find US B2B SaaS companies with 10 to 100 employees",
                                              "idempotency_key": str(uuid.uuid4()),
                                              "csrf_token": auth.csrf_token_for(MEMBER.user_id)},
                               headers={"HX-Request": "true"})
    assert r.status_code == 503
    assert "run out of credit" in r.text and "402" not in r.text and "APIFY" not in r.text


@pytest.mark.db
def test_system_issues_page_is_developer_only(client_as):
    assert client_as(DEV).get("/system").status_code == 200
    assert client_as(ADMIN).get("/system").status_code == 403  # admins see plain language only (D-90)
    assert client_as(MEMBER).get("/system").status_code == 403


# --- disqualifiers, soft preferences, tool fingerprints, scope pre-check ----------------------
from app.lib.qualification_rules import DisqualifierCheck, HardFilterCheck  # noqa: E402
from app.lib.scoring import compute_fit_score, decide  # noqa: E402
from app.lib.tech_signals import detect_tools  # noqa: E402
from app.services import scope as scope_svc  # noqa: E402

SRC = "https://acme.io/"
PASS = [HardFilterCheck(filter="US", result="pass", evidence="HQ Austin", source_url=SRC)]


def _status_with(disq):
    score = compute_fit_score(hard_checks=PASS, disqualifier_checks=disq, soft_checks=[], required_filters=["US"],
                              required_disqualifiers=["Agency"], discovery={}, company_domain="acme.io",
                              source_urls=[SRC])
    return decide("qualified", score)[0]


def test_disqualifier_that_applies_means_not_qualified():
    d = [DisqualifierCheck(disqualifier="Agency", applies="yes", evidence="sells agency services", source_url=SRC)]
    assert _status_with(d) == "not_qualified"


def test_disqualifier_unknown_or_unevidenced_means_needs_review():
    d = [DisqualifierCheck(disqualifier="Agency", applies="no", evidence="", source_url=None)]
    assert _status_with(d) == "needs_review"
    assert _status_with([]) == "needs_review"


def test_disqualifier_confirmed_not_applying_allows_qualified():
    d = [DisqualifierCheck(disqualifier="Agency", applies="no", evidence="own SaaS product", source_url=SRC)]
    assert _status_with(d) == "qualified"


def test_tool_fingerprints_found_in_html():
    html = ('<script src="https://js.hs-scripts.com/123.js"></script>'
            '<script src="https://widget.intercom.io/widget/abc"></script><a href="https://jobs.lever.co/acme">Jobs</a>')
    assert [t["name"] for t in detect_tools(html)] == ["HubSpot", "Intercom", "Lever"]
    assert detect_tools("<html><p>We use HubSpot</p></html>") == []  # a word in the text is not a fingerprint
    assert detect_tools(None) == []


class _FakeScopeClient:
    def __init__(self, verdict):
        self.messages = self
        self.verdict = verdict

    async def parse(self, **kwargs):
        assert kwargs["model"] == "claude-haiku-4-5"
        return SimpleNamespace(parsed_output=self.verdict, usage=SimpleNamespace(input_tokens=400, output_tokens=40))


async def test_scope_precheck_is_cheap_and_classifies():
    verdict = scope_svc.ScopeVerdict(request_type="question", reason="asks where to buy ice cream")
    res = await scope_svc.check_scope("can I buy icecream in ife?", client=_FakeScopeClient(verdict))
    assert res.verdict.request_type == "question" and float(res.cost_usd) == pytest.approx(0.0006)


def test_developers_get_the_fix_everyone_else_the_plain_message():
    from app.failures import admin_message_from_detail, message_for
    f = ServiceFailure("apify_no_credit", "$4.99 of $5.00 used")
    assert "Our support team has been told" in message_for(f, technical=False)
    tech = message_for(f, technical=True)
    assert "Our support team has been told" not in tech and "Apify Console" in tech and "$4.99" in tech
    assert "Apify Console" in admin_message_from_detail("[apify_no_credit] $4.99 of $5.00 used")
    assert admin_message_from_detail("no code here") is None


@pytest.mark.db
def test_developer_sees_fix_when_a_service_is_down(client_as, monkeypatch):
    import app.web.routes as routes
    monkeypatch.setattr(routes.health, "preflight", lambda limits: [ServiceFailure("apify_no_credit", "HTTP 402")])
    monkeypatch.setattr(routes.alerts, "raise_alert", lambda *a, **k: None)
    monkeypatch.setattr(routes.db, "active_run", lambda: None)
    def post(who):
        return client_as(who).post("/runs", data={"objective": "Find US B2B SaaS companies with 10 to 100 employees",
                                                  "idempotency_key": str(uuid.uuid4()),
                                                  "csrf_token": auth.csrf_token_for(who.user_id)},
                                   headers={"HX-Request": "true"})
    r = post(DEV)
    assert r.status_code == 503 and "Apify Console" in r.text and "Our support team has been told" not in r.text
    r = post(ADMIN)  # admins get plain language, like members (D-90)
    assert r.status_code == 503 and "Apify Console" not in r.text and "Our support team has been told" in r.text


@pytest.mark.db
def test_marking_an_issue_fixed_updates_the_badge_at_once(admin_dsn, monkeypatch):
    from app import alerts
    monkeypatch.setattr(alerts, "_post_webhook", lambda payload: True)
    code = "test_only_badge"
    real = alerts.CATALOGUE["apify_actor_not_found"]
    monkeypatch.setitem(alerts.CATALOGUE, code, type(real)(code, real.service, real.severity, real.client, real.admin))
    try:
        alerts.raise_alert(code, "x", notify=False)
        before = alerts.open_issue_count()  # now cached for 15 s
        row = db.fetch_one("select id from lead_agent.system_events where code = %s", (code,))
        alerts.resolve(str(row["id"]), None)
        assert alerts.open_issue_count() == before - 1
    finally:
        with psycopg.connect(admin_dsn, prepare_threshold=None, autocommit=True) as conn:
            conn.execute("delete from lead_agent.system_events where code = %s", (code,))


@pytest.mark.db
def test_startup_recovery_never_fails_a_run_another_server_is_working_on(admin_dsn):
    """The database is shared (laptop + Render): only runs silent for 5 minutes are orphans (D-99, live run c7cfea10)."""
    run, _ = db.create_run(idempotency_key=f"test-{uuid.uuid4()}", objective="heartbeat test objective",
                           objective_hash="h" + uuid.uuid4().hex, limits=DEV_LIMITS.to_dict(), run_kind="dev")
    rid = str(run["id"])
    try:
        db.set_status(rid, "researching", "working")
        db.touch_runs([rid])  # the heartbeat of the server doing the work
        db.fail_orphaned_runs()  # e.g. a laptop starting up at the same moment
        assert db.get_run(rid)["status"] == "researching"
        with psycopg.connect(admin_dsn, prepare_threshold=None, autocommit=True) as conn:
            try:  # backdate without the updated_at trigger (session-only), to simulate a server that died
                conn.execute("set session_replication_role = replica")
            except psycopg.Error:
                pytest.skip("can't bypass the updated_at trigger with this role")
            conn.execute("update lead_agent.runs set updated_at = now() - interval '10 minutes' where id = %s", (rid,))
        db.fail_orphaned_runs()
        assert db.get_run(rid)["status"] == "paused"  # can be continued (D-100)
    finally:
        with psycopg.connect(admin_dsn, prepare_threshold=None, autocommit=True) as conn:
            conn.execute("delete from lead_agent.runs where id = %s", (rid,))
