"""Resume a run paused at the repeat gate (same as the UI's buttons).

    .venv/Scripts/python scripts/resume_run.py <run_id> find_new|refresh_same
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402
from app.agent.runner import execute_run  # noqa: E402


async def main(run_id: str, choice: str) -> None:
    db.update_run(run_id, repeat_choice=choice, cross_run_dedupe=(choice != "refresh_same"))
    task = asyncio.create_task(execute_run(run_id, skip_icp=True))
    last = None
    while not task.done():
        await asyncio.sleep(5)
        r = db.get_run(run_id)
        line = f"  [{r['status']}] {r['status_detail'] or ''}"
        if line != last:
            print(line, flush=True)
            last = line
    await task
    r = db.get_run(run_id)
    print("\nstatus:", r["status"], "|", r["status_detail"], "| cost:", r["cost_usd"], "| turns:", r["num_turns"])
    print("usage:", {k: v for k, v in (r["usage"] or {}).items() if k != "queries"})
    if r["error_message"]:
        print("error:", r["error_message"])
    if r["shortfall_reason"]:
        print("shortfall:", r["shortfall_reason"])
    for lead in db.list_leads(run_id):
        print(f"  - {lead['company_domain']:<30} {lead['qualification_status']:<14} conf={lead['confidence']} "
              f"outreach={lead['outreach_status']} | {(lead['prescreen_reason'] or '')[:60]}")
    for c in db.list_tool_calls(run_id):
        print(f"  {c['seq']:>3} {c['agent_role']:<12} {c['tool_name']:<26} {c['status']:<8} "
              f"{(c['result_summary'] or c['error_message'] or '')[:110]}")
    print("project spend: $%.4f" % db.total_spend())


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2]))
