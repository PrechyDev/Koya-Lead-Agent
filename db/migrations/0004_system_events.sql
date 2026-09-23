-- 0004_system_events.sql — owner alerts, admin-only error detail, request type, preflight spend.

-- Alerts for admins (in-app banner + optional webhook to n8n → email).
create table if not exists lead_agent.system_events (
  id               uuid primary key default gen_random_uuid(),
  severity         text not null check (severity in ('info', 'warning', 'critical')),
  service          text not null,                 -- anthropic | apify | firecrawl | database | budget | app
  code             text not null,                 -- e.g. apify_no_credit
  message          text not null,                 -- admin-facing: what happened + how to fix
  run_id           uuid references lead_agent.runs(id),
  occurrences      integer not null default 1,
  first_seen_at    timestamptz not null default now(),
  last_seen_at     timestamptz not null default now(),
  notified_at      timestamptz,
  resolved_at      timestamptz,
  resolved_by      uuid references lead_agent.members(user_id)
);
create index if not exists system_events_open_idx on lead_agent.system_events (resolved_at, last_seen_at desc);
alter table lead_agent.system_events enable row level security;
drop policy if exists app_access on lead_agent.system_events;
create policy app_access on lead_agent.system_events for all to lead_agent_app using (true) with check (true);

-- Runs: what kind of request the objective was, and technical detail for admins only.
alter table lead_agent.runs add column if not exists request_type text
  check (request_type is null or request_type in ('lead_search', 'question', 'unrelated', 'too_vague'));
alter table lead_agent.runs add column if not exists error_detail text;

-- Spend ledger: allow the tiny pre-run credit probe to be recorded honestly.
alter table lead_agent.spend_ledger drop constraint if exists spend_ledger_source_check;
alter table lead_agent.spend_ledger add constraint spend_ledger_source_check
  check (source in ('run', 'icp', 'grounding', 'eval', 'spike', 'preflight'));
