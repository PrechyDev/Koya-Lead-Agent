"""Regressions for the second system check (2026-09-25). Pure or faked: no paid APIs, no real sign-ins."""

import asyncio
import logging
from io import StringIO
from types import SimpleNamespace

import httpx
import pytest
import respx

from app import auth
from app import runs as runs_mod
from app.lib.icp_defaults import apply_defaults
from app.lib.objective import normalize_headcount, objective_problem
from app.lib.outreach_checks import _contact_or_url_problems, _placeholder_problems
from app.logging_setup import RedactingFormatter
from app.services import firecrawl as fc
from app.services import scope as scope_svc


# --- runs: a person's Cancel vs. a server shutdown -------------------------------------------------------------
@pytest.fixture
def status_log(monkeypatch):
    calls = []

    async def fake_run(fn, *args, **kwargs):  # db.run(...) without a database
        calls.append((args, kwargs))
    monkeypatch.setattr(runs_mod.db, "run", fake_run)

    async def slow(run_id, skip_icp=False):
        await asyncio.sleep(30)
    monkeypatch.setattr(runs_mod, "execute_run", slow)
    return calls


async def test_a_person_cancelling_ends_the_run_cancelled(status_log):
    m = runs_mod.RunManager()
    m.start("r1")
    await asyncio.sleep(0)
    assert m.cancel("r1")
    await asyncio.sleep(0.05)
    assert status_log[-1][0][1] == "cancelled"


async def test_a_shutdown_marks_the_run_interrupted_not_cancelled_by_a_user(status_log):
    """Before: a deploy recorded "Cancelled by a user" (E-19 says "Interrupted by server restart")."""
    m = runs_mod.RunManager()
    m.start("r2")
    await asyncio.sleep(0)
    await m.shutdown()  # waits (up to SHUTDOWN_GRACE_S) for the status write
    args, _ = status_log[-1]
    assert args[1] == "failed" and "Interrupted by server restart" in args[2]


# --- auth: a rejected refresh token is cleared; a network problem is not -------------------------------------
def _request_with(refresh: str):
    return SimpleNamespace(cookies={auth.REFRESH_COOKIE: refresh})


@respx.mock
def test_a_rejected_refresh_token_asks_to_clear_the_session(monkeypatch):
    monkeypatch.setattr(auth, "_auth_url", lambda path: "https://sb.example/auth/v1/" + path)
    monkeypatch.setattr(auth, "_auth_headers", lambda: {})
    respx.post("https://sb.example/auth/v1/token").mock(return_value=httpx.Response(400, json={"error": "invalid"}))
    assert auth.resolve_session(_request_with("old")) == (None, auth.CLEAR_SESSION)


@respx.mock
def test_a_network_problem_keeps_the_session(monkeypatch):
    monkeypatch.setattr(auth, "_auth_url", lambda path: "https://sb.example/auth/v1/" + path)
    monkeypatch.setattr(auth, "_auth_headers", lambda: {})
    respx.post("https://sb.example/auth/v1/token").mock(side_effect=httpx.ConnectError("down"))
    assert auth.resolve_session(_request_with("old")) == (None, None)


# --- logging: tracebacks are masked too ------------------------------------------------------------------------
def test_tracebacks_are_masked():
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(RedactingFormatter("%(message)s"))
    log = logging.getLogger("test.redaction")
    log.addHandler(handler)
    fake_dsn = "postgresql://app:" + "hunter2secret" + "@db.example/postgres"  # built at runtime: the pre-commit
    try:                                                                     # secret scan rightly blocks the literal
        raise RuntimeError(f"{fake_dsn} failed for jane@acme.io")
    except RuntimeError:
        log.exception("boom")
    finally:
        log.removeHandler(handler)
    out = stream.getvalue()
    assert "hunter2secret" not in out and "jane@acme.io" not in out and "Traceback" in out


# --- config: a blank env var means the default, for every setting ----------------------------------------------
def test_blank_env_vars_fall_back_to_defaults(monkeypatch):
    from app.config import Settings
    for key in ("COOKIE_SECURE", "RESEARCH_REUSE_DAYS", "MAX_RUNS_PER_DAY", "DEV_LIMITS"):
        monkeypatch.setenv(key, "")
    s = Settings()
    assert s.cookie_secure is None and s.research_reuse_days == 30 and isinstance(s.max_runs_per_day, int)


# --- rules --------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("value,expected", [("50 or fewer employees", "1-50"), ("50 or less", "1-50")])
def test_or_fewer_is_an_upper_bound(value, expected):
    assert normalize_headcount(value) == expected


@pytest.mark.parametrize("objective", [
    "Find companies with 1000000 dollars revenue in Texas",
    "Найдите пять компаний "
    "по разработке софта в "
    "Москве",  # Russian: "Find five software development companies in Moscow"
])
def test_numbers_and_other_scripts_are_not_junk(objective):
    assert objective_problem(objective) is None


def test_a_blank_geography_gets_the_default():
    icp, added = apply_defaults({"target_company_type": "B2B SaaS", "geography": [""], "hard_filters": []})
    assert icp["geography"] == ["United States"] and any("Location" in a for a in added)


def test_odd_placeholders_are_caught():
    assert _placeholder_problems("email 1", "Hi {{company1}} and {{first-name}}")


def test_years_in_a_draft_are_not_a_phone_number():
    assert _contact_or_url_problems("email 1", "Since 2019-2020-2021 you tripled headcount.") == []
    assert _contact_or_url_problems("email 1", "Call 415-555-0100.") != []


# --- services -------------------------------------------------------------------------------------------------------
async def test_an_unparseable_scope_answer_falls_back_to_the_icp_step():
    class Broken:
        async def parse(self, **kwargs):
            raise ValueError("answer was cut off")
    client = SimpleNamespace(messages=Broken())
    res = await scope_svc.check_scope("find US dental clinics with ten staff", client=client)
    assert res.verdict is None and res.failure_code is None


@respx.mock
async def test_a_firecrawl_answer_that_is_not_json_is_a_scrape_error(monkeypatch):
    monkeypatch.setattr(fc, "get_settings", lambda: SimpleNamespace(firecrawl_api_key="k"))
    respx.post(fc.API_URL).mock(return_value=httpx.Response(200, text="<html>gateway</html>"))
    with pytest.raises(fc.ScrapeError):
        await fc.scrape("https://acme.io/")


# --- review 3 (2026-09-25): flow-trace fixes ------------------------------------------------------------------------
def test_a_fresh_sign_in_is_not_undone_by_clearing_a_stale_refresh_cookie():
    """Errors log #100: login set new cookies, then the middleware cleared them (stale refresh cookie)."""
    from starlette.responses import Response

    from app.main import _finish
    response = Response()
    auth.set_session_cookies(response, {"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600})
    _finish(response, auth.CLEAR_SESSION)
    cookies = response.headers.getlist("set-cookie")
    assert not any("Max-Age=0" in c or "max-age=0" in c for c in cookies)
    assert any(c.startswith(f"{auth.ACCESS_COOKIE}=new-access") for c in cookies)


def test_a_rejected_refresh_cookie_is_still_cleared_on_other_pages():
    from starlette.responses import Response

    from app.main import _finish
    response = _finish(Response(), auth.CLEAR_SESSION)
    assert any(c.startswith(f"{auth.ACCESS_COOKIE}=") and "max-age=0" in c.lower()
               for c in response.headers.getlist("set-cookie"))


def test_a_phase_that_got_its_final_message_records_that_cost_however_it_ended(monkeypatch):
    """Errors log #99: a timeout/cancel after the final message used to record nothing."""
    from app.agent import runner
    recorded = []
    monkeypatch.setattr(runner, "_record_cost", lambda run_id, source, result, model: recorded.append((source, model)))
    session = runner._Session()
    session.result = SimpleNamespace(total_cost_usd=0.01)
    runner._phase_cost("r1", "run", session, "claude-sonnet-5", "watchdog timeout")
    assert recorded == [("run", "claude-sonnet-5")]
    assert runner._phase_cost("r2", "run", runner._Session(), "claude-sonnet-5") == 0  # nothing started: nothing to record


def test_the_run_budget_rule_includes_the_fact_check_reserve():
    """One rule for the web route and both runner phases: run cap + fact-check reserve must fit (rule 5)."""
    from decimal import Decimal

    from app.config import GROUNDING_RUN_CAP_USD
    from app.lib.budget import BudgetExceeded, assert_run_fits
    assert_run_fits(Decimal("4.65"), 1.25, 6)  # 4.65 + 1.25 + 0.10 = 6.00: fits exactly
    with pytest.raises(BudgetExceeded):
        assert_run_fits(Decimal("4.66"), 1.25, 6)
    assert GROUNDING_RUN_CAP_USD == Decimal("0.10")


def test_only_the_protected_admin_trigger_becomes_not_allowed(monkeypatch):
    """A DB outage used to be shown as "That change isn't allowed." (and never alerted)."""
    import psycopg
    from fastapi.testclient import TestClient

    from app import db
    from app.main import app
    admin = auth.Member(user_id="00000000-0000-0000-0000-000000000001", email="a@acme.io", full_name="A",
                        role="admin", is_owner=True)
    monkeypatch.setattr(auth, "resolve_session", lambda request: ({"sub": admin.user_id}, None))
    monkeypatch.setattr(auth, "load_member", lambda uid: admin)

    def protected(*a, **k):
        raise psycopg.errors.CheckViolation("At least one active admin must remain")
    monkeypatch.setattr(db, "update_member", protected)
    c = TestClient(app, raise_server_exceptions=False)
    body = {"action": "make_member", "csrf_token": auth.csrf_token_for(admin.user_id)}
    target = "/team/00000000-0000-0000-0000-000000000002/update"
    r = c.post(target, data=body, headers={"HX-Request": "true"})
    assert r.status_code == 400 and "At least one active admin must remain" in r.text

    def outage(*a, **k):
        raise RuntimeError("something else broke")
    monkeypatch.setattr(db, "update_member", outage)
    monkeypatch.setattr("app.alerts.raise_alert", lambda *a, **k: None)
    r = c.post(target, data=body, headers={"HX-Request": "true"})
    assert r.status_code == 500 and "isn't allowed" not in r.text
