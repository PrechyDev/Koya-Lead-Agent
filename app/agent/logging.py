"""Every tool call is logged by code, not by the agent (specs.md §6 tool_calls; E-34).

`logged_call` wraps a tool handler:
  1. takes the next sequence number for the run;
  2. refuses (status 'blocked') once the run's max_tool_calls is reached;
  3. writes a 'running' row, runs the handler, then updates the row with the
     outcome, a short result summary, duration and any external cost;
  4. turns unexpected exceptions into a logged 'error' + a safe message for
     the agent (details never include secrets).
"""

import json
import time
from dataclasses import dataclass, field
from typing import Any
from collections.abc import Awaitable, Callable

from app import db
from app.agent.context import RunContext
from app.lib.sanitize import redact

ROLE_BY_TOOL = {
    "save_icp": "icp-refiner",
    "discover_companies": "orchestrator",
    "get_run_state": "orchestrator",
    "finish_run": "orchestrator",
    "get_research_brief": "researcher",
    "scrape_website": "researcher",
    "save_qualification": "researcher",
    "get_lead": "copywriter",
    "save_outreach": "copywriter",
}


@dataclass
class Outcome:
    status: str  # success | blocked | error
    data: dict = field(default_factory=dict)
    summary: str = ""
    error: str | None = None
    external_cost_usd: float | None = None


def success(data: dict, summary: str, cost: float | None = None) -> Outcome:
    return Outcome("success", {"ok": True, **data}, summary, None, cost)


def blocked(reason: str, message: str, **info: Any) -> Outcome:
    return Outcome("blocked", {"ok": False, "reason": reason, "message": message, **info}, message)


def failure(message: str, **info: Any) -> Outcome:
    return Outcome("error", {"ok": False, "error": message, **info}, message, message)


def _as_tool_result(outcome: Outcome) -> dict:
    result = {"content": [{"type": "text", "text": json.dumps(db.to_jsonable(outcome.data), ensure_ascii=False)}]}
    if outcome.status == "error":
        result["is_error"] = True
    return result


def summarize_input(args: dict, keys: tuple[str, ...]) -> str:
    parts = []
    for k in keys:
        if k in args and args[k] not in (None, "", []):
            v = args[k]
            text = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
            parts.append(f"{k}={text[:160]}")
    return redact("; ".join(parts))[0]


async def logged_call(
    ctx: RunContext, tool_name: str, args: dict, handler: Callable[[dict], Awaitable[Outcome]],
    input_keys: tuple[str, ...] = (),
) -> dict:
    role = ROLE_BY_TOOL.get(tool_name, "system")
    purpose = str(args.get("purpose") or "")[:500]
    seq = await db.run(db.next_tool_call_seq, ctx.run_id)
    input_summary = summarize_input(args, input_keys)

    if seq > int(ctx.limits.get("max_tool_calls", 10**6)):
        out = blocked("tool_call_limit_reached",
                      f"This run reached its limit of {ctx.limits['max_tool_calls']} tool calls. Stop and call "
                      "finish_run if you haven't.")
        call_id = await db.run(db.insert_tool_call, ctx.run_id, seq, role, tool_name, purpose, input_summary,
                               "blocked")
        await db.run(db.finish_tool_call, call_id, "blocked", out.summary)
        return _as_tool_result(out)

    call_id = await db.run(db.insert_tool_call, ctx.run_id, seq, role, tool_name, purpose, input_summary)
    started = time.monotonic()
    try:
        out = await handler(args)
    except Exception as exc:  # noqa: BLE001 — every failure must be visible, never silent
        detail = redact(f"{type(exc).__name__}: {exc}")[0][:900]
        out = failure("The tool failed unexpectedly. Continue with other work; this was logged.")
        out.error = detail
    duration_ms = int((time.monotonic() - started) * 1000)
    await db.run(db.finish_tool_call, call_id, out.status, redact(out.summary)[0], out.error, duration_ms,
                 out.external_cost_usd)
    return _as_tool_result(out)


async def log_event(ctx: RunContext, role: str, tool_name: str, input_summary: str, status: str = "success",
                    result_summary: str = "", purpose: str = "") -> None:
    """Log a non-tool event (skill load, subagent delegation, denied built-in) as its own row."""
    seq = await db.run(db.next_tool_call_seq, ctx.run_id)
    call_id = await db.run(db.insert_tool_call, ctx.run_id, seq, role, tool_name, purpose,
                           redact(input_summary)[0], status)
    await db.run(db.finish_tool_call, call_id, status, result_summary, None, 0)
