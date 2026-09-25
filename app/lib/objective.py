"""Fingerprints for the repeat-objective gate (specs.md §5.1; E-36, E-37).

Stage 1: objective_hash — the objective text, normalized, hashed. Free, catches
         exact repeats (ignoring case, spacing and punctuation).
Stage 2: icp_signature — a hash of the refined ICP's *hard filters* as data
         (geography, industries, headcount, company type). Catches the same
         intent in different words, without any AI "similarity" guess.
"""

import hashlib
import json
import re

import pycountry

# An objective must be at least this many words (owner's rule, 2026-09-24). A "word" is any space-separated
# token that contains a letter, so "B2B" and "US-based" count and "10" doesn't. app.js uses the same rule.
MIN_OBJECTIVE_WORDS = 5

# Names people use that ISO 3166 (pycountry) doesn't list. Everything else ("United States", "USA", "Kenya",
# "DE") is resolved by pycountry. "America" is deliberately absent: it could mean the US or the continent.
_GEO_ALIASES = {
    "u.s": "us", "u.s.a": "us", "the us": "us", "the usa": "us", "the united states": "us",
    "uk": "gb", "u.k": "gb", "the uk": "gb", "great britain": "gb", "britain": "gb", "england": "gb",
    "scotland": "gb", "wales": "gb", "northern ireland": "gb",
    "russia": "ru", "south korea": "kr", "korea": "kr", "north korea": "kp", "vietnam": "vn", "iran": "ir",
    "syria": "sy", "turkey": "tr", "czech republic": "cz", "uae": "ae", "taiwan": "tw", "tanzania": "tz",
    "bolivia": "bo", "venezuela": "ve", "moldova": "md", "laos": "la", "ivory coast": "ci",
    "the netherlands": "nl", "holland": "nl",
}

# Same kind of company, different words. Only feeds the repeat gate's signature, where a loose match costs
# one "you ran this before" question and a miss costs a repeated paid run, so matching generously is safer.
_TYPE_SYNONYMS = {
    "b2b saas": "b2b saas", "saas": "b2b saas", "b2b software": "b2b saas",
    "software as a service": "b2b saas", "b2b saas company": "b2b saas",
    "b2b saas companies": "b2b saas", "software company": "b2b saas",
}


def objective_problem(text: str) -> str | None:
    """Free check before any AI call: None if it could be a lead objective, else a plain-language reason.

    Deliberately simple: it stops obvious junk (symbols, keyboard mashing) and anything under 5 words. Whether the
    request is actually a *lead search* is decided next by the ICP step's request_type (cheap AI call).
    """
    t = (text or "").strip()
    if not t:
        return f"Describe the companies you want to find, in at least {MIN_OBJECTIVE_WORDS} words."
    if len(t) > 1000:
        return "Keep the objective under 1,000 characters."
    visible = [c for c in t if not c.isspace()]
    letters = sum(c.isalpha() for c in visible)
    if not visible or letters / len(visible) < 0.6:
        return "That looks like mostly symbols or numbers. Describe the companies you want to find in words."
    words = re.findall(r"[^\W\d_]{2,}", t)
    if count_words(t) < MIN_OBJECTIVE_WORDS:
        return (f"Describe the companies in at least {MIN_OBJECTIVE_WORDS} words, e.g. "
                "\"Find US B2B SaaS companies with 10 to 100 employees\".")
    if re.search(r"([^\d\s])\1{5,}", t):  # "1000000" is a number, not a key mash
        return "That looks like repeated characters. Describe the companies you want to find."
    latin = [w for w in words if re.fullmatch(r"[A-Za-z]+", w)]  # other scripts have no a/e/i/o/u (any language is fine)
    vowelless = [w for w in latin if len(w) >= 4 and not re.search(r"[aeiouyAEIOUY]", w)]
    long_mash = [w for w in words if len(w) >= 13 and len(set(w.lower())) <= 6]
    if (latin and len(vowelless) > len(latin) / 2) or long_mash:
        return "That doesn't look like a sentence. Describe the companies you want to find."
    return None


def count_words(text: str) -> int:
    return sum(1 for token in (text or "").split() if re.search(r"[^\W\d_]", token))


def normalize_text(text: str) -> str:
    text = (text or "").lower()
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def objective_hash(objective: str) -> str:
    return hashlib.sha256(normalize_text(objective).encode("utf-8")).hexdigest()


def country_code(value: str | None) -> str | None:
    """'United States' / 'USA' / 'U.S.' / 'Kenya' -> 'us' / 'us' / 'us' / 'ke'. None for regions ('Europe')."""
    v = re.sub(r"\s+", " ", (value or "").strip().lower().rstrip("."))
    if not v:
        return None
    if v in _GEO_ALIASES:
        return _GEO_ALIASES[v]
    try:
        return pycountry.countries.lookup(v).alpha_2.lower()
    except LookupError:
        return None


def normalize_geo(value: str) -> str:
    """ISO country code when the value is a country, else the normalized text (a region like 'europe')."""
    return country_code(value) or normalize_text(value)


_UNDER_RE = re.compile(r"\b(?:under|below|fewer\s+than|less\s+than)\b|<", re.I)
_UP_TO_RE = re.compile(r"\b(?:up\s+to|at\s+most|no\s+more\s+than|max(?:imum)?|or\s+(?:fewer|less|under|below))\b",
                       re.I)


def normalize_headcount(value: str | None) -> str:
    """'10 to 100 employees' / '10–100' / '10-100' -> '10-100'; '50+' / 'over 50' -> '50+';
    'under 50' -> '1-49' and 'up to 50' -> '1-50' (an upper bound, not a lower one)."""
    text = (value or "").replace(",", "")
    numbers = re.findall(r"\d+", text)
    if len(numbers) >= 2:
        return f"{int(numbers[0])}-{int(numbers[1])}"
    if len(numbers) == 1:
        n = int(numbers[0])
        if _UNDER_RE.search(text):
            return f"1-{max(1, n - 1)}"
        if _UP_TO_RE.search(text):
            return f"1-{n}"
        return f"{n}+"
    return ""


def parse_headcount_range(value: str | None) -> tuple[int | None, int | None]:
    norm = normalize_headcount(value)
    if not norm:
        return None, None
    if norm.endswith("+"):
        return int(norm[:-1]), None
    low, high = norm.split("-")
    return int(low), int(high)


def normalize_company_type(value: str | None) -> str:
    v = normalize_text(value or "")
    return _TYPE_SYNONYMS.get(v, v)


def icp_signature(icp: dict) -> str:
    geo = sorted({normalize_geo(g) for g in (icp.get("geography") or []) if g})
    industries = sorted({normalize_text(i) for i in (icp.get("industries") or []) if i})
    payload = {
        "geo": geo,
        "industries": industries,
        "headcount": normalize_headcount(icp.get("headcount_range")),
        "type": normalize_company_type(icp.get("target_company_type")),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
