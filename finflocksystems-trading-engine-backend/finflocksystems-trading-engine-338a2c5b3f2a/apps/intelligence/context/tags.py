"""Deterministic helpers for context items: HTML stripping, URL checks, event classification and story keys.

Everything here is plain Python (no model): classification rules are keyword lists a reviewer can read, and the
story key groups near-duplicate headlines so one story told by five outlets counts once.
"""
import hashlib
import html
import re

CENTRAL_BANK = ("interest rate decision", "rate decision", "fomc", "monetary policy", "repo rate", "policy rate",
                "mpc ", "central bank", "rbi policy", "fed press conference")
MACRO = ("cpi", "inflation", "pmi", "gdp", "payroll", "nonfarm", "unemployment", "retail sales", "industrial production",
         "manufacturing production", "trade balance", "pce", "durable goods", "consumer confidence", "jobless claims",
         "fx reserves", "foreign exchange reserves", "money supply", "fiscal deficit")
EARNINGS = ("earnings", "quarterly results", "q1 results", "q2 results", "q3 results", "q4 results")
STOPWORDS = {"the", "and", "for", "with", "from", "that", "this", "are", "was", "were", "will", "has", "have", "its", "into",
             "over", "after", "before", "about", "than", "amid", "says", "said", "new", "how", "why", "what", "who", "you",
             "your", "may", "can", "could", "would", "all", "more", "most", "not", "but", "out", "off", "per", "via"}
_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")
_TOKEN = re.compile(r"[a-z0-9]{3,}")


def strip_html(value):
    """Plain text from an HTML fragment (RBI press releases carry whole tables)."""
    if not value:
        return ""
    return _SPACE.sub(" ", html.unescape(_TAG.sub(" ", str(value)))).strip()


def clean_url(value):
    """Only absolute https URLs are kept (no javascript:, data:, or relative links), capped in length."""
    text = str(value or "").strip()
    return text[:500] if text.lower().startswith("https://") else ""


def classify_event(title, country=""):
    """CENTRAL_BANK | MACRO_RELEASE | EARNINGS | GENERAL, from the title only."""
    text = f" {str(title or '').lower()} "
    if any(k in text for k in CENTRAL_BANK):
        return "CENTRAL_BANK"
    if any(k in text for k in EARNINGS):
        return "EARNINGS"
    if any(k in text for k in MACRO):
        return "MACRO_RELEASE"
    return "GENERAL"


def tokens(title):
    return {t for t in _TOKEN.findall(str(title or "").lower()) if t not in STOPWORDS}


def jaccard(a, b):
    return len(a & b) / len(a | b) if a and b else 0.0


def new_story_key(title):
    return hashlib.sha1(" ".join(sorted(tokens(title))).encode()).hexdigest()[:20]


def raw_hash(value):
    import json

    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()
