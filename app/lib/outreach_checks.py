"""Deterministic checks on outreach drafts (specs.md §8.2 save_outreach; E-24).

These run before the (paid) grounding check, so obviously bad drafts are
bounced for free. Returns a list of human-readable problems; empty = pass.
"""

import re

from app.lib.domain import same_url
from app.lib.sanitize import EMAIL_RE, PHONE_RE

MAX_SUBJECT_CHARS = 60
MAX_BODY_WORDS = 120
MAX_FINAL_EMAIL_WORDS = 80   # the guide: "keep the final follow-up brief"
MAX_LINKEDIN_CHARS = 300
ALLOWED_PLACEHOLDERS = {"first_name", "sender_name"}
URL_RE = re.compile(r"https?://|www\.", re.I)
PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z_]+)\s*\}\}")

# From the outbound-copywriting guide: weak personalization, fake urgency, hype.
BANNED_PHRASES = [
    "loved what you are building", "love what you're building", "love what you are building",
    "your company looks impressive", "i saw your website", "i came across your website",
    "came across your company", "hope this finds you well", "act now", "limited time",
    "last chance", "don't miss out", "dont miss out", "guaranteed", "guarantee",
    "revolutionary", "game-changer", "game changer", "10x", "skyrocket", "urgent",
    "only a few spots", "exclusive offer",
]


def _words(text: str) -> int:
    return len(re.findall(r"\S+", text or ""))


def _contact_or_url_problems(label: str, text: str) -> list[str]:
    problems = []
    if EMAIL_RE.search(text or ""):
        problems.append(f"{label} contains an email address; remove it (no emails anywhere)")
    if PHONE_RE.search(text or ""):
        problems.append(f"{label} contains a phone number; remove it")
    if URL_RE.search(text or ""):
        problems.append(f"{label} contains a URL; keep links out of the copy (evidence goes in evidence_ref)")
    return problems


def _placeholder_problems(label: str, text: str) -> list[str]:
    unknown = {p for p in PLACEHOLDER_RE.findall(text or "")} - ALLOWED_PLACEHOLDERS
    return [f"{label} uses unknown placeholder {{{{{p}}}}}; only {{{{first_name}}}} and {{{{sender_name}}}} are allowed"
            for p in sorted(unknown)]


def _banned(label: str, text: str) -> list[str]:
    lower = (text or "").lower()
    return [f'{label} uses a banned phrase: "{p}"' for p in BANNED_PHRASES
            if re.search(rf"(?<!\w){re.escape(p)}(?!\w)", lower)]


# Shown to the copywriter up front (get_lead) so drafts are written to the rules, not fixed afterwards.
WRITING_RULES = {
    "emails": "exactly 3, steps 1, 2, 3, each with a different subject",
    "subject_max_chars": MAX_SUBJECT_CHARS,
    "body_max_words": MAX_BODY_WORDS,
    "email_3_max_words": MAX_FINAL_EMAIL_WORDS,
    "linkedin_max_chars": MAX_LINKEDIN_CHARS,
    "linkedin_target": "about 40 words; aim for 250 characters to leave room",
    "email_1": "opens with a company observation, links it to the offer, ends with a low-pressure question (a '?')",
    "email_2": "a different angle (a workflow bottleneck, scaling challenge or operational pattern)",
    "email_3": "brief; invites a reply if the timing or fit is wrong",
    "placeholders": "greet with {{first_name}} in email 1; sign every email with {{sender_name}}; no others",
    "never": "email addresses, phone numbers, URLs, banned phrases, invented facts",
    "every_email": "at least one company-specific detail from get_lead, with its source URL in evidence_ref",
}


def measure(steps: list[dict], linkedin_message: str) -> dict:
    """Exact counts for each part, so the writer doesn't have to estimate them."""
    return {
        "emails": [{"step": s.get("step"), "subject_chars": len((s.get("subject") or "").strip()),
                    "body_words": _words(s.get("body") or "")} for s in steps],
        "linkedin_chars": len((linkedin_message or "").strip()),
    }


def _template_problems(steps: list[dict]) -> list[str]:
    """The guide's suggested sequence structure, where code can check it."""
    problems = []
    if [s.get("step") for s in steps] != [1, 2, 3][: len(steps)]:
        problems.append("emails must be numbered step 1, 2, 3 in order")
    subjects = [(s.get("subject") or "").strip().lower() for s in steps if (s.get("subject") or "").strip()]
    if len(subjects) != len(set(subjects)):
        problems.append("each email needs its own subject line (two are the same)")
    if steps and "?" not in (steps[0].get("body") or ""):
        problems.append("email 1 must end with a low-pressure question (no '?' found)")
    if len(steps) >= 3 and _words(steps[2].get("body") or "") > MAX_FINAL_EMAIL_WORDS:
        problems.append(f"email 3 is {_words(steps[2].get('body') or '')} words; keep the final follow-up brief "
                        f"(max {MAX_FINAL_EMAIL_WORDS})")
    for i, s in enumerate(steps, start=1):
        if "{{sender_name}}" not in (s.get("body") or "").replace(" ", ""):
            problems.append(f"email {i} must be signed with the {{{{sender_name}}}} placeholder")
    return problems


def check_outreach(steps: list[dict], linkedin_message: str, allowed_sources: list[str]) -> list[str]:
    problems: list[str] = []
    if len(steps) != 3:
        problems.append(f"the sequence must have exactly 3 emails (got {len(steps)})")
    problems += _template_problems(steps)

    for i, step in enumerate(steps, start=1):
        label = f"email {i}"
        subject = (step.get("subject") or "").strip()
        body = (step.get("body") or "").strip()
        note = (step.get("personalization_note") or "").strip()
        evidence = (step.get("evidence_ref") or "").strip()

        if not subject:
            problems.append(f"{label} has no subject")
        elif len(subject) > MAX_SUBJECT_CHARS:
            problems.append(f"{label} subject is {len(subject)} chars (max {MAX_SUBJECT_CHARS})")
        if not body:
            problems.append(f"{label} has no body")
        elif _words(body) > MAX_BODY_WORDS:
            problems.append(f"{label} body is {_words(body)} words (max {MAX_BODY_WORDS})")
        if not note:
            problems.append(f"{label} has no personalization_note")
        if not evidence:
            problems.append(f"{label} has no evidence_ref (the source URL its observation came from)")
        elif not any(same_url(evidence, s) for s in allowed_sources):
            problems.append(f"{label} evidence_ref is not one of this lead's source URLs")

        for part_label, text in ((f"{label} subject", subject), (f"{label} body", body)):
            problems += _contact_or_url_problems(part_label, text)
            problems += _placeholder_problems(part_label, text)
            problems += _banned(part_label, text)

    if steps and "{{first_name}}" not in (steps[0].get("body") or "").replace(" ", ""):
        problems.append("email 1 must greet the recipient with the {{first_name}} placeholder (no guessed names)")

    li = (linkedin_message or "").strip()
    if not li:
        problems.append("the LinkedIn message is missing")
    elif len(li) > MAX_LINKEDIN_CHARS:
        problems.append(f"the LinkedIn message is {len(li)} chars (max {MAX_LINKEDIN_CHARS})")
    problems += _contact_or_url_problems("LinkedIn message", li)
    problems += _placeholder_problems("LinkedIn message", li)
    problems += _banned("LinkedIn message", li)
    return problems
