-- 0000_app_role.sql — schema + least-privilege grants for the app's DB user.
--
-- Run by scripts/create_app_role.py with the ADMIN (postgres) DSN, AFTER the
-- script has created the `lead_agent_app` login role. The role's password is
-- never in this file (the owner puts it in .env; the script only reads it).
--
-- What lead_agent_app can do: enter the lead_agent schema; SELECT / INSERT /
-- UPDATE its tables; use its sequences.
-- What it can't do: DELETE / TRUNCATE / DROP / CREATE anything, or touch any
-- other schema (Week 3 / Week 4 data lives in the same project).
--
-- Safe to re-run: every statement is idempotent.

create schema if not exists lead_agent;

grant usage on schema lead_agent to lead_agent_app;

-- Tables/sequences that already exist (none on first run; covers re-runs).
grant select, insert, update on all tables in schema lead_agent to lead_agent_app;
grant usage, select on all sequences in schema lead_agent to lead_agent_app;

-- Tables/sequences created LATER by postgres (i.e. by our migrations) get the
-- same rights automatically.
alter default privileges for role postgres in schema lead_agent
  grant select, insert, update on tables to lead_agent_app;
alter default privileges for role postgres in schema lead_agent
  grant usage, select on sequences to lead_agent_app;

-- Note on RLS: every lead_agent table (0001+) enables RLS and adds a policy
-- `for all to lead_agent_app using (true) with check (true)`. Without that
-- policy this role would see zero rows (only postgres bypasses RLS). The
-- policy says "all", but DELETE is still impossible: the grant above never
-- includes it.
