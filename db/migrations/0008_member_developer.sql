-- 0008_member_developer.sql — a developer flag, separate from the business role (specs D-90).
-- role = admin | member stays the business access (runs, team, spend). is_developer adds the technical view:
-- System issues, tool calls, raw error detail, models. Only the owner grants it; admins never see developers.
-- A developer is always an admin, enforced here so no app bug can create "developer but member".
alter table lead_agent.members add column if not exists is_developer boolean not null default false;

update lead_agent.members set is_developer = true where is_owner and not is_developer;

do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'members_developer_is_admin') then
    alter table lead_agent.members
      add constraint members_developer_is_admin check (not is_developer or role = 'admin');
  end if;
end $$;
