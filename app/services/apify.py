"""Company discovery through ONE pinned Apify actor (specs.md §8.2; E-07, E-08, E-11).

Actor: harvestapi/linkedin-company-search (pay-per-event: ~$0.004 per full
company + $0.001 per run start). Every call has a hard item cap AND Apify's
own max_total_charge_usd, and the actor's run_timeout stops it on Apify's side.

Failure policy (PRD: "if a run fails or behaves oddly, stop and ask before
re-running"): a failed/timed-out actor run is NEVER re-run automatically.
The only retry is when the start request itself failed before a run existed.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from apify_client import ApifyClientAsync
from apify_client.errors import ApifyApiError

from app.config import get_settings
from app.lib.domain import normalize_domain
from app.lib.sanitize import redact, redact_obj

# LinkedIn's fixed headcount bands (the actor's `companySize` enum).
SIZE_BANDS: list[tuple[str, int, int | None]] = [
    ("1-10", 1, 10), ("11-50", 11, 50), ("51-200", 51, 200), ("201-500", 201, 500),
    ("501-1000", 501, 1000), ("1001-5000", 1001, 5000), ("5001-10000", 5001, 10000), ("10001+", 10001, None),
]
LOCATION_NAMES = {"us": "United States", "gb": "United Kingdom", "ca": "Canada", "de": "Germany",
                  "fr": "France", "au": "Australia", "ng": "Nigeria", "in": "India", "ie": "Ireland",
                  "nl": "Netherlands"}


class DiscoveryError(Exception):
    def __init__(self, code: str, message: str, apify_run_id: str | None = None):
        super().__init__(message)
        self.code, self.message, self.apify_run_id = code, message, apify_run_id


@dataclass
class DiscoveryResult:
    companies: list[dict]
    raw_count: int
    dropped_no_domain: int
    apify_run_id: str | None
    cost_usd: float
    actor_input: dict = field(default_factory=dict)


def size_bands_for(low: int | None, high: int | None) -> list[str]:
    """LinkedIn bands that overlap [low, high]. A band touching only at one end (1-10 vs 10-100) is skipped."""
    if low is None and high is None:
        return []
    bands = []
    for name, b_low, b_high in SIZE_BANDS:
        top = b_high if b_high is not None else 10**9
        lo = low if low is not None else 0
        hi = high if high is not None else 10**9
        if b_low <= hi and top >= lo and not (top == lo and b_low < lo):
            bands.append(name)
    return bands


def location_names(geos: list[str]) -> list[str]:
    return [LOCATION_NAMES.get(g, g.title()) for g in geos if g]


def normalize_company(item: dict) -> dict | None:
    """Keep only what we need, redacted. None if the company has no usable website domain (E-09)."""
    domain = normalize_domain(item.get("website"))
    if not domain:
        return None
    hq = None
    for loc in item.get("locations") or []:
        if loc.get("headquarter"):
            parsed = loc.get("parsed") or {}
            hq = {"city": loc.get("city"), "state": parsed.get("state") or loc.get("geographicArea"),
                  "country_code": (parsed.get("countryCode") or loc.get("country") or "").upper() or None,
                  "text": parsed.get("text")}
            break
    description = redact((item.get("description") or "")[:800])[0]
    return {
        "name": (item.get("name") or domain).strip(),
        "domain": domain,
        "website": item.get("website"),
        "linkedin_url": item.get("linkedinUrl"),
        "tagline": redact(item.get("tagline") or "")[0],
        "description": description,
        "industries": [i.get("name") for i in (item.get("industries") or []) if i.get("name")],
        "specialities": (item.get("specialities") or [])[:8],
        "employee_count_linkedin": item.get("employeeCount"),
        "employee_count_range": item.get("employeeCountRange"),
        "hq": hq,
        "founded_year": (item.get("foundedOn") or {}).get("year"),
        # Raw fields the pre-screen reads (redacted copies).
        "locations": redact_obj(item.get("locations") or []),
        "employeeCountRange": item.get("employeeCountRange"),
        "employeeCount": item.get("employeeCount"),
    }


async def find_companies(
    *, query: str, geos: list[str], size_bands: list[str], max_items: int, max_charge_usd: float,
    start_page: int = 1, timeout_s: int = 180,
) -> DiscoveryResult:
    settings = get_settings()
    if max_items <= 0:
        raise DiscoveryError("no_budget", "No candidate slots left for this run.")
    actor_input = {
        "scraperMode": "full",
        "searchQuery": query[:200],
        "maxItems": int(max_items),
        "startPage": max(1, int(start_page)),
    }
    if geos:
        actor_input["locations"] = location_names(geos)
    if size_bands:
        actor_input["companySize"] = size_bands

    client = ApifyClientAsync(settings.apify_token)
    actor = client.actor(settings.apify_actor_id)
    run = None
    for attempt in range(2):
        try:
            run = await asyncio.wait_for(
                actor.call(
                    run_input=actor_input,
                    max_items=int(max_items),
                    max_total_charge_usd=Decimal(str(max_charge_usd)),
                    run_timeout=timedelta(seconds=timeout_s),
                    logger=None,
                ),
                timeout=timeout_s + 60,
            )
            break
        except ApifyApiError as exc:
            status = getattr(exc, "status_code", None)
            if status in (401, 403):
                raise DiscoveryError("auth", "Apify rejected the token (check APIFY_TOKEN is the team token).") from exc
            if attempt == 0 and (status is None or status >= 500):
                await asyncio.sleep(2)  # start request failed before any run existed: safe to retry once
                continue
            raise DiscoveryError("apify_error", f"Apify API error ({status}).") from exc
        except TimeoutError as exc:
            raise DiscoveryError("timeout", "Apify did not answer in time. Check the Apify console before re-running.") from exc

    if run is None:
        raise DiscoveryError("apify_error", "Apify returned no run.")
    r = run if isinstance(run, dict) else run.model_dump(by_alias=True)
    run_id, status = r.get("id"), r.get("status")
    cost = float(r.get("usageTotalUsd") or 0)
    if status != "SUCCEEDED":
        # Never re-run automatically (PRD). Tell the human where to look.
        raise DiscoveryError(
            "actor_" + str(status or "unknown").lower(),
            f"Apify run {run_id} ended {status}. Check it in the Apify console before re-running.",
            apify_run_id=run_id,
        )
    listing = await client.dataset(r["defaultDatasetId"]).list_items(limit=int(max_items))
    raw = listing.items or []
    companies, dropped = [], 0
    for item in raw[: int(max_items)]:
        company = normalize_company(item)
        if company is None:
            dropped += 1
        else:
            companies.append(company)
    return DiscoveryResult(companies=companies, raw_count=len(raw), dropped_no_domain=dropped,
                           apify_run_id=run_id, cost_usd=cost, actor_input=actor_input)
