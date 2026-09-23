"""Build step 0.2: prove how the Python Agent SDK behaves before building on it.

Checks (answers go into ../docs/progress.md §2):
  1. an in-process MCP tool is callable            -> tool handler prints
  2. a subagent runs on its own model              -> SubagentStart hook + model_usage
  3. the subagent can load a skill from a plugin   -> the skill's secret word arrives in the tool
  4. hooks fire for subagent tool calls            -> PreToolUse shows agent_type
  5. WebFetch is unavailable                       -> the agent reports it can't fetch
  6. cost is reported                              -> ResultMessage.total_cost_usd

Hard-capped: max_budget_usd=0.10, max_turns=10, Haiku only.
Run:  .venv/Scripts/python spikes/sdk_spike.py
"""

import asyncio
import logging
import sys
from pathlib import Path

from claude_agent_sdk import (
    AgentDefinition,
    ClaudeAgentOptions,
    HookMatcher,
    ResultMessage,
    create_sdk_mcp_server,
    query,
    tool,
)
from dotenv import dotenv_values

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.logging_setup import configure_logging  # noqa: E402

log = logging.getLogger("lead_agent.spikes.sdk_spike")
ROOT = Path(__file__).resolve().parent.parent
MODEL = "claude-haiku-4-5"
notes: list[str] = []


@tool("record_note", "Record a short note. Use this to report results.", {"text": str})
async def record_note(args):
    notes.append(args["text"])
    log.info(f"  [tool] record_note called with: {args['text']!r}")
    return {"content": [{"type": "text", "text": "noted"}]}


async def log_pre_tool(input_data, tool_use_id, context):
    log.info(f"  [hook] PreToolUse tool={input_data.get('tool_name')} agent_type={input_data.get('agent_type', 'main')}")
    return {}


async def log_subagent_start(input_data, tool_use_id, context):
    log.info(f"  [hook] SubagentStart agent_type={input_data.get('agent_type')}")
    return {}


async def main() -> None:
    env = dotenv_values(ROOT / ".env")
    server = create_sdk_mcp_server(name="spike", tools=[record_note])
    options = ClaudeAgentOptions(
        model=MODEL,
        system_prompt=(
            "You are a test harness. Follow the user's steps exactly and briefly. "
            "Never ask questions."
        ),
        tools=["Agent", "Skill"],                 # only these built-ins exist at all
        allowed_tools=["mcp__spike__record_note", "Agent"],
        disallowed_tools=["WebFetch", "WebSearch", "Bash", "Read", "Write", "Edit", "Glob", "Grep"],
        permission_mode="dontAsk",                # anything not pre-approved is denied
        mcp_servers={"spike": server},
        strict_mcp_config=True,                   # ignore any MCP servers on this machine
        setting_sources=[],                       # isolation: no user/project settings or CLAUDE.md
        plugins=[{"type": "local", "path": str(ROOT / "spikes" / "spike_plugin")}],
        skills=["leadagent:hello-check"],
        agents={
            "checker": AgentDefinition(
                description="Performs the hello check.",
                prompt="Load the hello-check skill, then follow it using the record_note tool. Be brief.",
                tools=["mcp__spike__record_note", "Skill"],
                skills=["leadagent:hello-check"],
                model=MODEL,
                maxTurns=5,
            )
        },
        hooks={
            "PreToolUse": [HookMatcher(hooks=[log_pre_tool])],
            "SubagentStart": [HookMatcher(hooks=[log_subagent_start])],
        },
        max_turns=10,
        max_budget_usd=0.10,
        cwd=str(ROOT / "spikes" / "spike_plugin"),
        env={"ANTHROPIC_API_KEY": env["ANTHROPIC_API_KEY"]},
    )
    prompt = (
        "Step 1: try to fetch https://example.com with WebFetch. If you have no such tool, "
        "call record_note with text 'NO-WEBFETCH'. "
        "Step 2: delegate to the 'checker' subagent to perform the hello check. "
        "Step 3: reply DONE."
    )
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, ResultMessage):
            log.info(f"  [result] subtype={message.subtype} is_error={message.is_error} turns={message.num_turns}")
            log.info(f"  [result] total_cost_usd={message.total_cost_usd}")
            log.info(f"  [result] models used={list((message.model_usage or {}).keys())}")
            log.info(f"  [result] permission_denials={message.permission_denials}")
    log.info("notes recorded: %s", notes)
    log.info("skill loaded in subagent: %s", "SKILL-LOADED-OK" in notes)


if __name__ == "__main__":
    configure_logging(cli=True)
    asyncio.run(main())
