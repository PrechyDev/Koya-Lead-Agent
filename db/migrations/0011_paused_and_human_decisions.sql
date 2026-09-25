-- 0011_paused_and_human_decisions.sql — runs can be paused and continued; a person can decide a "needs review"
-- lead (specs D-100).

-- "paused": the run stopped before finishing for a reason that isn't the objective's fault (server restart,
-- crash, time limit). Everything found is kept and the run can be continued from where it stopped.
alter table lead_agent.runs drop constraint if exists runs_status_check;
alter table lead_agent.runs add constraint runs_status_check check (status in (
  'queued', 'refining_icp', 'needs_clarification', 'awaiting_confirmation',
  'discovering', 'researching', 'drafting', 'finalizing',
  'completed', 'completed_partial', 'paused', 'failed', 'cancelled', 'superseded'));

-- A person's decision on a lead the AI couldn't settle ("needs review"): kept next to the AI's evidence.
alter table lead_agent.leads add column if not exists human_decision text
  check (human_decision is null or human_decision in ('qualified', 'not_qualified'));
alter table lead_agent.leads add column if not exists human_decision_by uuid references lead_agent.members(user_id);
alter table lead_agent.leads add column if not exists human_decision_at timestamptz;
alter table lead_agent.leads add column if not exists human_decision_note text;
