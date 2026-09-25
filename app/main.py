"""FastAPI application: pages (Jinja2 + HTMX), auth middleware, rate limits (specs.md §5.2, §10.2)."""

import logging
import uuid as _uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

import psycopg
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from starlette.middleware.base import BaseHTTPMiddleware

from app import alerts, auth, db
from app.config import get_settings
from app.logging_setup import configure_logging
from app.runs import manager, recover_orphans
from app.web.templating import templates

configure_logging()  # one format + secret/email redaction for every log line (app/logging_setup.py)
log = logging.getLogger("lead_agent")
ROOT = Path(__file__).resolve().parent
# Only our own scripts/styles/requests; inline is allowed for the few small inline scripts and style attributes.
CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; "
       "frame-ancestors 'none'")
PUBLIC_PATHS = ("/login", "/accept-invite", "/forgot-password", "/reset-password", "/health", "/static/",
                "/fixtures/", "/favicon.ico")


def client_ip(request: Request) -> str:
    """The visitor's address for rate limits. A visitor can send any X-Forwarded-For they like; Render's proxy
    APPENDS the real address, so the last entry is the one to trust (taking the first would let anyone dodge the
    login rate limit by sending a fake header)."""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


def rate_key(request: Request) -> str:
    member = getattr(request.state, "member", None)
    return f"user:{member.user_id}" if member else f"ip:{client_ip(request)}"


limiter = Limiter(key_func=rate_key, default_limits=["120/minute"], headers_enabled=False)


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        n = recover_orphans()
        if n:
            log.warning("marked %s interrupted run(s) as failed", n)
    except Exception:  # noqa: BLE001 — the app must still boot to show a clear error
        log.exception("orphan recovery failed")
    yield
    await manager.shutdown()
    db.close_pool()


app = FastAPI(title="Koya Lead Research Agent", lifespan=lifespan, docs_url=None, redoc_url=None)
app.state.limiter = limiter
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


def _is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


class AuthMiddleware(BaseHTTPMiddleware):
    """Resolve the session on every request; refresh tokens silently; enforce login on non-public paths."""

    async def dispatch(self, request: Request, call_next):
        request.state.member = None
        path = request.url.path
        new_tokens = None
        if not path.startswith(("/static/", "/fixtures/", "/health")):
            claims, new_tokens = await db.run(auth.resolve_session, request)
            if claims:
                request.state.member = await db.run(auth.load_member, claims.get("sub", ""))
                request.state.claims = claims
            if claims and request.state.member is None and not path.startswith(PUBLIC_PATHS) and path != "/logout":
                response = templates.TemplateResponse(request, "error.html", {
                    "title": "No access", "message": "Your account doesn't have access to this app, or it was "
                    "deactivated. Ask an admin to invite you.", "no_access": True}, status_code=403)
                return _finish(response, new_tokens)
        if request.state.member is None and not path.startswith(PUBLIC_PATHS) and path != "/logout":
            # Keep the query string (a filtered Runs view, a prefilled New run) and encode it as one parameter.
            wanted = request.url.path + (f"?{request.url.query}" if request.url.query else "")
            target = f"/login?next={quote(wanted, safe='/')}"
            if _is_htmx(request):
                return _finish(HTMLResponse("", status_code=401, headers={"HX-Redirect": target}), new_tokens)
            return _finish(RedirectResponse(target, status_code=303), new_tokens)
        return _finish(await call_next(request), new_tokens)


def _finish(response, new_tokens: dict | None):
    """Every response, early returns included, gets the session update and the security headers."""
    if new_tokens is auth.CLEAR_SESSION:
        # Supabase rejected the old refresh token: stop retrying it. Not when this very response signs the person
        # in (login / reset / accept invite): clearing after it would delete the new session (errors log #100).
        signing_in = any(h.startswith(f"{auth.ACCESS_COOKIE}=") for h in response.headers.getlist("set-cookie"))
        if not signing_in:
            auth.clear_session_cookies(response)
    elif new_tokens:
        auth.set_session_cookies(response, new_tokens)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Content-Security-Policy", CSP)
    if get_settings().secure_cookies:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


app.add_middleware(AuthMiddleware)


@app.exception_handler(RateLimitExceeded)
async def rate_limited(request: Request, exc: RateLimitExceeded):
    return _friendly_error(request, "Slow down", "Too many requests. Wait a minute and try again.", 429, "warning")


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    if exc.status_code == 401:
        return RedirectResponse("/login", status_code=303)
    message = exc.detail if isinstance(exc.detail, str) else "Something went wrong."
    return _friendly_error(request, "Can't do that", message, exc.status_code)


@app.exception_handler(psycopg.OperationalError)
async def database_down(request: Request, exc: psycopg.OperationalError):
    await db.run(alerts.raise_alert, "database_unavailable", str(exc)[:300])
    return _friendly_error(request, "Database unavailable",
                           "The app can't reach its database right now. Please try again in a few minutes.", 503)


@app.exception_handler(Exception)
async def unexpected_error(request: Request, exc: Exception):
    if isinstance(exc, psycopg.OperationalError):  # raised in middleware, outside the specific handler
        return await database_down(request, exc)
    ref = _uuid.uuid4().hex[:8]
    log.exception("unexpected error ref=%s path=%s", ref, request.url.path)
    await db.run(alerts.raise_alert, "unexpected", f"ref {ref} on {request.url.path}: {type(exc).__name__}: {exc}"[:500])
    return _friendly_error(request, "Something went wrong",
                           f"Something went wrong on our side. Your admin has been told (reference {ref}).", 500)


def _friendly_error(request: Request, title: str, message: str, status: int, kind: str = "error"):
    """One way to show an error: a banner for HTMX requests, JSON for .json routes, else the error page."""
    if _is_htmx(request):
        return templates.TemplateResponse(request, "partials/banner.html", {"kind": kind, "message": message},
                                          status_code=status, headers={"HX-Retarget": "#system-message",
                                                                       "HX-Reswap": "innerHTML"})
    if request.url.path.endswith(".json"):
        return JSONResponse({"error": {"code": status, "message": message}}, status_code=status)
    return templates.TemplateResponse(request, "error.html", {"title": title, "message": message}, status_code=status)


@app.get("/health")
async def health():
    settings = get_settings()
    try:
        await db.run(db.fetch_one, "select 1 as ok")
        database = "ok"
    except Exception:  # noqa: BLE001
        database = "unavailable"
    missing = settings.missing_run_config()
    status = 200 if database == "ok" else 503
    return JSONResponse({"status": "ok" if status == 200 else "degraded", "database": database,
                         "runs_configured": not missing, "active_runs": manager.active_count()}, status_code=status)


from app.web import routes  # noqa: E402  (routes import `app`, `limiter`)

app.include_router(routes.router)
