-- 0001_lead_agent_schema.sql — all tables, constraints, RLS policies and views
-- for the lead agent (specs.md §6). Applied by scripts/migrate.py with the
-- ADMIN DSN. Tables are owned by postgres; lead_agent_app gets
-- SELECT/INSERT/UPDATE automatically through 0000's default privileges.

-- ---------------------------------------------------------------------------
-- Shared trigger: keep updated_at current.
-- ---------------------------------------------------------------------------
create or replace function lead_agent.touch_updated_at()
returns trigger language plpgsql as $$
begin
  new.updated_at := now();
  return new;
end;
$$;

-- ---------------------------------------------------------------------------
-- members — who can use THIS app (login accounts live in auth.users).
-- No trigger on auth.users: only the Week 5 invite flow creates members.
-- ---------------------------------------------------------------------------
create table if not exists lead_agent.members (
  user_id     uuid primary key references auth.users(id) on delete cascade,
  email       text not null,
  full_name   text not null check (length(trim(full_name)) between 1 and 120),
  role        text not null default 'member' check (role in ('admin', 'member')),
  is_active   boolean not null default true,
  is_owner    boolean not null default false,
  invited_by  uuid references lead_agent.members(user_id),
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);
create unique index if not exists members_email_lower_idx on lead_agent.members (lower(email));

drop trigger if exists members_touch on lead_agent.members;
create trigger members_touch before update on lead_agent.members
  for each row execute function lead_agent.touch_updated_at();

-- The owner can't be demoted/deactivated, and the last active admin can't be
-- removed — enforced in the database so no app bug can lock the team out.
create or replace function lead_agent.protect_admins()
returns trigger language plpgsql as $$
begin
  if old.is_owner and (new.role <> 'admin' or not new.is_active or not new.is_owner) then
    raise exception 'The owner account cannot be demoted or deactivated.'
      using errcode = 'check_violation';
  end if;
  if old.role = 'admin' and old.is_active and (new.role <> 'admin' or not new.is_active) then
    if not exists (
      select 1 from lead_agent.members
      where role = 'admin' and is_active and user_id <> old.user_id
    ) then
      raise exception 'At least one active admin must remain.'
        using errcode = 'check_violation';
    end if;
  end if;
  return new;
end;
$$;

drop trigger if exists members_protect_admins on lead_agent.members;
create trigger members_protect_admins before update on lead_agent.members
  for each row execute function lead_agent.protect_admins();

-- ---------------------------------------------------------------------------
-- runs — one research run (objective → ICP → leads → drafts).
-- ---------------------------------------------------------------------------
create table if not exists lead_agent.runs (
  id                      uuid primary key default gen_random_uuid(),
  idempotency_key         text not null unique,
  parent_run_id           uuid references lead_agent.runs(id),
  run_kind                text not null default 'app' check (run_kind in ('app', 'dev', 'eval_record')),
  created_by              uuid references lead_agent.members(user_id),
  objective               text not null check (length(trim(objective)) between 5 and 1000),
  objective_hash          text not null,
  icp                     jsonb,
  icp_assumptions         jsonb not null default '[]'::jsonb,
  icp_signature           text,
  clarification_question  text,
  duplicate_of_run_id     uuid references lead_agent.runs(id),
  repeat_choice           text check (repeat_choice in ('open_previous', 'find_new', 'refresh_same', 'cancel')),
  cross_run_dedupe        boolean not null default true,
  limits                  jsonb not null,
  usage                   jsonb not null default '{}'::jsonb,
  models                  jsonb not null default '{}'::jsonb,
  status                  text not null default 'queued' check (status in (
                            'queued', 'refining_icp', 'needs_clarification', 'awaiting_confirmation',
                            'discovering', 'researching', 'drafting', 'finalizing',
                            'completed', 'completed_partial', 'failed', 'cancelled', 'superseded')),
  status_detail           text,
  error_message           text,
  summary                 text,
  shortfall_reason        text,
  quality_scorecard       jsonb,
  cost_usd                numeric(10, 4) not null default 0,
  num_turns               integer not null default 0,
  tool_call_count         integer not null default 0,
  created_at              timestamptz not null default now(),
  started_at              timestamptz,
  finished_at             timestamptz,
  updated_at              timestamptz not null default now()
);
create index if not exists runs_created_at_idx on lead_agent.runs (created_at desc);
create index if not exists runs_objective_hash_idx on lead_agent.runs (objective_hash);
create index if not exists runs_icp_signature_idx on lead_agent.runs (icp_signature);
create index if not exists runs_status_idx on lead_agent.runs (status);
create index if not exists runs_created_by_idx on lead_agent.runs (created_by, created_at desc);

drop trigger if exists runs_touch on lead_agent.runs;
create trigger runs_touch before update on lead_agent.runs
  for each row execute function lead_agent.touch_updated_at();

-- ---------------------------------------------------------------------------
-- leads — one company within one run.
-- ---------------------------------------------------------------------------
create table if not exists lead_agent.leads (
  id                    uuid primary key default gen_random_uuid(),
  run_id                uuid not null references lead_agent.runs(id),
  company_name          text not null,
  company_domain        text not null,
  linkedin_url          text,
  discovery_data        jsonb not null default '{}'::jsonb,
  prescreen_result      text,
  qualification_status  text not null default 'pending'
                          check (qualification_status in ('pending', 'qualified', 'not_qualified', 'needs_review')),
  confidence            numeric(3, 2) check (confidence between 0 and 1),
  hard_filter_checks    jsonb not null default '[]'::jsonb,
  fit_reasons           jsonb not null default '[]'::jsonb,
  concerns              jsonb not null default '[]'::jsonb,
  source_urls           text[] not null default '{}',
  source_summary        text,
  email_sequence        jsonb,
  linkedin_message      text check (linkedin_message is null or length(linkedin_message) <= 300),
  outreach_status       text not null default 'not_drafted'
                          check (outreach_status in ('not_drafted', 'drafted', 'failed_grounding')),
  outreach_attempts     integer not null default 0,
  grounding_report      jsonb,
  review_status         text not null default 'pending_review'
                          check (review_status in ('pending_review', 'approved', 'rejected')),
  reviewer_note         text,
  reviewed_by           uuid references lead_agent.members(user_id),
  reviewed_at           timestamptz,
  created_at            timestamptz not null default now(),
  updated_at            timestamptz not null default now(),
  unique (run_id, company_domain)
);
create index if not exists leads_run_idx on lead_agent.leads (run_id);
create index if not exists leads_domain_idx on lead_agent.leads (company_domain, created_at desc);

drop trigger if exists leads_touch on lead_agent.leads;
create trigger leads_touch before update on lead_agent.leads
  for each row execute function lead_agent.touch_updated_at();

-- ---------------------------------------------------------------------------
-- tool_calls — every tool call, skill load, delegation and denial.
-- ---------------------------------------------------------------------------
create table if not exists lead_agent.tool_calls (
  id                 uuid primary key default gen_random_uuid(),
  run_id             uuid not null references lead_agent.runs(id),
  seq                integer not null,
  agent_role         text not null,
  tool_name          text not null,
  purpose            text,
  input_summary      text,
  result_summary     text,
  status             text not null default 'running'
                       check (status in ('running', 'success', 'error', 'blocked')),
  error_message      text,
  duration_ms        integer,
  external_cost_usd  numeric(10, 5),
  created_at         timestamptz not null default now(),
  finished_at        timestamptz,
  unique (run_id, seq)
);
create index if not exists tool_calls_run_idx on lead_agent.tool_calls (run_id, seq);

-- ---------------------------------------------------------------------------
-- scrape_cache — sanitized page content, reused for SCRAPE_CACHE_DAYS.
-- ---------------------------------------------------------------------------
create table if not exists lead_agent.scrape_cache (
  url              text primary key,
  domain           text not null,
  final_url        text,
  title            text,
  content          text not null default '',
  truncated        boolean not null default false,
  status_code      integer,
  injection_flags  jsonb not null default '[]'::jsonb,
  fetched_at       timestamptz not null default now()
);
create index if not exists scrape_cache_domain_idx on lead_agent.scrape_cache (domain);

-- ---------------------------------------------------------------------------
-- spend_ledger — every Claude call's cost; the global budget guard reads it.
-- ---------------------------------------------------------------------------
create table if not exists lead_agent.spend_ledger (
  id             uuid primary key default gen_random_uuid(),
  source         text not null check (source in ('run', 'icp', 'grounding', 'eval', 'spike')),
  ref_id         uuid,
  model          text,
  input_tokens   integer,
  output_tokens  integer,
  cost_usd       numeric(10, 5) not null check (cost_usd >= 0),
  note           text,
  created_at     timestamptz not null default now()
);
create index if not exists spend_ledger_created_idx on lead_agent.spend_ledger (created_at);

-- ---------------------------------------------------------------------------
-- eval_results — the one-off model A/B test's evidence (specs.md §9).
-- ---------------------------------------------------------------------------
create table if not exists lead_agent.eval_results (
  id          uuid primary key default gen_random_uuid(),
  eval_name   text not null,
  stage       text not null,
  model       text not null,
  repeat_no   integer not null,
  case_id     text not null,
  expected    jsonb,
  actual      jsonb,
  passed      boolean,
  score       numeric,
  cost_usd    numeric(10, 5),
  latency_ms  integer,
  notes       text,
  created_at  timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- Row-level security: on everywhere; one policy per table for the app role
-- only. anon/authenticated (Supabase's public API roles) get nothing.
-- DELETE stays impossible for the app role because it has no DELETE grant.
-- ---------------------------------------------------------------------------
do $$
declare t text;
begin
  foreach t in array array['members', 'runs', 'leads', 'tool_calls', 'scrape_cache', 'spend_ledger', 'eval_results']
  loop
    execute format('alter table lead_agent.%I enable row level security', t);
    execute format('drop policy if exists app_access on lead_agent.%I', t);
    execute format(
      'create policy app_access on lead_agent.%I for all to lead_agent_app using (true) with check (true)', t);
  end loop;
end;
$$;

-- ---------------------------------------------------------------------------
-- Views for review & submission evidence.
-- ---------------------------------------------------------------------------
create or replace view lead_agent.qualified_leads_v as
select
  r.id as run_id, r.objective, r.created_at as run_created_at,
  l.id as lead_id, l.company_name, l.company_domain, l.confidence,
  l.fit_reasons, l.concerns, l.source_urls, l.source_summary, l.hard_filter_checks,
  l.email_sequence, l.linkedin_message, l.outreach_status, l.review_status,
  m.full_name as reviewed_by_name, l.reviewed_at
from lead_agent.leads l
join lead_agent.runs r on r.id = l.run_id
left join lead_agent.members m on m.user_id = l.reviewed_by
where l.qualification_status = 'qualified';

create or replace view lead_agent.run_audit_v as
select
  r.id as run_id, r.objective, r.status, r.created_at, r.cost_usd, r.num_turns,
  t.tool_name, t.status as call_status, count(*) as calls,
  sum(coalesce(t.duration_ms, 0)) as total_ms
from lead_agent.runs r
join lead_agent.tool_calls t on t.run_id = r.id
group by r.id, r.objective, r.status, r.created_at, r.cost_usd, r.num_turns, t.tool_name, t.status;

create or replace view lead_agent.spend_summary_v as
select source, coalesce(model, '(n/a)') as model, count(*) as entries,
       sum(cost_usd) as cost_usd, min(created_at) as first_at, max(created_at) as last_at
from lead_agent.spend_ledger
group by source, coalesce(model, '(n/a)');

grant select on lead_agent.qualified_leads_v, lead_agent.run_audit_v, lead_agent.spend_summary_v to lead_agent_app;
