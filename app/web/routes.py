"""Pages and actions (specs.md §5.2). Every state-changing route checks: login, role, CSRF, rate limit."""

import csv
import io
import re
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse

from app import alerts, auth, db
from app.auth import Member, current_member, require_admin
from app.config import get_settings, limits_for_run
from app.failures import ServiceFailure, admin_message_from_detail, message_for
from app.lib.budget import BudgetExceeded, assert_can_spend
from app.lib.objective import objective_hash, objective_problem
from app.main import limiter
from app.runs import manager
from app.services import health
from app.web.templating import ACTIVE, stepper, templates

router = APIRouter()
EMAIL_FORM_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
FINISHED = {"completed", "completed_partial", "failed", "cancelled", "superseded", "needs_clarification"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _ctx(request: Request, **extra) -> dict:
    member = getattr(request.state, "member", None)
    csrf = auth.csrf_token_for(member.user_id) if member else ""
    open_issues = alerts.open_issue_count() if member and member.is_admin else 0
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


def _get_run_or_404(run_id: str) -> dict:
    try:
        uuid.UUID(run_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Run not found.")
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
    return templates.TemplateResponse(request, "login.html", _ctx(request, next=next if next.startswith("/") else "/"))


@router.post("/login")
@limiter.limit("5/15minutes")
async def login_submit(request: Request, email: str = Form(...), password: str = Form(...), next: str = Form("/")):
    try:
        tokens = await db.run(auth.password_sign_in, email, password)
    except auth.AuthError as exc:
        return templates.TemplateResponse(request, "login.html", _ctx(request, next=next, error=exc.message,
                                                                      email=email), status_code=exc.status)
    claims = await db.run(auth.verify_access_token, tokens["access_token"])
    member = await db.run(auth.load_member, claims.get("sub", ""))
    if member is None:
        return templates.TemplateResponse(request, "login.html", _ctx(
            request, next=next, email=email,
            error="Your account doesn't have access to this app. Ask an admin to invite you."), status_code=403)
    response = RedirectResponse(next if next.startswith("/") and not next.startswith("//") else "/", status_code=303)
    auth.set_session_cookies(response, tokens)
    return response


@router.post("/logout")
async def logout(request: Request):
    response = RedirectResponse("/login", status_code=303)
    auth.clear_session_cookies(response)
    return response


@router.get("/accept-invite", response_class=HTMLResponse)
async def accept_invite_page(request: Request):
    return templates.TemplateResponse(request, "accept_invite.html", _ctx(request))


@router.post("/accept-invite")
@limiter.limit("10/15minutes")
async def accept_invite_submit(request: Request, access_token: str = Form(...), refresh_token: str = Form(""),
                               full_name: str = Form(...), password: str = Form(...), confirm: str = Form(...)):
    def fail(msg: str, status: int = 400):
        return templates.TemplateResponse(request, "accept_invite.html", _ctx(request, error=msg, full_name=full_name),
                                          status_code=status)

    full_name = full_name.strip()
    if not (1 <= len(full_name) <= 120):
        return fail("Enter your name (up to 120 characters).")
    if len(password) < 10 or password != confirm:
        return fail("Passwords must match and be at least 10 characters.")
    try:
        claims = await db.run(auth.verify_access_token, access_token)
    except Exception:  # noqa: BLE001
        return fail("This invite link is invalid or expired. Ask your admin for a new invite.", 401)
    member = await db.run(db.get_member, claims.get("sub", ""))
    if not member:
        return fail("This invite isn't for this app. Ask your admin to invite you here.", 403)
    try:
        await db.run(auth.set_password, access_token, password, full_name)
    except auth.AuthError as exc:
        return fail(exc.message)
    await db.run(db.update_member, str(member["user_id"]), full_name=full_name)
    response = RedirectResponse("/", status_code=303)
    auth.set_session_cookies(response, {"access_token": access_token, "refresh_token": refresh_token,
                                        "expires_in": 3600})
    return response


# ---------------------------------------------------------------------------
# Home + run creation
# ---------------------------------------------------------------------------
@router.get("/", response_class=HTMLResponse)
async def home(request: Request, member: Member = Depends(current_member)):
    settings = get_settings()
    runs = await db.run(db.list_runs, 30)
    runs = [r for r in runs if r["run_kind"] != "eval_record"]
    active = await db.run(db.active_run)
    spent = await db.run(db.total_spend) if member.is_admin else None
    mine_today = await db.run(db.count_full_runs_today, member.user_id)
    return templates.TemplateResponse(request, "home.html", _ctx(
        request, runs=runs, active=active, spent=spent, budget=settings.claude_budget_total_usd,
        limits=limits_for_run(10, dev=settings.dev_limits), mine_today=mine_today, daily_cap=_daily_cap(member),
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
async def create_run(request: Request, objective: str = Form(""), target_qualified: int = Form(10),
                     idempotency_key: str = Form(...), parent_run_id: str = Form(""), csrf_token: str = Form(""),
                     member: Member = Depends(current_member)):
    _check_csrf(request, member, csrf_token)
    settings = get_settings()
    objective = re.sub(r"\s+", " ", objective).strip()
    problem = objective_problem(objective)  # free: stops junk before any AI call (E-50)
    if problem:
        return _banner(request, "warning", problem, 400)
    if not 1 <= target_qualified <= 10:
        return _banner(request, "error", "Target qualified leads must be between 1 and 10.", 400)

    existing = await db.run(db.fetch_one, f"select id from {db.t('runs')} where idempotency_key = %s",
                            (idempotency_key,))
    if existing:  # double-click / resubmit (E-20): go to the run that already exists
        return Response(status_code=204, headers={"HX-Redirect": f"/runs/{existing['id']}"})
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
    limits = limits_for_run(target_qualified, dev=settings.dev_limits)
    try:
        assert_can_spend(await db.run(db.total_spend), limits.max_budget_usd + 0.10, settings.claude_budget_total_usd)
    except BudgetExceeded as exc:
        failure = ServiceFailure("budget_exhausted", str(exc))
        await db.run(alerts.raise_alert, failure.code, failure.detail)
        return _banner(request, "error", message_for(failure, member.is_admin), 402)
    failures = await db.run(health.preflight, limits.to_dict())  # free checks; cached for 10 minutes
    if failures:
        for f in failures:
            await db.run(alerts.raise_alert, f.code, f.detail)
        return _banner(request, "error", " ".join(message_for(f, member.is_admin) for f in failures)
                       if member.is_admin else failures[0].client, 503)

    parent = None
    if parent_run_id:
        parent = await db.run(_get_run_or_404, parent_run_id)
    run, created = await db.run(
        db.create_run, idempotency_key=idempotency_key, objective=objective,
        objective_hash=objective_hash(objective), limits=limits.to_dict(), run_kind="app",
        created_by=member.user_id, parent_run_id=str(parent["id"]) if parent else None,
    )
    if created:
        manager.start(str(run["id"]))
    return Response(status_code=204, headers={"HX-Redirect": f"/runs/{run['id']}"})


# ---------------------------------------------------------------------------
# Run page
# ---------------------------------------------------------------------------
@router.get("/runs/{run_id}", response_class=HTMLResponse)
async def run_page(request: Request, run_id: str, tab: str = "leads", member: Member = Depends(current_member)):
    run = await db.run(_get_run_or_404, run_id)
    duplicate = await db.run(db.get_run, str(run["duplicate_of_run_id"])) if run.get("duplicate_of_run_id") else None
    return templates.TemplateResponse(request, "run.html", _ctx(
        request, run=run, tab=tab if tab in {"icp", "leads", "calls", "summary"} else "leads",
        steps=stepper(run["status"], _last_active_step(run)), duplicate=duplicate,
        can_control=_can_control(member, run), idempotency_key=str(uuid.uuid4())))


@router.get("/runs/{run_id}/live", response_class=HTMLResponse)
async def run_live(request: Request, run_id: str, member: Member = Depends(current_member)):
    run = await db.run(_get_run_or_404, run_id)
    counts = await db.run(db.lead_counts, run_id)
    duplicate = await db.run(db.get_run, str(run["duplicate_of_run_id"])) if run.get("duplicate_of_run_id") else None
    response = templates.TemplateResponse(request, "partials/run_live.html", _ctx(
        request, run=run, counts=counts, steps=stepper(run["status"], _last_active_step(run)),
        can_control=_can_control(member, run), duplicate=duplicate, now=datetime.now(),
        admin_message=admin_message_from_detail(run.get("error_detail")) if member.is_admin else None))
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
        ctx["filter"] = status if status in {"qualified", "needs_review", "not_qualified", "pending"} else ""
        template = "partials/tab_leads.html"
    elif name == "calls":
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
        return Response(status_code=204, headers={"HX-Redirect": f"/runs/{run_id}"})
    if choice not in {"find_new", "refresh_same"}:
        raise HTTPException(status_code=400, detail="Unknown choice.")
    if manager.active_count():
        return _banner(request, "warning", "Another run is in progress. Try again when it finishes.", 409)
    await db.run(db.update_run, run_id, repeat_choice=choice, cross_run_dedupe=(choice == "find_new"),
                 status="queued", status_detail="Continuing with research")
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
    try:
        uuid.UUID(lead_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Lead not found.")
    lead = await db.run(db.get_lead, lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found.")
    return templates.TemplateResponse(request, "partials/lead_detail.html", _ctx(request, lead=lead))


@router.post("/leads/{lead_id}/review")
@limiter.limit("30/minute")
async def review_lead(request: Request, lead_id: str, review_status: str = Form(...), reviewer_note: str = Form(""),
                      csrf_token: str = Form(""), member: Member = Depends(current_member)):
    _check_csrf(request, member, csrf_token)
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


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------
@router.get("/runs/{run_id}/export.csv")
async def export_csv(request: Request, run_id: str, member: Member = Depends(current_member)):
    run = await db.run(_get_run_or_404, run_id)
    leads = [l for l in await db.run(db.list_leads, run_id) if l["qualification_status"] == "qualified"]
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["company_name", "company_domain", "confidence", "fit_reasons", "concerns", "source_urls",
                     "source_summary", "outreach_status", "review_status", "reviewed_by"])
    for l in leads:
        writer.writerow([l["company_name"], l["company_domain"], l["confidence"], " | ".join(l["fit_reasons"] or []),
                         " | ".join(l["concerns"] or []), " ".join(l["source_urls"] or []), l["source_summary"],
                         l["outreach_status"], l["review_status"], l.get("reviewed_by_name") or ""])
    name = f"qualified-leads-{str(run['id'])[:8]}.csv"
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/runs/{run_id}/export.json")
async def export_json(request: Request, run_id: str, member: Member = Depends(current_member)):
    run = await db.run(_get_run_or_404, run_id)
    leads = [l for l in await db.run(db.list_leads, run_id) if l["qualification_status"] == "qualified"]
    pack = {
        "run_id": str(run["id"]), "objective": run["objective"], "icp": run["icp"], "status": run["status"],
        "exported_at": datetime.now().astimezone().isoformat(),
        "note": "Drafts are included only for leads a human approved. Nothing here has been sent.",
        "leads": [{
            "company_name": l["company_name"], "company_domain": l["company_domain"], "confidence": l["confidence"],
            "qualification": {"hard_filter_checks": l["hard_filter_checks"], "fit_reasons": l["fit_reasons"],
                              "concerns": l["concerns"]},
            "source_context": {"source_urls": l["source_urls"], "source_summary": l["source_summary"]},
            "review_status": l["review_status"], "reviewed_by": l.get("reviewed_by_name"),
            "email_sequence": l["email_sequence"] if l["review_status"] == "approved" else "not approved",
            "linkedin_message": l["linkedin_message"] if l["review_status"] == "approved" else "not approved",
        } for l in leads],
    }
    return JSONResponse(db.to_jsonable(pack), headers={
        "Content-Disposition": f'attachment; filename="sample-pack-{str(run["id"])[:8]}.json"'})


# ---------------------------------------------------------------------------
# Team (admin)
# ---------------------------------------------------------------------------
@router.get("/team", response_class=HTMLResponse)
async def team_page(request: Request, member: Member = Depends(require_admin)):
    members = await db.run(db.list_members)
    admins = sum(1 for m in members if m["role"] == "admin" and m["is_active"])
    return templates.TemplateResponse(request, "team.html", _ctx(request, members=members, active_admins=admins))


@router.post("/team/invite")
@limiter.limit("10/hour")
async def team_invite(request: Request, email: str = Form(...), full_name: str = Form(...), role: str = Form("member"),
                      csrf_token: str = Form(""), member: Member = Depends(require_admin)):
    _check_csrf(request, member, csrf_token)
    email, full_name = email.strip().lower(), full_name.strip()
    if not EMAIL_FORM_RE.match(email):
        return _banner(request, "error", "Enter a valid email address.", 400)
    if not full_name or len(full_name) > 120:
        return _banner(request, "error", "Enter the person's name.", 400)
    if role not in {"admin", "member"}:
        role = "member"
    existing = await db.run(db.get_member_by_email, email)
    if existing and existing["is_active"]:
        return _banner(request, "info", f"{email} is already on the team.")
    try:
        user_id, had_account = await db.run(auth.invite_user, email, full_name)
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
    fields = {"deactivate": {"is_active": False}, "reactivate": {"is_active": True},
              "make_admin": {"role": "admin"}, "make_member": {"role": "member"}}.get(action)
    if fields is None:
        raise HTTPException(status_code=400, detail="Unknown action.")
    try:
        await db.run(db.update_member, user_id, **fields)
    except Exception as exc:  # the DB trigger protects the owner and the last admin (E-41)
        text = str(exc)
        reason = ("The owner can't be demoted or deactivated." if "owner" in text
                  else "At least one active admin must remain." if "admin" in text else "That change isn't allowed.")
        return _banner(request, "warning", reason, 400)
    response = _banner(request, "success", "Team updated.")
    response.headers["HX-Trigger"] = "teamChanged"
    return response


@router.get("/team/table", response_class=HTMLResponse)
async def team_table(request: Request, member: Member = Depends(require_admin)):
    members = await db.run(db.list_members)
    admins = sum(1 for m in members if m["role"] == "admin" and m["is_active"])
    return templates.TemplateResponse(request, "partials/team_table.html", _ctx(request, members=members,
                                                                                active_admins=admins))


# ---------------------------------------------------------------------------
# Spend (admin)
# ---------------------------------------------------------------------------
@router.get("/spend", response_class=HTMLResponse)
async def spend_page(request: Request, member: Member = Depends(require_admin)):
    settings = get_settings()
    return templates.TemplateResponse(request, "spend.html", _ctx(
        request, spent=await db.run(db.total_spend), budget=settings.claude_budget_total_usd,
        by_source=await db.run(db.spend_summary), by_run=await db.run(db.spend_by_run, 50),
        apify=await db.run(db.fetch_all, f"""select r.id, r.objective, sum(t.external_cost_usd) as apify_usd
            from {db.t('tool_calls')} t join {db.t('runs')} r on r.id = t.run_id
            where t.tool_name = 'discover_companies' and t.external_cost_usd is not null
            group by r.id, r.objective order by max(t.created_at) desc limit 50""")))


# ---------------------------------------------------------------------------
# System issues (admin): alerts about credit, keys, failures
# ---------------------------------------------------------------------------
@router.get("/system", response_class=HTMLResponse)
async def system_page(request: Request, member: Member = Depends(require_admin)):
    events = await db.run(alerts.list_events, 100)
    return templates.TemplateResponse(request, "system.html", _ctx(
        request, events=events, webhook=bool(get_settings().alert_webhook_url)))


@router.post("/system/{event_id}/resolve")
@limiter.limit("30/minute")
async def resolve_event(request: Request, event_id: str, csrf_token: str = Form(""),
                        member: Member = Depends(require_admin)):
    _check_csrf(request, member, csrf_token)
    try:
        uuid.UUID(event_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Issue not found.")
    await db.run(alerts.resolve, event_id, member.user_id)
    health.clear_cache()  # re-check services on the next run after a fix
    return Response(status_code=204, headers={"HX-Redirect": "/system"})


@router.post("/system/test-alert")
@limiter.limit("5/hour")
async def test_alert(request: Request, csrf_token: str = Form(""), member: Member = Depends(require_admin)):
    _check_csrf(request, member, csrf_token)
    ok = await db.run(alerts._post_webhook, alerts._payload("test_alert", "Test alert from the Koya Lead Research "
                                                           "Agent. If you got this, alerts work.", None, "info",
                                                           "app", 1))
    return _banner(request, "success" if ok else "error",
                   "Test alert sent to the webhook." if ok else
                   "The webhook didn't accept the test (check ALERT_WEBHOOK_URL and that the n8n workflow is active).")


# ---------------------------------------------------------------------------
# Test fixture pages (public, so Firecrawl can fetch them): E-13, T-inj
# ---------------------------------------------------------------------------
@router.get("/fixtures/{name}", response_class=HTMLResponse)
async def fixture_page(request: Request, name: str):
    if name not in {"injection.html", "parked.html"}:
        raise HTTPException(status_code=404, detail="Not found.")
    return templates.TemplateResponse(request, f"fixtures/{name}", {})
