"""Website scraping through Firecrawl v2 /scrape (specs.md §8.2; E-12, E-13, E-26, E-28, E-33).

* Public pages only: no cookies, no credentials, stealth/proxy options never set.
* 30s timeout; ONE retry on timeout/5xx only. 401/403/login walls are never retried around.
* Markdown is de-noised (images dropped, links -> their text) and then goes
  through the untrusted-content sanitizer before anyone sees it.
"""

import asyncio
import re
from dataclasses import dataclass, field

import httpx
from urllib.parse import urljoin, urlsplit

from app.config import get_settings
from app.lib.sanitize import sanitize_page

API_URL = "https://api.firecrawl.dev/v2/scrape"
IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
LINK_RE = re.compile(r"\[([^\]]*)\]\((?:[^()]|\([^)]*\))*\)")
LOGIN_WALL_RE = re.compile(r"\b(?:sign in to continue|log in to continue|please log in|verify you are human|captcha)\b", re.I)
USEFUL_PATH_RE = re.compile(r"/(?:about|company|team|who-we-are|our-story|customers|case-studies|careers|jobs|pricing|product|platform|solutions|industries)", re.I)
PARKED_RE = re.compile(r"\b(?:this domain (?:is|may be) for sale|buy this domain|domain parking|parked free)\b", re.I)


class ScrapeError(Exception):
    def __init__(self, code: str, message: str, status_code: int | None = None):
        super().__init__(message)
        self.code, self.message, self.status_code = code, message, status_code


@dataclass
class ScrapedPage:
    url: str
    final_url: str
    title: str
    content: str
    truncated: bool
    status_code: int | None
    injection_flags: list[str] = field(default_factory=list)
    redactions: dict[str, int] = field(default_factory=dict)
    parked: bool = False
    links: list[str] = field(default_factory=list)


def internal_links(markdown: str, base_url: str, limit: int = 12) -> list[str]:
    """Same-site paths worth a second look (about/customers/careers...), found before links are stripped."""
    host = (urlsplit(base_url).hostname or "").removeprefix("www.")
    found: list[str] = []
    for target in re.findall(r"\]\(([^)\s]+)", markdown or ""):
        url = urljoin(base_url, target)
        parts = urlsplit(url)
        if (parts.hostname or "").removeprefix("www.") != host or parts.scheme not in ("http", "https"):
            continue
        path = parts.path.rstrip("/")
        if path and USEFUL_PATH_RE.match(path) and path.count("/") <= 2 and path not in found:
            found.append(path)
        if len(found) >= limit:
            break
    return found


def denoise(markdown: str) -> str:
    text = IMAGE_RE.sub("", markdown or "")
    text = LINK_RE.sub(lambda m: m.group(1), text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


async def scrape(url: str, *, max_chars: int = 6000, client: httpx.AsyncClient | None = None) -> ScrapedPage:
    settings = get_settings()
    payload = {"url": url, "formats": ["markdown"], "onlyMainContent": True, "timeout": 30000,
               "blockAds": True, "removeBase64Images": True}
    headers = {"Authorization": f"Bearer {settings.firecrawl_api_key}"}
    own_client = client is None
    client = client or httpx.AsyncClient(timeout=60)
    try:
        response = None
        for attempt in range(2):
            try:
                response = await client.post(API_URL, json=payload, headers=headers)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt == 0:
                    await asyncio.sleep(1.5)
                    continue
                raise ScrapeError("timeout", f"Firecrawl timed out for {url}.") from exc
            if response.status_code >= 500 and attempt == 0:
                await asyncio.sleep(1.5)
                continue
            break
    finally:
        if own_client:
            await client.aclose()

    assert response is not None
    if response.status_code == 402:
        raise ScrapeError("credits_exhausted", "Firecrawl credits are used up; remaining scrapes are skipped.", 402)
    if response.status_code == 429:
        raise ScrapeError("rate_limited", "Firecrawl is rate-limiting requests right now.", 429)
    if response.status_code == 401:
        raise ScrapeError("auth", "Firecrawl rejected the API key.", 401)
    if response.status_code >= 400:
        raise ScrapeError("firecrawl_error", f"Firecrawl returned HTTP {response.status_code} for {url}.",
                          response.status_code)

    body = response.json()
    data = body.get("data") or {}
    meta = data.get("metadata") or {}
    site_status = meta.get("statusCode")
    final_url = meta.get("url") or meta.get("sourceURL") or url
    if not body.get("success", True):
        raise ScrapeError("firecrawl_error", str(body.get("error") or "Firecrawl could not scrape the page."))
    if site_status in (401, 403):
        raise ScrapeError("access_denied", "Site is not publicly accessible (401/403).", site_status)
    if site_status and site_status >= 400:
        raise ScrapeError("site_error", f"Site returned HTTP {site_status}.", site_status)

    raw_markdown = data.get("markdown") or ""
    text = denoise(raw_markdown)
    if len(text) < 80:
        raise ScrapeError("empty", "Page had almost no readable text (JS-only or blocked).", site_status)
    if LOGIN_WALL_RE.search(text[:2000]) and len(text) < 1500:
        raise ScrapeError("access_denied", "Page is behind a login or bot check.", site_status)

    clean = sanitize_page(text, max_chars=max_chars)
    return ScrapedPage(
        url=url, final_url=final_url, title=(meta.get("title") or "")[:200], content=clean.text,
        truncated=clean.truncated, status_code=site_status, injection_flags=clean.injection_flags,
        redactions=clean.redactions, parked=bool(PARKED_RE.search(text[:3000])),
        links=internal_links(raw_markdown, final_url),
    )
