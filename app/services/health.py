"""Free pre-run checks (specs.md E-50): refuse a run before ANY spend if a service can't work.

Checks (all free, except a 1-token Haiku call ≈ $0.00002 that is the only way to detect an empty
Anthropic account; recorded in the ledger as 'preflight'):
  * required settings present;
  * Anthropic key valid + account has credit;
  * Apify token valid, actor exists, monthly usage left >= what one run may spend;
  * Firecrawl key valid, credits left >= the run's scrape cap.
Successful results are cached for 10 minutes; failures for 1 minute (so a fix is noticed quickly).
"""

import time

import anthropic
import httpx

from app.config import get_settings
from app.failures import ServiceFailure, classify_apify, classify_claude_error
from app.lib.budget import cost_from_usage

OK_TTL_S = 600
FAIL_TTL_S = 60
_cache: dict[str, tuple[float, ServiceFailure | None]] = {}


def _cached(name: str):
    hit = _cache.get(name)
    if hit and hit[0] > time.monotonic():
        return True, hit[1]
    return False, None


def _store(name: str, failure: ServiceFailure | None) -> None:
    _cache[name] = (time.monotonic() + (FAIL_TTL_S if failure else OK_TTL_S), failure)


def clear_cache() -> None:
    _cache.clear()


def check_anthropic() -> ServiceFailure | None:
    settings = get_settings()
    try:
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key, max_retries=1, timeout=20)
        response = client.messages.create(model="claude-haiku-4-5", max_tokens=1,
                                          messages=[{"role": "user", "content": "ok"}])
    except anthropic.APIStatusError as exc:
        return ServiceFailure(classify_claude_error(exc.status_code, str(exc)), f"HTTP {exc.status_code}")
    except anthropic.APIConnectionError:
        return ServiceFailure("anthropic_unavailable", "could not reach api.anthropic.com")
    try:
        from app import db
        u = response.usage
        db.record_spend("preflight", cost_from_usage("claude-haiku-4-5", u.input_tokens, u.output_tokens),
                        model="claude-haiku-4-5", input_tokens=u.input_tokens, output_tokens=u.output_tokens,
                        note="pre-run credit probe (1 token)")
    except Exception:  # noqa: BLE001 — never fail a check because the ledger write failed
        pass
    return None


def check_apify(min_remaining_usd: float) -> ServiceFailure | None:
    settings = get_settings()
    headers = {"Authorization": f"Bearer {settings.apify_token}"}
    actor = settings.apify_actor_id.replace("/", "~")
    try:
        r = httpx.get(f"https://api.apify.com/v2/acts/{actor}", headers=headers, timeout=20)
        if r.status_code != 200:
            return ServiceFailure(classify_apify(r.status_code, r.text), f"actor check HTTP {r.status_code}: "
                                  f"{settings.apify_actor_id}")
        r = httpx.get("https://api.apify.com/v2/users/me/limits", headers=headers, timeout=20)
        if r.status_code == 200:
            data = r.json().get("data") or {}
            limit = float((data.get("limits") or {}).get("maxMonthlyUsageUsd") or 0)
            used = float((data.get("current") or {}).get("monthlyUsageUsd") or 0)
            if limit and limit - used < min_remaining_usd:
                return ServiceFailure("apify_no_credit", f"${used:.2f} of ${limit:.2f} monthly usage used; a run "
                                                         f"may need ${min_remaining_usd:.2f}")
    except httpx.HTTPError:
        return ServiceFailure("apify_unavailable", "could not reach api.apify.com")
    return None


def check_firecrawl(min_credits: int) -> ServiceFailure | None:
    settings = get_settings()
    try:
        r = httpx.get("https://api.firecrawl.dev/v2/team/credit-usage",
                      headers={"Authorization": f"Bearer {settings.firecrawl_api_key}"}, timeout=20)
    except httpx.HTTPError:
        return None  # Firecrawl status unknown: not worth blocking a run; scrapes handle their own errors
    if r.status_code == 401:
        return ServiceFailure("firecrawl_auth", "credit check HTTP 401")
    if r.status_code == 200:
        remaining = int((r.json().get("data") or {}).get("remainingCredits") or 0)
        if remaining < min_credits:
            return ServiceFailure("firecrawl_no_credit", f"{remaining} credits left; a run may need {min_credits}")
    return None


def preflight(limits: dict) -> list[ServiceFailure]:
    """All checks that must pass before a run may spend anything. Returns the failures (empty = go)."""
    settings = get_settings()
    missing = settings.missing_run_config()
    if missing:
        return [ServiceFailure("not_configured", "missing: " + ", ".join(missing))]
    failures: list[ServiceFailure] = []
    apify_need = float(limits.get("apify_max_charge_usd", 0.25)) * int(limits.get("max_discovery_calls", 3))
    checks = [
        ("anthropic", check_anthropic),
        ("apify", lambda: check_apify(min_remaining_usd=round(apify_need, 2))),
        ("firecrawl", lambda: check_firecrawl(min_credits=int(limits.get("max_scrapes", 20)))),
    ]
    for name, fn in checks:
        hit, cached = _cached(name)
        result = cached if hit else fn()
        if not hit:
            _store(name, result)
        if result:
            failures.append(result)
    return failures

