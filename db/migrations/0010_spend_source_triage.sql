-- 0010_spend_source_triage.sql — record the candidate-triage call honestly in the ledger (specs D-98).
alter table lead_agent.spend_ledger drop constraint if exists spend_ledger_source_check;
alter table lead_agent.spend_ledger add constraint spend_ledger_source_check
  check (source in ('run', 'icp', 'grounding', 'eval', 'spike', 'preflight', 'triage'));
