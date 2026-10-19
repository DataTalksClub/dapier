"""Domain normalization, source-key calculation, and funding detail extraction."""

import hashlib
import re
from typing import Optional, Tuple
from urllib.parse import urlparse

KNOWN_MEDIA_HOSTS = frozenset({
    "techcrunch.com",
    "eu-startups.com",
    "venturebeat.com",
    "forbes.com",
    "bloomberg.com",
    "reuters.com",
    "wsj.com",
    "ft.com",
    "businessinsider.com",
    "cnbc.com",
    "theverge.com",
    "wired.com",
    "sifted.eu",
    "news.ycombinator.com",
    "substack.com",
    "medium.com",
    "twitter.com",
    "x.com",
    "linkedin.com",
    "youtube.com",
    "github.com",
    "reddit.com",
    "facebook.com",
    "instagram.com",
    "google.com",
    "bit.ly",
    "t.co",
    "tinyurl.com",
    "goo.gl",
    "ow.ly",
})

_DOMAIN_PATTERN = re.compile(
    r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$",
    re.IGNORECASE,
)

_AMOUNT_RE = re.compile(
    r"(\$|€|£|USD\s*|EUR\s*|GBP\s*)(\d+(?:\.\d+)?)\s*(billion|million|b|m|k)?\b",
    re.IGNORECASE,
)

_ROUND_RE = re.compile(
    r"\b(pre[- ]?seed|seed|series\s+[a-z]|growth|debt|grant|angel)\b",
    re.IGNORECASE,
)

_TITLE_HEADLINE_RE = re.compile(
    r"^\s*([A-Za-z0-9_.\- ]+?)\s+(?:raises|secures|closes|announces|bags|lands|nabs|gets|scores|fetches|collects)\s+",
    re.IGNORECASE,
)


def normalize_domain(url_or_domain: str, filter_media: bool = True) -> Optional[str]:
    """Normalize a URL or domain string into a clean, lowercased host.

    Strips scheme, www, port, paths, query parameters, and fragments.
    When `filter_media` is True, returns None if the domain belongs to a known
    media outlet, aggregator, or social network (since that is not the target company).
    """
    raw = str(url_or_domain or "").strip().lower()
    if not raw:
        return None

    # Strip scheme if present
    if "://" in raw:
        try:
            parsed = urlparse(raw)
            host = parsed.netloc or parsed.path
        except Exception:
            host = raw.split("://", 1)[1]
    else:
        host = raw

    # Strip port, userinfo, path, query
    host = host.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    if "@" in host:
        host = host.split("@", 1)[1]
    if ":" in host:
        host = host.split(":", 1)[0]

    # Strip leading www.
    if host.startswith("www."):
        host = host[4:]

    host = host.strip(".").strip()
    if not host or not _DOMAIN_PATTERN.match(host):
        return None

    if filter_media:
        for media_host in KNOWN_MEDIA_HOSTS:
            if host == media_host or host.endswith("." + media_host):
                return None

    return host


def compute_source_key(kind: str, domain: str, evidence_ref: str) -> str:
    """Compute the deterministic deduplication source-key.

    Format locked by AC: signal:{kind}:{domain}:{url-or-msgid-hash}
    """
    clean_kind = str(kind or "signal").strip().lower()
    clean_domain = str(domain or "unknown").strip().lower()
    ref_bytes = str(evidence_ref or "").strip().encode("utf-8")
    hash_part = hashlib.sha256(ref_bytes).hexdigest()[:16]
    return f"signal:{clean_kind}:{clean_domain}:{hash_part}"


def extract_amount(text: str) -> Optional[str]:
    """Extract funding amount string like '$10M', '€5.5 million' from text."""
    match = _AMOUNT_RE.search(str(text or ""))
    if not match:
        return None
    curr, num, unit = match.groups()
    curr = curr.strip()
    unit = (unit or "").strip()
    if unit:
        return f"{curr}{num}{unit.upper() if len(unit) == 1 else ' ' + unit.lower()}"
    return f"{curr}{num}"


def extract_round(text: str) -> Optional[str]:
    """Extract funding round string like 'Series A', 'Seed' from text."""
    match = _ROUND_RE.search(str(text or ""))
    if not match:
        return None
    raw = match.group(1).strip()
    lowered = raw.lower()
    if "pre" in lowered:
        return "Pre-Seed"
    if "seed" in lowered:
        return "Seed"
    if "series" in lowered:
        parts = raw.split()
        return f"Series {parts[-1].upper()}"
    return raw.capitalize()


def extract_company_from_title(title: str) -> Optional[str]:
    """Extract candidate company name from headline like 'Acme raises $10M...'."""
    raw = str(title or "").strip()
    # Strip common email prefixes like Fwd:, Re:, [Tag]
    raw = re.sub(r"^(?:\[[^\]]*\]\s*|\b(?:fwd?|re)\s*:\s*)+", "", raw, flags=re.IGNORECASE).strip()
    match = _TITLE_HEADLINE_RE.match(raw)
    if not match:
        return None
    candidate = match.group(1).strip()
    # Reject if candidate is too long or contains punctuation that suggests a sentence
    if len(candidate) > 60 or any(p in candidate for p in [":", "?", "!", ";", ","]):
        return None
    return candidate


def extract_company_domain_from_text(text: str) -> Optional[str]:
    """Search text for candidate web links or domain mentions of the company."""
    # Find urls in text
    urls = re.findall(r"https?://[^\s<>\"'()]+", str(text or ""))
    for url in urls:
        norm = normalize_domain(url, filter_media=True)
        if norm:
            return norm
    # Find domain mentions like (company.com) or at company.ai
    domain_matches = re.findall(r"\b([a-z0-9\-]+\.(?:com|io|ai|co|org|net|dev|app|tech))\b", str(text or ""), re.IGNORECASE)
    for dm in domain_matches:
        norm = normalize_domain(dm, filter_media=True)
        if norm:
            return norm
    return None
