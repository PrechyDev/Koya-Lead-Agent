"""Company domain handling (specs.md §10.3; edge cases E-09, E-10).

A company's identity in this system is its registrable domain
("app.acme.io" and "https://www.acme.io/pricing" are both "acme.io"), which
is also the dedupe key. Profile/social URLs are *not* company domains.
"""

import ipaddress
from urllib.parse import urlsplit

import tldextract

# Offline: use the suffix list bundled with tldextract, never fetch it at runtime.
_extract = tldextract.TLDExtract(suffix_list_urls=())

# Hosts that are profiles/directories/app stores, not a company's own site.
NOT_A_COMPANY_SITE = {
    "linkedin.com", "crunchbase.com", "facebook.com", "twitter.com", "x.com",
    "instagram.com", "youtube.com", "tiktok.com", "medium.com", "github.com",
    "wellfound.com", "angel.co", "linktr.ee", "google.com", "apple.com",
    "notion.site", "wixsite.com", "carrd.co", "substack.com", "glassdoor.com",
    "indeed.com", "ycombinator.com", "producthunt.com", "g2.com", "capterra.com",
}


def normalize_domain(value: str | None) -> str | None:
    """Return the registrable domain (e.g. "acme.io"), or None if it isn't a usable company site."""
    if not value or not isinstance(value, str):
        return None
    raw = value.strip().lower()
    if not raw:
        return None
    if "://" not in raw:
        raw = "http://" + raw
    try:
        host = urlsplit(raw).hostname or ""
    except ValueError:
        return None
    host = host.strip(".")
    if not host or host == "localhost" or "." not in host:
        return None
    try:
        ipaddress.ip_address(host)
        return None  # bare IPs are never a company domain
    except ValueError:
        pass
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return None

    parts = _extract(host)
    if not parts.domain or not parts.suffix:
        return None
    registrable = f"{parts.domain}.{parts.suffix}"
    if registrable in NOT_A_COMPANY_SITE:
        return None
    if parts.suffix in {"local", "internal", "lan", "test", "invalid", "example"}:
        return None
    return registrable


def homepage_url(domain: str) -> str:
    return f"https://{domain}/"


def same_url(a: str, b: str) -> bool:
    """Loose URL equality for source checks: scheme, www. and trailing slash don't matter."""
    return canonical_url(a) == canonical_url(b)


def canonical_url(url: str) -> str:
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip().lower()
    host = (parts.hostname or "").lower().removeprefix("www.")
    path = parts.path.rstrip("/") or ""
    query = f"?{parts.query}" if parts.query else ""
    return f"{host}{path}{query}"
