-- 0002_lead_fetched_urls.sql — every URL our tools actually fetched/returned for
-- a lead (discovery LinkedIn URL + website, and each scraped page's final URL).
-- save_qualification only accepts source_urls from this list (E-16).
alter table lead_agent.leads add column if not exists fetched_urls text[] not null default '{}';
alter table lead_agent.leads add column if not exists prescreen_reason text;
