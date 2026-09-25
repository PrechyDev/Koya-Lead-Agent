"""Company discovery through ONE pinned Apify actor (specs.md §8.2; E-07, E-08, E-11).

Actor: harvestapi/linkedin-company-search (pay-per-event: ~$0.004 per full
company + $0.001 per run start). Every call has a hard item cap AND Apify's
own max_total_charge_usd, and the actor's run_timeout stops it on Apify's side.

Failure policy (PRD: "if a run fails or behaves oddly, stop and ask before
re-running"): a failed/timed-out actor run is NEVER re-run automatically.
The only retry is when the start request itself failed before a run existed.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

import pycountry
from apify_client import ApifyClientAsync
from apify_client.errors import ApifyApiError

from app.config import get_settings
from app.failures import classify_apify
from app.lib.domain import normalize_domain
from app.lib.objective import country_code
from app.lib.sanitize import find_injection_flags, redact

# LinkedIn's fixed headcount bands (the actor's `companySize` enum).
SIZE_BANDS: list[tuple[str, int, int | None]] = [
    ("1-10", 1, 10), ("11-50", 11, 50), ("51-200", 51, 200), ("201-500", 201, 500),
    ("501-1000", 501, 1000), ("1001-5000", 1001, 5000), ("5001-10000", 5001, 10000), ("10001+", 10001, None),
]
LOCATION_NAMES = {"us": "United States", "gb": "United Kingdom", "ca": "Canada", "de": "Germany",
                  "fr": "France", "au": "Australia", "ng": "Nigeria", "in": "India", "ie": "Ireland",
                  "nl": "Netherlands"}


log = logging.getLogger("lead_agent.apify")


class DiscoveryError(Exception):
    """`failure_code` is a key of app.failures.CATALOGUE (None for 'no slots left', which isn't a failure)."""

    def __init__(self, code: str, message: str, apify_run_id: str | None = None, failure_code: str | None = None,
                 cost_usd: float | None = None):
        super().__init__(message)
        self.code, self.message, self.apify_run_id, self.failure_code = code, message, apify_run_id, failure_code
        self.cost_usd = cost_usd  # what a run that DID start was billed, so failed runs are costed too


@dataclass
class DiscoveryResult:
    companies: list[dict]
    raw_count: int
    dropped_no_domain: int
    apify_run_id: str | None
    cost_usd: float
    empty_retries: int = 0                                  # identical re-runs after an empty result
    apify_run_ids: list[str] = field(default_factory=list)  # every actor run behind this search


def size_bands_for(low: int | None, high: int | None) -> list[str]:
    """LinkedIn bands that overlap [low, high]. A band that only touches the range at one end (1-10 for 10-100,
    or 51-200 for 10-51) is skipped, unless nothing else is left (an exact size like 10-10 keeps its band)."""
    if low is None and high is None:
        return []
    lo = low if low is not None else 0
    hi = high if high is not None else 10**9
    overlapping, touching = [], []
    for name, b_low, b_high in SIZE_BANDS:
        top = b_high if b_high is not None else 10**9
        if b_low > hi or top < lo:
            continue
        only_touches = (top == lo and b_low < lo) or (b_low == hi and top > hi)
        (touching if only_touches and lo < hi else overlapping).append(name)
    return overlapping or touching


def location_names(geos: list[str]) -> list[str]:
    """ICP geography as LinkedIn location names. Short forms ('US', 'UK') are expanded; anything else
    (a full country name or a region such as 'Europe') is passed as written, which LinkedIn resolves."""
    names = []
    for g in geos:
        text = (g or "").strip()
        if not text:
            continue
        code = country_code(text)
        if code in LOCATION_NAMES:
            names.append(LOCATION_NAMES[code])
        elif code and len(text.replace(".", "")) <= 3:
            country = pycountry.countries.get(alpha_2=code.upper())
            names.append(getattr(country, "common_name", None) or country.name if country else text)
        else:
            names.append(text)
    return list(dict.fromkeys(names))


def normalize_company(item: dict) -> dict | None:
    """Keep only what we need, redacted. None if the company has no usable website domain (E-09)."""
    domain = normalize_domain(item.get("website"))
    if not domain:
        return None
    hq = None
    locations = item.get("locations") or []
    chosen = next((loc for loc in locations if loc.get("headquarter")), locations[0] if len(locations) == 1 else None)
    if chosen:
        parsed = chosen.get("parsed") or {}
        hq = {"city": chosen.get("city"), "state": parsed.get("state") or chosen.get("geographicArea"),
              "country_code": (parsed.get("countryCode") or chosen.get("country") or "").upper() or None,
              "text": redact(parsed.get("text") or "")[0] or None}
    description = redact((item.get("description") or "")[:800])[0]
    tagline = redact(item.get("tagline") or "")[0]
    return {
        "name": redact((item.get("name") or domain).strip())[0][:200],
        "domain": domain,
        "website": item.get("website"),
        "linkedin_url": item.get("linkedinUrl"),
        "tagline": tagline,
        # Everything text-like from Apify is redacted before storage or the model (E-11).
        "description": description,
        "industries": [redact(i.get("name"))[0] for i in (item.get("industries") or []) if i.get("name")],
        "specialities": [redact(str(s))[0] for s in (item.get("specialities") or [])[:8]],
        "employee_count_linkedin": item.get("employeeCount"),
        "employee_count_range": item.get("employeeCountRange"),
        "hq": hq,
        "founded_year": (item.get("foundedOn") or {}).get("year"),
        # The LinkedIn "About" text is written by the company, so it is as untrusted as its website.
        "injection_flags": find_injection_flags(f"{item.get('tagline') or ''}\n{item.get('description') or ''}"),
    }


# The actor sometimes answers a search with 0 companies and then 528 for the identical input a minute later
# (measured 2026-09-24: 5 of 15 keyword searches empty, same input, same build; errors log #63). An empty run
# returns no data and is billed only the $0.001 start event, so it is retried up to twice with the identical
# input (owner decision D-76). FAILED/aborted/timed-out runs are still never re-run (rule 9).
EMPTY_RESULT_RETRIES = 2
# The provider goes quiet in bursts (errors log #63/#65; run 30237325: the same query gave 5 companies at 20:33 and
# 0 three times at 20:44). Immediate retries land in the same burst, so wait first (D-103): time, not money.
EMPTY_RETRY_DELAYS_S = (20, 60)
PRICE_PER_COMPANY_USD = 0.004   # "full-company" event, FREE tier (checked 2026-09-23)
PRICE_PER_START_USD = 0.001


async def _settled_cost(client, run_id: str | None, reported: float, items: int) -> float:
    """Apify posts per-result charges a few seconds after the run ends, so the cost read at completion
    under-counts (found 2026-09-23: $0.003 recorded vs $0.045 billed). Re-read once charges settle, and
    never record less than the known price for what we received."""
    floor = round(items * PRICE_PER_COMPANY_USD + PRICE_PER_START_USD, 4)
    settled = reported
    if run_id:
        await asyncio.sleep(4)
        try:
            final = await client.run(run_id).get()
            f = final if isinstance(final, dict) else (final.model_dump(by_alias=True) if final else {})
            settled = float(f.get("usageTotalUsd") or reported)
        except (ApifyApiError, AttributeError):
            pass
    return max(settled, floor)


async def find_companies(
    *, query: str, geos: list[str], size_bands: list[str], max_items: int, max_charge_usd: float,
    start_page: int = 1, timeout_s: int = 180, industry_ids: list[str] | None = None,
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
    if industry_ids:  # LinkedIn industry ids as strings, e.g. ["4"] = Software Development (D-98)
        actor_input["industryIds"] = [str(i) for i in industry_ids]

    client = ApifyClientAsync(settings.apify_token)
    total_cost, run_ids = 0.0, []
    for attempt in range(1 + EMPTY_RESULT_RETRIES):
        try:
            run_id, raw, cost = await _run_once(client, settings.apify_actor_id, actor_input, int(max_items),
                                                max_charge_usd, timeout_s)
        except DiscoveryError as exc:  # keep what the earlier (empty) runs of this search cost
            exc.cost_usd = round((exc.cost_usd or 0.0) + total_cost, 4)
            raise
        total_cost += cost
        run_ids.append(run_id)
        if raw:
            break
        if attempt < EMPTY_RESULT_RETRIES:
            log.info("Apify run %s found 0 companies for %r; retrying the identical search in %ss (%d of %d)",
                     run_id, query, EMPTY_RETRY_DELAYS_S[attempt], attempt + 1, EMPTY_RESULT_RETRIES)
            await asyncio.sleep(EMPTY_RETRY_DELAYS_S[attempt])
    companies, dropped = [], 0
    for item in raw[: int(max_items)]:
        company = normalize_company(item)
        if company is None:
            dropped += 1
        else:
            companies.append(company)
    return DiscoveryResult(companies=companies, raw_count=len(raw), dropped_no_domain=dropped,
                           apify_run_id=run_id, cost_usd=round(total_cost, 4),
                           empty_retries=len(run_ids) - 1, apify_run_ids=run_ids)


async def _run_once(client, actor_id: str, actor_input: dict, max_items: int, max_charge_usd: float,
                    timeout_s: int) -> tuple[str, list[dict], float]:
    """One actor run: (run id, raw items, settled cost). Raises DiscoveryError with the run id and its cost."""
    actor = client.actor(actor_id)
    # 1) START the run. Only this request is retried, once, and only on HTTP 429 (Apify refused it before doing
    #    anything). A 5xx is NOT retried: the run may have been created before the error (rule 9). Waiting is a
    #    separate step, so a hiccup while waiting can never start a second paid run.
    started = None
    for attempt in range(2):
        try:
            started = await actor.start(
                run_input=actor_input,
                max_items=int(max_items),
                max_total_charge_usd=Decimal(str(max_charge_usd)),
                run_timeout=timedelta(seconds=timeout_s),
            )
            break
        except ApifyApiError as exc:
            status = getattr(exc, "status_code", None)
            failure = classify_apify(status, f"{getattr(exc, 'type', '')} {exc}")
            if status == 429 and attempt == 0:
                await asyncio.sleep(2)
                continue
            raise DiscoveryError(failure, f"Apify API error ({status}): {str(exc)[:200]}", failure_code=failure) from exc
    if started is None:
        raise DiscoveryError("apify_error", "Apify returned no run.", failure_code="apify_unavailable")
    s = started if isinstance(started, dict) else started.model_dump(by_alias=True)
    run_id = s.get("id")

    # 2) From here a paid run exists: never retry, and always carry its id and cost into any error (rule 9).
    def stopped(code: str, message: str, failure_code: str, cost: float | None = None) -> DiscoveryError:
        return DiscoveryError(code, f"{message} Check run {run_id} in the Apify console before re-running.",
                              apify_run_id=run_id, failure_code=failure_code, cost_usd=cost)

    try:
        run = await asyncio.wait_for(client.run(run_id).wait_for_finish(wait_duration=timedelta(seconds=timeout_s + 30)),
                                     timeout=timeout_s + 60)
    except (ApifyApiError, TimeoutError) as exc:
        raise stopped("wait_failed", f"Lost track of the Apify run while waiting ({type(exc).__name__}).",
                      "apify_run_failed", await _settled_cost(client, run_id, 0.0, 0)) from exc
    r = run if isinstance(run, dict) else (run.model_dump(by_alias=True) if run else {})
    status = r.get("status")
    cost = float(r.get("usageTotalUsd") or 0)
    if status != "SUCCEEDED":
        # Never re-run automatically (PRD). Tell the human where to look; the billed cost is still recorded.
        raise stopped("actor_" + str(status or "unknown").lower(), f"The Apify run ended {status}.",
                      "apify_run_failed", await _settled_cost(client, run_id, cost, 0))
    try:
        listing = await client.dataset(r["defaultDatasetId"]).list_items(limit=int(max_items))
    except Exception as exc:  # noqa: BLE001 — the run succeeded and was paid for; stop, don't search again
        raise stopped("dataset_failed", f"The Apify run finished but its results couldn't be read ({type(exc).__name__}).",
                      "apify_run_failed", await _settled_cost(client, run_id, cost, 0)) from exc
    raw = listing.items or []
    return run_id, raw, await _settled_cost(client, run_id, cost, len(raw))
