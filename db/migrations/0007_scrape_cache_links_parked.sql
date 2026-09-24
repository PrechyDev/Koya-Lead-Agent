-- 0007_scrape_cache_links_parked.sql — a cached homepage must give the researcher the same facts as a fresh
-- scrape: its internal links (which pages it may open next) and whether the domain looks parked / for sale.
-- Before this, a cache hit returned no links and parked=false (final audit, errors log #41).
alter table lead_agent.scrape_cache add column if not exists links jsonb not null default '[]'::jsonb;
alter table lead_agent.scrape_cache add column if not exists parked boolean not null default false;
