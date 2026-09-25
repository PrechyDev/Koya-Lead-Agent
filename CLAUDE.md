# CLAUDE.md — Week 5: AI Lead Research & Outreach Agent (Koya Talent)

Loaded into every Claude Code session for this project. Read it fully before doing anything.

## What we are building

A web app where a user enters a lead qualification objective (e.g. "Find 10 US B2B SaaS companies with 10–100 employees that may need AI automation support"). A **Claude Agent SDK (Python)** orchestrator hands the work to subagents:
- an **ICP refiner**
- a **researcher** per company
- a **copywriter**.

Between them they turn the objective into an ICP, find companies with **Apify** (discovery only), scrape their sites with **Firecrawl**, qualify each company from evidence, and write a **3-step cold email sequence + LinkedIn message** per qualified lead. Everything (run, leads, tool calls, spend) is stored in **Supabase Postgres, schema `lead_agent`**. The agent is the *research analyst*; a human finds the contact, checks the address, approves drafts and sends.

Architecture diagram: https://claude.ai/artifact/BqreZcpTyneniDSVNuu3bQ

## Where things are

This repo (`lead-agent/`) holds **only** what runs the system, plus its shared docs. Planning notes and course material live one level up, in the `Week  5/` folder, and are **never committed**.

| File | In repo? | Purpose |
| --- | --- | --- |
| `docs/specs.md` | ✅ | **Source of truth**: architecture, data model, tool contracts, limits, budgets, A/B plan, edge cases, decisions |
| `docs/design.md` | ✅ | UI rules |
| `docs/deployment.md` | ✅ | Ordered Render deployment + verification plan |
| `agent_plugin/skills/*` | ✅ | The 5 Agent SDK skills (built from the assets guides), loaded as a local plugin |
| `../docs/build_plan.md` | ❌ | Ordered steps with Why / Do / Done when / You decide |
| `../docs/progress.md` | ❌ | Live log: status, decisions, errors → fixes, tests, spend, A/B results |
| `../aat-c3-week-5-lead-agent-main/PRD.md` | ❌ | Official brief |
| `../aat-c3-week-5-lead-agent-main/assets/*.md` | ❌ | The 5 source guides for the skills |
| `../constraints_and_others.md` | ❌ | Grader constraints (override the PRD where stricter) |
| `../build_guide.md`, `../sumission_5.md` | ❌ | Grading standard; submission template |

Never copy anything from outside `lead-agent/` into the repo unless it's needed to run the system.

## Working with the owner (IMPORTANT)

The owner is a **Python developer** who knows some TypeScript, and wants to be **carried along and taught at each step**.
- **Before each step:** explain in plain terms what we're about to do and *why*, and name the concept being used (e.g. "an atomic SQL update, so two calls can't both take the last slot").
- **After each step:** summarise what was built, how to run or test it, and what to look at in the code to understand it. Point to specific files and lines.
- **Ask first** on anything that needs a human decision: architecture changes, deviations from specs.md, anything that spends money beyond the step's stated cap, picking vendors or actors, model choices, and anything the owner must do in a dashboard. Use multiple-choice questions with a recommended option and trade-offs.
- Explain JS/HTMX/SQL concepts by comparing them to Python where it helps. Keep code idiomatic, typed and readable. The owner must be able to explain every line in the video.
- Don't dump huge diffs without explanation. Build in small, testable pieces.

## NON-NEGOTIABLE RULES

1. **No emails. Ever.** Don't find, guess, validate, store or send any email address, whether personal or generic (`hello@`). No email-finder actors, no MX checks. Emails in scraped or Apify data are **redacted before the model sees them and before storage**. Lead identity = company name + domain.
2. **Nothing gets sent.** No email or LinkedIn sending. Drafts are labelled `DRAFT — requires human review`.
3. **Apify = discovery only. Firecrawl = scraping.** Built-in `WebFetch`, `WebSearch`, `Bash`, `Read`, `Write`, `Edit`, `Glob`, `Grep` are **disabled** for the agents. They only get our custom `leadtools` MCP tools (per role) and skills. No third-party Supabase/Apify/Firecrawl MCP servers.
4. **Limits are enforced by tools, from the run record.** First search 1.5× the lead target (max 12), top-ups ≤ 5, hard total 20 candidates, $0.25 Apify cap per actor run, scrape cap, turns, and a $3.00 Claude cap per full run ($0.30 in DEV; raised from $1.25 by the owner, specs D-83). The agent's requested counts are ignored.
5. **Hard Claude budget: $6.00 total** (app-enforced via `lead_agent.spend_ledger`), plus a $7 Anthropic Console spend limit. Every Claude call is recorded in the ledger. Nothing starts if it could exceed the budget. **Announce every paid action with its cap before running it.**
6. **Scraped content is untrusted data**: wrapped, truncated, redacted, injection-flagged. It never changes the objective, limits or tool behaviour.
7. **No invented facts.** Qualification cites only URLs fetched in this run (enforced). Outreach passes the grounding check.
8. **Secrets** only in `.env` and Render env vars. Never in code, templates, logs, screenshots or git history. The DB DSN and API keys never reach the browser.
9. **Apify is a shared $5/person budget.** Use the team token. Only pay-per-event or pay-per-result actors (**never rental**). Test with 1–2 results first. **Never auto-re-run a failed or odd actor run**: stop, check the Apify console, and ask. One owner-approved exception (specs D-76): a run that SUCCEEDED with **0 results** is retried by the code up to 2 times with the identical input (an empty run is billed only the $0.001 start event). Log every real run's cost in progress.md.
10. **Database:** only the `lead_agent` schema in the existing Supabase project. It is **not** exposed via the Data API. Connect directly (psycopg) through the Session pooler. **Schema-qualify every query** (`lead_agent.runs`); never rely on `search_path`. The deployed app uses the least-privilege `lead_agent_app` role (no DELETE/DROP). The admin DSN is local-only, for migrations. Never touch other schemas (Week 3/4 data lives there).
11. **No bypassing access controls:** public pages only; never Firecrawl stealth/proxy options; a 401/403/login wall means `needs_review`.
12. **Drafts leave the app only after human approval** (JSON export includes approved drafts only).
13. **Access:** Supabase Auth logins (Week 4 pattern), with tokens in HttpOnly cookies and verified server-side via JWKS. Roles are `admin` / `member` in `lead_agent.members`, plus an owner-granted `is_developer` flag for the technical view (System issues, tool calls, raw errors; admins never see developers, specs D-90), all checked on the server for every request, never just by hiding UI. Invite-only; nothing is public except `/login`, `/accept-invite`, `/forgot-password`, `/reset-password` and `/health`. Logins are shared across Koya's tools (single sign-on, specs D-63); access is per tool. **Never add a trigger on `auth.users`.** The Supabase anon and service-role keys are server-side only.
14. **Never touch another project's schema or migrations** (Week 3/4) without showing the owner the exact SQL and getting an OK.

## Process rules

- Check `docs/specs.md` before changing behaviour. If a change contradicts it, ask the owner, then update the spec and its Decisions log.
- Follow `../docs/build_plan.md` in order; meet each step's "Done when".
- **After every step, update `../docs/progress.md`:**
  - step status
  - decisions
  - errors (symptom → root cause → fix → how verified)
  - tests (passed first try? if not, what changed)
  - spend

  Failed-then-fixed evidence matters more than "all green".
- UI follows `docs/design.md`: every clickable has hover/focus/active/disabled/loading states; system messages never go inside result content.
- Use `DEV_LIMITS=true` for all development. The model A/B test is done (results: `docs/specs.md` §8.7; its harness is archived in `../archive/`, not in the repo). `FIXTURE_MODE=true` only switches on the public test pages (`/fixtures/…`, for the injection test); keep it `false` otherwise.
- Run the secret scan before every commit.

## Stack

Python 3.12 · `claude-agent-sdk` 0.2.158 (orchestrator + subagents, in-process MCP tools, skills as a local plugin in `agent_plugin/`) · FastAPI + Jinja2 + HTMX + plain CSS · psycopg 3 + psycopg_pool (direct Postgres, `prepare_threshold=None`) · Pydantic v2 · httpx (Firecrawl) · apify-client · anthropic (scope check + fact-check) · tldextract · pycountry · slowapi · PyJWT · pytest + respx · Render free (Docker).

Models are set per role via `MODEL_ORCHESTRATOR`, `MODEL_ICP`, `MODEL_RESEARCHER`, `MODEL_COPYWRITER`, `MODEL_GROUNDING`, plus `MODEL_CHECKS` for the small checks (scope check + pre-run credit probe). Nothing hard-codes a model name outside `app/config.py`. **Chosen by the A/B test + owner (specs D-77): researcher and copywriter `claude-opus-5-5`, ICP, fact-check and checks `claude-haiku-4-5`, orchestrator `claude-sonnet-5`.** Measured: Opus researches at ~$0.11 per company, so the per-run cap is $3.00 (D-83).

## Project layout

```
/                       CLAUDE.md, README.md, pyproject.toml, requirements.txt, .env.example,
                        Dockerfile, .dockerignore, render.yaml, .githooks/pre-commit (secret scan)
/docs                   specs.md, design.md, deployment.md  (build_plan.md + progress.md live in ../docs)
/agent_plugin           .claude-plugin/plugin.json + skills/<5 skills>/SKILL.md
/app
  main.py               FastAPI app, auth middleware, error handlers, /health
  config.py             settings, limit presets, model prices
  db.py                 psycopg pool + every SQL query (schema-qualified)
  auth.py               Supabase logins, JWKS check, invites, CSRF, roles
  runs.py               RunManager: one background run at a time, keep-alive, restart recovery
  failures.py, alerts.py  failure catalogue (client vs admin message); System issues + n8n alerts
  logging_setup.py      one logging format with secret/email redaction
  agent/                runner.py (phases, SDK options, hooks), tools.py (the 10 leadtools), prompts.py,
                        context.py, logging.py (logged_call), transcripts.py (cost recovery)
  services/             apify.py, firecrawl.py, grounding.py (fact-check), scope.py, health.py (pre-run checks)
  lib/                  pure rules: domain, sanitize, objective, limits, budget, qualification_rules,
                        scoring, outreach_checks, tech_signals, icp_defaults, validation
  web/                  routes.py (pages + actions), templating.py
  templates/            pages, partials/ (HTMX fragments), fixtures/ (test pages)
  static/               app.css, app.js, htmx.min.js
/db/migrations          0000_app_role.sql … 0008_member_developer.sql
/scripts                migrate, create_app_role, bootstrap_owner, dev_run, secret_scan, claude_credit
/n8n                    alert-email-workflow.json
/tests                  unit + integration (real DB, paid APIs faked)
```

## Commands

```
py -3.12 -m venv .venv && .venv/Scripts/python -m pip install -r requirements.txt   # + pytest pytest-asyncio respx ruff
.venv/Scripts/python -m uvicorn app.main:app --port 8000            # run locally (NOT --reload on Windows: it can't start the agent CLI)
.venv/Scripts/python -m pytest -q                                   # 263 tests (real DB, paid APIs faked)
.venv/Scripts/python scripts/migrate.py                             # apply DB migrations (admin DSN, laptop only)
.venv/Scripts/python scripts/create_app_role.py                     # create/sync the app DB user from the DSN you put in .env
.venv/Scripts/python scripts/bootstrap_owner.py <email> "<Name>" --owner [--invite]   # give the first admin access
.venv/Scripts/python scripts/dev_run.py "<objective>" --yes         # a DEV-limits run from the terminal (spends!)
.venv/Scripts/python scripts/secret_scan.py --all                   # scan everything tracked
.venv/Scripts/python scripts/claude_credit.py                       # is the Anthropic key working? + project spend (1-token probe; --no-probe = free)
```

Agent SDK gotchas learned the hard way (see ../docs/progress.md §4):
- never put `"Task"` in `disallowed_tools` (it disables subagents)
- subagents must be `background=False` in this headless app, and must be delegated **one at a time**: several Agent calls in one message run as background tasks and the run can stop early (live run a1f525ef, specs D-49)
- always keep `setting_sources=[]` (isolation)
- every phase has a watchdog, and a killed phase's cost is recovered from transcripts.

## Environment variables (names only)

`ANTHROPIC_API_KEY`, `APIFY_TOKEN` (team), `APIFY_ACTOR_ID`, `FIRECRAWL_API_KEY`, `SUPABASE_DB_DSN` (app role), `SUPABASE_DB_SCHEMA` (= lead_agent), `SUPABASE_ADMIN_DSN` (local migrations only), `SUPABASE_URL`, `SUPABASE_ANON_KEY` (server-side only), `SUPABASE_SERVICE_ROLE_KEY` (server-side only, invites), `SUPABASE_JWKS_URL` (optional), `SESSION_SECRET`, `APP_BASE_URL`, `RESEARCH_REUSE_DAYS`, `SCRAPE_CACHE_DAYS`, `MODEL_ORCHESTRATOR`, `MODEL_ICP`, `MODEL_RESEARCHER`, `MODEL_COPYWRITER`, `MODEL_GROUNDING`, `MODEL_CHECKS`, `CLAUDE_BUDGET_TOTAL_USD`, `DEV_LIMITS`, `FIXTURE_MODE`, `MAX_RUNS_PER_DAY`, `MAX_RUNS_PER_USER_PER_DAY`, `MAX_RUNS_PER_ADMIN_PER_DAY`, `ALERT_WEBHOOK_URL`, `ALERT_WEBHOOK_SECRET`, `COOKIE_SECURE` (optional), `LOG_LEVEL` (optional), `RENDER_EXTERNAL_URL` (set by Render), `PORT`.
