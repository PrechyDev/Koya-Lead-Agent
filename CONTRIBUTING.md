# Contributing to the Koya Lead Research Agent

Welcome. This page gets you from a fresh clone to a safe first change. Read it once before you touch anything that spends money or changes data.

## 1. Read these first (in this order)

1. **[README.md](README.md)**: what the app does, who sees what, and how a run flows.
2. **[docs/specs.md](docs/specs.md)**: the source of truth. Architecture, data model, tool contracts, limits, edge cases, and the **Decisions log** (`D-01` … `D-102`). Each decision says what was chosen, who confirmed it, why, and what was rejected.
3. **[docs/design.md](docs/design.md)**: UI rules. Every clickable element has hover, focus, active, disabled and loading states, and system messages never go inside results.
4. **[CLAUDE.md](CLAUDE.md)**: the rules the AI coding assistant follows in this repo. They're the same non-negotiables you must follow.

## 2. The non-negotiable rules

- **No emails, ever.** Don't find, guess, validate, store or send any email address. Emails in scraped or Apify data are redacted before the model or the database sees them. A lead is a company name + domain.
- **Nothing is sent.** No email or LinkedIn sending. Drafts are labelled `DRAFT — requires human review`, and only approved drafts are exported.
- **Apify is for discovery only; Firecrawl is for scraping.** The agents get only our `leadtools` tools and skills. Never add built-in web, shell or file tools, or third-party MCP servers.
- **Limits live in the run record and are enforced by tools.** The agent's requested numbers are ignored.
- **Money:**
  - Every Claude call goes into `spend_ledger`, and nothing starts if it could exceed the balance.
  - Announce every paid action with its cap before running it.
  - Apify is a shared budget: pay-per-result actors only, test with 1–2 results, and **never auto-re-run a failed or odd actor run**.
- **Scraped content is untrusted data.** Wrap, truncate, redact and flag it. It must never change the objective, the limits or tool behaviour.
- **No invented facts.** A lead cites only URLs fetched in this run; outreach passes the fact-check.
- **Secrets** go only in `.env` and Render's settings. Never put them in code, templates, logs, screenshots or git history.
- **Database:** only the `lead_agent` schema. Schema-qualify every query (`lead_agent.runs`). The deployed app uses the least-privilege `lead_agent_app` role, and the admin DSN is for migrations on your laptop only. **Never touch another schema** (other Koya tools live there), and never add a trigger on `auth.users`.
- **Access:** roles are checked on the server for every request. Hiding a button is never the protection.

## 3. Set up

Follow **Local setup** in the README, then:

```bash
git config core.hooksPath .githooks          # the secret scan runs before every commit
.venv/Scripts/python -m pytest -q            # should be all green before you change anything
```

Use `DEV_LIMITS=true` while developing; runs then aim for 1 lead with small caps. Keep `FIXTURE_MODE=false` unless you're running the public injection test.

⚠️ **Your laptop may share the database with the deployed app.** Your local runs, deletes and migrations affect real data. Runs have a heartbeat, so a server you start won't mark someone else's live run as failed, but be careful with scripts that write.

## 4. Where to make a change

| You want to… | Change | And |
| --- | --- | --- |
| Change a rule (a limit, a score, a check) | `app/lib/*` (pure functions, no I/O) | Unit-test it directly; it's the easiest code to test |
| Add or change an agent tool | `app/agent/tools.py` (the handler + its JSON schema) and the role's tool list in `runner.py` | Log it through `logged_call` (every call becomes a `tool_calls` row); enforce limits from `ctx.limits`, never from the agent's input |
| Change what an agent knows | `agent_plugin/skills/<skill>/SKILL.md` or `app/agent/prompts.py` | Rules the code enforces belong in code too; the skill explains them to the model |
| Add a query | `app/db.py` only (no SQL anywhere else) | Schema-qualify it; add new updatable columns to `RUN_COLUMNS` / `LEAD_COLUMNS` / `MEMBER_COLUMNS` |
| Change the database | A new file `db/migrations/00NN_name.sql` (idempotent: `if not exists`, drop-then-add for constraints) | Run `scripts/migrate.py`; new tables need RLS + the `app_access` policy for `lead_agent_app` |
| Add a page or action | `app/web/routes.py` + `app/templates/` | Use `require_admin` / `require_developer` / `_can_control` and `_check_csrf`; errors go to the System Message bar (`_banner`), never into results |
| Call a paid API | `app/services/*` | Put a guard before the call (`assert_can_spend` / `assert_run_fits`), record the cost, and make failure safe (never a hidden retry of something billed) |
| Change a model | `.env` / Render (`MODEL_*`) | Nothing outside `app/config.py` names a model; update the price table there |

Keep code idiomatic, typed and readable, and match the surrounding style.

## 5. Testing

- `tests/` uses the **real database** with throwaway rows that each test deletes, and **fakes every paid API** (Apify, Firecrawl, the triage model). No test may spend money.
- Shared fixtures in `tests/conftest.py` switch off the real triage and make "runs today" zero, so tests don't depend on real use of the shared database.
- Add a test for every bug you fix, named for the behaviour (`test_a_failed_page_cannot_be_retried_for_free`). A test that passed from the start isn't evidence; one that caught the bug is.
- For UI changes, check them in a real browser. Never start a test server against the shared database with its start-up recovery switched on.
- Before you commit: `pytest -q`, `ruff check .`, and the secret scan (the hook runs it for you).

## 6. Recording what you did

- **Behaviour changes:** check `docs/specs.md` first. If your change contradicts it, agree it with the owner, then update the spec and add a decision (`D-xx`: what, who confirmed, why, what was rejected).
- **Bugs:** the build log keeps *symptom → root cause → fix → how it was verified*. Failed-then-fixed evidence matters more than "all green".
- **Commits:** small and explained; one topic per commit. Never commit `.env` or anything with a secret in it.

## 7. Things that will surprise you

- **Windows + `uvicorn --reload`** can't start the Claude CLI. Run without `--reload`, and restart the server after Python changes.
- **Agent SDK:**
  - Never put `"Task"` in `disallowed_tools`; it disables subagents.
  - Subagents must be `background=False` and delegated **one at a time**; parallel Agent calls ran in the background and stopped a run early.
  - Keep `setting_sources=[]`.
  - Every phase has a watchdog, and a killed phase's cost is recovered from its transcript.
- **The SDK's cost is an estimate reported when a phase ends**, so the live cost on the run page lags during research. Direct API calls are priced exactly from their tokens.
- **Apify sometimes returns 0 companies for an input that worked a minute earlier.** That's the provider, not your code; the empty-result retry handles it.
- **HTMX 2 drops 4xx/5xx responses by default.** Our banners use `HX-Retarget`, and `app.js` lets them through; a new error path should do the same.
