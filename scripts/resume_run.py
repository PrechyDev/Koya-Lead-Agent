"""Resume a run paused at the repeat gate (same as the UI's buttons). SPENDS: runs the paid research phase.

    .venv/Scripts/python scripts/resume_run.py <run_id> find_new|refresh_same --yes

Refuses unless the run is really waiting at the repeat gate, so it can't re-run a finished run's research.
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402
from app.agent.runner import execute_run  # noqa: E402
from app.logging_setup import configure_logging  # noqa: E402

log = logging.getLogger("lead_agent.scripts.resume_run")


async def main(run_id: str, choice: str) -> None:
    run = db.get_run(run_id)
    if not run or run["status"] != "awaiting_confirmation":
        raise SystemExit(f"Run {run_id} isn't waiting at the repeat gate (status: {run and run['status']}).")
    if db.active_run():
        raise SystemExit("Another run is in progress; only one runs at a time.")
    # Same as the web route (routes.confirm_repeat): skip recently researched companies only for "find new".
    db.set_status(run_id, "queued", "Resuming after the repeat check", repeat_choice=choice,
                  cross_run_dedupe=(choice == "find_new"))
    task = asyncio.create_task(execute_run(run_id, skip_icp=True))
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
    log.info("\nstatus: %s | %s | cost: %s | turns: %s", r["status"], r["status_detail"], r["cost_usd"],
             r["num_turns"])
    log.info("usage: %s", {k: v for k, v in (r["usage"] or {}).items() if k != "queries"})
    if r["error_message"]:
        log.info("error: %s", r["error_message"])
    if r["shortfall_reason"]:
        log.info("shortfall: %s", r["shortfall_reason"])
    for lead in db.list_leads(run_id):
        log.info(f"  - {lead['company_domain']:<30} {lead['qualification_status']:<14} conf={lead['confidence']} "
              f"outreach={lead['outreach_status']} | {(lead['prescreen_reason'] or '')[:60]}")
    for c in db.list_tool_calls(run_id):
        log.info(f"  {c['seq']:>3} {c['agent_role']:<12} {c['tool_name']:<26} {c['status']:<8} "
              f"{(c['result_summary'] or c['error_message'] or '')[:110]}")
    log.info("project spend: $%.4f", db.total_spend())


if __name__ == "__main__":
    configure_logging(cli=True)
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("run_id")
    parser.add_argument("choice", choices=["find_new", "refresh_same"])
    parser.add_argument("--yes", action="store_true", help="confirm: this runs the paid research phase")
    args = parser.parse_args()
    if not args.yes:
        raise SystemExit("This spends (Claude + Apify + Firecrawl, capped by the run's limits). Add --yes to go ahead.")
    asyncio.run(main(args.run_id, args.choice))
