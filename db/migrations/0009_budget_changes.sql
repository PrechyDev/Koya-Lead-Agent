-- 0009_budget_changes.sql — the Claude budget lives in the database, changed by a developer in the app (specs D-94).
-- Append-only: every change is a new row (who, when, old -> new, why, and that the Anthropic account was confirmed
-- to have credit). The current budget is the newest row; with no rows, CLAUDE_BUDGET_TOTAL_USD is the starting value.
-- The app role has no DELETE, so the history can't be erased from the app.
create table if not exists lead_agent.budget_changes (
  id                uuid primary key default gen_random_uuid(),
  changed_at        timestamptz not null default now(),
  changed_by        uuid not null references lead_agent.members(user_id),
  old_usd           numeric(10, 2) not null,
  new_usd           numeric(10, 2) not null check (new_usd > 0 and new_usd <= 1000),
  reason            text not null check (length(btrim(reason)) >= 5),
  credit_confirmed  boolean not null check (credit_confirmed)  -- the developer ticked "Anthropic has credit for this"
);
create index if not exists budget_changes_at_idx on lead_agent.budget_changes (changed_at desc);
alter table lead_agent.budget_changes enable row level security;
drop policy if exists app_access on lead_agent.budget_changes;
create policy app_access on lead_agent.budget_changes for all to lead_agent_app using (true) with check (true);
