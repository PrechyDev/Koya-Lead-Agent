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
    assert all(lead["email_sequence"] == "not approved" or lead["review_status"] == "approved" for lead in pack["leads"])


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
    monkeypatch.setattr(routes.health, "preflight", lambda limits: [])
    monkeypatch.setattr(routes.alerts, "raise_alert", lambda *a, **k: None)
    before = db.search_runs()[1]
    r = client_as(ADMIN).post("/runs", data={"objective": "Find US B2B SaaS companies with 10 to 100 staff",
                                             "idempotency_key": str(uuid.uuid4()),
                                             "csrf_token": auth.csrf_token_for(ADMIN.user_id)},
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
    assert "Showing 3–4 of" in c.get("/?page=2").text
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
