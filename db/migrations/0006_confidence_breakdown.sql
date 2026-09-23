-- 0006_confidence_breakdown.sql — the fit score is computed by code (D-48); store its itemised breakdown.
alter table lead_agent.leads add column if not exists confidence_breakdown jsonb not null default '[]'::jsonb;
