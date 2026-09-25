"""Logins with Supabase Auth, server-side (specs.md §10.2; Week 4 pattern adapted for cookies).

* Sign-in: the server exchanges email + password with Supabase Auth
  (grant_type=password, anon key used server-side only) and stores the access
  and refresh tokens in HttpOnly cookies. Page JavaScript never sees a token.
* Every request: the access token (an ES256 JWT) is verified locally against
  Supabase's cached JWKS public keys — no call to Supabase per request. When it
  has expired, the refresh token silently gets a new one.
* Access to THIS app = an active row in lead_agent.members, checked on every
  request (so deactivation takes effect immediately).
* CSRF: every POST carries a signed token bound to the user (form field or
  X-CSRF-Token header), plus SameSite=Lax cookies.
"""

from dataclasses import dataclass
from typing import Any

import httpx
import jwt
from fastapi import HTTPException, Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app import db
from app.config import APP_NAME, APP_TAGLINE, get_settings

ACCESS_COOKIE = "la_access"
REFRESH_COOKIE = "la_refresh"
CSRF_MAX_AGE_S = 12 * 3600
# Supabase stamps each login token with the time it was issued. A server whose clock runs even a second behind
# Supabase's would see a brand-new token as "not yet valid" and refuse the sign-in (found in Docker on Windows,
# whose VM clock drifts). Allowing a small difference is standard practice; expired tokens are still refused.
JWT_CLOCK_SKEW_S = 60
_jwks_client: jwt.PyJWKClient | None = None


@dataclass(frozen=True)
class Member:
    user_id: str
    email: str
    full_name: str
    role: str
    is_owner: bool

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


class AuthError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message, self.status = message, status


def _jwks() -> jwt.PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        _jwks_client = jwt.PyJWKClient(get_settings().jwks_url, cache_keys=True, lifespan=3600)
    return _jwks_client


def verify_access_token(token: str) -> dict:
    settings = get_settings()
    key = _jwks().get_signing_key_from_jwt(token)
    return jwt.decode(token, key.key, algorithms=["ES256", "RS256"], audience=settings.supabase_jwt_audience,
                      leeway=JWT_CLOCK_SKEW_S)


def _auth_headers(service: bool = False) -> dict[str, str]:
    settings = get_settings()
    key = settings.supabase_service_role_key if service else settings.supabase_anon_key
    headers = {"apikey": key, "Content-Type": "application/json"}
    if service:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def _auth_url(path: str) -> str:
    return f"{get_settings().supabase_url.rstrip('/')}/auth/v1/{path.lstrip('/')}"


def _message_from(response: httpx.Response, default: str) -> str:
    try:
        body = response.json()
    except ValueError:
        return default
    return str(body.get("msg") or body.get("error_description") or body.get("message") or default)


def password_sign_in(email: str, password: str) -> dict:
    response = httpx.post(_auth_url("token"), params={"grant_type": "password"}, headers=_auth_headers(),
                          json={"email": email.strip(), "password": password}, timeout=15)
    if response.status_code in (400, 401):
        raise AuthError("Email or password is incorrect.", 401)
    if response.status_code == 429:
        raise AuthError("Too many sign-in attempts. Wait a minute and try again.", 429)
    if response.status_code >= 400:
        raise AuthError("Sign-in is unavailable right now. Try again shortly.", 502)
    return response.json()


CLEAR_SESSION = {"clear": True}  # returned instead of new tokens when the cookies should be deleted


def refresh_session(refresh_token: str) -> dict | None:
    """New tokens; {} when Supabase REJECTS the refresh token (revoked/expired); None on a network problem."""
    try:
        response = httpx.post(_auth_url("token"), params={"grant_type": "refresh_token"}, headers=_auth_headers(),
                              json={"refresh_token": refresh_token}, timeout=15)
    except httpx.HTTPError:
        return None
    if response.status_code == 200:
        return response.json()
    return {} if 400 <= response.status_code < 500 else None


def set_password(access_token: str, password: str, full_name: str | None = None) -> None:
    headers = {**_auth_headers(), "Authorization": f"Bearer {access_token}"}
    body: dict = {"password": password}
    if full_name:
        body["data"] = {"full_name": full_name}
    response = httpx.put(_auth_url("user"), headers=headers, json=body, timeout=15)
    if response.status_code >= 400:
        raise AuthError(_message_from(response, "Couldn't set your password. Ask for a new invite."), 400)


def send_password_reset(email: str) -> None:
    """Ask Supabase to email a password-reset link that opens /reset-password (D-64).

    Callers decide WHO may get one (only active members of this app); this just sends it. The login is shared
    by Koya's tools (D-63), so the new password applies to all of them.
    """
    settings = get_settings()
    response = httpx.post(_auth_url("recover"), headers=_auth_headers(),
                          params={"redirect_to": f"{settings.app_base_url.rstrip('/')}/reset-password"},
                          json={"email": email}, timeout=20)
    if response.status_code == 429:
        raise AuthError("Reset emails are being rate-limited by Supabase. Wait a bit and try again.", 429)
    if response.status_code >= 400:
        raise AuthError(_message_from(response, "Couldn't send the reset email right now."), 502)


def invite_metadata(full_name: str, role: str, invited_by_name: str | None) -> dict:
    """What the shared invite email is personalised with (D-65). `invited_to` also tells Week 4's trigger to skip
    this person (D-26). Cosmetic only: a user can edit their own metadata, so nothing here grants anything."""
    data = {"invited_to": "lead_agent", "full_name": full_name, "app_name": APP_NAME, "app_tagline": APP_TAGLINE,
            "role": role, "invited_by_name": invited_by_name}
    return {k: v for k, v in data.items() if v}


def invite_user(email: str, full_name: str, *, role: str = "member",
                invited_by_name: str | None = None) -> tuple[str | None, bool]:
    """Invite by email. Returns (user_id, already_had_account). Existing accounts get no email (E-39)."""
    settings = get_settings()
    response = httpx.post(
        _auth_url("invite"), headers=_auth_headers(service=True),
        params={"redirect_to": f"{settings.app_base_url.rstrip('/')}/accept-invite"},
        json={"email": email, "data": invite_metadata(full_name, role, invited_by_name)}, timeout=20,
    )
    if response.status_code in (200, 201):
        body = response.json()
        return (body.get("id") or (body.get("user") or {}).get("id")), False
    text = _message_from(response, "").lower()
    if response.status_code in (400, 422) and ("already" in text or "registered" in text or "exists" in text):
        row = db.fetch_one(f"select {db.SCHEMA}.auth_user_id_by_email(%s) as id", (email,))
        return (str(row["id"]) if row and row["id"] else None), True
    if response.status_code == 429:
        raise AuthError("Invite emails are being rate-limited by Supabase. Wait a bit and try again.", 429)
    if response.status_code in (400, 422):
        raise AuthError(_message_from(response, "That email address can't be invited."), 400)
    raise AuthError("Couldn't send the invite right now. Try again shortly.", 502)


# ---------------------------------------------------------------------------
# Request helpers
# ---------------------------------------------------------------------------
def _serializer() -> URLSafeTimedSerializer:
    secret = get_settings().session_secret
    if not secret:
        raise RuntimeError("SESSION_SECRET is not set")
    return URLSafeTimedSerializer(secret, salt="lead-agent-csrf")


def csrf_token_for(user_id: str) -> str:
    return _serializer().dumps(user_id)


def csrf_ok(token: str | None, user_id: str) -> bool:
    if not token:
        return False
    try:
        return _serializer().loads(token, max_age=CSRF_MAX_AGE_S) == user_id
    except (BadSignature, SignatureExpired):
        return False


def load_member(user_id: str) -> Member | None:
    row = db.get_member(user_id)
    if not row or not row["is_active"]:
        return None
    return Member(user_id=str(row["user_id"]), email=row["email"], full_name=row["full_name"], role=row["role"],
                  is_owner=row["is_owner"])


def resolve_session(request: Request) -> tuple[dict | None, dict | None]:
    """Returns (claims, new_tokens). new_tokens is set when the access token was refreshed."""
    access = request.cookies.get(ACCESS_COOKIE)
    refresh = request.cookies.get(REFRESH_COOKIE)
    if access:
        try:
            return verify_access_token(access), None
        except jwt.ExpiredSignatureError:
            pass
        except jwt.PyJWTError:
            access = None
    if refresh:
        tokens = refresh_session(refresh)
        if tokens == {}:  # rejected: forget it, so we don't ask Supabase again on every request
            return None, CLEAR_SESSION
        if tokens and tokens.get("access_token"):
            try:
                return verify_access_token(tokens["access_token"]), tokens
            except jwt.PyJWTError:
                return None, None
    return None, None


def set_session_cookies(response: Any, tokens: dict) -> None:
    settings = get_settings()
    common = {"httponly": True, "secure": settings.secure_cookies, "samesite": "lax", "path": "/"}
    response.set_cookie(ACCESS_COOKIE, tokens["access_token"], max_age=int(tokens.get("expires_in") or 3600), **common)
    if tokens.get("refresh_token"):
        response.set_cookie(REFRESH_COOKIE, tokens["refresh_token"], max_age=30 * 24 * 3600, **common)


def clear_session_cookies(response: Any) -> None:
    for name in (ACCESS_COOKIE, REFRESH_COOKIE):
        response.delete_cookie(name, path="/")


def current_member(request: Request) -> Member:
    member = getattr(request.state, "member", None)
    if member is None:
        raise HTTPException(status_code=401, detail="Please sign in.")
    return member


def require_admin(request: Request) -> Member:
    member = current_member(request)
    if not member.is_admin:
        raise HTTPException(status_code=403, detail="Admins only.")
    return member
