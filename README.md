# Koya Lead Research Agent

An AI research analyst for Koya Talent's outbound team. You describe the companies you want to reach. A Claude Agent SDK orchestrator and its subagents then:

1. turn the objective into an ideal customer profile (ICP);
2. find companies with **Apify** (LinkedIn company search);
3. read their public websites with **Firecrawl**;
4. qualify each company from evidence;
5. draft a **3-step cold email sequence + a LinkedIn message** for each qualified lead, for a person to review.

Every run, lead, tool call and cent of Claude spend is stored in Supabase Postgres (schema `lead_agent`).

**Two things it never does:** find, guess or store email addresses, and send anything. A person approves every draft, and only approved drafts leave the app.

- Architecture diagram: https://claude.ai/artifact/BqreZcpTyneniDSVNuu3bQ
- System spec (source of truth, with every decision `D-xx`): [docs/specs.md](docs/specs.md)
- UI rules: [docs/design.md](docs/design.md)
- Deployment plan: [docs/deployment.md](docs/deployment.md)
- New to the code? Start with [CONTRIBUTING.md](CONTRIBUTING.md).

---

## Who sees what

| | Member | Admin | Developer |
| --- | --- | --- | --- |
| Start runs, see **Qualified** leads first, possible fits, all companies checked | ✅ | ✅ | ✅ (+ every status) |
| Decide "needs review" leads (Qualify / Not a fit), approve or reject drafts, Download CSV | ✅ | ✅ | ✅ |
| Continue / Retry a stopped run | own runs | any run | any run |
| Team (invite, roles, deactivate, password reset) | – | ✅ (developers aren't listed) | ✅ (the owner grants developer access) |
| Spend: runs left, by run, by month, Apify, request more budget | – | ✅ | ✅ + dollars, top-ups, where the money went |
| Tool calls tab, model names, technical errors, System issues + alerts | – | – | ✅ |

Roles are checked on the server for every request; hiding a button is never the protection. Client-facing messages are plain language only.

---

## How a run works

```
Browser (Supabase login) → FastAPI (roles, CSRF, rate limits, one run at a time, budget guard)
 Phase A — understand the objective
   free input gate → free pre-run checks (keys, credit, settings) → Haiku scope check ("is this a lead search?")
   → ICP refiner (Agent SDK): Koya defaults as assumptions, competitor exclusion, concrete niches for a vague type
   → clarification question if a detail is missing → "you've run this before" gate
 Phase B — research
   orchestrator (Sonnet) → researcher subagent per company (Opus) → copywriter subagent per qualified lead (Opus)
   one subagent at a time; every action goes through our own guarded tools (the `leadtools` MCP server)
 → Supabase: runs · leads · tool_calls · spend_ledger · scrape_cache · system_events · members · budget_changes
```

1. **Objective checks.** A free code gate rejects junk. A ~$0.001 Haiku call rejects questions and chit-chat. A request with nothing to search on gets one clarification question.
2. **ICP.**
   - Empty fields get fixed Koya defaults (US, 10–100 employees, founders/operations leads), each shown as an assumption.
   - Recruiting and staffing firms that place AI talent (Koya's competitors) are always excluded, unless the objective asks for them.
   - Search terms must name what companies sell, do or serve. Bare category words like "B2B SaaS" are refused.
3. **Discovery (Apify).**
   - The first search fetches 1.5× the leads wanted (max 12); top-ups are ≤ 5; the total is ≤ 20 companies.
   - Filters: location, size band, and LinkedIn's *Software Development* industry for software objectives.
   - A free pre-screen drops companies clearly outside the size or location.
   - One Haiku call ranks the rest before any paid research. It can only rule a company out by quoting that company's own words.
4. **Research (Firecrawl).**
   - Public pages only, capped. Pages are cached for 7 days.
   - Text is truncated, stripped of emails, fenced as untrusted data and injection-flagged.
   - A 401/403 or login wall means *needs review*.
5. **Qualification.**
   - Each hard filter is pass / fail / unknown, with its evidence and a source URL fetched in this run.
   - The fit score is computed by code.
   - A LinkedIn size band that overlaps the range passes.
   - Unclear cases go to **needs review**, where a person decides.
6. **Drafting.**
   - The copywriter gets the writing rules up front, and a free `check_drafts` tool measures a draft before saving it.
   - `save_outreach` runs the code checks plus a Haiku fact-check against the pages that were really read.
   - A claim flagged last time must be gone before paying for another fact-check.
7. **Finish.** A list-quality scorecard, plus a plain-language banner that says what's missing and why, if anything.
8. **Stopped runs.** A restart or crash **pauses** a run; **Continue** carries on from where it stopped (research the rest, write drafts, or search for more). A run stopped by a service problem gets **Retry**.

---

## The agent's tools and skills

The agents have **no** built-in web, shell or file tools. They only get these, per role:

| Tool | What it does (and enforces) |
| --- | --- |
| `save_icp` | Saves the ICP: applies Koya defaults and the competitor rule, refuses bare category search terms, sizes the run's cap from the lead count and the balance |
| `discover_companies` | One Apify search within the run's limits: filters, pre-screen, triage, dedupe, logs the Apify cost |
| `get_research_brief` | What the researcher must check for one company |
| `scrape_website` | One Firecrawl page within the page cap: cached, sanitized, injection-flagged; a failed page can't be retried for free |
| `save_qualification` | Saves a decision. Only URLs fetched in this run can be cited; the fit score and final status are computed by code |
| `get_lead` | The stored facts a copywriter may use, plus the writing rules |
| `check_drafts` | Free length and format check before saving |
| `save_outreach` | Code checks + Haiku fact-check; accepted drafts are labelled *DRAFT — requires human review* |
| `get_run_state` | Progress so far, from the database |
| `finish_run` | Ends the run with a quality scorecard and a plain shortfall reason |

The skills (in `agent_plugin/skills/`, loaded as a local plugin): `icp-refinement`, `lead-qualification`, `outbound-copywriting`, `outreach-safety`, `lead-list-quality`.

---

## Models (chosen by an A/B test on recorded data, specs D-77)

| Role | Model | Why, in one line |
| --- | --- | --- |
| Researcher (qualification) | Opus 5.5 | Haiku marked 4 of 8 non-fits "qualified", Sonnet 1; Opus is the most careful |
| Copywriter | Opus 5.5 | 0 invented claims in two fact-checks, and cheaper per company than Sonnet |
| Orchestrator | Sonnet 5 | Coordinates; doesn't judge or write |
| ICP, fact-check, scope check, triage, pre-run probe | Haiku 4.5 | Met the bar at a fraction of the cost |

Every model is a setting (`MODEL_*`); nothing outside `app/config.py` names a model.

---

## Limits and money

- **Per run:** the Claude cap is sized from the run: (companies it may research × $0.13 + leads × $0.05 + $0.05) × 1.15. That's $0.72 for 1 lead and $3.63 for 10. It's a ceiling, not a charge.
- **Total:** a prepaid balance in `lead_agent.budget_changes`. A developer adds to it on the Spend page, with a reason and a confirmed Anthropic credit. Nothing expires; the monthly (UTC) statement shows what carried over. `CLAUDE_BUDGET_TOTAL_USD` is only the starting value.
- A run that could take the total over the balance is **refused before any spend**. Every Claude call is recorded in `lead_agent.spend_ledger`; the SDK's cost is an estimate, and the Anthropic Console shows the real bill.
- **Apify:** $0.25 cap per search (DEV $0.05); a failed or odd run is never re-run automatically.
- `DEV_LIMITS=true` runs aim for 1 lead with small limits, for development.

---

## Local setup (Windows, PowerShell or Git Bash)

```bash
py -3.12 -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
.venv/Scripts/python -m pip install pytest pytest-asyncio respx ruff
cp .env.example .env                       # fill in (see the comments); you write SUPABASE_DB_DSN yourself
git config core.hooksPath .githooks        # secret scan before every commit

.venv/Scripts/python scripts/create_app_role.py   # creates the least-privilege DB user from the SUPABASE_DB_DSN in .env
.venv/Scripts/python scripts/migrate.py           # applies db/migrations in order (safe to re-run; admin DSN, laptop only)
.venv/Scripts/python scripts/bootstrap_owner.py you@example.com "Your Name" --owner --invite
.venv/Scripts/python -m uvicorn app.main:app --port 8000     # http://localhost:8000
```

- **Windows: never add `--reload`.** It switches to an event loop that can't start the Claude CLI, so every run would fail (the app refuses to start runs and says so).
- **Restart the server after any Python change.** Templates reload on their own; Python doesn't, and the two can get out of step.
- **The database may be shared with the deployed app.** Your laptop and Render can point at the same Supabase project. Runs have a heartbeat, so a starting server never touches a run another server is working on; but anything you do locally changes the same data.

### Configuration (names only; values go in `.env` locally and in Render's settings, never in code)

| Group | Variables |
| --- | --- |
| Claude | `ANTHROPIC_API_KEY`, `MODEL_ORCHESTRATOR`, `MODEL_ICP`, `MODEL_RESEARCHER`, `MODEL_COPYWRITER`, `MODEL_GROUNDING`, `MODEL_CHECKS`, `CLAUDE_BUDGET_TOTAL_USD` (starting value) |
| Discovery / scraping | `APIFY_TOKEN` (the **team** token), `APIFY_ACTOR_ID`, `FIRECRAWL_API_KEY` |
| Supabase | `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY` (server-side only), `SUPABASE_DB_DSN` (app role), `SUPABASE_DB_SCHEMA` (= `lead_agent`), `SUPABASE_ADMIN_DSN` (**laptop only**, migrations), `SUPABASE_JWKS_URL` (optional) |
| App | `SESSION_SECRET`, `APP_BASE_URL`, `DEV_LIMITS`, `FIXTURE_MODE` (keep `false`), `RESEARCH_REUSE_DAYS`, `SCRAPE_CACHE_DAYS`, `MAX_RUNS_PER_DAY`, `MAX_RUNS_PER_USER_PER_DAY`, `MAX_RUNS_PER_ADMIN_PER_DAY`, `COOKIE_SECURE`, `LOG_LEVEL` |
| Alerts | `ALERT_WEBHOOK_URL`, `ALERT_WEBHOOK_SECRET` |
| Render sets | `PORT`, `RENDER_EXTERNAL_URL` |

A blank value means "use the default", so `KEY=` never crashes start-up.

---

## Tests

```bash
.venv/Scripts/python -m pytest -q          # 295 tests: the real database (throwaway rows, cleaned up), every paid API faked
.venv/Scripts/python -m ruff check .       # lint
.venv/Scripts/python scripts/secret_scan.py --all
```

No test spends money: Apify, Firecrawl and the triage model are replaced by fakes, and tests don't depend on today's real run count.

## Useful scripts

| Script | What it does | Cost |
| --- | --- | --- |
| `scripts/dev_run.py "<objective>" --yes` | A research run from the terminal with DEV limits (refuses without `--yes`) | ≤ the run's cap |
| `scripts/claude_credit.py` | Is the Anthropic key working (1-token probe)? Plus the project's recorded spend by source and model | ~$0.00001 (`--no-probe`: free) |
| `scripts/migrate.py` | Applies `db/migrations/*.sql` in order | free |
| `scripts/bootstrap_owner.py` | Gives the first admin access (`--owner` also makes them the developer) | free |

---

## Deploy (Render free, Docker)

1. Render → **New → Blueprint** → this repo (`render.yaml`). Auto-deploy is **off** on purpose: a deploy restarts the app and would pause a running run. Deploy with **Manual Deploy**.
2. Set the secret values in Render (see Configuration). `SUPABASE_DB_DSN` is the **app-role** DSN; never put the admin DSN on Render. `APP_BASE_URL` is your `https://….onrender.com`. Set `ALERT_WEBHOOK_SECRET` to the same value as in n8n.
3. Supabase → Authentication → URL configuration: add `https://….onrender.com/**` to the Redirect URLs (the Site URL is shared by other tools; leave it).
4. Open `/health`: you should see `"database": "ok", "runs_configured": true`.

The full ordered plan with checks and costs is in [docs/deployment.md](docs/deployment.md). Render's free plan sleeps after 15 minutes without traffic; while a run is active the app pings itself so it stays awake.

## Alerts (n8n → email)

Problems like "out of credit", "wrong key", "a run failed" or "an admin asked for more budget" appear under **System issues** (developers), with a banner on every page. To also get an email:
1. In n8n: import `n8n/alert-email-workflow.json`, set the Gmail credential, your email and the secret, and activate it.
2. Set `ALERT_WEBHOOK_URL` (the production `/webhook/…` URL, not `/webhook-test/`) and `ALERT_WEBHOOK_SECRET`.
3. System issues → **Send a test alert**. A wrong secret doesn't show an error (n8n still answers 200); it just sends no email.

The same problem within 30 minutes is counted, not re-sent.

---

## Safety summary

- Nothing public except `/login`, `/accept-invite`, `/forgot-password`, `/reset-password` and `/health` (plus two test pages, only while `FIXTURE_MODE=true`).
- Supabase Auth logins in HttpOnly cookies, verified on the server; admin / member roles plus an owner-granted developer flag; CSRF tokens on every action.
- The app's database user has no DELETE and no DROP, and can't see other schemas; RLS is on. Every query is schema-qualified.
- The agents have no web, shell or file tools, and the tools enforce every limit from the run record.
- Outside text is redacted, fenced as untrusted and injection-flagged. Leads cite only pages fetched in the run. Drafts pass code checks and a separate fact-check.
- Every log line, including tracebacks, is filtered for keys, tokens, passwords and emails. A secret scan runs before every commit.

## Where things are

```
app/            main.py (app, auth middleware, errors) · config.py (settings, limits, run caps, prices) · db.py (all SQL)
                auth.py · runs.py (one run at a time, heartbeat, keep-awake) · alerts.py · failures.py
  agent/        runner.py (phases, SDK options, watchdogs, cost) · tools.py (the 10 tools) · prompts.py
  services/     apify.py · firecrawl.py · grounding.py (fact-check) · scope.py · triage.py · health.py
  lib/          pure rules: limits, budget, scoring, qualification_rules, outreach_checks, icp_defaults,
                triage (search terms, industry filter), resume (continue a run, size bands), spend_breakdown, …
  web/          routes.py (pages + actions) · templating.py
  templates/ static/   Jinja + HTMX pages, app.css, app.js, theme.js
agent_plugin/   the 5 skills          db/migrations/   0000 … 0011          n8n/   alert workflow
docs/           specs.md · design.md · deployment.md          tests/   unit + integration
```
