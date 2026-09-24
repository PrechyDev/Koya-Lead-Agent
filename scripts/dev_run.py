"""Start a research run from the command line (no web UI needed). Uses DEV limits by default.

    .venv/Scripts/python scripts/dev_run.py "Find US B2B SaaS companies with 10-100 employees that may need AI automation"
    .venv/Scripts/python scripts/dev_run.py --full "..."      # full limits (10 leads) — costs up to $1.25 + Apify
    .venv/Scripts/python scripts/dev_run.py --kind eval_record "..."   # A/B fixture recording run

Prints the run's progress and final summary; everything is stored in Supabase.
"""

import argparse
import asyncio
import logging
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402
from app.agent.runner import execute_run  # noqa: E402
from app.config import get_settings, limits_for_run  # noqa: E402
from app.lib.objective import objective_hash  # noqa: E402
from app.logging_setup import configure_logging  # noqa: E402

log = logging.getLogger("lead_agent.scripts.dev_run")


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("objective")
    parser.add_argument("--full", action="store_true", help="use full limits instead of DEV limits")
    parser.add_argument("--target", type=int, default=10)
    parser.add_argument("--kind", default="dev", choices=["dev", "eval_record", "app"])
    parser.add_argument("--refresh", action="store_true", help="re-research companies seen in the last 30 days")
    args = parser.parse_args()

    settings = get_settings()
    if db.active_run():  # one run at a time, the same rule as the web app (a web run may be going on)
        log.error("Another run is in progress; wait for it to finish.")
        return 1
    limits = limits_for_run(args.target, dev=not args.full)
    run, _ = db.create_run(
        idempotency_key=f"cli-{uuid.uuid4()}", objective=args.objective, objective_hash=objective_hash(args.objective),
        limits=limits.to_dict(), run_kind=args.kind,
    )
    run_id = str(run["id"])
    if args.refresh:
        db.update_run(run_id, cross_run_dedupe=False, repeat_choice="refresh_same")
    log.info(f"run {run_id} | limits: target {limits.target_qualified}, candidates {limits.max_candidates}, "
          f"scrapes {limits.max_scrapes}, Claude cap ${limits.max_budget_usd}")
    log.info(f"models: orchestrator={settings.model_orchestrator} icp={settings.model_icp} "
          f"researcher={settings.model_researcher} copywriter={settings.model_copywriter} "
          f"grounding={settings.model_grounding}")

    task = asyncio.create_task(execute_run(run_id))
    last = None
    while not task.done():
        await asyncio.sleep(5)
        r = db.get_run(run_id)
        line = f"  [{r['status']}] {r['status_detail'] or ''}"
        if line != last:
            log.info(line)
            last = line
    await task

    r = db.get_run(run_id)
    log.info("\nstatus: %s | %s", r["status"], r["status_detail"])
    log.info("cost_usd: %s | turns: %s | usage: %s", r["cost_usd"], r["num_turns"], r["usage"])
    if r["error_message"]:
        log.info("error: %s", r["error_message"])
    if r["clarification_question"]:
        log.info("clarification: %s", r["clarification_question"])
    for lead in db.list_leads(run_id):
        log.info(f"  - {lead['company_domain']:<28} {lead['qualification_status']:<14} "
              f"conf={lead['confidence']} outreach={lead['outreach_status']}")
    log.info("tool calls:")
    for c in db.list_tool_calls(run_id):
        log.info(f"  {c['seq']:>3} {c['agent_role']:<12} {c['tool_name']:<28} {c['status']:<8} {(c['result_summary'] or '')[:90]}")
    log.info("project spend so far: $%.4f", db.total_spend())
    return 0


if __name__ == "__main__":
    configure_logging(cli=True)
    sys.exit(asyncio.run(main()))
