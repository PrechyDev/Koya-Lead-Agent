"""Pages and actions through FastAPI's TestClient. The auth boundary is faked (no real Supabase login);
roles, CSRF, validation and rendering are real. No paid calls: no run is actually started."""

import uuid
from datetime import UTC

import pytest
from fastapi.testclient import TestClient

from app import auth, db
from app.auth import Member

pytestmark = pytest.mark.db

ADMIN = Member(user_id=str(uuid.uuid4()), email="admin@example.invalid", full_name="Test Admin", role="admin",
               is_owner=False)
MEMBER = Member(user_id=str(uuid.uuid4()), email="member@example.invalid", full_name="Test Member", role="member",
                is_owner=False)
DEVELOPER = Member(user_id=str(uuid.uuid4()), email="dev@example.invalid", full_name="Test Developer", role="admin",
                   is_owner=True, is_developer=True)


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
    runs = [r for r in db.search_runs(limit=20)[0] if r["run_kind"] in ("dev", "app")]
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
    r = client_as(ADMIN).get("/runs/new")
    assert r.status_code == 200
    html = r.text
    assert 'id="start-run"' in html and "disabled" in html            # disabled until valid, with a reason
    assert "Enter at least 5 characters" not in html  # an empty box shows no message; the button is just disabled
    assert "Nothing is sent from this app" in html and "Claude budget" not in html  # budget lives on Spend
    assert "Limits:" not in html and "PRD" not in html  # no cost limits or internal jargon on the home page
    assert "Test mode" not in client_as(MEMBER).get("/runs/new").text  # members never see test-mode details


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
    assert spend.status_code == 200 and "left" in spend.text and "Claude budget used" not in spend.text  # runs, not dollars (D-94)


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
    c = client_as(DEVELOPER)
    rid = a_run["id"]
    assert c.get(f"/runs/{rid}").status_code == 200
    live = c.get(f"/runs/{rid}/live")
    assert live.status_code in (200, 286) and 'class="stepper"' in live.text
    for tab in ("icp", "leads", "calls", "summary"):
        t = c.get(f"/runs/{rid}/tab/{tab}")
        assert t.status_code in (200, 286), tab
        assert 'id="tab-panel"' in t.text
    csv = c.get(f"/runs/{rid}/export.csv")
    assert csv.status_code == 200 and csv.text.startswith("objective,company_name,company_domain,website,linkedin_url")
    assert c.get(f"/runs/{rid}/export.json").status_code == 404  # one export: the CSV sample pack (D-100)


def test_lead_drawer_and_review_rules(client_as, admin_dsn):
    import psycopg

    from app.config import DEV_LIMITS
    # A throwaway run + lead: if the "note required" rule ever broke, no real lead gets rejected.
    run, _ = db.create_run(idempotency_key=f"test-{uuid.uuid4()}", objective="review rules test objective",
                           objective_hash="h" + uuid.uuid4().hex, limits=DEV_LIMITS.to_dict(), run_kind="dev")
    lead = db.insert_lead_if_new(str(run["id"]), company_name="Review Test", company_domain="review-webtest.com",
                                 linkedin_url=None, discovery_data={}, prescreen_result="passed", prescreen_reason="",
                                 qualification_status="qualified", fetched_urls=[])
    try:
        c = client_as(ADMIN)
        r = c.get(f"/leads/{lead['id']}")
        assert r.status_code == 200 and 'role="dialog"' in r.text
        token = auth.csrf_token_for(ADMIN.user_id)
        reject = c.post(f"/leads/{lead['id']}/review", data={"review_status": "rejected", "reviewer_note": "",
                                                             "csrf_token": token}, headers={"HX-Request": "true"})
        assert reject.status_code == 400  # a note is required to reject
        assert db.get_lead(str(lead["id"]))["review_status"] != "rejected"
    finally:
        with psycopg.connect(admin_dsn, prepare_threshold=None, autocommit=True) as conn:
            conn.execute("delete from lead_agent.leads where run_id = %s", (run["id"],))
            conn.execute("delete from lead_agent.runs where id = %s", (run["id"],))


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
    monkeypatch.setattr(routes.db, "active_run", lambda: None)  # a live run in the shared DB must not change this
    monkeypatch.setattr(routes.health, "preflight", lambda limits: [])
    monkeypatch.setattr(routes.alerts, "raise_alert", lambda *a, **k: None)
    before = db.search_runs()[1]
    r = client_as(DEVELOPER).post("/runs", data={"objective": "Find US B2B SaaS companies with 10 to 100 staff",
                                                 "idempotency_key": str(uuid.uuid4()),
                                                 "csrf_token": auth.csrf_token_for(DEVELOPER.user_id)},
                              headers={"HX-Request": "true"})
    assert r.status_code == 503 and "without --reload" in r.text.replace("WITHOUT", "without")
    assert db.search_runs()[1] == before  # nothing created, nothing spent
    r = client_as(MEMBER).post("/runs", data={"objective": "Find US B2B SaaS companies with 10 to 100 staff",
                                              "idempotency_key": str(uuid.uuid4()),
                                              "csrf_token": auth.csrf_token_for(MEMBER.user_id)},
                               headers={"HX-Request": "true"})
    assert "research engine" in r.text and "reload" not in r.text  # members get the plain message


@pytest.mark.parametrize("target, expected", [
    ("/runs/1", "/runs/1"), ("//evil.com", "/"), ("/\\evil.com", "/"), ("https://evil.com", "/"), ("", "/"),
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


def _signed_token(iat_offset_s: int):
    import time
    from types import SimpleNamespace

    import jwt as pyjwt
    from cryptography.hazmat.primitives.asymmetric import ec
    key = ec.generate_private_key(ec.SECP256R1())
    now = int(time.time())
    token = pyjwt.encode({"sub": "u1", "aud": "authenticated", "iat": now + iat_offset_s, "exp": now + 3600},
                         key, algorithm="ES256")
    return token, SimpleNamespace(get_signing_key_from_jwt=lambda t: SimpleNamespace(key=key.public_key()))


def test_token_from_a_slightly_faster_clock_is_accepted(monkeypatch):
    """Docker's clock ran ~1 s behind Supabase's: a brand-new token looked 'not yet valid' (owner's sign-in)."""
    import jwt as pyjwt
    token, jwks = _signed_token(iat_offset_s=5)
    monkeypatch.setattr(auth, "_jwks", lambda: jwks)
    assert auth.verify_access_token(token)["sub"] == "u1"
    far, jwks = _signed_token(iat_offset_s=300)  # a clock 5 minutes off is still refused
    monkeypatch.setattr(auth, "_jwks", lambda: jwks)
    with pytest.raises(pyjwt.ImmatureSignatureError):
        auth.verify_access_token(far)


def test_sign_in_with_an_unverifiable_token_gets_a_plain_message(client_as, monkeypatch):
    import jwt as pyjwt

    from app.web import routes
    monkeypatch.setattr(auth, "password_sign_in", lambda e, p: {"access_token": "t", "refresh_token": "r"})
    def refuse(token):
        raise pyjwt.ImmatureSignatureError("The token is not yet valid (iat)")
    monkeypatch.setattr(auth, "verify_access_token", refuse)
    monkeypatch.setattr(routes.alerts, "raise_alert", lambda *a, **k: None)
    r = client_as(None).post("/login", data={"email": "sam@acme.io", "password": "x", "next": "/"})
    assert r.status_code == 503 and "confirm your sign-in" in r.text and "Something went wrong" not in r.text


# --- password reset (D-64) ---------------------------------------------------------------------------------
def _uid(email: str) -> str:
    """A stable fake user id; real ones are UUIDs, and routes 404 anything else."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, email))


def _people(monkeypatch, **by_email):
    rows = {e: {"user_id": _uid(e), "email": e, "is_active": active} for e, active in by_email.items()}
    monkeypatch.setattr(db, "get_member_by_email", lambda email: rows.get(email))
    monkeypatch.setattr(db, "get_member", lambda uid: next((r for r in rows.values() if r["user_id"] == uid), None))
    return rows


def test_reset_email_only_for_active_members_and_the_same_answer_for_everyone(client_as, monkeypatch):
    _people(monkeypatch, **{"active@acme.io": True, "gone@acme.io": False})
    sent = []
    monkeypatch.setattr(auth, "send_password_reset", lambda email: sent.append(email))
    pages = [client_as(None).post("/forgot-password", data={"email": e}).text
             for e in ("active@acme.io", "gone@acme.io", "stranger@acme.io")]
    assert sent == ["active@acme.io"]                          # deactivated people and strangers get no email
    assert all("If this email has access" in p for p in pages)  # and nobody can tell who has an account


def test_deactivated_person_cannot_finish_a_reset_here(client_as, monkeypatch):
    _people(monkeypatch, **{"gone@acme.io": False, "active@acme.io": True})
    changed = []
    monkeypatch.setattr(auth, "set_password", lambda token, pw, name=None: changed.append(token))
    monkeypatch.setattr(auth, "verify_access_token", lambda t: {"sub": _uid("gone@acme.io")})
    form = {"access_token": "t1", "password": "new-password", "confirm": "new-password"}
    r = client_as(None).post("/reset-password", data=form)
    assert r.status_code == 403 and "doesn" in r.text and changed == []
    monkeypatch.setattr(auth, "verify_access_token", lambda t: {"sub": _uid("active@acme.io")})
    r = client_as(None).post("/reset-password", data=form)
    assert r.status_code == 303 and changed == ["t1"] and "la_access" in r.headers.get("set-cookie", "")


def test_only_admins_send_reset_links(client_as, monkeypatch):
    _people(monkeypatch, **{"active@acme.io": True, "gone@acme.io": False})
    sent = []
    monkeypatch.setattr(auth, "send_password_reset", lambda email: sent.append(email))
    def admin_post(uid):
        return client_as(ADMIN).post(f"/team/{uid}/update", headers={"HX-Request": "true"},
                                     data={"action": "send_reset", "csrf_token": auth.csrf_token_for(ADMIN.user_id)})
    assert "Reset link sent" in admin_post(_uid("active@acme.io")).text and sent == ["active@acme.io"]
    assert "Reactivate them first" in admin_post(_uid("gone@acme.io")).text and sent == ["active@acme.io"]
    r = client_as(MEMBER).post(f"/team/{_uid('active@acme.io')}/update", headers={"HX-Request": "true"},
                               data={"action": "send_reset", "csrf_token": auth.csrf_token_for(MEMBER.user_id)})
    assert r.status_code == 403 and sent == ["active@acme.io"]


def test_invites_carry_the_details_the_shared_email_template_uses(monkeypatch):
    """One Supabase invite template serves every Koya tool, filled in from what each tool sends (D-65)."""
    from types import SimpleNamespace
    sent = {}
    def fake_post(url, **kw):
        sent.update(kw["json"])
        return SimpleNamespace(status_code=200, json=lambda: {"id": "u1"})
    monkeypatch.setattr(auth.httpx, "post", fake_post)
    auth.invite_user("ada@acme.io", "Ada Obi", role="member", invited_by_name="Precious Okafor")
    data = sent["data"]
    assert data["app_name"] == "Koya Lead Research Agent" and data["invited_by_name"] == "Precious Okafor"
    assert data["role"] == "member" and data["full_name"] == "Ada Obi" and data["app_tagline"]
    assert data["invited_to"] == "lead_agent"  # Week 4's trigger guard still sees it (D-26)
    auth.invite_user("b@acme.io", "B")
    assert "invited_by_name" not in sent["data"]  # nothing empty is sent (the template falls back instead)


# --- "waiting for you" dialogs (owner feedback: the repeat check was buried below the fold) ----------------
def test_stepper_shows_waiting_not_a_spinner_while_paused():
    from app.web.templating import stepper
    assert [s["state"] for s in stepper("awaiting_confirmation")][:3] == ["done", "waiting", "pending"]
    assert [s["state"] for s in stepper("needs_clarification")][:2] == ["waiting", "pending"]


@pytest.fixture
def paused_run(admin_dsn):
    import psycopg

    from app.config import DEV_LIMITS
    run, _ = db.create_run(idempotency_key=f"t-{uuid.uuid4()}", objective="Find US B2B SaaS companies with 10 to 100 staff",
                           objective_hash="h" + uuid.uuid4().hex, limits=DEV_LIMITS.to_dict(), run_kind="dev",
                           created_by=None)
    db.set_status(str(run["id"]), "awaiting_confirmation", "matches an earlier run")
    yield str(run["id"])
    with psycopg.connect(admin_dsn, prepare_threshold=None, autocommit=True) as conn:
        conn.execute("delete from lead_agent.runs where id = %s", (run["id"],))


def test_repeat_check_is_a_dialog_and_cancel_returns_to_the_form(client_as, paused_run):
    c = client_as(ADMIN)
    html = c.get(f"/runs/{paused_run}/live", headers={"HX-Request": "true"}).text
    assert 'role="dialog"' in html and "data-modal-cancel" in html and "Find new companies" in html
    r = c.post(f"/runs/{paused_run}/confirm", headers={"HX-Request": "true"},
               data={"choice": "cancel", "csrf_token": auth.csrf_token_for(ADMIN.user_id)})
    assert r.status_code == 204 and r.headers["HX-Redirect"].startswith("/runs/new?objective=Find+US+B2B+SaaS")
    home = c.get(r.headers["HX-Redirect"]).text
    assert ">Find US B2B SaaS companies with 10 to 100 staff</textarea>" in home  # objective filled back in



# --- Runs history: filters + pagination; the form lives on its own page -----------------------------------
def test_runs_page_lists_history_with_filters_and_a_new_run_button(client_as, paused_run):
    c = client_as(ADMIN)
    page = c.get("/").text
    assert 'href="/runs/new"' in page and "New run" in page and 'id="new-run-title"' not in page
    waiting = c.get("/?status=needs_you").text
    assert paused_run in waiting and "Waiting for you (" in waiting
    assert paused_run not in c.get("/?status=completed").text
    assert paused_run in c.get("/?q=10+to+100+staff").text and paused_run not in c.get("/?q=zzz-no-match").text
    assert "No runs match these filters" in c.get("/?q=zzz-no-match").text
    assert c.get("/?q=100%25").status_code == 200  # a literal % in the search is fine (escaped, not a wildcard)


def test_runs_pagination_and_old_form_links(client_as, monkeypatch):
    from app.web import routes
    monkeypatch.setattr(routes, "RUNS_PER_PAGE", 2)
    c = client_as(ADMIN)
    total = db.search_runs()[1]
    if total <= 2:
        pytest.skip("needs more than 2 runs in the database")
    first = c.get("/").text
    assert "Showing 1–2 of" in first and "Next →</a>" in first and 'aria-disabled="true">← Previous' in first
    assert "Showing 3–" in c.get("/?page=2").text  # page 2 starts at run 3, however many runs exist
    r = c.get("/?page=999")
    assert r.status_code == 303 and "page=" in r.headers["location"]  # past the end: go to the last page
    old = c.get("/?objective=Find+SaaS")  # links from before the split still land on the form
    assert old.status_code == 303 and old.headers["location"].startswith("/runs/new?objective=")


def test_a_rejected_password_keeps_the_reset_link_usable(client_as, monkeypatch):
    """Supabase links are single-use: if the new password is refused, the page must keep the session (audit fix)."""
    _people(monkeypatch, **{"active@acme.io": True})
    monkeypatch.setattr(auth, "verify_access_token", lambda t: {"sub": _uid("active@acme.io")})

    def refuse(*_a, **_k):
        raise auth.AuthError("Choose a password you haven't used before.", 422)
    monkeypatch.setattr(auth, "set_password", refuse)
    r = client_as(None).post("/reset-password", data={"access_token": "tok-1", "refresh_token": "ref-1",
                                                      "password": "new-password", "confirm": "new-password"})
    assert r.status_code == 400 and 'value="tok-1"' in r.text and 'value="ref-1"' in r.text


def test_a_deactivated_person_cannot_accept_an_old_invite(client_as, monkeypatch):
    _people(monkeypatch, **{"gone@acme.io": False})
    changed = []
    monkeypatch.setattr(auth, "verify_access_token", lambda t: {"sub": _uid("gone@acme.io")})
    monkeypatch.setattr(auth, "set_password", lambda *a, **k: changed.append(a))
    r = client_as(None).post("/accept-invite", data={"access_token": "t", "full_name": "Gone", "password": "new-password",
                                                     "confirm": "new-password"})
    assert r.status_code == 403 and changed == []


def test_bad_ids_are_404_not_500(client_as):
    csrf = auth.csrf_token_for(ADMIN.user_id)
    c = client_as(ADMIN)
    assert c.post("/leads/not-a-uuid/review", data={"review_status": "approved", "csrf_token": csrf}).status_code == 404
    assert c.post("/team/not-a-uuid/update", data={"action": "make_admin", "csrf_token": csrf}).status_code == 404


def test_sign_in_redirect_keeps_the_query_string(client_as):
    r = client_as(None).get("/runs/new?objective=US%20dental%20clinics", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?next=/runs/new%3Fobjective%3DUS%2520dental%2520clinics"


# --- admins vs developers (D-90) ---------------------------------------------------------------------------------
def test_technical_views_are_developer_only(client_as, a_run):
    rid = a_run["id"]
    for who in (ADMIN, MEMBER):
        c = client_as(who)
        assert c.get(f"/runs/{rid}/tab/calls").status_code == 403  # refused on the server, not just hidden
        page = c.get(f"/runs/{rid}?tab=calls").text
        assert "Tool calls" not in page and "/tab/calls" not in page
        assert "Agent turns" not in c.get(f"/runs/{rid}/tab/summary").text
        assert c.post("/system/test-alert", data={"csrf_token": auth.csrf_token_for(who.user_id)}).status_code == 403
    admin = client_as(ADMIN)
    assert 'href="/system"' not in admin.get("/runs/new").text and 'href="/spend"' in admin.get("/runs/new").text
    assert "Where the money went" not in admin.get("/spend").text
    assert "By step" not in admin.get("/spend?tab=breakdown").text  # refused on the server: falls back to By run
    assert "Where the money went" in client_as(DEVELOPER).get("/spend").text
    assert "By step" in client_as(DEVELOPER).get("/spend?tab=breakdown").text
    assert "Tool calls" in client_as(DEVELOPER).get(f"/runs/{rid}").text


def _team_rows(monkeypatch, *rows):
    monkeypatch.setattr(db, "list_members", lambda: list(rows))
    monkeypatch.setattr(db, "get_member", lambda uid: next((r for r in rows if str(r["user_id"]) == uid), None))
    updates = []
    monkeypatch.setattr(db, "update_member", lambda uid, **f: updates.append((uid, f)) or {"user_id": uid, **f})
    return updates


def _row(email, role="admin", developer=False, owner=False):
    return {"user_id": _uid(email), "email": email, "full_name": email.split("@")[0], "role": role,
            "is_active": True, "is_owner": owner, "is_developer": developer}


def test_admins_never_see_or_change_developers(client_as, monkeypatch):
    dev, boss = _row("dev@acme.io", developer=True), _row("boss@acme.io")
    updates = _team_rows(monkeypatch, dev, boss)
    page = client_as(ADMIN).get("/team").text
    assert "boss@acme.io" in page and "dev@acme.io" not in page and "Make developer" not in page
    token = auth.csrf_token_for(ADMIN.user_id)
    for action in ("deactivate", "make_member", "send_reset"):  # a hidden person can't be changed by guessing the id
        r = client_as(ADMIN).post(f"/team/{dev['user_id']}/update", data={"action": action, "csrf_token": token})
        assert r.status_code == 404, action
    r = client_as(ADMIN).post(f"/team/{boss['user_id']}/update", data={"action": "make_developer", "csrf_token": token})
    assert r.status_code == 403 and updates == []  # only the owner grants developer access


def test_the_owner_grants_and_removes_developer_access(client_as, monkeypatch):
    boss, dev = _row("boss@acme.io"), _row("dev@acme.io", developer=True)
    updates = _team_rows(monkeypatch, boss, dev)
    c, token = client_as(DEVELOPER), auth.csrf_token_for(DEVELOPER.user_id)
    page = c.get("/team").text
    assert "dev@acme.io" in page and "Developer" in page and "Make developer" in page
    r = c.post(f"/team/{boss['user_id']}/update", headers={"HX-Request": "true"},
               data={"action": "make_developer", "csrf_token": token})
    assert "now has developer access" in r.text and updates[-1] == (boss["user_id"], {"role": "admin", "is_developer": True})
    r = c.post(f"/team/{dev['user_id']}/update", headers={"HX-Request": "true"},
               data={"action": "make_member", "csrf_token": token})
    assert r.status_code == 400 and "developer access first" in r.text
    r = c.post(f"/team/{dev['user_id']}/update", headers={"HX-Request": "true"},
               data={"action": "remove_developer", "csrf_token": token})
    assert "no longer has" in r.text and updates[-1] == (dev["user_id"], {"is_developer": False})


def test_the_database_refuses_a_developer_who_is_not_an_admin(admin_dsn):
    import psycopg
    with psycopg.connect(admin_dsn, prepare_threshold=None) as conn:
        uid = conn.execute("select user_id from lead_agent.members where is_owner").fetchone()[0]
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("update lead_agent.members set is_owner = false, role = 'member' where user_id = %s", (uid,))
        conn.rollback()
        conn.execute("alter table lead_agent.members disable trigger members_protect_admins")  # isolate the new check
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("update lead_agent.members set is_owner = false, role = 'member' where user_id = %s", (uid,))
        conn.rollback()  # nothing is changed: the trigger stays enabled and the owner row is untouched


# --- the partial-run banner says what the client is missing, and why (D-91) ---------------------------------------
def test_partial_banner_names_the_real_gap():
    from app.web.templating import shortfall_sentence
    base = {"limits": {"target_qualified": 2}, "error_detail": None, "shortfall_reason": "the agent's long story"}
    budget = {**base, "error_detail": "[agent_stopped] error_max_budget_usd",
              "shortfall_reason": "The research stopped because it reached this run's AI budget."}
    # all leads found, but the budget ran out before drafting (live run dc8b74f9): drafts are the gap, not the leads
    s = shortfall_sentence({**budget, "usage": {"candidates_found": 3, "qualified": 2}}, 0)
    assert "2 have no email drafts" in s and "AI budget" in s and "met every requirement" not in s
    s = shortfall_sentence({**budget, "usage": {"candidates_found": 5, "qualified": 1}}, 1)
    assert s.startswith("Found 1 of 2") and "AI budget" in s
    s = shortfall_sentence({**base, "usage": {"candidates_found": 5, "qualified": 1}}, 1)
    assert "other companies found didn't meet" in s and "5" not in s  # no rejected-company counts for the client
    assert "no matching companies" in shortfall_sentence({**base, "usage": {}}, 0)
    assert "the agent's long story" not in shortfall_sentence({**base, "usage": {"qualified": 1}}, 1)


def test_run_limit_counters_are_developer_only(client_as, a_run):
    for who, shown in ((MEMBER, False), (ADMIN, False), (DEVELOPER, True)):
        live = client_as(who).get(f"/runs/{a_run['id']}/live").text
        assert ("Pages scraped" in live) is shown and "Qualified" in live



# --- Spend page: tabs (By run first), 20 a page, a readable breakdown (D-92) ------------------------------------
def test_spend_tabs_paginate_20_a_page(client_as, monkeypatch):
    seen = []
    def fake(limit, offset):
        seen.append((limit, offset))
        return [], 45
    monkeypatch.setattr(db, "spend_by_run", fake)
    c = client_as(ADMIN)
    page = c.get("/spend").text
    assert page.index("By run") < page.index("Apify")  # By run is the first tab and the default
    page = c.get("/spend?tab=runs&page=2").text
    assert seen[-1] == (20, 20) and "Page 2 of 3" in page and "/spend?tab=runs&amp;page=3" in page
    assert c.get("/spend?tab=runs&page=9").headers["location"] == "/spend?tab=runs&page=3"


def test_spend_breakdown_names_steps_and_merges_model_variants():
    from datetime import datetime
    from decimal import Decimal

    from app.lib.spend_breakdown import breakdown, model_family
    assert model_family("claude-haiku-4-5-20251001") == model_family("claude-haiku-4-5") == "Haiku 4.5"
    assert model_family("claude-opus-5-5[1m]") == "Opus 5.5" and model_family("claude-sonnet-5") == "Sonnet 5"
    now = datetime.now(UTC)
    rows = [{"source": "run", "model": "claude-opus-5-5[1m]", "entries": 1, "cost_usd": Decimal("0.30"), "last_at": now},
            {"source": "run", "model": "claude-opus-5-5", "entries": 1, "cost_usd": Decimal("0.10"), "last_at": now},
            {"source": "icp", "model": "claude-haiku-4-5-20251001", "entries": 4, "cost_usd": Decimal("0.10"),
             "last_at": now}]
    steps, models = breakdown(rows)
    assert [(s.name, s.calls, round(s.share)) for s in steps] == [("Research & drafting", 2, 80),
                                                                    ("Understanding the objective", 4, 20)]
    assert [m.name for m in models] == ["Opus 5.5", "Haiku 4.5"] and models[0].per_call == Decimal("0.20")
    assert models[1].detail == "Used for: Understanding the objective"



# --- New run from anywhere; exports only when there is something to export (D-93) -------------------------------
def test_new_run_is_in_the_nav_and_a_busy_system_answers_on_start(client_as, monkeypatch):
    from app.web import routes
    c = client_as(MEMBER)
    assert 'href="/runs/new"' in c.get("/spend").text or 'href="/runs/new"' in c.get("/").text
    monkeypatch.setattr(routes.db, "active_run", lambda: {"id": "00000000-0000-0000-0000-00000000abcd"})
    page = c.get("/runs/new").text
    assert "data-locked" not in page and "run is in progress" not in page  # the form stays usable
    r = c.post("/runs", data={"objective": "Find US B2B SaaS companies with 10 to 100 employees",
                              "idempotency_key": str(uuid.uuid4()), "csrf_token": auth.csrf_token_for(MEMBER.user_id)},
               headers={"HX-Request": "true"})
    assert r.status_code == 409 and "already in progress" in r.text and "/runs/00000000-0000-0000-0000-00000000abcd" in r.text


def test_exports_are_disabled_for_a_run_with_no_leads(client_as, a_run, monkeypatch):
    c = client_as(ADMIN)
    monkeypatch.setattr(db, "lead_counts", lambda run_id: {})
    monkeypatch.setattr(db, "list_leads", lambda run_id: [])
    for tab in ("summary", "leads"):
        html = c.get(f"/runs/{a_run['id']}/tab/{tab}").text
        assert "export.csv" not in html and "Nothing to export" in html, tab



# --- the budget lives in the database; developers change it, admins ask for more (D-94) --------------------------
def test_runs_left_is_what_the_guard_allows():
    from decimal import Decimal

    from app.lib.budget import BudgetExceeded, assert_run_fits, runs_left
    spent, total, cap = Decimal("2.61"), Decimal("6.00"), Decimal("3.00")
    assert runs_left(spent, total, cap) == 1  # 3.39 left, 3.10 per run
    assert_run_fits(spent, cap, total)  # one fits...
    with pytest.raises(BudgetExceeded):
        assert_run_fits(spent + Decimal("3.10"), cap, total)  # ...a second doesn't
    assert runs_left(Decimal("5.99"), total, cap) == 0 and runs_left(spent, total, Decimal("0.30")) == 8


def test_admins_see_runs_left_and_request_more_budget(client_as, monkeypatch):
    from app.web import routes
    raised = []
    monkeypatch.setattr(routes.alerts, "raise_alert", lambda code, detail="", **k: raised.append((code, detail)))
    monkeypatch.setattr(db, "open_budget_request", lambda: None)
    c, token = client_as(ADMIN), auth.csrf_token_for(ADMIN.user_id)
    page = c.get("/spend").text
    assert "run" in page and "left" in page and "Request more budget" in page and "Change the budget" not in page
    r = c.post("/spend/request-budget", data={"note": "5 more runs please", "csrf_token": token},
               headers={"HX-Request": "true"})
    assert "developer has been asked" in r.text and raised == [("budget_requested", "requested by Test Admin: 5 more runs please")]
    monkeypatch.setattr(db, "open_budget_request", lambda: {"last_seen_at": None, "message": "x"})
    r = c.post("/spend/request-budget", data={"csrf_token": token}, headers={"HX-Request": "true"})
    assert "already been requested" in r.text and len(raised) == 1  # one pending request, not one per click
    r = c.post("/spend/budget", data={"new_total": "20", "reason": "topped up", "credit_confirmed": "yes",
                                      "csrf_token": token})
    assert r.status_code == 403  # only developers change the budget


def test_developer_adds_to_the_budget_with_a_reason_and_confirmed_credit(client_as, monkeypatch):
    """A top-up is ADDED to the balance (prepaid, D-95), with a reason and the Anthropic-credit confirmation."""
    from decimal import Decimal
    changes = []
    monkeypatch.setattr(db, "claude_budget", lambda: Decimal("6.00"))
    monkeypatch.setattr(db, "change_budget", lambda new, reason, by: changes.append((new, reason, by)))
    c, token = client_as(DEVELOPER), auth.csrf_token_for(DEVELOPER.user_id)
    def post(**form):
        return c.post("/spend/budget", data={"csrf_token": token, **form}, headers={"HX-Request": "true"})
    good = {"amount": "20", "reason": "Koya topped up $20", "credit_confirmed": "yes"}
    assert "Confirm the Anthropic account" in post(**{**good, "credit_confirmed": ""}).text
    assert "above $0" in post(**{**good, "amount": "0"}).text
    assert "Say where the money came from" in post(**{**good, "reason": "x"}).text
    assert "typo guard" in post(**{**good, "amount": "5000"}).text
    assert "in dollars" in post(**{**good, "amount": "lots"}).text
    assert changes == []
    r = post(**good)
    assert r.status_code == 204 and r.headers["HX-Redirect"] == "/spend?tab=budget"
    assert changes == [(Decimal("26.00"), "Koya topped up $20", DEVELOPER.user_id)]  # 6 + 20, not "set to 20"


def test_the_monthly_statement_carries_unspent_money_forward():
    """Prepaid (D-95): each UTC month opens with what the last one left; the newest closing = budget - spent."""
    from decimal import Decimal as D

    from app.lib.budget import monthly_statement
    rows = monthly_statement(D("6.00"), {"2026-09": D("2.61"), "2026-11": D("1.00")}, {"2026-10": D("20.00")}, "2026-12")
    assert [r["month"] for r in rows] == ["2026-12", "2026-11", "2026-10", "2026-09"]  # newest first, gaps filled
    sep, oct_, nov, dec = rows[::-1]
    assert (sep["opening"], sep["closing"]) == (D("6.00"), D("3.39"))
    assert (oct_["opening"], oct_["added"], oct_["closing"]) == (D("3.39"), D("20.00"), D("23.39"))
    assert (nov["closing"], dec["opening"], dec["closing"]) == (D("22.39"), D("22.39"), D("22.39"))
    assert dec["closing"] == D("6.00") + D("20.00") - D("2.61") - D("1.00")
    assert monthly_statement(D("6"), {}, {}, "2027-01")[0]["month"] == "2027-01"  # year roll-over, empty ledger


def test_months_turn_over_at_midnight_utc():
    from datetime import UTC, datetime

    from app.web import routes
    assert routes._utc_month() == datetime.now(UTC).strftime("%Y-%m")


def test_spend_by_month_tab_renders_for_admins(client_as):
    page = client_as(ADMIN).get("/spend?tab=month").text
    assert "Opening balance" in page and "Carried over" in page and "this month" in page


def test_budget_changes_are_stored_append_only_and_resolve_the_request(admin_dsn):
    """Real DB: the newest change is the budget, the old value is kept, and an open request is resolved."""
    import psycopg

    from app import alerts
    owner = db.fetch_one("select user_id from lead_agent.members where is_owner")
    before = db.claude_budget()
    alerts.raise_alert("budget_requested", "requested by test", notify=False)
    change = None
    try:
        change = db.change_budget(before + 1, "test change, reverted", str(owner["user_id"]))
        assert db.claude_budget() == before + 1 and change["old_usd"] == before
        assert db.open_budget_request() is None
        with psycopg.connect(admin_dsn, prepare_threshold=None) as conn, pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("insert into lead_agent.budget_changes (changed_by, old_usd, new_usd, reason, credit_confirmed) "
                         "values (%s, 1, 2, 'no credit check', false)", (owner["user_id"],))
    finally:
        with psycopg.connect(admin_dsn, prepare_threshold=None, autocommit=True) as conn:
            if change:
                conn.execute("delete from lead_agent.budget_changes where id = %s", (change["id"],))
            conn.execute("delete from lead_agent.system_events where code = 'budget_requested' "
                         "and message like %s", ("%requested by test%",))
    assert db.claude_budget() == before


# --- UI pass (D-95): theme switch everywhere, one Manage menu per person, New run first ------------------------
def test_theme_switch_is_on_every_page_including_sign_in(client_as):
    for who, url in ((None, "/login"), (MEMBER, "/"), (DEVELOPER, "/spend")):
        html = client_as(who).get(url).text
        assert 'id="theme-toggle"' in html and "/static/theme.js" in html, url
    css = client_as(None).get("/static/app.css").text
    assert ':root[data-theme="dark"]' in css and ':root:not([data-theme="light"])' in css  # auto + forced dark


def test_header_puts_new_run_first_and_team_rows_use_one_menu(client_as, monkeypatch):
    html = client_as(ADMIN).get("/spend").text
    nav = html[html.index('<nav class="nav"'):html.index("</nav>")]
    assert nav.index("/runs/new") < nav.index('href="/"')  # "+ New run" is the first item
    row = {"user_id": str(uuid.uuid4()), "email": "x@acme.io", "full_name": "X", "role": "member",
           "is_active": True, "is_owner": False, "is_developer": False}
    monkeypatch.setattr(db, "list_members", lambda: [row])
    team = client_as(ADMIN).get("/team").text
    assert team.count('<details class="menu">') == 1 and 'value="deactivate"' in team and 'value="make_admin"' in team
    assert 'class="invite-row"' in team  # name, email, role and the button on one row



def test_zero_qualified_banner_reads_correctly():
    from app.web.templating import shortfall_sentence
    s = shortfall_sentence({"limits": {"target_qualified": 1}, "error_detail": None,
                            "usage": {"candidates_found": 2, "qualified": 0}}, 0)
    assert s == "Found 0 of 1 qualified leads. None of the companies found met every requirement. See the Summary tab."


def test_cancel_button_survives_the_status_refresh(client_as, monkeypatch):
    """The live panel refreshes every 3 s; hx-preserve keeps the Cancel form as it is (hover, focus, spinner)."""
    from app.web import routes
    run = dict(db.search_runs(limit=1)[0][0], status="researching", created_by=DEVELOPER.user_id)
    monkeypatch.setattr(routes, "_get_run_or_404", lambda run_id: run)
    html = client_as(DEVELOPER).get(f"/runs/{run['id']}/live").text
    assert 'id="cancel-run" hx-preserve' in html



# --- continue a stopped run; a person decides "needs review"; the CSV is the sample pack (D-100) -----------------
def _lead(**kw):
    base = {"company_domain": "a.com", "qualification_status": "pending", "outreach_status": "not_drafted"}
    return {**base, **kw}


def test_continue_plan_says_what_is_left():
    from app.lib.resume import continue_cap, continue_plan
    run = {"status": "paused", "icp": {"x": 1}, "cost_usd": 0.50,
           "usage": {"candidates_found": 5, "discovery_calls": 1},
           "limits": {"target_qualified": 3, "max_candidates": 20, "max_discovery_calls": 3, "max_budget_usd": 1.43}}
    leads = [_lead(company_domain="p1.com"), _lead(company_domain="p2.com"),
             _lead(company_domain="q.com", qualification_status="qualified")]
    plan = continue_plan(run, leads)
    assert plan["label"] == "Continue: research 2 remaining companies" and plan["undrafted"] == ["q.com"]
    only_drafts = [_lead(company_domain=d, qualification_status="qualified") for d in ("q1.com", "q2.com", "q3.com")]
    assert continue_plan(run, only_drafts)["label"] == "Continue: write drafts for 3 leads"
    assert continue_plan({**run, "status": "failed"}, leads)["label"].startswith("Retry: ")
    assert continue_plan({**run, "status": "completed"}, leads) is None  # finished runs aren't continued
    done = {**run, "usage": {"candidates_found": 20, "discovery_calls": 3}}
    assert continue_plan(done, [_lead(qualification_status="not_qualified")]) is None  # nothing left: refine
    assert continue_cap({**run, "cost_usd": 1.40}, plan) > 1.43  # cap used up: a new cap on top of what was spent
    rich = {**run, "limits": {**run["limits"], "max_budget_usd": 5.00}}
    assert continue_cap(rich, plan) == 5.00  # enough of the cap left: keep it


def test_size_band_overlap_settles_unknown_headcount():
    from app.lib.resume import settle_headcount
    checks = [{"filter": "20-80 employees", "result": "unknown", "evidence": "LinkedIn gives a band"},
              {"filter": "Headquartered in the United States", "result": "unknown", "evidence": ""}]
    out = settle_headcount(checks, "20-80", {"start": 11, "end": 50}, "https://www.linkedin.com/company/x/")
    assert out[0]["result"] == "pass" and "11-50 overlaps 20-80" in out[0]["evidence"]
    assert out[1]["result"] == "unknown"  # only the size filter
    assert settle_headcount(checks, "20-80", {"start": 201, "end": 500}, None)[0]["result"] == "unknown"


def test_continue_route_restarts_the_run_and_hides_the_money(client_as, monkeypatch):
    from decimal import Decimal

    from app.web import routes
    run = {"id": uuid.uuid4(), "status": "paused", "icp": {"a": 1}, "objective": "o", "created_by": MEMBER.user_id,
           "cost_usd": 0.2, "usage": {"candidates_found": 2, "discovery_calls": 1},
           "limits": {"target_qualified": 1, "max_candidates": 5, "max_discovery_calls": 2, "max_budget_usd": 0.72}}
    monkeypatch.setattr(routes, "_get_run_or_404", lambda rid: run)
    monkeypatch.setattr(db, "list_leads", lambda rid: [_lead(company_domain="p.com")])
    monkeypatch.setattr(db, "active_run", lambda: None)
    monkeypatch.setattr(db, "fail_orphaned_runs", lambda: 0)
    saved, started = [], []
    monkeypatch.setattr(db, "set_target_qualified", lambda rid, t, cap: saved.append(cap) or {})
    monkeypatch.setattr(db, "update_run", lambda rid, **f: saved.append(f["status"]) or {})
    monkeypatch.setattr(routes.manager, "start", lambda rid, skip_icp=False: started.append(skip_icp))

    def post(who):
        return client_as(who).post(f"/runs/{run['id']}/continue", headers={"HX-Request": "true"},
                                   data={"csrf_token": auth.csrf_token_for(who.user_id)})
    r = post(MEMBER)
    assert r.status_code == 204 and started == [True] and saved[-1] == "researching"  # the real step (D-104)
    monkeypatch.setattr(db, "claude_budget", lambda: Decimal("0"))  # no money left
    r = post(MEMBER)
    assert r.status_code == 402 and "Ask an admin" in r.text and "$" not in r.text  # plain words, no numbers


def test_a_person_decides_a_needs_review_lead(client_as, admin_dsn):
    """Real throwaway run + lead; the decider must be a real member (the decision is linked to them)."""
    import psycopg

    from app.config import DEV_LIMITS
    owner = db.fetch_one("select user_id, full_name from lead_agent.members where is_owner")
    who = Member(user_id=str(owner["user_id"]), email="o@example.invalid", full_name=owner["full_name"],
                 role="member", is_owner=False)
    run, _ = db.create_run(idempotency_key=f"test-{uuid.uuid4()}", objective="decide test objective",
                           objective_hash="h" + uuid.uuid4().hex, limits=DEV_LIMITS.to_dict(), run_kind="dev")
    lead = db.insert_lead_if_new(str(run["id"]), company_name="Decide Test", company_domain="decide-webtest.com",
                                 linkedin_url=None, discovery_data={}, prescreen_result="passed", prescreen_reason="",
                                 qualification_status="needs_review", fetched_urls=[])
    db.add_usage(str(run["id"]), "needs_review", 1)
    try:
        c, token = client_as(who), auth.csrf_token_for(who.user_id)
        assert "Your call" in c.get(f"/leads/{lead['id']}").text
        r = c.post(f"/leads/{lead['id']}/decide", headers={"HX-Request": "true"},
                   data={"decision": "qualified", "note": "They sell SaaS", "csrf_token": token})
        saved = db.get_lead(str(lead["id"]))
        assert r.status_code == 200 and "Write drafts for this lead" in r.text and "Decided by a person" in r.text
        assert saved["qualification_status"] == "qualified" and str(saved["human_decision_by"]) == who.user_id
        usage = db.get_run(str(run["id"]))["usage"]
        assert usage["needs_review"] == 0 and usage["qualified"] == 1
        again = c.post(f"/leads/{lead['id']}/decide", headers={"HX-Request": "true"},
                       data={"decision": "not_qualified", "csrf_token": token})
        assert again.status_code == 400  # decided once
    finally:
        with psycopg.connect(admin_dsn, prepare_threshold=None, autocommit=True) as conn:
            conn.execute("delete from lead_agent.leads where run_id = %s", (run["id"],))
            conn.execute("delete from lead_agent.runs where id = %s", (run["id"],))


def test_sample_pack_csv_has_drafts_only_once_approved(client_as, monkeypatch):
    from app.web import routes
    run = {"id": uuid.uuid4(), "objective": "Find clinics software"}
    steps = [{"step": i, "subject": f"S{i}", "body": f"B{i}"} for i in (1, 2, 3)]
    base = {"qualification_status": "qualified", "company_name": "A", "company_domain": "a.com", "linkedin_url": "L",
            "confidence": 0.8, "hard_filter_checks": [{"filter": "US", "result": "pass", "evidence": "HQ Austin"}],
            "disqualifier_checks": [], "fit_reasons": ["r"], "concerns": [], "source_summary": "s", "source_urls": ["u"],
            "email_sequence": steps, "linkedin_message": "Hi", "outreach_status": "drafted", "discovery_data": {}}
    monkeypatch.setattr(routes, "_get_run_or_404", lambda rid: run)
    monkeypatch.setattr(db, "list_leads", lambda rid: [
        {**base, "review_status": "approved", "reviewed_at": None},
        {**base, "company_domain": "b.com", "review_status": "pending_review", "reviewed_at": None}])
    text = client_as(MEMBER).get(f"/runs/{run['id']}/export.csv").text
    approved, pending = text.splitlines()[1], text.splitlines()[2]
    assert "Find clinics software" in approved and "S1,B1,S2,B2,S3,B3,Hi" in approved and "US: HQ Austin" in approved
    assert "S1" not in pending and pending.count("not approved yet") == 7  # 6 email fields + LinkedIn


def test_clients_open_on_qualified_leads(client_as, a_run, monkeypatch):
    leads = [_lead(company_domain="q.com", qualification_status="qualified", company_name="Q", id=uuid.uuid4(),
                   confidence=0.9, review_status="pending_review"),
             _lead(company_domain="n.com", qualification_status="not_qualified", company_name="N", id=uuid.uuid4(),
                   confidence=0.1, review_status="pending_review")]
    monkeypatch.setattr(db, "list_leads", lambda rid: leads)
    member_view = client_as(MEMBER).get(f"/runs/{a_run['id']}/tab/leads").text
    assert "q.com" in member_view and "n.com" not in member_view and "All companies checked" in member_view
    assert "Pending" not in member_view
    dev_view = client_as(DEVELOPER).get(f"/runs/{a_run['id']}/tab/leads").text
    assert "q.com" in dev_view and "n.com" not in dev_view  # everyone opens on Qualified (D-103)
    dev_all = client_as(DEVELOPER).get(f"/runs/{a_run['id']}/tab/leads?status=all").text
    assert "n.com" in dev_all and "Pending" in dev_all  # developers still get every status chip



# --- your run, from any page (D-101) ------------------------------------------------------------------------------
def test_run_notice_follows_your_run_from_other_pages(client_as, monkeypatch):
    rid = uuid.uuid4()
    run = {"id": rid, "objective": "Find clinic software companies", "status": "researching", "usage": {}}
    monkeypatch.setattr(db, "latest_run_of", lambda uid: run)
    c = client_as(MEMBER)
    r = c.get("/me/run-notice?here=/team")
    assert r.status_code == 200 and "Your search is running" in r.text and f"/runs/{rid}" in r.text  # keeps polling
    assert c.get(f"/me/run-notice?here=/runs/{rid}").text.strip() == ""  # not on the run's own page
    run.update(status="completed", usage={"qualified": 4})
    r = c.get("/me/run-notice?here=/")
    assert r.status_code == 286 and "finished: <b>4 qualified leads</b>" in r.text and "View results" in r.text
    assert f'data-dismiss-key="notice:{rid}"' in r.text  # closing it (or opening the run) keeps it closed
    monkeypatch.setattr(db, "latest_run_of", lambda uid: None)
    assert c.get("/me/run-notice").status_code == 286  # nothing running: stop polling
