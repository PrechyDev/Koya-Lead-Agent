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

_GEO_SYNONYMS = {
    "us": "us", "usa": "us", "u.s.": "us", "u.s.a.": "us", "united states": "us",
    "united states of america": "us", "america": "us",
    "uk": "gb", "u.k.": "gb", "united kingdom": "gb", "great britain": "gb", "england": "gb",
    "canada": "ca", "ca": "ca", "germany": "de", "france": "fr", "australia": "au",
    "nigeria": "ng", "india": "in", "ireland": "ie", "netherlands": "nl",
}

_TYPE_SYNONYMS = {
    "b2b saas": "b2b saas", "saas": "b2b saas", "b2b software": "b2b saas",
    "software as a service": "b2b saas", "b2b saas company": "b2b saas",
    "b2b saas companies": "b2b saas", "software company": "b2b saas",
}


def objective_problem(text: str) -> str | None:
    """Free check before any AI call: None if it could be a lead objective, else a plain-language reason.

    Deliberately simple: it only stops obvious junk (symbols, keyboard mashing, one or two words). Whether the
    request is actually a *lead search* is decided next by the ICP step's request_type (cheap AI call).
    """
    t = (text or "").strip()
    if len(t) < 5:
        return "Describe the companies you want to find (at least a few words)."
    if len(t) > 1000:
        return "Keep the objective under 1,000 characters."
    visible = [c for c in t if not c.isspace()]
    letters = sum(c.isalpha() for c in visible)
    if not visible or letters / len(visible) < 0.6:
        return "That looks like mostly symbols or numbers. Describe the companies you want to find in words."
    words = re.findall(r"[^\W\d_]{2,}", t)
    if len(words) < 3:
        return ("Please describe the companies in a short sentence, e.g. "
                "\"Find US B2B SaaS companies with 10 to 100 employees\".")
    if re.search(r"(.)\1{5,}", t):
        return "That looks like repeated characters. Describe the companies you want to find."
    vowelless = [w for w in words if len(w) >= 4 and not re.search(r"[aeiouyAEIOUY]", w)]
    long_mash = [w for w in words if len(w) >= 13 and len(set(w.lower())) <= 6]
    if len(vowelless) > len(words) / 2 or long_mash:
        return "That doesn't look like a sentence. Describe the companies you want to find."
    return None


def normalize_text(text: str) -> str:
    text = (text or "").lower()
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def objective_hash(objective: str) -> str:
    return hashlib.sha256(normalize_text(objective).encode("utf-8")).hexdigest()


def normalize_geo(value: str) -> str:
    v = (value or "").strip().lower().rstrip(".")
    return _GEO_SYNONYMS.get(v, normalize_text(v))


def normalize_headcount(value: str | None) -> str:
    """'10 to 100 employees' / '10–100' / '10-100' -> '10-100'."""
    numbers = re.findall(r"\d+", (value or "").replace(",", ""))
    if len(numbers) >= 2:
        return f"{int(numbers[0])}-{int(numbers[1])}"
    if len(numbers) == 1:
        return f"{int(numbers[0])}+"
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
