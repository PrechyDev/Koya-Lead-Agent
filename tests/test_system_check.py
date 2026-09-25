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
