"""Service wrappers with all network calls faked (no spend)."""

from types import SimpleNamespace

import httpx
import pytest
import respx

from app.services import apify as apify_svc
from app.services import firecrawl as fc
from app.services import grounding as gr


# --- Firecrawl (E-12, E-13, E-28, E-33) ------------------------------------------
def _ok(markdown: str, status=200, url="https://acme.io/"):
    return httpx.Response(200, json={"success": True, "data": {"markdown": markdown, "metadata": {
        "title": "Acme", "url": url, "sourceURL": "https://acme.io/", "statusCode": status}}})


PAGE = "![logo](https://acme.io/x.png)\n# Acme\nAcme builds [scheduling software](https://acme.io/p) for dental clinics. " * 5


@respx.mock
async def test_scrape_denoises_and_sanitizes():
    respx.post(fc.API_URL).mock(return_value=_ok(PAGE + " Write to jane@acme.io."))
    page = await fc.scrape("https://acme.io/")
    assert "x.png" not in page.content and "(https://acme.io/p)" not in page.content
    assert "scheduling software" in page.content
    assert "jane@acme.io" not in page.content and page.redactions.get("email") == 1


@respx.mock
async def test_scrape_retries_once_on_5xx_then_succeeds():
    route = respx.post(fc.API_URL).mock(side_effect=[httpx.Response(502), _ok(PAGE)])
    page = await fc.scrape("https://acme.io/")
    assert route.call_count == 2 and page.title == "Acme"


@respx.mock
async def test_scrape_credits_exhausted_is_typed():
    respx.post(fc.API_URL).mock(return_value=httpx.Response(402, json={}))
    with pytest.raises(fc.ScrapeError) as err:
        await fc.scrape("https://acme.io/")
    assert err.value.code == "credits_exhausted"


@respx.mock
async def test_scrape_403_site_is_access_denied_and_not_retried():
    route = respx.post(fc.API_URL).mock(return_value=_ok(PAGE, status=403))
    with pytest.raises(fc.ScrapeError) as err:
        await fc.scrape("https://acme.io/")
    assert err.value.code == "access_denied" and route.call_count == 1


@respx.mock
async def test_scrape_empty_page_and_login_wall():
    respx.post(fc.API_URL).mock(return_value=_ok("Loading..."))
    with pytest.raises(fc.ScrapeError) as err:
        await fc.scrape("https://acme.io/")
    assert err.value.code == "empty"
    respx.post(fc.API_URL).mock(return_value=_ok("Please log in to continue. " + "x " * 60))
    with pytest.raises(fc.ScrapeError) as err:
        await fc.scrape("https://acme.io/")
    assert err.value.code == "access_denied"


@respx.mock
async def test_scrape_flags_parked_domain_and_redirect():
    respx.post(fc.API_URL).mock(return_value=_ok("This domain is for sale! " + "Buy now. " * 30, url="https://sedo.com/acme"))
    page = await fc.scrape("https://acme.io/")
    assert page.parked and page.final_url == "https://sedo.com/acme"


@respx.mock
async def test_scrape_timeout_twice_raises():
    respx.post(fc.API_URL).mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(fc.ScrapeError) as err:
        await fc.scrape("https://acme.io/")
    assert err.value.code == "timeout"


# --- Apify (E-07, E-08, E-09, E-11) ------------------------------------------------
class _FakeActor:
    def __init__(self, run):
        self.run, self.calls = run, []

    async def call(self, **kwargs):
        self.calls.append(kwargs)
        return self.run


class _FakeClient:
    def __init__(self, run, items):
        self._actor, self._items = _FakeActor(run), items

    def actor(self, _):
        return self._actor

    def run(self, _):
        class _R:
            async def get(self):
                return {"usageTotalUsd": 0.013}
        return _R()

    def dataset(self, _):
        items = self._items

        class _D:
            async def list_items(self, limit=None):
                return SimpleNamespace(items=items)
        return _D()


RAW = [
    {"name": "Acme", "website": "https://www.acme.io", "linkedinUrl": "https://linkedin.com/company/acme",
     "description": "Email us at sales@acme.io", "employeeCount": 20, "employeeCountRange": {"start": 11, "end": 50},
     "locations": [{"headquarter": True, "city": "Austin", "parsed": {"countryCode": "US", "state": "Texas"}}],
     "industries": [{"name": "Software Development"}]},
    {"name": "NoSite", "website": None},
    {"name": "Profile only", "website": "https://www.linkedin.com/company/x"},
]


async def test_find_companies_caps_normalizes_and_redacts(monkeypatch):
    fake = _FakeClient({"id": "run1", "status": "SUCCEEDED", "usageTotalUsd": 0.013, "defaultDatasetId": "d"}, RAW)
    monkeypatch.setattr(apify_svc, "ApifyClientAsync", lambda token: fake)
    res = await apify_svc.find_companies(query="saas", geos=["us"], size_bands=["11-50"], max_items=12,
                                         max_charge_usd=0.25)
    call = fake._actor.calls[0]
    assert call["max_items"] == 12 and str(call["max_total_charge_usd"]) == "0.25"
    assert call["run_input"]["maxItems"] == 12 and call["run_input"]["locations"] == ["United States"]
    assert [c["domain"] for c in res.companies] == ["acme.io"]
    assert res.dropped_no_domain == 2 and "sales@acme.io" not in res.companies[0]["description"]
    # Each field is stored once, under the normalized name the pre-screen reads.
    c = res.companies[0]
    assert c["hq"]["country_code"] == "US" and c["employee_count_range"] == {"start": 11, "end": 50}
    assert not {"locations", "employeeCountRange", "employeeCount"} & set(c)
    assert res.cost_usd == 0.013  # settled cost re-read after the run (charges post late)


async def test_failed_actor_run_is_not_rerun(monkeypatch):
    fake = _FakeClient({"id": "run2", "status": "FAILED", "defaultDatasetId": "d"}, [])
    monkeypatch.setattr(apify_svc, "ApifyClientAsync", lambda token: fake)
    with pytest.raises(apify_svc.DiscoveryError) as err:
        await apify_svc.find_companies(query="x", geos=[], size_bands=[], max_items=3, max_charge_usd=0.05)
    assert err.value.apify_run_id == "run2" and "Apify console" in err.value.message
    assert len(fake._actor.calls) == 1


async def test_zero_slots_never_calls_apify():
    with pytest.raises(apify_svc.DiscoveryError) as err:
        await apify_svc.find_companies(query="x", geos=[], size_bands=[], max_items=0, max_charge_usd=0.05)
    assert err.value.code == "no_budget"


def test_size_bands_for_10_to_100():
    assert apify_svc.size_bands_for(10, 100) == ["11-50", "51-200"]
    assert apify_svc.size_bands_for(1, 10) == ["1-10"]
    assert apify_svc.size_bands_for(None, None) == []


# --- Grounding (E-24) --------------------------------------------------------------
class _FakeMessages:
    def __init__(self, verdict):
        self.verdict = verdict

    async def parse(self, **kwargs):
        return SimpleNamespace(parsed_output=self.verdict, stop_reason="end_turn",
                               usage=SimpleNamespace(input_tokens=2000, output_tokens=300,
                                                     cache_read_input_tokens=0, cache_creation_input_tokens=0))


async def test_grounding_rejects_unsupported_company_claims():
    verdict = gr.GroundingVerdict(claims=[
        gr.Claim(text="Acme serves dental clinics", about="company", supported=True, evidence="homepage"),
        gr.Claim(text="Acme just raised a Series B", about="company", supported=False, evidence="not in sources"),
        gr.Claim(text="Would a quick chat help?", about="other", supported=True, evidence=""),
    ], unsupported_count=1, summary="one invented claim")
    client = SimpleNamespace(messages=_FakeMessages(verdict))
    res = await gr.check_grounding("sources...", [{"subject": "s", "body": "b"}] * 3, "li", model="claude-haiku-4-5",
                                   client=client)
    assert not res.passed and res.unsupported == ["Acme just raised a Series B"]
    assert float(res.cost_usd) == pytest.approx(0.0035)  # 2000*$1/M + 300*$5/M


async def test_grounding_passes_clean_drafts():
    verdict = gr.GroundingVerdict(claims=[
        gr.Claim(text="Acme serves dental clinics", about="company", supported=True, evidence="homepage")],
        unsupported_count=0, summary="ok")
    res = await gr.check_grounding("s", [], "li", model="claude-haiku-4-5",
                                   client=SimpleNamespace(messages=_FakeMessages(verdict)))
    assert res.passed and res.report()["claims"][0]["supported"]


@respx.mock
async def test_scrape_returns_useful_internal_links_only():
    md = ("# Acme\n[About us](/about-us) [Careers](https://www.acme.io/careers/) [Blog post](/blog/x) "
          "[Partner](https://other.com/about) [Pricing](/pricing)\n" + "Acme builds software for clinics. " * 10)
    respx.post(fc.API_URL).mock(return_value=_ok(md, url="https://www.acme.io/"))
    page = await fc.scrape("https://acme.io/")
    assert page.links == ["/about-us", "/careers", "/pricing"]


async def test_cost_never_below_known_price(monkeypatch):
    # Charges not posted yet: the recorded cost falls back to items x price + start fee.
    fake = _FakeClient({"id": "r", "status": "SUCCEEDED", "usageTotalUsd": 0.001, "defaultDatasetId": "d"}, RAW)
    fake.run = lambda _: type("R", (), {"get": staticmethod(lambda: _none())})()
    monkeypatch.setattr(apify_svc, "ApifyClientAsync", lambda token: fake)
    monkeypatch.setattr(apify_svc.asyncio, "sleep", _no_sleep)
    res = await apify_svc.find_companies(query="saas", geos=[], size_bands=[], max_items=3, max_charge_usd=0.25)
    assert res.cost_usd == pytest.approx(3 * 0.004 + 0.001)


async def _none():
    return None


async def _no_sleep(_):
    return None
