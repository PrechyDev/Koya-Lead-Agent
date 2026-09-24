"""Runs one research run in two phases (specs.md §4-5).

Phase A (cheap):  icp-refiner query -> save_icp -> clarification check -> repeat gate.
Phase B (costly): orchestrator + researcher/copywriter subagents -> finish_run.

Isolation: setting_sources=[] and strict_mcp_config=True, so nothing from the
machine's Claude Code setup (settings, hooks, CLAUDE.md, MCP servers) leaks in.
Built-in tools are limited to Agent/Skill; everything else goes through our
guarded `leadtools` server. Every Claude cost lands in the spend ledger.
"""

import asyncio
import logging
import sys
import tempfile
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from claude_agent_sdk import (
    AgentDefinition,
    ClaudeAgentOptions,
    HookMatcher,
    ResultMessage,
    query,
)

from app import alerts, db
from app.agent import prompts
from app.agent.context import RunContext
from app.agent.logging import log_event
from app.agent.tools import (
    GROUNDING_RUN_CAP_USD,
    OUT_OF_SCOPE_QUESTION,
    build_scorecard,
    build_server,
    mcp_name,
)
from app.agent.transcripts import session_cost
from app.config import ICP_PHASE_MAX_BUDGET_USD, ICP_PHASE_MAX_TURNS, ICP_PHASE_TIMEOUT_S, get_settings
from app.failures import ServiceFailure, classify_claude_error
from app.lib.budget import BudgetExceeded, assert_can_spend
from app.lib.objective import objective_problem
from app.lib.sanitize import redact
from app.services import health
from app.services import scope as scope_svc

log = logging.getLogger("lead_agent.runner")
ROOT = Path(__file__).resolve().parents[2]
PLUGIN_PATH = ROOT / "agent_plugin"
WORKDIR = Path(tempfile.gettempdir()) / "koya_lead_agent_cwd"
# NOTE: never list "Task" here. In this CLI version the subagent tool ("Agent") is tied to "Task", and
# disallowing it silently disables delegation (found in dev run 736fd297; see progress.md errors log).
BLOCKED_BUILTINS = ["Bash", "Read", "Write", "Edit", "MultiEdit", "Glob", "Grep", "WebFetch", "WebSearch",
                    "NotebookEdit", "TodoWrite", "BashOutput", "KillShell"]

ICP_TOOLS = ["save_icp"]
ORCHESTRATOR_TOOLS = ["discover_companies", "get_run_state", "finish_run"]
RESEARCHER_TOOLS = ["get_research_brief", "scrape_website", "save_qualification"]
COPYWRITER_TOOLS = ["get_lead", "check_drafts", "save_outreach"]
GROUNDING_RESERVE_USD = Decimal(str(GROUNDING_RUN_CAP_USD))  # the fact-checks' share, enforced per run by save_outreach


def agent_can_start() -> bool:
    """False when the running event loop can't start the Claude CLI subprocess.

    On Windows only the Proactor event loop can start subprocesses; `uvicorn --reload` switches to the Selector
    loop, and the SDK then fails with an empty "Failed to start Claude Code: " (found in owner test run 27b7e5f0).
    Call from inside the running loop.
    """
    if sys.platform != "win32":
        return True
    return isinstance(asyncio.get_running_loop(), asyncio.ProactorEventLoop)


AGENT_CANNOT_START = "the server's event loop can't start subprocesses (Windows + uvicorn --reload)"


def _skill(name: str) -> str:
    return f"leadagent:{name}"


def _make_hooks(ctx: RunContext, main_role: str, allowed: set[str], main_thread_tools: set[str]) -> dict:
    """Log skill loads + delegations; deny (and log) anything outside the allowlist.

    The main thread may only use `main_thread_tools`: the orchestrator must delegate research and
    copywriting to subagents instead of doing it itself (keeps its context, and cost, small).
    """

    parallel_cap = max(1, int(ctx.limits.get("max_parallel_subagents", 1)))
    in_flight: set[str] = set()  # tool_use_ids of subagents currently working

    async def pre_tool(input_data, tool_use_id, context):
        name = input_data.get("tool_name", "")
        tool_input = input_data.get("tool_input") or {}
        role = input_data.get("agent_type") or main_role
        if name == "Skill":
            skill = str(tool_input.get("skill") or tool_input.get("name") or "?")
            await log_event(ctx, role, f"Skill:{skill.split(':')[-1]}", f"skill={skill}")
            return {}
        if name == "Agent":
            sub = str(tool_input.get("subagent_type") or "?")
            brief = str(tool_input.get("prompt") or tool_input.get("description") or "")[:200]
            if len(in_flight) >= parallel_cap:
                # Code owns the parallel limit (D-49): the model can't start more workers than the run allows.
                await log_event(ctx, role, f"Delegate:{sub}", brief, status="blocked",
                                result_summary=f"denied: {parallel_cap} subagents already working")
                return {"hookSpecificOutput": {
                    "hookEventName": "PreToolUse", "permissionDecision": "deny",
                    "permissionDecisionReason": f"parallel_limit_reached: {parallel_cap} subagents are already "
                                                "working. Delegate this one after they report back."}}
            in_flight.add(tool_use_id or f"anon-{len(in_flight)}")
            await log_event(ctx, role, f"Delegate:{sub}", brief, purpose=str(tool_input.get("description") or "")[:200])
            return {}
        if input_data.get("agent_id") is None:
            # The main thread only acts again after its foreground subagents have reported back, so nothing is in
            # flight any more (a safety net in case a completion hook was missed).
            in_flight.clear()
        if name in allowed:
            if input_data.get("agent_id") is None and name not in main_thread_tools:
                await log_event(ctx, role, name.split("__")[-1], "main thread tried a subagent-only tool",
                                status="blocked", result_summary="denied: delegate instead")
                return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                               "permissionDecisionReason": "Delegate this to the right subagent."}}
            return {}
        await log_event(ctx, role, name or "unknown_tool", "attempted a tool outside the allowlist", status="blocked",
                        result_summary="denied by policy")
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                       "permissionDecisionReason": "This tool is not available in this system."}}

    async def agent_done(input_data, tool_use_id, context):
        if input_data.get("tool_name") == "Agent":
            in_flight.discard(tool_use_id)
        return {}

    return {"PreToolUse": [HookMatcher(hooks=[pre_tool])],
            "PostToolUse": [HookMatcher(matcher="Agent", hooks=[agent_done])],
            "PostToolUseFailure": [HookMatcher(matcher="Agent", hooks=[agent_done])]}


def _options(ctx: RunContext, *, model: str, system_prompt: str, tool_names: list[str], skills: list[str],
             max_turns: int, max_budget_usd: float, main_role: str,
             agents: dict[str, AgentDefinition] | None = None, extra_tools: list[str] | None = None,
             ) -> ClaudeAgentOptions:
    settings = get_settings()
    WORKDIR.mkdir(parents=True, exist_ok=True)
    all_tools = list(tool_names) + list(extra_tools or [])
    allowed = {mcp_name(t) for t in all_tools}
    return ClaudeAgentOptions(
        model=model,
        system_prompt=system_prompt,
        tools=["Agent", "Skill"] if agents else ["Skill"],
        allowed_tools=sorted(allowed) + (["Agent"] if agents else []),
        disallowed_tools=BLOCKED_BUILTINS,
        permission_mode="dontAsk",
        mcp_servers={"leadtools": build_server(ctx, all_tools)},
        strict_mcp_config=True,
        setting_sources=[],
        plugins=[{"type": "local", "path": str(PLUGIN_PATH)}],
        skills=[_skill(s) for s in skills],
        agents=agents,
        hooks=_make_hooks(ctx, main_role, allowed, {mcp_name(t) for t in tool_names}),
        max_turns=max_turns,
        max_budget_usd=max_budget_usd,
        cwd=str(WORKDIR),
        env={"ANTHROPIC_API_KEY": settings.anthropic_api_key,
             "CLAUDE_AGENT_SDK_CLIENT_APP": "koya-lead-agent/0.1"},
        stderr=lambda line: log.debug("cli: %s", redact(line)[0][:300]),
    )


class PhaseTimeout(Exception):
    pass


class _Session:
    """Remembers the CLI session id so cost can be recovered if the phase never reports it."""

    def __init__(self) -> None:
        self.session_id: str | None = None
        self.result: ResultMessage | None = None
        self.stopped_for_fatal = False


async def _consume(prompt: str, options: ClaudeAgentOptions, session: _Session, timeout_s: int,
                   ctx: RunContext | None = None) -> ResultMessage | None:
    async def read() -> None:
        async for message in query(prompt=prompt, options=options):
            data = getattr(message, "data", None)
            sid = getattr(message, "session_id", None) or (data.get("session_id") if isinstance(data, dict) else None)
            if sid and not session.session_id:
                session.session_id = sid
            if isinstance(message, ResultMessage):
                session.result = message  # the last one carries the cumulative cost
            if ctx is not None and ctx.fatal is not None:
                # A service can't work (out of credit, bad key, wrong actor...): stop now, don't let the
                # agent keep trying and spending (E-50). Leaving the loop closes the CLI process.
                session.stopped_for_fatal = True
                break

    try:
        await asyncio.wait_for(read(), timeout=timeout_s)
    except TimeoutError as exc:
        raise PhaseTimeout(f"the agent did not finish within {timeout_s // 60} minutes") from exc
    return session.result


def _recover_cost(run_id: str, source: str, session: _Session, why: str) -> Decimal:
    """Phase ended without a ResultMessage (timeout/crash/cancel/stop): recover the true cost from transcripts."""
    if not session.session_id or session.result is not None:
        return Decimal("0")
    total = Decimal("0")
    for model, cost in session_cost(session.session_id).items():
        if cost > 0:
            db.record_spend(source, cost, ref_id=run_id, model=model,
                            note=f"{source} phase recovered from transcript {session.session_id} ({why})")
            total += cost
    return total


def _record_cost(run_id: str, source: str, result: ResultMessage | None, fallback_model: str) -> Decimal:
    if result is None:
        return Decimal("0")
    total = Decimal(str(result.total_cost_usd or 0))
    per_model = result.model_usage or {}
    if per_model:
        for model, usage in per_model.items():
            cost = Decimal(str(usage.get("costUSD") or 0))
            if cost > 0:
                db.record_spend(source, cost, ref_id=run_id, model=model,
                                input_tokens=int(usage.get("inputTokens") or 0)
                                + int(usage.get("cacheReadInputTokens") or 0)
                                + int(usage.get("cacheCreationInputTokens") or 0),
                                output_tokens=int(usage.get("outputTokens") or 0),
                                note=f"{source} phase ({result.num_turns} turns, {result.subtype})")
    elif total > 0:
        db.record_spend(source, total, ref_id=run_id, model=fallback_model, note=f"{source} phase")
    return total


def _refresh_run_cost(run_id: str, turns_add: int = 0) -> None:
    row = db.fetch_one(f"select coalesce(sum(cost_usd), 0) as s from {db.t('spend_ledger')} where ref_id = %s",
                       (run_id,))
    run = db.get_run(run_id)
    db.update_run(run_id, cost_usd=Decimal(str(row["s"])), num_turns=int(run["num_turns"] or 0) + turns_add)
    alerts.check_budget()


def _result_failure(result: ResultMessage | None) -> ServiceFailure | None:
    """Turn an SDK error result (API error inside the CLI) into a classified failure."""
    if result is None or not result.is_error or result.subtype in ("error_max_budget_usd", "error_max_turns"):
        return None
    text = " ".join([str(result.result or ""), *[str(e) for e in (result.errors or [])]])
    return ServiceFailure(classify_claude_error(result.api_error_status, text),
                          f"{result.subtype}; HTTP {result.api_error_status}; {text[:300]}")


def fail_run(run_id: str, failure: ServiceFailure, detail_status: str | None = None) -> None:
    """Store a failed run with the client message + admin detail, and alert the owner."""
    scorecard, qualified, target = build_scorecard(run_id)
    status = "completed_partial" if qualified else "failed"
    db.set_status(run_id, status, detail_status or ("Stopped: " + failure.client if qualified else failure.client),
                  error_message=failure.client, error_detail=f"[{failure.code}] {failure.detail}"[:2000],
                  quality_scorecard=scorecard if qualified else None,
                  **({"shortfall_reason": failure.client} if qualified else {}))
    alerts.raise_alert(failure.code, failure.detail, run_id=run_id)


# ---------------------------------------------------------------------------
# Phase A
# ---------------------------------------------------------------------------
async def run_icp_phase(run_id: str) -> str:
    """Returns the status the run is left in: needs_clarification | awaiting_confirmation | ready | failed."""
    settings = get_settings()
    ctx = await db.run(RunContext.load, run_id)
    run = await db.run(db.get_run, run_id)
    if not agent_can_start():
        await db.run(fail_run, run_id, ServiceFailure("agent_cannot_start", AGENT_CANNOT_START))
        return "failed"
    problem = objective_problem(run["objective"])  # free gate again: runs can also start from scripts
    if problem:
        await db.run(db.update_run, run_id, request_type="too_vague", clarification_question=problem)
        await db.run(db.set_status, run_id, "needs_clarification", "The objective needs rewording before searching.")
        return "needs_clarification"
    spent = await db.run(db.total_spend)
    try:
        assert_can_spend(spent, Decimal(str(ctx.limits["max_budget_usd"])) + GROUNDING_RESERVE_USD,
                         settings.claude_budget_total_usd)
    except BudgetExceeded as exc:
        await db.run(fail_run, run_id, ServiceFailure("budget_exhausted", str(exc)))
        return "failed"
    failures = await db.run(health.preflight, ctx.limits)
    if failures:
        await db.run(fail_run, run_id, failures[0])
        for extra in failures[1:]:
            await db.run(alerts.raise_alert, extra.code, extra.detail, run_id)
        return "failed"

    await db.run(db.set_status, run_id, "refining_icp", "Understanding your objective",
                 started_at=datetime.now().astimezone())

    # Cheap scope check first (Haiku, ~$0.002): only lead searches may start the full ICP step (D-34).
    scope = await scope_svc.check_scope(run["objective"])
    if scope.cost_usd:
        await db.run(db.record_spend, "icp", scope.cost_usd, ref_id=run_id, model=scope_svc.MODEL,
                     input_tokens=scope.input_tokens, output_tokens=scope.output_tokens, note="scope pre-check")
        await db.run(_refresh_run_cost, run_id)
    if scope.failure_code in ("anthropic_no_credit", "anthropic_auth"):
        await db.run(fail_run, run_id, ServiceFailure(scope.failure_code, "scope pre-check"))
        return "failed"
    if scope.verdict and scope.verdict.request_type != "lead_search":
        kind = scope.verdict.request_type
        question = (scope.verdict.clarification_question if kind == "too_vague" and scope.verdict.clarification_question
                    else OUT_OF_SCOPE_QUESTION.get(kind) or OUT_OF_SCOPE_QUESTION["unrelated"])
        await db.run(db.update_run, run_id, request_type=kind, clarification_question=question[:500])
        detail = ("This doesn't look like a search for companies." if kind in ("question", "unrelated")
                  else "The objective needs a little more detail before searching.")
        await db.run(db.set_status, run_id, "needs_clarification", detail + " Answer the question to continue.")
        return "needs_clarification"

    parent = await db.run(db.get_run, str(run["parent_run_id"])) if run.get("parent_run_id") else None
    options = _options(ctx, model=settings.model_icp, system_prompt=prompts.ICP_SYSTEM, tool_names=ICP_TOOLS,
                       skills=["icp-refinement", "outreach-safety"], max_turns=ICP_PHASE_MAX_TURNS,
                       max_budget_usd=ICP_PHASE_MAX_BUDGET_USD, main_role="icp-refiner")
    session = _Session()
    try:
        result = await _consume(prompts.icp_user_prompt(run["objective"], parent and parent["objective"],
                                                        "answered" if parent else None), options, session,
                                timeout_s=ICP_PHASE_TIMEOUT_S, ctx=ctx)
    except PhaseTimeout as exc:
        await db.run(_recover_cost, run_id, "icp", session, "watchdog timeout")
        await db.run(_refresh_run_cost, run_id)
        await db.run(fail_run, run_id, ServiceFailure("anthropic_unavailable", f"ICP step timed out: {exc}"))
        return "failed"
    except BaseException:
        await db.run(_recover_cost, run_id, "icp", session, "phase interrupted")
        await db.run(_refresh_run_cost, run_id)
        raise
    await db.run(_record_cost, run_id, "icp", result, settings.model_icp)
    await db.run(_refresh_run_cost, run_id, result.num_turns if result else 0)

    run = await db.run(db.get_run, run_id)
    if run["clarification_question"] and not run["icp_signature"]:
        detail = ("This doesn't look like a search for companies." if run.get("request_type") in ("question", "unrelated")
                  else "The objective needs a little more detail before searching.")
        await db.run(db.set_status, run_id, "needs_clarification", detail + " Answer the question to continue.")
        return "needs_clarification"
    if not run["icp"]:
        failure = _result_failure(result) or ServiceFailure(
            "anthropic_unavailable", f"ICP step ended without saving an ICP ({result.subtype if result else 'no result'})")
        await db.run(fail_run, run_id, failure)
        return "failed"

    match = await db.run(db.find_recent_run_by_signature, run["icp_signature"], settings.research_reuse_days, run_id)
    if match and not run.get("repeat_choice"):
        await db.run(db.set_status, run_id, "awaiting_confirmation",
                     f"This matches a run from {match['created_at']:%b %d} "
                     f"({(match['usage'] or {}).get('qualified', 0)} qualified). Choose how to continue.",
                     duplicate_of_run_id=match["id"])
        return "awaiting_confirmation"
    return "ready"


# ---------------------------------------------------------------------------
# Phase B
# ---------------------------------------------------------------------------
async def run_research_phase(run_id: str) -> None:
    settings = get_settings()
    ctx = await db.run(RunContext.load, run_id)
    run = await db.run(db.get_run, run_id)
    remaining_budget = max(0.05, float(ctx.limits["max_budget_usd"]) - float(run["cost_usd"] or 0))
    spent = await db.run(db.total_spend)
    try:
        assert_can_spend(spent, Decimal(str(remaining_budget)) + GROUNDING_RESERVE_USD,
                         settings.claude_budget_total_usd)
    except BudgetExceeded as exc:
        await db.run(fail_run, run_id, ServiceFailure("budget_exhausted", str(exc)))
        return
    failures = await db.run(health.preflight, ctx.limits)  # cached; matters when resuming from the repeat gate
    if failures:
        await db.run(fail_run, run_id, failures[0])
        return
    await db.run(db.set_status, run_id, "discovering", "Searching for companies",
                 models={"orchestrator": settings.model_orchestrator, "icp": settings.model_icp,
                         "researcher": settings.model_researcher, "copywriter": settings.model_copywriter,
                         "grounding": settings.model_grounding})
    ctx.status_seen.add("discovering")

    agents = {
        "researcher": AgentDefinition(
            description="Researches ONE candidate company and saves an evidence-based qualification decision.",
            prompt=prompts.RESEARCHER_PROMPT,
            tools=[mcp_name(t) for t in RESEARCHER_TOOLS] + ["Skill"],
            skills=[_skill("lead-qualification"), _skill("outreach-safety")],
            model=settings.model_researcher,
            maxTurns=10,
            background=False,  # foreground: the orchestrator waits for each result (progress.md errors log)
        ),
        "copywriter": AgentDefinition(
            description="Writes the 3-email sequence and LinkedIn message for ONE qualified lead.",
            prompt=prompts.COPYWRITER_PROMPT,
            tools=[mcp_name(t) for t in COPYWRITER_TOOLS] + ["Skill"],
            skills=[_skill("outbound-copywriting"), _skill("outreach-safety")],
            model=settings.model_copywriter,
            maxTurns=10,
            background=False,
        ),
    }
    options = _options(
        ctx, model=settings.model_orchestrator, system_prompt=prompts.ORCHESTRATOR_SYSTEM,
        tool_names=ORCHESTRATOR_TOOLS, extra_tools=RESEARCHER_TOOLS + COPYWRITER_TOOLS,
        skills=["lead-list-quality", "outreach-safety", "lead-qualification", "outbound-copywriting"],
        max_turns=int(ctx.limits["max_turns"]), max_budget_usd=remaining_budget, main_role="orchestrator",
        agents=agents,
    )
    session = _Session()
    timeout_s = int(ctx.limits.get("phase_timeout_s", 1800))
    try:
        result = await _consume(prompts.orchestrator_user_prompt(run), options, session, timeout_s=timeout_s, ctx=ctx)
    except PhaseTimeout as exc:
        await db.run(_recover_cost, run_id, "run", session, "watchdog timeout")
        await db.run(_refresh_run_cost, run_id)
        failure = ServiceFailure("run_timeout", str(exc))
        await db.run(_force_finish, ctx, None, "it ran too long and was stopped safely", failure)
        await db.run(alerts.raise_alert, failure.code, failure.detail, run_id)
        return
    except BaseException:
        await db.run(_recover_cost, run_id, "run", session, "phase interrupted")
        await db.run(_refresh_run_cost, run_id)
        raise
    if session.stopped_for_fatal:
        await db.run(_recover_cost, run_id, "run", session, f"stopped: {ctx.fatal.code}")
    else:
        await db.run(_record_cost, run_id, "run", result, settings.model_orchestrator)
    await db.run(_refresh_run_cost, run_id, result.num_turns if result else 0)

    if ctx.fatal is not None:
        await db.run(fail_run, run_id, ctx.fatal)
        return
    failure = _result_failure(result)
    if failure and not ctx.finished:
        await db.run(fail_run, run_id, failure)
        return
    if not ctx.finished:
        await db.run(_force_finish, ctx, result)


def _force_finish(ctx: RunContext, result: ResultMessage | None, why: str | None = None,
                  failure: ServiceFailure | None = None) -> None:
    """The agent stopped without finish_run (limit, timeout, or it just stopped): finalize from real counts (E-22/23)."""
    scorecard, qualified, target = build_scorecard(ctx.run_id)
    reason = (result.subtype if result else "no result")
    why = why or {"error_max_budget_usd": "it reached this run's AI budget",
                  "error_max_turns": "it reached this run's step limit"}.get(reason, "the agent stopped early")
    client = f"The research stopped because {why}. Everything found so far is saved below."
    detail = f"[{failure.code if failure else 'agent_stopped'}] {failure.detail if failure else reason}"
    if qualified == 0:
        db.set_status(ctx.run_id, "failed", client, error_message=client, error_detail=detail,
                      quality_scorecard=scorecard)
    else:
        db.set_status(ctx.run_id, "completed_partial", f"{qualified} of {target} qualified (stopped early)",
                      shortfall_reason=client, error_detail=detail, quality_scorecard=scorecard,
                      summary="Finalized by the system.")


async def execute_run(run_id: str, skip_icp: bool = False) -> None:
    """Entry point used by the RunManager. Never raises: failures are stored on the run and alerted."""
    try:
        if not skip_icp:
            state = await run_icp_phase(run_id)
            if state != "ready":
                return
        await run_research_phase(run_id)
    except Exception as exc:  # noqa: BLE001 — visible failure, never silent
        log.exception("run %s failed", run_id)
        text = redact(f"{type(exc).__name__}: {exc}")[0][:900]
        if "psycopg" in type(exc).__module__:
            code = "database_unavailable"
        elif "Failed to start Claude Code" in text or type(exc).__name__ == "CLINotFoundError":
            code = "agent_cannot_start"  # the CLI process never started: not an Anthropic outage
        elif "claude" in text.lower() or "anthropic" in text.lower():
            code = classify_claude_error(None, text)
        else:
            code = "unexpected"
        try:
            await db.run(fail_run, run_id, ServiceFailure(code, text))
        except Exception:  # noqa: BLE001 — the DB may be what failed
            alerts.raise_alert(code, text, run_id)
