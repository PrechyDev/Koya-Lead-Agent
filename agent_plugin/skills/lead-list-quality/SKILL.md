---
name: lead-list-quality
description: Use before calling finish_run to check the final lead list against the quality standard (count, completeness, duplicates, safety) and to decide whether to search again within limits or finish with an explanation.
---

# Lead-List Quality

## Required Checks

- The list contains 10 qualified companies.
- Each company has a name and domain.
- Each company has qualification reasoning.
- Each company has source context.
- Each company has outreach drafts.
- No personal email finding or email validation was attempted.
- Duplicate companies were removed.
- Companies marked `needs_review` are not counted as qualified leads.

## Suggested Scorecard

| Dimension | What To Check |
| --- | --- |
| ICP Fit | The lead matches the hard filters in the qualification objective. |
| Evidence Quality | The qualification decision uses real source context. |
| Duplicate Rate | The same company does not appear more than once. |
| Outreach Relevance | The email sequence uses company-specific context. |
| Data Completeness | Required fields are present in Supabase. |
| Safety Compliance | The agent did not find emails, validate emails, or send outreach. |

## Pass Standard

The submitted list should include 10 qualified companies that pass the core checks above.

If the agent cannot find 10 qualified companies from the first candidate pool, it should either search again within the tool-call limit or return fewer leads with a clear explanation.

## Project rules

- "10" means **the run's target** (`target_qualified` from `get_run_state`), which may be smaller in test runs.
- Before finishing, call `get_run_state`. If qualified < target **and** the discovery tool still has budget (it tells you), run one more discovery with the **next** query in the ICP's `discovery_query_plan`, research the new candidates, and draft for any new qualified ones.
- If the tools report a limit is reached (`blocked`), stop searching. Call `finish_run` with a `shortfall_reason` in plain language for a sales team (e.g. "Found 7 qualified of 10: 13 companies had more than 100 employees, and this run's company limit was reached"). Never mention tool names, field names or error codes (no `discover_companies`, `next_search_can_fetch`, "tool errored").
- `finish_run` runs the scorecard in code and can be called only ONCE: if a check fails, the run is saved as `completed_partial` with the failing checks as the reason. So fix what you can BEFORE calling it (use `get_run_state`: for example, a qualified lead with no drafts), and give a plain-language `shortfall_reason` for anything you can't fix.
- Always call `finish_run` exactly once at the end, even when the run is short or something failed.
