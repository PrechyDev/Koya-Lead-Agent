"""Jinja2 setup and small display helpers shared by all templates."""

from datetime import UTC, datetime
from pathlib import Path

from fastapi.templating import Jinja2Templates

from app.config import get_settings
from app.db import ACTIVE_STATUSES
from app.lib.icp_defaults import COMPETITOR_EXCLUSION, COMPETITOR_WHY, DEFAULT_LEAD_COUNT, DEFAULTS
from app.lib.validation import EMAIL_PATTERN, MIN_PASSWORD_LENGTH, safe_url

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=TEMPLATES_DIR)

STATUS_BADGES = {
    "qualified": ("success", "✓ Qualified"),
    "needs_review": ("warning", "⚠ Needs review"),
    "not_qualified": ("neutral", "✕ Not qualified"),
    "pending": ("outline", "… Pending"),
    "success": ("success", "✓ Success"),
    "error": ("danger", "✕ Error"),
    "blocked": ("warning", "⛔ Blocked"),
    "running": ("info", "… Running"),
    "drafted": ("success", "✓ Drafted"),
    "failed_grounding": ("danger", "✕ Failed checks"),
    "not_drafted": ("outline", "Not drafted"),
    "pending_review": ("outline", "Pending review"),
    "approved": ("success", "✓ Approved"),
    "rejected": ("danger", "✕ Rejected"),
    "queued": ("info", "Queued"), "refining_icp": ("info", "Refining ICP"),
    "needs_clarification": ("warning", "Needs clarification"), "awaiting_confirmation": ("warning", "Waiting for you"),
    "discovering": ("info", "Discovering"), "researching": ("info", "Researching"), "drafting": ("info", "Drafting"),
    "finalizing": ("info", "Finalizing"), "completed": ("success", "✓ Completed"),
    "completed_partial": ("warning", "Partial"), "failed": ("danger", "✕ Failed"), "cancelled": ("neutral", "Cancelled"),
    "superseded": ("neutral", "Replaced"),
}

STEPS = [("refine", "Refine ICP"), ("discover", "Discover"), ("research", "Research"), ("draft", "Draft"),
         ("done", "Done")]
STEP_OF_STATUS = {
    "queued": 0, "refining_icp": 0, "needs_clarification": 0, "awaiting_confirmation": 0,
    "discovering": 1, "researching": 2, "drafting": 3, "finalizing": 4,
    "completed": 5, "completed_partial": 5, "failed": -1, "cancelled": -1, "superseded": 5,
}
ACTIVE = set(ACTIVE_STATUSES)  # the one list lives in db.py


def badge(status: str | None) -> tuple[str, str]:
    return STATUS_BADGES.get(status or "", ("neutral", (status or "—").replace("_", " ")))


def ago(value: datetime | None) -> str:
    if not value:
        return "—"
    delta = datetime.now(UTC) - value
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    if seconds < 86400:
        return f"{seconds // 3600} h ago"
    return value.strftime("%b %d, %Y")


def shortfall_sentence(run: dict, drafted: int) -> str:
    """Why a run is partial, in the client's terms (D-91): what they're missing (leads or drafts) and why. A run
    stopped by a limit says so (its stored reason); otherwise the companies didn't meet the requirements."""
    usage, limits = run.get("usage") or {}, run.get("limits") or {}
    found, qualified = int(usage.get("candidates_found", 0)), int(usage.get("qualified", 0))
    target = int(limits.get("target_qualified", 0) or 0)
    stopped = run.get("shortfall_reason") if run.get("error_detail") else None  # the system's own plain reason
    missing = max(qualified - drafted, 0)
    leads = f"{qualified} qualified lead{'s' if qualified != 1 else ''}"
    if qualified >= target:
        if missing:
            return (f"Found {leads}, but {missing} {'has' if missing == 1 else 'have'} no email drafts yet. "
                    + (stopped or "The run stopped before writing them."))
        return f"Found {leads} with drafts. Some list-quality checks weren't met: see the Summary tab."
    if stopped:
        return f"Found {qualified} of {target} qualified leads. {stopped}"
    if found == 0:
        return "No qualified leads: the company search found no matching companies. Try a broader objective."
    rest = ("None of the companies found met every requirement" if qualified == 0
            else "The other companies found didn't meet every requirement")
    return f"Found {qualified} of {target} qualified leads. {rest}. See the Summary tab."


def money(value) -> str:
    try:
        return f"${float(value or 0):.2f}"
    except (TypeError, ValueError):
        return "$0.00"


def _step_happened(i: int, usage: dict) -> bool:
    """For a finished run: did step i actually do anything? (Otherwise it shows as skipped, not done.)"""
    researched = (int(usage.get("qualified", 0)) + int(usage.get("needs_review", 0))
                  + int(usage.get("not_qualified", 0)) - int(usage.get("prescreen_rejected", 0)))
    return {
        1: int(usage.get("discovery_calls", 0)) + int(usage.get("candidates_found", 0)) > 0,
        2: int(usage.get("scrapes", 0)) + int(usage.get("cache_hits", 0)) + researched > 0,
        3: int(usage.get("qualified", 0)) > 0,
    }.get(i, True)


def stepper(status: str, last_active_step: int = 0, usage: dict | None = None) -> list[dict]:
    """[{label, state: done|current|pending|failed|skipped|waiting}] for the run page stepper."""
    idx = STEP_OF_STATUS.get(status, 0)
    usage = usage or {}
    out = []
    for i, (_, label) in enumerate(STEPS):
        if status == "awaiting_confirmation":  # ICP done; paused before discovery until the person chooses
            state = "done" if i == 0 else ("waiting" if i == 1 else "pending")
        elif status == "needs_clarification":  # paused at the ICP step until the person answers
            state = "waiting" if i == 0 else "pending"
        elif idx == -1:
            state = "done" if i < last_active_step else ("failed" if i == last_active_step else "pending")
        elif status == "superseded":
            state = "done" if i in (0, 4) else "skipped"
        elif idx >= 5:
            state = "done" if _step_happened(i, usage) else "skipped"
        elif i < idx:
            state = "done"
        elif i == idx:
            state = "current"
        else:
            state = "pending"
        out.append({"label": label, "state": state})
    return out


templates.env.globals.update(badge=badge, ago=ago, money=money, ACTIVE=ACTIVE, EMAIL_PATTERN=EMAIL_PATTERN, shortfall_sentence=shortfall_sentence,
                            ICP_DEFAULTS=DEFAULTS, DEFAULT_LEAD_COUNT=DEFAULT_LEAD_COUNT,
                            COMPETITOR_EXCLUSION=COMPETITOR_EXCLUSION, COMPETITOR_WHY=COMPETITOR_WHY,
                            MIN_PASSWORD_LENGTH=MIN_PASSWORD_LENGTH,
                            REUSE_DAYS=get_settings().research_reuse_days)
templates.env.filters["money"] = money
templates.env.filters["safe_url"] = safe_url
templates.env.filters["ago"] = ago
