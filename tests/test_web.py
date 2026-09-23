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
    assert "Enter at least 5 characters to start." in html or "A run is in progress" in html
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
    assert "at least a few words" in r.text


def test_double_submit_goes_to_existing_run(client_as, a_run):
    token = auth.csrf_token_for(ADMIN.user_id)
    r = client_as(ADMIN).post("/runs", data={"objective": "anything long enough", "csrf_token": token,
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
    assert 'addEventListener("submit"' in js and "formProblem" in js and 'data-requires' in js


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
