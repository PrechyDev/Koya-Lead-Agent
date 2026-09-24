-- 0005_disqualifiers_soft_tools.sql — per-company disqualifier checks, soft-preference evidence and
-- tool fingerprints detected in page HTML (specs.md §8.3, decisions D-35, D-36, D-36b).

alter table lead_agent.leads add column if not exists disqualifier_checks jsonb not null default '[]'::jsonb;
alter table lead_agent.leads add column if not exists soft_preference_checks jsonb not null default '[]'::jsonb;
alter table lead_agent.leads add column if not exists tools_detected jsonb not null default '[]'::jsonb;

alter table lead_agent.scrape_cache add column if not exists tools_detected jsonb not null default '[]'::jsonb;
