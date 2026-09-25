"""Pages and actions (specs.md §5.2). Every state-changing route checks: login, role, CSRF, rate limit."""

import csv
import io
import re
import uuid
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

import jwt
import psycopg
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse

from app import alerts, auth, db
from app.agent.runner import AGENT_CANNOT_START, agent_can_start
from app.auth import Member, current_member, require_admin, require_developer
from app.config import get_settings, limits_for_run
from app.failures import CATALOGUE, ServiceFailure, admin_message_from_detail, message_for
from app.lib.budget import MAX_BUDGET_USD, BudgetExceeded, assert_run_fits, monthly_statement, runs_left
from app.lib.icp_defaults import MAX_LEAD_COUNT
from app.lib.objective import objective_hash, objective_problem
from app.lib.resume import RESUMABLE, continue_cap, continue_plan
from app.lib.spend_breakdown import breakdown
from app.lib.validation import EMAIL_HINT, MIN_PASSWORD_LENGTH, csv_cell, is_valid_email, safe_next
from app.main import limiter
from app.runs import manager
from app.services import health
from app.web.templating import ACTIVE, stepper, templates

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _ctx(request: Request, **extra) -> dict:
    member = getattr(request.state, "member", None)
    csrf = auth.csrf_token_for(member.user_id) if member else ""
    open_issues = alerts.open_issue_count() if member and member.is_developer else 0  # System issues: developers
    return {"member": member, "csrf": csrf, "settings": get_settings(), "open_issues": open_issues, **extra}


def _check_csrf(request: Request, member: Member, form_token: str | None) -> None:
    token = form_token or request.headers.get("x-csrf-token")
    if not auth.csrf_ok(token, member.user_id):
        raise HTTPException(status_code=403, detail="This form expired. Reload the page and try again.")


def _banner(request: Request, kind: str, message: str, status: int = 200, retarget: bool = True,
            message_html: str | None = None) -> HTMLResponse:
    """Render a System Message banner. `message` is escaped; `message_html` is only for our own fixed markup."""
    headers = {"HX-Retarget": "#system-message", "HX-Reswap": "innerHTML"} if retarget else {}
    return templates.TemplateResponse(request, "partials/banner.html",
                                      {"kind": kind, "message": message, "message_html": message_html},
                                      status_code=status, headers=headers)


def _require_uuid(value: str, what: str) -> None:
    """IDs in URLs are UUIDs; anything else is a 404 (not a database error and a 500)."""
    try:
        uuid.UUID(value)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"{what} not found.") from None


def _get_run_or_404(run_id: str) -> dict:
    _require_uuid(run_id, "Run")
    run = db.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found.")
    return run


def _can_control(member: Member, run: dict) -> bool:
    return member.is_admin or (run.get("created_by") and str(run["created_by"]) == member.user_id)


def _daily_cap(member: Member) -> int:
    s = get_settings()
    return s.max_runs_per_admin_per_day if member.is_admin else s.max_runs_per_user_per_day


def _last_active_step(run: dict) -> int:
    """For a failed/cancelled run: which stepper step to mark as the failing one."""
    usage = run.get("usage") or {}
    if usage.get("qualified"):
        return 3
    if usage.get("scrapes") or usage.get("not_qualified") or usage.get("needs_review"):
        return 2
    if usage.get("discovery_calls"):
        return 1
    return 0


# ---------------------------------------------------------------------------
# Auth pages
# ---------------------------------------------------------------------------
@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/"):
    if getattr(request.state, "member", None):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", _ctx(request, next=safe_next(next)))


@router.post("/login")
@limiter.limit("5/15minutes")
async def login_submit(request: Request, email: str = Form(...), password: str = Form(...), next: str = Form("/")):
    if not is_valid_email(email):  # same rule as the form; saves a Supabase call
        return templates.TemplateResponse(request, "login.html", _ctx(request, next=next, error=EMAIL_HINT,
                                                                      email=email), status_code=400)
    try:
        tokens = await db.run(auth.password_sign_in, email, password)
    except auth.AuthError as exc:
        return templates.TemplateResponse(request, "login.html", _ctx(request, next=next, error=exc.message,
                                                                      email=email), status_code=exc.status)
    try:
        claims = await db.run(auth.verify_access_token, tokens["access_token"])
    except jwt.PyJWTError as exc:  # e.g. this server's clock is behind Supabase's (auth_token_rejected)
        await db.run(alerts.raise_alert, "auth_token_rejected", f"{type(exc).__name__}: {exc}")
        return templates.TemplateResponse(request, "login.html", _ctx(
            request, next=safe_next(next), email=email, error=CATALOGUE["auth_token_rejected"].client), status_code=503)
    member = await db.run(auth.load_member, claims.get("sub", ""))
    if member is None:
        return templates.TemplateResponse(request, "login.html", _ctx(
            request, next=next, email=email,
            error="Your account doesn't have access to this app. Ask an admin to invite you."), status_code=403)
    response = RedirectResponse(safe_next(next), status_code=303)
    auth.set_session_cookies(response, tokens)
    return response


@router.post("/logout")
async def logout(request: Request):
    response = RedirectResponse("/login", status_code=303)
    auth.clear_session_cookies(response)
    return response


# ---------------------------------------------------------------------------
# Password reset (D-64): self-service and admin-triggered; only active members of THIS app get an email, and
# everyone sees the same answer, so the page never reveals who has an account.
# ---------------------------------------------------------------------------
@router.get("/forgot-password", response_class=HTMLResponse)
async def forgot_password_page(request: Request):
    return templates.TemplateResponse(request, "forgot_password.html", _ctx(request))


@router.post("/forgot-password")
@limiter.limit("3/15minutes")
async def forgot_password_submit(request: Request, email: str = Form(...)):
    email = email.strip().lower()
    if not is_valid_email(email):
        return templates.TemplateResponse(request, "forgot_password.html",
                                          _ctx(request, error=EMAIL_HINT, email=email), status_code=400)
    member = await db.run(db.get_member_by_email, email)
    if member and member["is_active"]:
        try:
            await db.run(auth.send_password_reset, email)
        except auth.AuthError as exc:  # tell the admin; the person still sees the neutral answer
            await db.run(alerts.raise_alert, "unexpected", f"password reset email failed: {exc.message}")
    return templates.TemplateResponse(request, "forgot_password.html", _ctx(request, sent=True))


@router.get("/reset-password", response_class=HTMLResponse)
async def reset_password_page(request: Request):
    return templates.TemplateResponse(request, "reset_password.html", _ctx(request))


def _password_problem(password: str, confirm: str) -> str | None:
    """The one password rule, for reset and accept-invite (the forms show the same rule before submit)."""
    if len(password) < MIN_PASSWORD_LENGTH or password != confirm:
        return f"Passwords must match and be at least {MIN_PASSWORD_LENGTH} characters."
    return None


def _signed_in(access_token: str, refresh_token: str) -> RedirectResponse:
    """The session from an emailed link becomes the person's sign-in: cookies set, off to the Runs page."""
    response = RedirectResponse("/", status_code=303)
    auth.set_session_cookies(response, {"access_token": access_token, "refresh_token": refresh_token})
    return response


@router.post("/reset-password")
@limiter.limit("10/15minutes")
async def reset_password_submit(request: Request, access_token: str = Form(...), refresh_token: str = Form(""),
                                password: str = Form(...), confirm: str = Form(...)):
    def fail(msg: str, status: int = 400):
        # Keep the (single-use) reset session on the page so the person can simply try another password;
        # a rejected or expired link (401) is dropped, since it can't work anyway.
        keep = {} if status == 401 else {"access_token": access_token, "refresh_token": refresh_token}
        return templates.TemplateResponse(request, "reset_password.html", _ctx(request, error=msg, **keep),
                                          status_code=status)

    if problem := _password_problem(password, confirm):
        return fail(problem)
    try:
        claims = await db.run(auth.verify_access_token, access_token)
    except Exception:  # noqa: BLE001
        return fail("This reset link is invalid or has expired. Request a new one from the sign-in page.", 401)
    member = await db.run(db.get_member, claims.get("sub", ""))
    if not member or not member["is_active"]:
        # A reset never grants access: only active members of this app can finish one here.
        return fail("This account doesn't have access to this app. Ask an admin if you think it should.", 403)
    try:
        await db.run(auth.set_password, access_token, password)
    except auth.AuthError as exc:
        return fail(exc.message)
    return _signed_in(access_token, refresh_token)


@router.get("/accept-invite", response_class=HTMLResponse)
async def accept_invite_page(request: Request):
    return templates.TemplateResponse(request, "accept_invite.html", _ctx(request))


@router.post("/accept-invite")
@limiter.limit("10/15minutes")
async def accept_invite_submit(request: Request, access_token: str = Form(...), refresh_token: str = Form(""),
                               full_name: str = Form(...), password: str = Form(...), confirm: str = Form(...)):
    def fail(msg: str, status: int = 400):
        keep = {} if status == 401 else {"access_token": access_token, "refresh_token": refresh_token}
        return templates.TemplateResponse(request, "accept_invite.html",
                                          _ctx(request, error=msg, full_name=full_name, **keep), status_code=status)

    full_name = full_name.strip()
    if not (1 <= len(full_name) <= 120):
        return fail("Enter your name (up to 120 characters).")
    if problem := _password_problem(password, confirm):
        return fail(problem)
    try:
        claims = await db.run(auth.verify_access_token, access_token)
    except Exception:  # noqa: BLE001
        return fail("This invite link is invalid or expired. Ask your admin for a new invite.", 401)
    member = await db.run(db.get_member, claims.get("sub", ""))
    if not member:
        return fail("This invite isn't for this app. Ask your admin to invite you here.", 403)
    if not member["is_active"]:
        return fail("Your access to this app has been removed. Ask an admin if you think that's a mistake.", 403)
    try:
        await db.run(auth.set_password, access_token, password, full_name)
    except auth.AuthError as exc:
        return fail(exc.message)
    await db.run(db.update_member, str(member["user_id"]), full_name=full_name)
    return _signed_in(access_token, refresh_token)


# ---------------------------------------------------------------------------
# Home + run creation
# ---------------------------------------------------------------------------
RUNS_PER_PAGE = 20
SPEND_PER_PAGE = 20  # every Spend list (D-92)


def _utc_month() -> str:
    """This month as "YYYY-MM"; months turn over at 00:00 UTC on the 1st (D-95)."""
    return datetime.now(UTC).strftime("%Y-%m")


@router.get("/", response_class=HTMLResponse)
async def runs_history(request: Request, status: str = "", q: str = "", mine: str = "", page: int = 1,
                       objective: str = "", member: Member = Depends(current_member)):
    """The Runs page: history with status filters, 'only mine', objective search and pagination."""
    if objective:  # old "back to the form" links: the form now lives at /runs/new
        return RedirectResponse("/runs/new?" + urlencode({"objective": objective}), status_code=303)
    status = status if status in db.RUN_STATUS_GROUPS else ""
    q, only_mine = q.strip()[:100], mine == "1"
    page = max(1, page)
    runs, total, counts = await db.run(db.search_runs, group=status, created_by=member.user_id if only_mine else None,
                                       q=q, limit=RUNS_PER_PAGE, offset=(page - 1) * RUNS_PER_PAGE)
    pages = max(1, -(-total // RUNS_PER_PAGE))
    if page > pages:  # e.g. a filter shrank the list: go to its last page
        return RedirectResponse(_history_url(status=status, q=q, mine=only_mine, page=pages), status_code=303)

    def filter_url(**change) -> str:
        current = {"status": status, "q": q, "mine": only_mine, "page": page, **change}
        return _history_url(**current)

    return templates.TemplateResponse(request, "history.html", _ctx(
        request, runs=runs, total=total, counts=counts, status=status, q=q, mine=only_mine, page=page, pages=pages,
        first=(page - 1) * RUNS_PER_PAGE + 1 if total else 0, last=min(page * RUNS_PER_PAGE, total),
        filter_url=filter_url, active=await db.run(db.active_run)))


def _history_url(*, status: str = "", q: str = "", mine: bool = False, page: int = 1) -> str:
    params = {k: v for k, v in {"status": status, "q": q, "mine": "1" if mine else "", "page": page if page > 1 else ""}.items() if v}
    return "/" + ("?" + urlencode(params) if params else "")


@router.get("/me/run-notice", response_class=HTMLResponse)
async def run_notice(request: Request, here: str = "", member: Member = Depends(current_member)):
    """Your latest run on every other page (D-101): a quiet line while it runs, a banner once it's done.
    Answers 286 (HTMX: stop polling) when nothing of yours is running."""
    run = await db.run(db.latest_run_of, member.user_id)
    if run and here.rstrip("/") == f"/runs/{run['id']}":
        run = None  # the run's own page already shows it live
    response = templates.TemplateResponse(request, "partials/run_notice.html", _ctx(request, notice_run=run))
    if not run or run["status"] not in ACTIVE:
        response.status_code = 286
    return response


@router.get("/runs/new", response_class=HTMLResponse)  # declared before /runs/{run_id}, or "new" would be an id
async def new_run_page(request: Request, objective: str = "", member: Member = Depends(current_member)):
    settings = get_settings()  # a run already in progress is reported when Start is pressed (409 banner, D-93)
    mine_today = await db.run(db.count_full_runs_today, member.user_id)
    return templates.TemplateResponse(request, "new_run.html", _ctx(
        request, prefill=objective.strip()[:1000],
        limits=limits_for_run(MAX_LEAD_COUNT, dev=settings.dev_limits), mine_today=mine_today, daily_cap=_daily_cap(member),
        idempotency_key=str(uuid.uuid4())))


@router.get("/objective-check", response_class=HTMLResponse)
async def objective_check(request: Request, objective: str = "", member: Member = Depends(current_member)):
    objective = objective.strip()
    if len(objective) < 5:
        return HTMLResponse("")
    match = await db.run(db.find_recent_run_by_hash, objective_hash(objective), get_settings().research_reuse_days)
    return templates.TemplateResponse(request, "partials/objective_hint.html", {"match": match})


@router.post("/runs")
@limiter.limit("10/minute")
async def create_run(request: Request, objective: str = Form(""),
                     idempotency_key: str = Form(...), parent_run_id: str = Form(""), csrf_token: str = Form(""),
                     member: Member = Depends(current_member)):
    _check_csrf(request, member, csrf_token)
    settings = get_settings()
    objective = re.sub(r"\s+", " ", objective).strip()
    problem = objective_problem(objective)  # free: stops junk before any AI call (E-50)
    if problem:
        return _banner(request, "warning", problem, 400)

    existing = await db.run(db.get_run_by_idempotency_key, idempotency_key)
    if existing:  # double-click / resubmit (E-20): go to the run that already exists
        return Response(status_code=204, headers={"HX-Redirect": f"/runs/{existing['id']}"})
    await db.run(db.fail_orphaned_runs)  # a run whose server died must not block new runs forever (D-99)
    active = await db.run(db.active_run)
    if active or manager.active_count():
        link = f"/runs/{active['id']}" if active else "/"  # a UUID path we built: safe markup
        return _banner(request, "warning", "A run is already in progress.", 409,
                       message_html=f'A run is already in progress. <a href="{link}">View it</a>. '
                                    "Only one run can run at a time.")
    if await db.run(db.count_full_runs_today) >= settings.max_runs_per_day:
        return _banner(request, "warning", f"The team's daily limit of {settings.max_runs_per_day} runs is reached. "
                                           "Try again tomorrow.", 429)
    if await db.run(db.count_full_runs_today, member.user_id) >= _daily_cap(member):
        return _banner(request, "warning", f"You've reached your daily limit of {_daily_cap(member)} runs.", 429)
    # The lead target comes from the objective once the ICP step reads it (D-57); until then, the preset maximum.
    limits = limits_for_run(MAX_LEAD_COUNT, dev=settings.dev_limits)
    try:  # can at least a 1-lead run fit? The ICP step sizes the real run to the balance (D-97)
        assert_run_fits(await db.run(db.total_spend), limits_for_run(1, dev=settings.dev_limits).max_budget_usd,
                        await db.run(db.claude_budget))
    except BudgetExceeded as exc:
        failure = ServiceFailure("budget_exhausted", str(exc))
        await db.run(alerts.raise_alert, failure.code, failure.detail)
        return _banner(request, "error", message_for(failure, member.is_developer), 402)
    failures = await db.run(health.preflight, limits.to_dict())  # free checks; cached for 10 minutes
    if not agent_can_start():  # e.g. Windows + uvicorn --reload: the agent could never start
        failures = [ServiceFailure("agent_cannot_start", AGENT_CANNOT_START), *failures]
    if failures:
        for f in failures:
            await db.run(alerts.raise_alert, f.code, f.detail)
        return _banner(request, "error", " ".join(message_for(f, member.is_developer) for f in failures)
                       if member.is_developer else failures[0].client, 503)

    parent = None
    if parent_run_id:
        parent = await db.run(_get_run_or_404, parent_run_id)
        if not _can_control(member, parent):  # the follow-up marks the parent superseded (D-73)
            raise HTTPException(status_code=403, detail="Only the person who started that run, or an admin, can answer it.")
    run, created = await db.run(
        db.create_run, idempotency_key=idempotency_key, objective=objective,
        objective_hash=objective_hash(objective), limits=limits.to_dict(), run_kind="app",
        created_by=member.user_id, parent_run_id=str(parent["id"]) if parent else None,
    )
    if created:
        manager.start(str(run["id"]))
        if parent and parent["status"] == "needs_clarification":
            # The question is answered: the old run stops waiting (and leaves "Waiting for you" in the history).
            await db.run(db.set_status, str(parent["id"]), "superseded", "Answered; continued in a follow-up run")
    return Response(status_code=204, headers={"HX-Redirect": f"/runs/{run['id']}"})


# ---------------------------------------------------------------------------
# Run page
# ---------------------------------------------------------------------------
@router.get("/runs/{run_id}", response_class=HTMLResponse)
async def run_page(request: Request, run_id: str, tab: str = "leads", member: Member = Depends(current_member)):
    run = await db.run(_get_run_or_404, run_id)
    return templates.TemplateResponse(request, "run.html", _ctx(
        request, run=run, tab=tab if tab in _tabs_for(member) else "leads", tabs=_tabs_for(member)))


def _tabs_for(member: Member) -> dict[str, str]:
    """The run tabs a person may open (D-90): Tool calls is the technical view, for developers only."""
    tabs = {"leads": "Leads", "icp": "ICP", "calls": "Tool calls", "summary": "Summary"}
    return tabs if member.is_developer else {k: v for k, v in tabs.items() if k != "calls"}


@router.get("/runs/{run_id}/live", response_class=HTMLResponse)
async def run_live(request: Request, run_id: str, member: Member = Depends(current_member)):
    run = await db.run(_get_run_or_404, run_id)
    duplicate = await db.run(db.get_run, str(run["duplicate_of_run_id"])) if run.get("duplicate_of_run_id") else None
    follow_up = (await db.run(db.child_run, run_id)
                 if run["status"] == "superseded" and run.get("repeat_choice") != "open_previous" else None)
    response = templates.TemplateResponse(request, "partials/run_live.html", _ctx(
        request, run=run, steps=stepper(run["status"], _last_active_step(run), run.get("usage")),
        can_control=_can_control(member, run), duplicate=duplicate, follow_up=follow_up,
        drafted=await db.run(db.drafted_count, run_id) if run["status"] == "completed_partial" else 0,
        tech_message=admin_message_from_detail(run.get("error_detail")) if member.is_developer else None,
        plan=continue_plan(run, await db.run(db.list_leads, run_id)) if run["status"] in RESUMABLE else None,
        refine_url="/runs/new?" + urlencode({"objective": run["objective"]})))
    if run["status"] not in ACTIVE:
        response.status_code = 286  # HTMX: stop polling
    return response


@router.get("/runs/{run_id}/tab/{name}", response_class=HTMLResponse)
async def run_tab(request: Request, run_id: str, name: str, status: str = "", member: Member = Depends(current_member)):
    run = await db.run(_get_run_or_404, run_id)
    ctx = _ctx(request, run=run, tab_name=name, filter="")
    if name == "icp":
        template = "partials/tab_icp.html"
    elif name == "leads":
        ctx["leads"] = await db.run(db.list_leads, run_id)
        wanted = status if status in {"qualified", "needs_review", "not_qualified", "pending", "all"} else None
        if wanted is None:  # first open: clients see the qualified leads, then the possible fits (D-100)
            have = {lead["qualification_status"] for lead in ctx["leads"]}
            wanted = "all" if member.is_developer else next((k for k in ("qualified", "needs_review") if k in have), "all")
        ctx["filter"] = "" if wanted == "all" else wanted
        template = "partials/tab_leads.html"
    elif name == "calls":
        if not member.is_developer:  # checked on the server, not just hidden (D-90)
            raise HTTPException(status_code=403, detail="Developers only.")
        ctx["calls"] = await db.run(db.list_tool_calls, run_id)
        ctx["filter"] = status if status in {"success", "error", "blocked"} else ""
        template = "partials/tab_calls.html"
    elif name == "summary":
        ctx["counts"] = await db.run(db.lead_counts, run_id)
        template = "partials/tab_summary.html"
    else:
        raise HTTPException(status_code=404, detail="Unknown tab.")
    response = templates.TemplateResponse(request, template, ctx)
    if run["status"] not in ACTIVE:
        response.status_code = 286
    return response


@router.post("/runs/{run_id}/confirm")
@limiter.limit("10/minute")
async def confirm_repeat(request: Request, run_id: str, choice: str = Form(...), csrf_token: str = Form(""),
                         member: Member = Depends(current_member)):
    _check_csrf(request, member, csrf_token)
    run = await db.run(_get_run_or_404, run_id)
    if not _can_control(member, run):
        raise HTTPException(status_code=403, detail="Only the person who started this run, or an admin, can choose.")
    if run["status"] != "awaiting_confirmation":
        return _banner(request, "info", "This run isn't waiting for a choice any more.")
    if choice == "open_previous":
        await db.run(db.set_status, run_id, "superseded", "Opened the previous matching run instead",
                     repeat_choice="open_previous")
        return Response(status_code=204, headers={"HX-Redirect": f"/runs/{run['duplicate_of_run_id']}"})
    if choice == "cancel":
        await db.run(db.set_status, run_id, "cancelled", "Cancelled at the repeat check", repeat_choice="cancel")
        # Back to the new-run form, objective filled in, so the person can adjust it and try again.
        return Response(status_code=204, headers={"HX-Redirect": "/runs/new?" + urlencode({"objective": run["objective"]})})
    if choice not in {"find_new", "refresh_same"}:
        raise HTTPException(status_code=400, detail="Unknown choice.")
    if manager.active_count():
        return _banner(request, "warning", "Another run is in progress. Try again when it finishes.", 409)
    await db.run(db.update_run, run_id, repeat_choice=choice, cross_run_dedupe=(choice == "find_new"),
                 status="queued", status_detail="Continuing with research")
    manager.start(run_id, skip_icp=True)
    return Response(status_code=204, headers={"HX-Redirect": f"/runs/{run_id}"})


@router.post("/runs/{run_id}/continue")
@limiter.limit("10/minute")
async def continue_run(request: Request, run_id: str, csrf_token: str = Form(""),
                       member: Member = Depends(current_member)):
    """Carry a stopped run on from where it stopped (D-100). The budget is handled here; the person only sees
    Continue, or "not enough AI budget" with who to ask."""
    _check_csrf(request, member, csrf_token)
    run = await db.run(_get_run_or_404, run_id)
    if not _can_control(member, run):
        raise HTTPException(status_code=403, detail="Only the person who started this run, or an admin, can continue it.")
    plan = continue_plan(run, await db.run(db.list_leads, run_id))
    if not plan:
        return _banner(request, "info", "There's nothing left to continue in this run. Refine the objective to "
                                        "search again.")
    await db.run(db.fail_orphaned_runs)
    if await db.run(db.active_run) or manager.active_count():
        return _banner(request, "warning", "Another run is in progress. Continue this one when it finishes.", 409)
    cap = continue_cap(run, plan)
    try:
        assert_run_fits(await db.run(db.total_spend), cap - float(run["cost_usd"] or 0), await db.run(db.claude_budget))
    except BudgetExceeded:
        who = ("Add to the budget on the Spend page." if member.is_developer else
               "Ask your developer for more budget on the Spend page." if member.is_admin else
               "Ask an admin to request more budget.")
        return _banner(request, "warning", f"There isn't enough AI budget left to continue this run. {who}", 402)
    await db.run(db.set_target_qualified, run_id, int(run["limits"]["target_qualified"]), cap)
    await db.run(db.update_run, run_id, status="queued", status_detail="Continuing from where it stopped",
                 finished_at=None, error_message=None, error_detail=None, shortfall_reason=None, summary=None,
                 quality_scorecard=None)
    manager.start(run_id, skip_icp=True)
    return Response(status_code=204, headers={"HX-Redirect": f"/runs/{run_id}"})


@router.post("/runs/{run_id}/cancel")
@limiter.limit("10/minute")
async def cancel_run(request: Request, run_id: str, csrf_token: str = Form(""), member: Member = Depends(current_member)):
    _check_csrf(request, member, csrf_token)
    run = await db.run(_get_run_or_404, run_id)
    if not _can_control(member, run):
        raise HTTPException(status_code=403, detail="Only the person who started this run, or an admin, can cancel it.")
    if not manager.cancel(run_id):
        if run["status"] in ACTIVE:
            await db.run(db.set_status, run_id, "cancelled", "Cancelled (the run wasn't active in this server)")
        else:
            return _banner(request, "info", "This run has already finished.")
    return _banner(request, "success", "Run cancelled. Work saved so far is kept.")


# ---------------------------------------------------------------------------
# Leads
# ---------------------------------------------------------------------------
@router.get("/leads/{lead_id}", response_class=HTMLResponse)
async def lead_detail(request: Request, lead_id: str, member: Member = Depends(current_member)):
    _require_uuid(lead_id, "Lead")
    lead = await db.run(db.get_lead, lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found.")
    return templates.TemplateResponse(request, "partials/lead_detail.html", _ctx(request, lead=lead))


@router.post("/leads/{lead_id}/review")
@limiter.limit("30/minute")
async def review_lead(request: Request, lead_id: str, review_status: str = Form(...), reviewer_note: str = Form(""),
                      csrf_token: str = Form(""), member: Member = Depends(current_member)):
    _check_csrf(request, member, csrf_token)
    _require_uuid(lead_id, "Lead")
    if review_status not in {"approved", "rejected", "pending_review"}:
        raise HTTPException(status_code=400, detail="Unknown review decision.")
    lead = await db.run(db.get_lead, lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found.")
    if review_status == "approved" and lead["outreach_status"] != "drafted":
        return _banner(request, "warning", "Only leads with accepted drafts can be approved.", 400)
    if review_status == "rejected" and not reviewer_note.strip():
        return _banner(request, "warning", "Add a short note saying why you're rejecting these drafts.", 400)
    await db.run(db.update_lead, lead_id, review_status=review_status, reviewer_note=reviewer_note.strip()[:1000] or None,
                 reviewed_by=member.user_id, reviewed_at=datetime.now().astimezone())
    lead = await db.run(db.get_lead, lead_id)
    response = templates.TemplateResponse(request, "partials/lead_detail.html", _ctx(
        request, lead=lead, flash=f"Saved: {review_status.replace('_', ' ')} by {member.full_name}."))
    response.headers["HX-Trigger"] = "leadReviewed"
    return response


@router.post("/leads/{lead_id}/decide")
@limiter.limit("30/minute")
async def decide_lead(request: Request, lead_id: str, decision: str = Form(...), note: str = Form(""),
                      csrf_token: str = Form(""), member: Member = Depends(current_member)):
    """A person settles a lead the AI couldn't ("needs review", D-100). The AI's evidence stays; the decision,
    who and when are recorded next to it. Qualifying it lets the run write its drafts (Continue)."""
    _check_csrf(request, member, csrf_token)
    _require_uuid(lead_id, "Lead")
    if decision not in {"qualified", "not_qualified"}:
        raise HTTPException(status_code=400, detail="Unknown decision.")
    lead = await db.run(db.get_lead, lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found.")
    if lead["qualification_status"] != "needs_review":
        return _banner(request, "info", "This lead has already been decided.", 400)
    await db.run(db.update_lead, lead_id, qualification_status=decision, human_decision=decision,
                 human_decision_by=member.user_id, human_decision_at=datetime.now().astimezone(),
                 human_decision_note=note.strip()[:1000] or None)
    run_id = str(lead["run_id"])
    await db.run(db.add_usage, run_id, "needs_review", -1)
    await db.run(db.add_usage, run_id, decision, 1)
    lead = await db.run(db.get_lead, lead_id)
    run = await db.run(db.get_run, run_id)
    response = templates.TemplateResponse(request, "partials/lead_detail.html", _ctx(
        request, lead=lead, run_status=run["status"],
        flash=f"Saved: {'qualified' if decision == 'qualified' else 'not a fit'} by {member.full_name}."))
    response.headers["HX-Trigger"] = "leadReviewed"
    return response


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------
@router.get("/runs/{run_id}/export.csv")
async def export_csv(request: Request, run_id: str, member: Member = Depends(current_member)):
    """The outreach sample pack as one CSV (PRD, D-100): per qualified lead, the objective, the company, the
    qualification reasoning with its sources, and the drafts. Drafts leave the app only once a person approved
    them (rule 12); otherwise the row says so."""
    run = await db.run(_get_run_or_404, run_id)
    leads = [lead for lead in await db.run(db.list_leads, run_id) if lead["qualification_status"] == "qualified"]
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["objective", "company_name", "company_domain", "website", "linkedin_url", "fit_score",
                     "qualification_reasoning", "exclusions_checked", "fit_reasons", "concerns", "source_summary",
                     "source_urls", "decided_by", "review_status", "reviewed_by", "reviewed_at", "reviewer_note",
                     "email_1_subject", "email_1_body", "email_2_subject", "email_2_body", "email_3_subject",
                     "email_3_body", "linkedin_message"])
    for lead in leads:
        approved = lead["review_status"] == "approved"
        steps = sorted(lead["email_sequence"] or [], key=lambda st: st.get("step", 0)) if approved else []
        emails = [x for st in steps[:3] for x in (st.get("subject", ""), st.get("body", ""))]
        not_yet = "not approved yet" if lead["outreach_status"] == "drafted" else "no drafts yet"
        emails = (emails + [""] * 6)[:6] if approved else [not_yet] * 6
        reasoning = " | ".join(f"{'✓' if c.get('result') == 'pass' else '✕' if c.get('result') == 'fail' else '?'} "
                               f"{c.get('filter')}: {c.get('evidence', '')}" for c in lead["hard_filter_checks"] or [])
        exclusions = " | ".join(f"{c.get('disqualifier')}: {'applies' if c.get('applies') == 'yes' else 'no'}"
                                for c in lead["disqualifier_checks"] or [])
        writer.writerow([csv_cell(v) for v in (
            run["objective"], lead["company_name"], lead["company_domain"],
            (lead.get("discovery_data") or {}).get("website") or f"https://{lead['company_domain']}",
            lead.get("linkedin_url") or "", lead["confidence"], reasoning, exclusions,
            " | ".join(lead["fit_reasons"] or []), " | ".join(lead["concerns"] or []), lead["source_summary"],
            " ".join(lead["source_urls"] or []), "a person" if lead.get("human_decision") else "the AI (evidence-based)",
            lead["review_status"], lead.get("reviewed_by_name") or "",
            lead["reviewed_at"].strftime("%Y-%m-%d %H:%M UTC") if lead.get("reviewed_at") else "",
            lead.get("reviewer_note") or "", *emails,
            lead["linkedin_message"] if approved else not_yet)])
    name = f"outreach-sample-pack-{str(run['id'])[:8]}.csv"
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ---------------------------------------------------------------------------
# Team (admin)
# ---------------------------------------------------------------------------
@router.get("/team", response_class=HTMLResponse)
async def team_page(request: Request, member: Member = Depends(require_admin)):
    return templates.TemplateResponse(request, "team.html", _ctx(request, **await _team(member)))


async def _team(viewer: Member) -> dict:
    """The team as this person may see it: admins who aren't developers never see developers (D-90)."""
    members = await db.run(db.list_members)
    admins = sum(1 for m in members if m["role"] == "admin" and m["is_active"])  # all of them: the DB rule counts all
    visible = members if viewer.is_developer else [m for m in members if not m.get("is_developer")]
    return {"members": visible, "active_admins": admins}


@router.post("/team/invite")
@limiter.limit("10/hour")
async def team_invite(request: Request, email: str = Form(...), full_name: str = Form(...), role: str = Form("member"),
                      csrf_token: str = Form(""), member: Member = Depends(require_admin)):
    _check_csrf(request, member, csrf_token)
    email, full_name = email.strip().lower(), full_name.strip()
    if not is_valid_email(email):
        return _banner(request, "error", EMAIL_HINT, 400)
    if not full_name or len(full_name) > 120:
        return _banner(request, "error", "Enter the person's name.", 400)
    if role not in {"admin", "member"}:
        role = "member"
    existing = await db.run(db.get_member_by_email, email)
    if existing and existing["is_active"]:
        return _banner(request, "info", f"{email} is already on the team.")
    try:
        user_id, had_account = await db.run(auth.invite_user, email, full_name, role=role,
                                            invited_by_name=member.full_name)
    except auth.AuthError as exc:
        return _banner(request, "error", exc.message, exc.status)
    if not user_id:
        return _banner(request, "error", "Couldn't find that account. Try again, or contact the owner.", 502)
    await db.run(db.add_member, user_id, email, full_name, role, member.user_id)
    msg = (f"{full_name} already has an account in this workspace. They can sign in with their existing password."
           if had_account else f"Invite sent to {email}. They'll set a password from the email link.")
    response = _banner(request, "success", msg)
    response.headers["HX-Trigger"] = "teamChanged"
    return response


@router.post("/team/{user_id}/update")
@limiter.limit("20/minute")
async def team_update(request: Request, user_id: str, action: str = Form(...), csrf_token: str = Form(""),
                      member: Member = Depends(require_admin)):
    _check_csrf(request, member, csrf_token)
    _require_uuid(user_id, "Member")
    if user_id == member.user_id and action in ("deactivate", "make_member"):
        return _banner(request, "warning", "You can't remove your own admin access. Ask another admin to do it.", 400)
    target = await db.run(db.get_member, user_id)
    if not target or (target.get("is_developer") and not member.is_developer):  # hidden from admins (D-90)
        raise HTTPException(status_code=404, detail="Member not found.")
    if action in ("make_developer", "remove_developer"):
        return await _set_developer(request, member, target, action == "make_developer")
    if action == "send_reset":  # admin-triggered password reset (D-64)
        if not target["is_active"]:
            return _banner(request, "warning", "Reactivate them first; deactivated people can't reset a password here.", 400)
        try:
            await db.run(auth.send_password_reset, target["email"])
        except auth.AuthError as exc:
            return _banner(request, "error", exc.message, exc.status)
        return _banner(request, "success", f"Reset link sent to {target['email']}. They choose a new password from the "
                                           "email; it changes their password for every Koya tool.")
    fields = {"deactivate": {"is_active": False}, "reactivate": {"is_active": True},
              "make_admin": {"role": "admin"}, "make_member": {"role": "member"}}.get(action)
    if fields is None:
        raise HTTPException(status_code=400, detail="Unknown action.")
    if action == "make_member" and target.get("is_developer"):
        return _banner(request, "warning", "Remove their developer access first: a developer is always an admin.", 400)
    try:
        updated = await db.run(db.update_member, user_id, **fields)
    except psycopg.errors.CheckViolation as exc:  # the DB trigger protects the owner and the last admin (E-41)
        text = str(exc)
        reason = ("The owner can't be demoted or deactivated." if "owner" in text
                  else "At least one active admin must remain." if "admin" in text else "That change isn't allowed.")
        return _banner(request, "warning", reason, 400)
    if not updated:
        raise HTTPException(status_code=404, detail="Member not found.")
    response = _banner(request, "success", "Team updated.")
    response.headers["HX-Trigger"] = "teamChanged"
    return response


async def _set_developer(request: Request, member: Member, target: dict, on: bool) -> Response:
    """Only the owner grants or removes developer access (D-90); a developer is always an admin."""
    if not member.is_owner:
        raise HTTPException(status_code=403, detail="Only the owner can change developer access.")
    if not on and str(target["user_id"]) == member.user_id:
        return _banner(request, "warning", "The owner keeps developer access.", 400)
    if on and not target["is_active"]:
        return _banner(request, "warning", "Reactivate them first.", 400)
    await db.run(db.update_member, str(target["user_id"]), **({"role": "admin", "is_developer": True} if on
                                                              else {"is_developer": False}))
    response = _banner(request, "success", f"{target.get('full_name') or 'They'} "
                                           f"{'now has' if on else 'no longer has'} developer access.")
    response.headers["HX-Trigger"] = "teamChanged"
    return response


@router.get("/team/table", response_class=HTMLResponse)
async def team_table(request: Request, member: Member = Depends(require_admin)):
    return templates.TemplateResponse(request, "partials/team_table.html", _ctx(request, **await _team(member)))


# ---------------------------------------------------------------------------
# Spend (admin)
# ---------------------------------------------------------------------------
@router.get("/spend", response_class=HTMLResponse)
async def spend_page(request: Request, tab: str = "runs", page: int = 1, member: Member = Depends(require_admin)):
    """Tabs (D-92): By run first, then Apify; "Where the money went" is the technical view (developers, D-90).
    Every list is paginated, SPEND_PER_PAGE rows a page."""
    tabs = {"runs": "By run", "month": "By month", "apify": "Apify"} | (
        {"breakdown": "Where the money went", "budget": "Top-ups"} if member.is_developer else {})
    tab = tab if tab in tabs else "runs"
    page, models = max(1, page), []
    offset = (page - 1) * SPEND_PER_PAGE
    if tab == "runs":
        items, total = await db.run(db.spend_by_run, SPEND_PER_PAGE, offset)
    elif tab == "apify":
        items, total = await db.run(db.apify_spend_by_run, SPEND_PER_PAGE, offset)
    elif tab == "budget":
        items, total = await db.run(db.budget_history, SPEND_PER_PAGE, offset)
    elif tab == "month":
        statement = monthly_statement(await db.run(db.budget_start), *await db.run(db.monthly_money), _utc_month())
        items, total = statement[offset:offset + SPEND_PER_PAGE], len(statement)
    else:
        steps, models = breakdown(await db.run(db.spend_summary))
        items, total = steps[offset:offset + SPEND_PER_PAGE], len(steps)
    pages = max(1, -(-total // SPEND_PER_PAGE))
    if page > pages:
        return RedirectResponse(f"/spend?tab={tab}&page={pages}", status_code=303)
    spent, budget = await db.run(db.total_spend), await db.run(db.claude_budget)
    spent_by_month, _ = await db.run(db.monthly_money)
    size = limits_for_run(MAX_LEAD_COUNT, dev=get_settings().dev_limits)
    run_cap, run_leads = size.max_budget_usd, size.target_qualified
    return templates.TemplateResponse(request, "spend.html", _ctx(
        request, spent=spent, budget=budget, runs_left=runs_left(spent, budget, run_cap), run_cap=run_cap,
        run_leads=run_leads,
        this_month=_utc_month(), spent_this_month=spent_by_month.get(_utc_month(), Decimal(0)),
        request_pending=await db.run(db.open_budget_request), max_budget=MAX_BUDGET_USD, tabs=tabs,
        tab=tab, items=items, models=models, page=page, pages=pages, total=total,
        first=offset + 1 if total else 0, last=min(offset + SPEND_PER_PAGE, total),
        page_url=lambda n: f"/spend?tab={tab}&page={n}"))


@router.post("/spend/budget")
@limiter.limit("10/hour")
async def add_budget(request: Request, amount: str = Form(""), reason: str = Form(""),
                     credit_confirmed: str = Form(""), csrf_token: str = Form(""),
                     member: Member = Depends(require_developer)):
    """Developers top up the prepaid Claude budget in the app (D-94, D-95): the amount is ADDED to what's left;
    nothing expires. No redeploy, no stopped run, every top-up logged."""
    _check_csrf(request, member, csrf_token)
    try:
        add = Decimal(amount.strip().lstrip("$")).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return _banner(request, "error", "Enter the amount to add in dollars, e.g. 20.00.", 400)
    budget = await db.run(db.claude_budget)
    if add <= 0:
        return _banner(request, "error", "Enter an amount above $0.", 400)
    if budget + add > MAX_BUDGET_USD:
        return _banner(request, "error", f"That would take the total over ${MAX_BUDGET_USD:.0f} (a typo guard).", 400)
    if len(reason.strip()) < 5:
        return _banner(request, "error", "Say where the money came from (at least 5 characters).", 400)
    if credit_confirmed != "yes":
        return _banner(request, "error", "Confirm the Anthropic account has this credit first.", 400)
    await db.run(db.change_budget, budget + add, reason.strip()[:300], member.user_id)
    return Response(status_code=204, headers={"HX-Redirect": "/spend?tab=budget"})


@router.post("/spend/request-budget")
@limiter.limit("5/hour")
async def request_budget(request: Request, note: str = Form(""), csrf_token: str = Form(""),
                         member: Member = Depends(require_admin)):
    """Admins ask the developer for more budget: a System issue + the n8n email (D-94)."""
    _check_csrf(request, member, csrf_token)
    if await db.run(db.open_budget_request):
        return _banner(request, "info", "More budget has already been requested; your developer has been told.")
    detail = f"requested by {member.full_name}" + (f": {note.strip()[:300]}" if note.strip() else "")
    await db.run(alerts.raise_alert, "budget_requested", detail)
    response = _banner(request, "success", "Your developer has been asked for more budget. Runs continue until "
                                           "the current budget is used.")
    response.headers["HX-Trigger"] = "budgetRequested"
    return response


# ---------------------------------------------------------------------------
# System issues (developers, D-90): alerts about credit, keys, failures
# ---------------------------------------------------------------------------
@router.get("/system", response_class=HTMLResponse)
async def system_page(request: Request, member: Member = Depends(require_developer)):
    events = await db.run(alerts.list_events, 100)
    return templates.TemplateResponse(request, "system.html", _ctx(
        request, events=events, webhook=bool(get_settings().alert_webhook_url)))


@router.post("/system/{event_id}/resolve")
@limiter.limit("30/minute")
async def resolve_event(request: Request, event_id: str, csrf_token: str = Form(""),
                        member: Member = Depends(require_developer)):
    _check_csrf(request, member, csrf_token)
    _require_uuid(event_id, "Issue")
    await db.run(alerts.resolve, event_id, member.user_id)
    health.clear_cache()  # re-check services on the next run after a fix
    return Response(status_code=204, headers={"HX-Redirect": "/system"})


@router.post("/system/test-alert")
@limiter.limit("5/hour")
async def test_alert(request: Request, csrf_token: str = Form(""), member: Member = Depends(require_developer)):
    _check_csrf(request, member, csrf_token)
    ok = await db.run(alerts.send_test_alert)
    return _banner(request, "success" if ok else "error",
                   "Test alert sent to the webhook." if ok else
                   "The webhook didn't accept the test (check ALERT_WEBHOOK_URL and that the n8n workflow is active).")


# ---------------------------------------------------------------------------
# Test fixture pages (public, so Firecrawl can fetch them): E-13, T-inj.
# Served only while FIXTURE_MODE=true, so a normal deployment has no public test pages.
# ---------------------------------------------------------------------------
@router.get("/fixtures/{name}", response_class=HTMLResponse)
async def fixture_page(request: Request, name: str):
    if not get_settings().fixture_mode or name not in {"injection.html", "parked.html"}:
        raise HTTPException(status_code=404, detail="Not found.")
    return templates.TemplateResponse(request, f"fixtures/{name}", {})
