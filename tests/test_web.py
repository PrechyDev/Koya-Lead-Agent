"""Pages and actions through FastAPI's TestClient. The auth boundary is faked (no real Supabase login);
roles, CSRF, validation and rendering are real. No paid calls: no run is actually started."""

import uuid

import pytest
from fastapi.testclient import TestClient

from app import auth, db
from app.auth import Member

pytestmark = pytest.mark.db

ADMIN = Member(user_id=str(uuid.uuid4()), email="admin@example.invalid", full_name="Test Admin", role="admin",
               is_owner=False)
MEMBER = Member(user_id=str(uuid.uuid4()), email="member@example.invalid", full_name="Test Member", role="member",
                is_owner=False)


@pytest.fixture
def client_as(monkeypatch, app_dsn):
    from app.main import app

    def make(member: Member | None):
        monkeypatch.setattr(auth, "resolve_session",
                            lambda request: ({"sub": member.user_id}, None) if member else (None, None))
        monkeypatch.setattr(auth, "load_member", lambda uid: member if member and uid == member.user_id else None)
        return TestClient(app, follow_redirects=False)
    return make


@pytest.fixture
def a_run():
    runs = [r for r in db.list_runs(20) if r["run_kind"] in ("dev", "app")]
    if not runs:
        pytest.skip("no runs in the database yet")
    return runs[0]


def test_logged_out_is_redirected_and_public_pages_work(client_as, monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "fixture_mode", False)
    c = client_as(None)
    assert c.get("/fixtures/injection.html").status_code == 404  # off by default: no public test pages
    monkeypatch.setattr(get_settings(), "fixture_mode", True)
    r = c.get("/")
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert c.get("/login").status_code == 200
    assert c.get("/health").json()["database"] == "ok"
    page = c.get("/fixtures/injection.html")
    assert page.status_code == 200 and "IGNORE ALL PREVIOUS INSTRUCTIONS" in page.text
    htmx = c.get("/runs/x/live", headers={"HX-Request": "true"})
    assert htmx.status_code == 401 and htmx.headers["HX-Redirect"].startswith("/login")


def test_home_renders_form_with_states(client_as):
    r = client_as(ADMIN).get("/")
    assert r.status_code == 200
    html = r.text
    assert 'id="start-run"' in html and "disabled" in html            # disabled until valid, with a reason
    assert "Enter at least 5 characters" not in html  # an empty box shows no message; the button is just disabled
    assert "Drafts are never sent" in html and "Claude budget" in html  # admin sees budget
    assert client_as(MEMBER).get("/").text.count("Claude budget") == 0  # member does not


def test_member_cannot_open_admin_pages(client_as):
    c = client_as(MEMBER)
    assert c.get("/team").status_code == 403
    assert c.get("/spend").status_code == 403
    r = c.post("/team/invite", data={"email": "x@example.invalid", "full_name": "X", "csrf_token":
                                    auth.csrf_token_for(MEMBER.user_id)})
    assert r.status_code == 403


def test_admin_pages_render(client_as):
    c = client_as(ADMIN)
    assert c.get("/team").status_code == 200
    spend = c.get("/spend")
    assert spend.status_code == 200 and "Claude budget used" in spend.text


def test_post_without_csrf_is_refused(client_as):
    r = client_as(ADMIN).post("/runs", data={"objective": "Find US SaaS companies", "idempotency_key": "k"},
                              headers={"HX-Request": "true"})
    assert r.status_code == 403 and "expired" in r.text


def test_objective_validation_goes_to_banner(client_as):
    token = auth.csrf_token_for(ADMIN.user_id)
    r = client_as(ADMIN).post("/runs", data={"objective": "abc", "idempotency_key": str(uuid.uuid4()),
                                             "csrf_token": token}, headers={"HX-Request": "true"})
    assert r.status_code == 400 and r.headers["HX-Retarget"] == "#system-message"
    assert "at least 5 words" in r.text


def test_double_submit_goes_to_existing_run(client_as, a_run):
    token = auth.csrf_token_for(ADMIN.user_id)
    r = client_as(ADMIN).post("/runs", data={"objective": "find software companies in Texas please", "csrf_token": token,
                                             "idempotency_key": a_run["idempotency_key"]}, headers={"HX-Request": "true"})
    assert r.status_code == 204 and r.headers["HX-Redirect"] == f"/runs/{a_run['id']}"


def test_run_page_live_tabs_and_exports(client_as, a_run):
    c = client_as(ADMIN)
    rid = a_run["id"]
    assert c.get(f"/runs/{rid}").status_code == 200
    live = c.get(f"/runs/{rid}/live")
    assert live.status_code in (200, 286) and 'class="stepper"' in live.text
    for tab in ("icp", "leads", "calls", "summary"):
        t = c.get(f"/runs/{rid}/tab/{tab}")
        assert t.status_code in (200, 286), tab
        assert 'id="tab-panel"' in t.text
    csv = c.get(f"/runs/{rid}/export.csv")
    assert csv.status_code == 200 and csv.text.startswith("company_name,company_domain")
    pack = c.get(f"/runs/{rid}/export.json").json()
    assert pack["run_id"] == str(rid)
    assert all(l["email_sequence"] == "not approved" or l["review_status"] == "approved" for l in pack["leads"])


def test_lead_drawer_and_review_rules(client_as, a_run):
    leads = db.list_leads(str(a_run["id"]))
    if not leads:
        pytest.skip("run has no leads")
    c = client_as(ADMIN)
    lead = leads[0]
    r = c.get(f"/leads/{lead['id']}")
    assert r.status_code == 200 and 'role="dialog"' in r.text
    token = auth.csrf_token_for(ADMIN.user_id)
    reject = c.post(f"/leads/{lead['id']}/review", data={"review_status": "rejected", "reviewer_note": "",
                                                         "csrf_token": token}, headers={"HX-Request": "true"})
    assert reject.status_code == 400  # a note is required to reject


def test_banner_escapes_user_text(client_as):
    from starlette.requests import Request

    from app.web.routes import _banner
    req = Request({"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b""})
    html = _banner(req, "info", "<script>alert(1)</script>").body.decode()
    assert "<script>" not in html and "&lt;script&gt;" in html


def test_buttons_wait_for_required_inputs():
    """A click must never start a loading state before the browser validates required fields (owner bug report)."""
    from pathlib import Path
    root = Path(__file__).parents[1] / "app"
    login = (root / "templates" / "login.html").read_text(encoding="utf-8")
    assert "onclick" not in login and "required" in login
    detail = (root / "templates" / "partials" / "lead_detail.html").read_text(encoding="utf-8")
    assert 'value="rejected" data-requires="note-' in detail
    js = (root / "static" / "app.js").read_text(encoding="utf-8")
    assert 'addEventListener("submit"' in js and "showFieldError" in js and 'data-requires' in js


@pytest.mark.parametrize("email, ok", [
    ("weknofn@wowi", False), ("sam@acme.", False), ("sam@acme.c", False), ("@acme.io", False), ("sam@@acme.io", False),
    ("wefppq@pfpqn.iwe", True), ("xxx@xxx.xxxx.xxxx", True), ("first.last+tag@mail.acme.co.uk", True),
])
def test_email_rule(email, ok):
    from app.lib.validation import is_valid_email
    assert is_valid_email(email) is ok


def test_login_rejects_malformed_email_before_supabase(client_as, monkeypatch):
    from app import auth
    called = []
    monkeypatch.setattr(auth, "password_sign_in", lambda *a: called.append(a))
    r = client_as(None).post("/login", data={"email": "weknofn@wowi", "password": "x", "next": "/"})
    assert r.status_code == 400 and "valid email" in r.text and called == []
    page = client_as(None).get("/login").text
    assert 'pattern="' in page  # the browser uses the same rule


def test_agent_can_start_depends_on_the_event_loop():
    """Windows + `uvicorn --reload` gives a Selector loop that can't start the Claude CLI (owner run 27b7e5f0)."""
    import asyncio
    import sys

    from app.agent.runner import agent_can_start

    async def probe():
        return agent_can_start()
    if sys.platform != "win32":
        assert asyncio.run(probe())
        return
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        assert asyncio.run(probe()) is False
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        assert asyncio.run(probe()) is True
    finally:
        asyncio.set_event_loop_policy(None)


def test_run_refused_with_the_real_fix_when_the_agent_cannot_start(client_as, monkeypatch):
    from app.web import routes
    monkeypatch.setattr(routes, "agent_can_start", lambda: False)
    monkeypatch.setattr(routes.health, "preflight", lambda limits: [])
    monkeypatch.setattr(routes.alerts, "raise_alert", lambda *a, **k: None)
    before = len(db.list_runs(100))
    r = client_as(ADMIN).post("/runs", data={"objective": "Find US B2B SaaS companies with 10 to 100 staff",
                                             "idempotency_key": str(uuid.uuid4()),
                                             "csrf_token": auth.csrf_token_for(ADMIN.user_id)},
                              headers={"HX-Request": "true"})
    assert r.status_code == 503 and "without --reload" in r.text.replace("WITHOUT", "without")
    assert len(db.list_runs(100)) == before  # nothing created, nothing spent
    r = client_as(MEMBER).post("/runs", data={"objective": "Find US B2B SaaS companies with 10 to 100 staff",
                                              "idempotency_key": str(uuid.uuid4()),
                                              "csrf_token": auth.csrf_token_for(MEMBER.user_id)},
                               headers={"HX-Request": "true"})
    assert "research engine" in r.text and "reload" not in r.text  # members get the plain message


@pytest.mark.parametrize("target, expected", [
    ("/runs/1", "/runs/1"), ("//evil.com", "/"), ("/\evil.com", "/"), ("https://evil.com", "/"), ("", "/"),
])
def test_login_redirect_stays_on_this_site(target, expected):
    from app.lib.validation import safe_next
    assert safe_next(target) == expected


def test_links_from_data_can_never_run_script():
    from app.lib.validation import safe_url
    assert safe_url("javascript:alert(1)") == "#" and safe_url("data:text/html,x") == "#"
    assert safe_url("https://acme.io/about") == "https://acme.io/about"


def test_csv_cells_cannot_become_formulas():
    from app.lib.validation import csv_cell
    assert csv_cell("=HYPERLINK(\"x\")").startswith("'=") and csv_cell("Acme") == "Acme"


def test_rate_limit_ip_ignores_a_spoofed_forwarded_header():
    from starlette.requests import Request

    from app.main import client_ip
    scope = {"type": "http", "headers": [(b"x-forwarded-for", b"6.6.6.6, 203.0.113.9")], "client": ("10.0.0.1", 1)}
    assert client_ip(Request(scope)) == "203.0.113.9"  # the address Render's proxy appended


def test_security_headers_and_no_self_demotion(client_as):
    r = client_as(ADMIN).get("/")
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
    token = auth.csrf_token_for(ADMIN.user_id)
    r = client_as(ADMIN).post(f"/team/{ADMIN.user_id}/update", data={"action": "deactivate", "csrf_token": token},
                              headers={"HX-Request": "true"})
    assert r.status_code == 400 and "own admin access" in r.text
