# Koya Lead Research Agent

An AI research analyst for Koya Talent's outbound team. You give it a lead qualification objective. A Claude Agent SDK orchestrator and its subagents then:
1. refine the objective into an ICP
2. find companies with Apify
3. research their websites with Firecrawl
4. qualify each company from evidence
5. draft a 3-step cold email sequence + LinkedIn message for human review.

Every run, lead, tool call and cent of Claude spend is stored in Supabase Postgres (schema `lead_agent`).

The agent **does not** find or validate email addresses, and **never sends** anything. A human reviews every draft.

- System spec: [docs/specs.md](docs/specs.md) · UI rules: [docs/design.md](docs/design.md) · Deployment: [docs/deployment.md](docs/deployment.md)

## How it works (short)

```
Browser (login) → FastAPI (roles, CSRF, rate limits, 1 run at a time, budget guard)
  → Phase A: free input gate → free pre-run checks → Haiku scope check → ICP refiner (Agent SDK)
             → clarification check → "you've run this before" gate
  → Phase B: orchestrator → researcher subagent per company → copywriter subagent per qualified lead
             (one subagent at a time)
       every action goes through our own guarded tools (leadtools MCP server):
       discover (Apify, capped, free pre-screen) · scrape (Firecrawl, cached, sanitized)
       · save_qualification (evidence rules, code-computed fit score) · check_drafts (free, before saving)
       · save_outreach (code checks + Haiku fact-check) · finish_run (quality scorecard)
  → Supabase: members · runs · leads · tool_calls · scrape_cache · spend_ledger · system_events · eval_results
```

## Local setup (Windows / PowerShell or Git Bash)

```bash
py -3.12 -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
.venv/Scripts/python -m pip install pytest pytest-asyncio respx ruff
cp .env.example .env                       # fill in (see comments in the file; you write SUPABASE_DB_DSN yourself)
git config core.hooksPath .githooks        # secret scan before every commit

.venv/Scripts/python scripts/create_app_role.py   # creates the least-privilege DB user from the SUPABASE_DB_DSN you put in .env (read-only)
.venv/Scripts/python scripts/migrate.py           # applies db/migrations in order (safe to re-run)
.venv/Scripts/python scripts/bootstrap_owner.py you@example.com "Your Name" --owner --invite   # give yourself admin access
.venv/Scripts/python -m uvicorn app.main:app --port 8000                             # http://localhost:8000
# Windows: don't add --reload. It switches to an event loop that can't start the Claude CLI, so every run
# would fail (the app refuses to start runs and says so). Docker/Linux/Render are not affected.
```

## Tests

```bash
.venv/Scripts/python -m pytest -q          # 226 tests; uses the real DB (throwaway rows), fakes all paid APIs
```

## Useful scripts

| Script | What it does | Cost |
| --- | --- | --- |
| `scripts/dev_run.py "<objective>"` | Runs a research run from the terminal with DEV limits (`--full` for 10 leads, `--refresh` to re-research) | ≤ $0.30 Claude + ≤ $0.05 Apify (DEV) |
| `scripts/resume_run.py <run_id> find_new\|refresh_same` | Continues a run paused at the repeat gate | as a run |
| `scripts/transcript_cost.py <session_id> [--record <run_id>]` | Recovers a killed session's true cost from Claude Code transcripts | free |
| `evals/ab.py export\|estimate\|reference\|run\|report` | One-off model A/B (docs/specs.md §9; Haiku 4.5, Sonnet 5, and Opus 5.5 on qualification + copywriting); refuses without `--yes` and above $1.20 | ≤ $1.20 total |
| `spikes/sdk_spike.py` | The SDK behaviour check run before building (tools, subagents, skills, hooks, cost) | ≤ $0.10 |

## Deploy (Render free)

1. Push this repo to GitHub (private is fine).
2. Render → New → Blueprint → pick the repo (`render.yaml`), or a Docker web service.
3. Set the secret env vars in the Render dashboard:
   - `SUPABASE_DB_DSN` is the **app-role** DSN. Never set the admin DSN on Render.
   - Also: the API keys, `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY`, and `APP_BASE_URL` (your `https://….onrender.com`).
4. In Supabase → Authentication → URL configuration, add `https://….onrender.com/accept-invite` and `https://….onrender.com/reset-password` to the redirect URLs.
5. Deploy, then open `/health` (expects `"database": "ok"`). The full, ordered plan with checks and costs is in `docs/deployment.md`.

## Owner alerts (n8n → email)

Problems like "out of credit", "wrong key" or "a run failed" show up in the app under **System issues** (admins), with a
banner on every page. To also get an email:
1. In n8n: Import → `n8n/alert-email-workflow.json`. Set the Gmail credential, replace `REPLACE_WITH_YOUR_EMAIL`, and
   replace `REPLACE_WITH_ALERT_WEBHOOK_SECRET` with a random string. Activate the workflow.
2. Set `ALERT_WEBHOOK_URL` (the webhook's production URL) and `ALERT_WEBHOOK_SECRET` (the same string) in `.env` / Render.
3. System issues → **Send a test alert**.

The same problem within 30 minutes is counted, not re-sent. Client-facing messages never contain technical detail.

## Safety summary

- Nothing public except `/login`, `/accept-invite`, `/forgot-password`, `/reset-password` and `/health` (plus two test pages, only while `FIXTURE_MODE=true`).
- Supabase Auth logins, admin/member roles checked on the server, and CSRF tokens on every action.
- The app DB user has no DELETE, no DROP, and no access to other schemas. RLS is on.
- The agent has no web browsing, shell or file tools. Our tools enforce all limits from the run record.
- Scraped text and LinkedIn descriptions are redacted and injection-flagged; every piece of outside text in a prompt is fenced as untrusted data.
- Drafts are checked by code and a separate fact-checker; nothing is sent, and only approved drafts are exported.
- Every log line is filtered for emails, keys and passwords.
- The Claude budget is enforced from the spend ledger. Each phase has a watchdog. Cost is recovered even if a phase is killed.
