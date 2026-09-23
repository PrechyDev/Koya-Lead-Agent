-- 0003_auth_user_lookup.sql — one narrow window into auth.users for invites (E-39).
--
-- The app role has NO access to the auth schema. When an admin invites
-- someone who already has an account in this Supabase project (e.g. a Week 3/4
-- user), we need that user's id to add a lead_agent.members row. This
-- SECURITY DEFINER function returns ONLY the id for an exact email match —
-- no other columns, no listing — and only lead_agent_app may call it.

create or replace function lead_agent.auth_user_id_by_email(p_email text)
returns uuid
language sql
stable
security definer
set search_path = pg_catalog, auth
as $$
  select id from auth.users where lower(email) = lower(p_email) limit 1;
$$;

revoke all on function lead_agent.auth_user_id_by_email(text) from public;
grant execute on function lead_agent.auth_user_id_by_email(text) to lead_agent_app;
