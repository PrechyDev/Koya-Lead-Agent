"""Jinja2 setup and small display helpers shared by all templates."""

from datetime import datetime, timezone
from pathlib import Path

from fastapi.templating import Jinja2Templates

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
    "superseded": ("neutral", "Opened previous"),
}

STEPS = [("refine", "Refine ICP"), ("discover", "Discover"), ("research", "Research"), ("draft", "Draft"),
         ("done", "Done")]
STEP_OF_STATUS = {
    "queued": 0, "refining_icp": 0, "needs_clarification": 0, "awaiting_confirmation": 0,
    "discovering": 1, "researching": 2, "drafting": 3, "finalizing": 4,
    "completed": 5, "completed_partial": 5, "failed": -1, "cancelled": -1, "superseded": 5,
}
ACTIVE = {"queued", "refining_icp", "discovering", "researching", "drafting", "finalizing"}


def badge(status: str | None) -> tuple[str, str]:
    return STATUS_BADGES.get(status or "", ("neutral", (status or "—").replace("_", " ")))


def ago(value: datetime | None) -> str:
    if not value:
        return "—"
    delta = datetime.now(timezone.utc) - value
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    if seconds < 86400:
        return f"{seconds // 3600} h ago"
    return value.strftime("%b %d, %Y")


def money(value) -> str:
    try:
        return f"${float(value or 0):.2f}"
    except (TypeError, ValueError):
        return "$0.00"


def stepper(status: str, last_active_step: int = 0) -> list[dict]:
    """[{label, state: done|current|pending|failed}] for the run page stepper."""
    idx = STEP_OF_STATUS.get(status, 0)
    out = []
    for i, (_, label) in enumerate(STEPS):
        if idx == -1:
            state = "done" if i < last_active_step else ("failed" if i == last_active_step else "pending")
        elif idx >= 5:
            state = "done"
        elif i < idx:
            state = "done"
        elif i == idx:
            state = "current"
        else:
            state = "pending"
        out.append({"label": label, "state": state})
    return out


templates.env.globals.update(badge=badge, ago=ago, money=money, ACTIVE=ACTIVE)
templates.env.filters["money"] = money
templates.env.filters["ago"] = ago
