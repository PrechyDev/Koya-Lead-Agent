"""Free, code-only checks that make discovery find better companies (D-98). Pure functions, no I/O.

Seen in live run 2553f20d (2026-09-25): a vague objective became the search words "B2B SaaS", LinkedIn matched them
in company NAMES ("SaaS Bro", "B2B Rizz"), and 13 of 15 companies weren't the SaaS companies asked for. So:
- search terms must name what the companies sell (`generic_queries`),
- a software target also filters on LinkedIn's "Software Development" industry (`industry_ids_for`),
- the triage's "unlikely" must quote the company's own words (`quote_is_in`).
Agencies and consultancies are Koya's primary customers, so nothing here rejects a company for being one (D-102):
only the triage judges a company, and only against the objective.
"""

import re

SOFTWARE_DEVELOPMENT_INDUSTRY_ID = "4"  # LinkedIn "Software Development" (the Apify actor's own example id)

# Words that name a category, not what a company sells. A search made only of these returns noise.
GENERIC_WORDS = {
    "saas", "b2b", "b2c", "software", "company", "companies", "startup", "startups", "platform", "platforms", "tech",
    "technology", "operations", "operational", "ops", "automation", "automate", "service", "services", "business",
    "businesses", "firm", "firms", "solution", "solutions", "digital", "cloud", "app", "apps", "online", "small",
    "smb", "mid", "market", "sized", "growing", "scaling", "us", "usa", "a", "an", "the", "and", "of", "for", "as",
    "in", "with", "that", "might", "need", "needs", "help", "provider", "providers",
}

SERVICE_WANTED = re.compile(r"\b(agenc\w*|consult\w*|services? (?:firms?|compan\w*|providers?)|studios?|outsourc\w*|"
                            r"staffing|recruit\w*|dev(?:elopment)? shops?|freelanc\w*)\b", re.I)
SOFTWARE_TARGET = re.compile(r"\b(saas|software|apps?|platforms?)\b", re.I)


def _target_text(icp: dict) -> str:
    return " ".join([str(icp.get("target_company_type") or ""), *(str(i) for i in icp.get("industries") or [])])


def generic_queries(queries: list[str]) -> list[str]:
    """The search terms that name no product, niche or customer, e.g. "B2B SaaS operations platform"."""
    return [q for q in queries if not [w for w in re.findall(r"[a-z0-9]+", str(q).lower()) if w not in GENERIC_WORDS]]


def wants_service_firms(icp: dict) -> bool:
    """The objective itself is about agencies, consultancies, studios, staffing… (then services aren't rejected)."""
    return bool(SERVICE_WANTED.search(_target_text(icp)))


def industry_ids_for(icp: dict) -> list[str]:
    """LinkedIn industry filter for the search: "Software Development" for a software target, else none."""
    if SOFTWARE_TARGET.search(_target_text(icp)) and not wants_service_firms(icp):
        return [SOFTWARE_DEVELOPMENT_INDUSTRY_ID]
    return []


def quote_is_in(quote: str | None, company: dict) -> bool:
    """A triage "unlikely" must quote the company's own words (checked here, so the model can't invent a reason)."""
    if not quote or len(quote.strip()) < 4:
        return False
    norm = lambda s: re.sub(r"\s+", " ", str(s)).strip().lower()  # noqa: E731
    text = norm(" ".join([str(company.get("name") or ""), str(company.get("tagline") or ""),
                          str(company.get("description") or ""), *(str(s) for s in company.get("specialities") or []),
                          *(str(i) for i in company.get("industries") or [])]))
    return norm(quote).strip("\"'“” ") in text
