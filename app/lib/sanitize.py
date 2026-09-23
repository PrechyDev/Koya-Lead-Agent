"""Untrusted content pipeline (specs.md §10.1; edge cases E-11, E-14).

Everything that comes from outside — scraped pages and Apify results — goes
through here before Claude sees it or it is stored:
  1. redact emails, phone numbers and token-like strings;
  2. flag prompt-injection patterns (logged + shown in the UI);
  3. truncate on a sentence boundary;
  4. wrap in <untrusted_website_content> so prompts can say "this is data".
"""

import re
from dataclasses import dataclass, field
from typing import Any

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# Phone-shaped numbers only (separators or a leading +), so years/prices survive.
PHONE_RE = re.compile(
    r"(?<!\w)(?:\+\d{1,3}[\s.\-]?)?(?:\(\d{2,4}\)[\s.\-]?|\d{2,4}[\s.\-])\d{3,4}[\s.\-]\d{3,4}(?!\w)"
)
# Key-shaped strings. The generic branch needs 40+ chars of letters AND digits
# with no hyphens, so long URL slugs ("how-we-scaled-our-...") are left alone.
TOKEN_RE = re.compile(
    r"\b(?:sk-[A-Za-z0-9_\-]{16,}|eyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-.]+"
    r"|(?=[A-Za-z0-9]*\d)(?=[A-Za-z0-9]*[A-Za-z])[A-Za-z0-9]{40,})\b"
)

INJECTION_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("ignore_instructions", re.compile(r"ignore\s+(?:all\s+|any\s+|the\s+)?(?:previous|prior|above|earlier)?\s*instructions", re.I)),
    ("system_prompt", re.compile(r"system\s+prompt", re.I)),
    ("role_override", re.compile(r"\byou\s+are\s+now\b|\bact\s+as\s+(?:an?\s+)?(?:ai|assistant|admin)", re.I)),
    ("send_email", re.compile(r"\b(?:send|write)\s+(?:an?\s+)?e-?mail\s+to\b|\bemail\s+(?:me|us)\s+at\b|\bcontact\s+.{0,30}\s+now\b", re.I)),
    ("secrets", re.compile(r"\b(?:api[\s_-]?key|secret|password|credentials?)\b.{0,40}\b(?:reveal|print|share|export|send|output)", re.I)),
    ("secrets_reverse", re.compile(r"\b(?:reveal|print|share|export|send|output)\b.{0,40}\b(?:api[\s_-]?key|secret|password|credentials?)\b", re.I)),
    ("limit_override", re.compile(r"\b(?:set|change|increase|raise)\s+(?:the\s+)?(?:limit|max(?:imum)?|budget|count)\b", re.I)),
    ("role_tags", re.compile(r"</?\s*(?:system|assistant|instructions?)\s*>", re.I)),
    ("mark_qualified", re.compile(r"\b(?:mark|classify|rate)\s+(?:this|us|the)\s+(?:company|lead)?\s*(?:as\s+)?(?:qualified|a\s+fit|top)", re.I)),
]

DEFAULT_MAX_CHARS = 6000


@dataclass
class Sanitized:
    text: str
    truncated: bool = False
    injection_flags: list[str] = field(default_factory=list)
    redactions: dict[str, int] = field(default_factory=dict)


def redact(text: str) -> tuple[str, dict[str, int]]:
    counts: dict[str, int] = {}

    def _sub(pattern: re.Pattern, label: str, s: str) -> str:
        new, n = pattern.subn(f"[{label} redacted]", s)
        if n:
            counts[label] = counts.get(label, 0) + n
        return new

    text = _sub(EMAIL_RE, "email", text)
    text = _sub(TOKEN_RE, "token", text)
    text = _sub(PHONE_RE, "phone", text)
    return text, counts


def find_injection_flags(text: str) -> list[str]:
    return [name for name, pattern in INJECTION_PATTERNS if pattern.search(text)]


def truncate(text: str, max_chars: int = DEFAULT_MAX_CHARS) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    cut = text[:max_chars]
    # Prefer ending on a sentence or paragraph boundary in the last 20%.
    window_start = int(max_chars * 0.8)
    candidates = [cut.rfind(sep, window_start) for sep in ("\n\n", ". ", ".\n", "! ", "? ")]
    best = max(candidates)
    if best > 0:
        cut = cut[: best + 1]
    return cut.rstrip() + " …", True


def wrap_untrusted(url: str, text: str) -> str:
    safe_url = url.replace('"', "%22")
    # Neutralise any attempt to close our wrapper from inside the page.
    body = re.sub(r"</?\s*untrusted_website_content[^>]*>", "[tag removed]", text, flags=re.I)
    return f'<untrusted_website_content url="{safe_url}">\n{body}\n</untrusted_website_content>'


def sanitize_page(text: str, max_chars: int = DEFAULT_MAX_CHARS) -> Sanitized:
    text = text or ""
    flags = find_injection_flags(text)  # flag on the raw text, before truncation hides anything
    redacted, counts = redact(text)
    short, was_truncated = truncate(redacted, max_chars)
    return Sanitized(text=short, truncated=was_truncated, injection_flags=flags, redactions=counts)


def redact_obj(value: Any) -> Any:
    """Recursively redact strings inside dicts/lists (used on Apify items and stored lead text)."""
    if isinstance(value, str):
        return redact(value)[0]
    if isinstance(value, list):
        return [redact_obj(v) for v in value]
    if isinstance(value, dict):
        return {k: redact_obj(v) for k, v in value.items()}
    return value


def contains_contact_details(text: str) -> bool:
    return bool(EMAIL_RE.search(text or "") or PHONE_RE.search(text or ""))
