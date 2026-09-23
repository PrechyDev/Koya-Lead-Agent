"""Input rules shared by the browser and the server, so both accept exactly the same values.

EMAIL_PATTERN is used as the HTML `pattern` attribute on email fields (the browser anchors it) and, anchored,
by the login and invite routes. It needs `name@domain.tld`: at least one dot after the @, and a final part of
2+ letters. So `sam@acme.io` and `sam@mail.acme.co.uk` pass; `sam@acme`, `sam@acme.` and `sam@acme.c` don't.
(The browser's own type="email" check alone accepts `sam@acme`.)

Hyphens inside character classes are escaped because modern browsers compile `pattern` with the `v` regex flag.
"""

import re
from urllib.parse import urlsplit

EMAIL_PATTERN = r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}"
EMAIL_HINT = "Enter a valid email address, like name@company.com."
# Owner's choice (2026-09-23): 8, the common minimum for user-chosen passwords. Supabase Auth's own project
# setting must not be higher, or it will refuse passwords this page accepts.
MIN_PASSWORD_LENGTH = 8
_EMAIL_RE = re.compile(rf"^{EMAIL_PATTERN}$")


def is_valid_email(value: str | None) -> bool:
    return bool(_EMAIL_RE.match((value or "").strip()))


def safe_next(value: str | None, default: str = "/") -> str:
    """Where to go after signing in: only a path on THIS site. Blocks "//evil.com" and "/\\evil.com" (browsers
    treat a backslash like a slash), full URLs and control characters, so the login page can't be used to send
    someone to a look-alike site (an open redirect)."""
    v = (value or "").strip()
    if not v.startswith("/") or v.startswith("//") or "\\" in v or any(ord(ch) < 32 for ch in v):
        return default
    parts = urlsplit(v)
    return v if not parts.scheme and not parts.netloc else default


def safe_url(value: str | None) -> str:
    """Only http(s) links from data (scraped sources, AI-written evidence refs) become clickable; anything else
    (e.g. "javascript:...") becomes "#", so stored text can never run script in a reviewer's browser."""
    v = (value or "").strip()
    return v if urlsplit(v).scheme in ("http", "https") and urlsplit(v).netloc else "#"


_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def csv_cell(value) -> str:
    """Neutralise spreadsheet formulas: a cell that starts with = + - @ is run as a formula by Excel/Sheets
    (CSV injection). Company names and summaries come from websites, so they're prefixed with an apostrophe."""
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(_FORMULA_START) else text
