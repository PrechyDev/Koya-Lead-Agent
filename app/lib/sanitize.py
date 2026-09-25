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
# Disguised emails: "jane [at] acme [dot] io", "jane(at)acme(dot)io", "jane at acme dot io", "jane AT acme DOT io".
OBFUSCATED_EMAIL_RE = re.compile(
    r"\b[A-Za-z0-9._%+\-]+\s*(?:\[\s*at\s*\]|\(\s*at\s*\)|\{\s*at\s*\}|\s+at\s+)\s*[A-Za-z0-9\-]+"
    r"(?:\s*(?:\[\s*dot\s*\]|\(\s*dot\s*\)|\{\s*dot\s*\}|\s+dot\s+)\s*[A-Za-z0-9\-]+)+\b",
    re.I,
)
# Phone-shaped numbers only (separators or a leading +), so years/prices survive. "+14155550100" (no separators)
# is caught by the second branch; "10 000 000" and "2019-2020-2021" are left alone by looks_like_phone.
PHONE_RE = re.compile(
    r"(?<!\w)(?:(?:\+\d{1,3}[\s.\-]?)?(?:\(\d{2,4}\)[\s.\-]?|\d{2,4}[\s.\-])\d{3,4}[\s.\-]\d{3,4}|\+\d{10,15})(?!\w)"
)
_YEAR_RE = re.compile(r"^(?:19|20)\d\d$")


def looks_like_phone(match: re.Match) -> bool:
    s = match.group(0)
    if s.startswith(("+", "(")):
        return True
    groups = re.split(r"[\s.\-]", s)
    if all(_YEAR_RE.match(g) for g in groups):
        return False  # a list of years
    separators = set(re.findall(r"[\s.\-]", s))
    if len(separators) == 1 and separators <= {" ", "."} and len(groups[0]) <= 3 \
            and all(len(g) == 3 for g in groups[1:]):
        return False  # a big number with thousands separators: "10 000 000", "1.250.000"
    return True


# Key-shaped strings. The generic branch needs 40+ chars of letters AND digits
# with no hyphens, so long URL slugs ("how-we-scaled-our-...") are left alone.
TOKEN_RE = re.compile(
    r"\b(?:sk-[A-Za-z0-9_\-]{16,}|eyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-.]+"
    r"|(?=[A-Za-z0-9]*\d)(?=[A-Za-z0-9]*[A-Za-z])[A-Za-z0-9]{40,})\b"
)

INJECTION_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("ignore_instructions", re.compile(r"\b(?:ignore|disregard|forget)\s+(?:(?:all|any|the|your|these|my|of)\s+)*(?:previous|prior|above|earlier|existing)?\s*(?:instructions|rules|prompts?)\b", re.I)),
    ("system_prompt", re.compile(r"system\s+prompt", re.I)),
    ("role_override", re.compile(r"\byou\s+are\s+now\b|\bact\s+as\s+(?:an?\s+)?(?:ai|assistant|admin)", re.I)),
    ("send_email", re.compile(r"\b(?:send|write)\s+(?:an?\s+)?e-?mail\s+to\b|\bemail\s+(?:me|us)\s+at\b", re.I)),
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
    text = _sub(OBFUSCATED_EMAIL_RE, "email", text)
    text = _sub(TOKEN_RE, "token", text)

    def _phone(m: re.Match) -> str:
        if not looks_like_phone(m):
            return m.group(0)
        counts["phone"] = counts.get("phone", 0) + 1
        return "[phone redacted]"
    text = PHONE_RE.sub(_phone, text)
    return text, counts


# Security advice on a normal website ("never share your password") is not an attack on the agent.
_ADVICE_BEFORE = re.compile(r"\b(?:never|don'?t|do\s+not|won'?t|will\s+not)\s+(?:\w+\s+){0,2}$", re.I)


def find_injection_flags(text: str) -> list[str]:
    flags = []
    for name, pattern in INJECTION_PATTERNS:
        matches = list(pattern.finditer(text))
        if name in ("secrets", "secrets_reverse"):
            matches = [m for m in matches if not _ADVICE_BEFORE.search(text[max(0, m.start() - 30):m.start()])]
        if matches:
            flags.append(name)
    return flags


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


def fence(tag: str, text: str, **attrs: str) -> str:
    """Wrap text that didn't come from us (a page, a user's objective, drafts built from pages) in <tag>…</tag>.

    Any <tag> or </tag> inside the text is removed first, so the text can't close the fence early and put
    words outside it, where the model would read them as part of our prompt.
    """
    body = re.sub(rf"</?\s*{re.escape(tag)}\b[^>]*>", "[tag removed]", text or "", flags=re.I)
    attr_text = "".join(f' {k}="{str(v).replace(chr(34), "%22")}"' for k, v in attrs.items())
    return f"<{tag}{attr_text}>\n{body}\n</{tag}>"


def wrap_untrusted(url: str, text: str) -> str:
    return fence("untrusted_website_content", text, url=url)


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
    """Any email (plain or disguised) or phone number left in stored text (the end-of-run safety check)."""
    text = text or ""
    return bool(EMAIL_RE.search(text) or OBFUSCATED_EMAIL_RE.search(text)
                or any(looks_like_phone(m) for m in PHONE_RE.finditer(text)))
