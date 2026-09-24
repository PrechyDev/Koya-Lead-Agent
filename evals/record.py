"""Record the companies the model A/B replays (specs.md §9 step 1). Dev-only; never part of the app.

Finds companies with Apify and scrapes each homepage with Firecrawl, through the SAME service functions the
agent uses (redaction, injection flags, caps), then writes evals/fixtures/<name>.json in the format ab.py reads.
No Claude calls: the models are compared later, on this fixed data, by `evals/ab.py`.

    python evals/record.py --name prd_example --max 2 --yes      # rule 9: test with 1-2 results first
    python evals/record.py --name prd_example --max 12 --yes     # the real recording (re-uses cached pages)
    --query-index 1 searches the ICP's second query instead (LinkedIn search can return 0 for a query)

SPENDS: Apify (capped by --max-charge, default $0.25 per actor run) + one Firecrawl credit per new page.
"""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.lib.icp_defaults import apply_defaults  # noqa: E402
from app.lib.objective import parse_headcount_range  # noqa: E402
from app.lib.qualification_rules import prescreen  # noqa: E402
from app.logging_setup import configure_logging  # noqa: E402
from app.services import apify as apify_svc  # noqa: E402
from app.services import firecrawl as fc  # noqa: E402

log = logging.getLogger("lead_agent.evals.record")
FIXTURES = ROOT / "evals" / "fixtures"

# The PRD's example objective, refined the way save_icp does it today (Koya defaults, D-58/D-66), so the A/B
# grades models against the current rules.
OBJECTIVE = "Find US B2B SaaS companies with 10 to 100 employees that may need AI automation support"
ICP = apply_defaults({
    "target_company_type": "B2B SaaS company",
    "industries": ["B2B SaaS"],
    "geography": ["United States"],
    "headcount_range": "10-100",
    "hard_filters": ["Headquartered in the United States", "Sells software to businesses (B2B SaaS)",
                     "10-100 employees"],
    "disqualifiers": ["Consumer (B2C) product"],
    "discovery_query_plan": ["B2B SaaS operations platform", "workflow automation software company"],
    "assumptions": [],
    "user_constraints_preserved": ["US", "B2B SaaS", "10 to 100 employees", "may need AI automation support"],
})[0]


async def record(name: str, max_items: int, max_charge: float, query_index: int) -> None:
    query = ICP["discovery_query_plan"][query_index]
    low, high = parse_headcount_range(ICP["headcount_range"])
    result = await apify_svc.find_companies(query=query, geos=ICP["geography"],
                                            size_bands=apify_svc.size_bands_for(low, high),
                                            max_items=max_items, max_charge_usd=max_charge)
    log.info("query %r", query)
    log.info("Apify run %s: %d companies (%d without a website), cost $%.4f", result.apify_run_id,
             len(result.companies), result.dropped_no_domain, result.cost_usd)

    companies, credits = [], 0
    for c in result.companies:
        screen, reason = prescreen(c, ICP)  # kept either way: the A/B needs fits AND misfits
        url = f"https://{c['domain']}/"
        cached = db.get_cached_page(url, get_settings().scrape_cache_days)
        if cached and cached["content"]:
            content = cached["content"]
        else:
            try:
                page = await fc.scrape(url)
            except fc.ScrapeError as exc:
                log.info("  %-28s skipped: %s", c["domain"], exc.message)
                if exc.failure_code:  # out of credit / bad key: stop, don't burn more
                    raise
                credits += 1
                continue
            credits += 1
            db.put_cached_page(url=url, domain=c["domain"], final_url=page.final_url, title=page.title,
                               content=page.content, truncated=page.truncated, status_code=page.status_code,
                               injection_flags=page.injection_flags, tools_detected=page.tools,
                               links=page.links, parked=page.parked)
            content = page.content
        log.info("  %-28s %-22s %s", c["domain"], screen, reason[:70])
        companies.append({"domain": c["domain"], "name": c["name"], "linkedin_url": c.get("linkedin_url"),
                          "discovery": c, "pages": [{"url": url, "content": content}],
                          "prescreen": screen, "source_run": f"apify:{result.apify_run_id}"})

    # A recording only ADDS companies (by domain): an empty or smaller search never wipes what was paid for.
    FIXTURES.mkdir(parents=True, exist_ok=True)
    out = FIXTURES / f"{name}.json"
    previous = json.loads(out.read_text(encoding="utf-8"))["companies"] if out.exists() else []
    merged = {c["domain"]: c for c in previous} | {c["domain"]: c for c in db.to_jsonable(companies)}
    out.write_text(json.dumps({"objective": OBJECTIVE, "icp": ICP, "companies": list(merged.values())}, indent=1),
                   encoding="utf-8")
    log.info("wrote %s: %d new, %d in total; Firecrawl credits used: %d; Apify $%.4f",
             out, len(companies), len(merged), credits, result.cost_usd)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--name", required=True)
    parser.add_argument("--max", type=int, default=2, help="companies to fetch from Apify (default 2)")
    parser.add_argument("--max-charge", type=float, default=0.25, help="Apify cost cap for the actor run (USD)")
    parser.add_argument("--query-index", type=int, default=0, choices=range(len(ICP["discovery_query_plan"])),
                        help="which ICP discovery query to search (LinkedIn search is variable; errors log #10)")
    parser.add_argument("--yes", action="store_true", help="confirm the spend")
    args = parser.parse_args()
    if not 1 <= args.max <= 12:
        raise SystemExit("--max must be 1-12 (the first-pool size).")
    if not args.yes:
        raise SystemExit(f"This spends: Apify up to ${args.max_charge:.2f} and up to {args.max} Firecrawl credits. "
                         "Add --yes to go ahead.")
    asyncio.run(record(args.name, args.max, args.max_charge, args.query_index))
    return 0


if __name__ == "__main__":
    configure_logging(cli=True)
    sys.exit(main())
