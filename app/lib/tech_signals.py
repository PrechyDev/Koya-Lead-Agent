"""Tool fingerprints in a page's HTML (soft-preference evidence, D-36). Pure code: no AI, no extra credits.

Firecrawl returns the raw HTML alongside the markdown for the same 1 credit. Many B2B tools leave a script
or embed URL in the page (e.g. js.hs-scripts.com for HubSpot). Finding one is evidence for soft preferences
like "uses tools that may connect to automation workflows". Absence proves nothing (tools can be server-side).
"""

import re

# name -> (category, regex on raw HTML). Keep patterns specific to avoid false positives.
SIGNATURES: dict[str, tuple[str, re.Pattern]] = {name: (cat, re.compile(pat, re.I)) for name, cat, pat in [
    ("HubSpot", "CRM / marketing", r"js\.hs-scripts\.com|js\.hsforms\.net|js\.hs-analytics\.net|hubspot\.com/hs/"),
    ("Salesforce Pardot", "CRM / marketing", r"pi\.pardot\.com|go\.pardot\.com"),
    ("Marketo", "marketing automation", r"munchkin\.marketo\.net|mktoForms"),
    ("Intercom", "support / chat", r"widget\.intercom\.io|js\.intercomcdn\.com"),
    ("Drift", "support / chat", r"js\.driftt\.com"),
    ("Zendesk", "support", r"static\.zdassets\.com|zopim\.com"),
    ("Freshworks", "support / CRM", r"freshchat\.com|freshdesk\.com/widget|fw-cdn\.com"),
    ("Crisp", "support / chat", r"client\.crisp\.chat"),
    ("Calendly", "scheduling", r"assets\.calendly\.com|calendly\.com/assets"),
    ("Chili Piper", "scheduling", r"js\.chilipiper\.com"),
    ("Stripe", "payments / billing", r"js\.stripe\.com"),
    ("Chargebee", "payments / billing", r"js\.chargebee\.com"),
    ("Segment", "data pipeline", r"cdn\.segment\.com"),
    ("Mixpanel", "product analytics", r"cdn\.mxpnl\.com|mixpanel\.com/libs"),
    ("Amplitude", "product analytics", r"cdn\.amplitude\.com"),
    ("Hotjar", "analytics", r"static\.hotjar\.com"),
    ("Google Tag Manager", "analytics", r"googletagmanager\.com/gtm\.js"),
    ("Typeform", "forms", r"embed\.typeform\.com"),
    ("Zapier", "automation", r"zapier\.com/(?:apps|embed|zapbook)|interfaces\.zapier\.com"),
    ("Make", "automation", r"hook\.[a-z0-9]+\.make\.com|integromat\.com"),
    ("Airtable", "database / ops", r"airtable\.com/embed"),
    ("Notion", "docs / ops", r"notion\.site|notion\.so/embed"),
    ("Webflow", "website builder", r"<meta[^>]+generator[^>]+webflow|assets\.website-files\.com"),
    ("WordPress", "website builder", r"/wp-content/|/wp-includes/"),
    ("Shopify", "e-commerce", r"cdn\.shopify\.com"),
    ("Greenhouse", "hiring (ATS)", r"boards\.greenhouse\.io|greenhouse\.io/embed"),
    ("Lever", "hiring (ATS)", r"jobs\.lever\.co"),
    ("Ashby", "hiring (ATS)", r"jobs\.ashbyhq\.com"),
    ("Workable", "hiring (ATS)", r"apply\.workable\.com"),
]}


def detect_tools(raw_html: str | None) -> list[dict]:
    """[{name, category}] for every signature found, in a stable order."""
    if not raw_html:
        return []
    return [{"name": name, "category": cat} for name, (cat, pattern) in SIGNATURES.items() if pattern.search(raw_html)]
