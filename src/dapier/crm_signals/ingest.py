"""Core ingestion logic for company signals (Sources A+B+C) into DataOps CRM notes."""

from datetime import datetime, timezone
import html
import logging
import os
import re
from typing import Any, Callable, Dict, List, Optional
import urllib.request
import xml.etree.ElementTree as ET
import yaml

from .dataops_client import DataOpsCRMClient, DataOpsCliClient, MockDataOpsClient
from .hold import UnmatchedHoldStore
from .icp import evaluate_icp
from .models import SignalItem
from .normalizer import (
    compute_source_key,
    extract_amount,
    extract_company_domain_from_text,
    extract_company_from_title,
    extract_round,
    normalize_domain,
)
from .seen import SeenStore

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))),
    "config",
    "crm-signal-feeds.yaml",
)

RSS_USER_AGENT = "Dapier-Signal-Ingest/1.0 (+https://dapier.dtcdev.click)"


def load_config(config_path: Optional[str] = None) -> Dict[str, Any]:
    """Load configuration from the specified path or default location."""
    path = config_path or DEFAULT_CONFIG_PATH
    if not os.path.exists(path):
        # Fallback to local config directory if relative
        alt_path = os.path.join(os.getcwd(), "config", "crm-signal-feeds.yaml")
        if os.path.exists(alt_path):
            path = alt_path
    if not os.path.exists(path):
        return {
            "feeds": [],
            "icp": {},
            "settings": {},
        }
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def format_signal_note_body(item: SignalItem, source_key: str) -> str:
    """Format the P1 note body using the locked schema from AC.md.

    Required fields:
    kind, source (newsletter|rss|email), observedAt, evidenceUrl, amount, round,
    signalSummary, review=ok|needed, and sourceKey.
    """
    amount_str = item.amount or "N/A"
    round_str = item.round or "N/A"
    company_display = item.company_name or "Unknown Company"
    domain_display = item.domain or "unknown"

    return (
        f"[P1 Signal] kind: {item.kind} | source: {item.source} | review: {item.review}\n"
        f"Company: {company_display} ({domain_display})\n"
        f"Amount: {amount_str}\n"
        f"Round: {round_str}\n"
        f"Observed At: {item.observed_at}\n"
        f"Evidence: {item.evidence_url}\n"
        f"Source-Key: {source_key}\n\n"
        f"Summary:\n{item.signal_summary.strip()}"
    )


def process_signal(
    item: SignalItem,
    config: Dict[str, Any],
    client: DataOpsCRMClient,
    hold_store: UnmatchedHoldStore,
    seen_store: SeenStore,
) -> str:
    """Process a single signal item.

    Returns one of:
      - 'created': Org matched/created and P1 note appended.
      - 'deduped': Skipped because source-key already processed or existing note found.
      - 'unmatched': Company unclear; added to hold store (TTL 14d) and Hub intake formatted.
      - 'filtered': Filtered out by ICP allow/deny lists.
    """
    # Auto signal kinds: funding, sponsored_elsewhere. Ignore vague company_news in auto path.
    allowed_kinds = set(config.get("settings", {}).get("auto_signal_kinds", ["funding", "sponsored_elsewhere"]))
    if item.kind not in allowed_kinds:
        return "filtered"

    # 1. ICP filter check before note write
    icp_cfg = config.get("icp", {})
    passed, _reason = evaluate_icp(item, icp_cfg)
    if not passed:
        return "filtered"

    # 2. Compute source-key dedupe hash
    effective_domain = item.domain or "unclear"
    source_key = item.source_key or compute_source_key(item.kind, effective_domain, item.evidence_url)
    item.source_key = source_key

    # Dedupe check against local seen store
    if seen_store.has(source_key):
        return "deduped"

    # 3. Check if company is clear
    if not item.is_clear():
        # Unclear company -> unmatched hold with 14d TTL
        hold_store.add_unmatched(item, reason="unclear_company_identity_or_domain")
        seen_store.add(source_key)
        return "unmatched"

    # 4. Clear company -> match or create org by domain
    org = client.find_organization_by_domain(item.domain)
    if not org:
        org = client.create_organization(name=item.company_name or item.domain, domain=item.domain)

    org_id = str(org.get("id") or "")
    if not org_id:
        # Fallback if creation didn't return an id
        hold_store.add_unmatched(item, reason="org_creation_failed")
        return "unmatched"

    # Check for existing notes on org with the same sourceKey
    try:
        existing_notes = client.list_organization_notes(org_id)
        for note in existing_notes:
            if note.get("sourceKey") == source_key:
                seen_store.add(source_key)
                return "deduped"
    except Exception:
        pass

    # Append P1 signal note (source-owned note; does not overwrite sales notes)
    note_body = format_signal_note_body(item, source_key)
    client.add_note_to_organization(org_id, note_body, source_key=source_key)
    seen_store.add(source_key)
    return "created"


# ---- Source A: Newsletter sponsor reuse ----

def reuse_newsletter_sponsors(
    newsletter_data: Dict[str, Any],
    config: Dict[str, Any],
    client: DataOpsCRMClient,
    hold_store: UnmatchedHoldStore,
    seen_store: SeenStore,
) -> Dict[str, int]:
    """Hook for Source A: Reuses extracted sponsor placements from newsletter@ emails.

    Sponsors from newsletter issues (like extract-sponsors output in newsletter-sponsors flow)
    are mapped to `sponsored_elsewhere` signals.
    """
    newsletter_meta = newsletter_data.get("newsletter", {})
    nl_name = str(newsletter_meta.get("name") or newsletter_data.get("name") or "Newsletter")
    observed_at = str(newsletter_meta.get("receivedAt") or newsletter_data.get("date") or datetime.now(timezone.utc).isoformat())
    message_id = str(newsletter_data.get("messageId") or newsletter_data.get("message_id") or "")

    sponsors = newsletter_data.get("sponsors") or []
    counts = {"created": 0, "deduped": 0, "unmatched": 0, "filtered": 0, "total": len(sponsors)}

    for entry in sponsors:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        url = str(entry.get("url") or "").strip()
        domain = normalize_domain(url, filter_media=False) if url else None
        blurb = str(entry.get("blurb") or entry.get("context") or "Paid placement").strip()

        evidence = url or (f"msg:{message_id}" if message_id else f"newsletter:{nl_name}")
        summary = f"Sponsored {nl_name}: {blurb}"

        item = SignalItem(
            kind="sponsored_elsewhere",
            source="newsletter",
            observed_at=observed_at,
            evidence_url=evidence,
            signal_summary=summary,
            company_name=name or None,
            domain=domain,
            raw_text=blurb,
            review="ok",
        )

        status = process_signal(item, config, client, hold_store, seen_store)
        if status in counts:
            counts[status] += 1

    return counts


# ---- Source C: Human forward reuse (orin@) ----

def process_human_forward(
    email_data: Dict[str, Any],
    config: Dict[str, Any],
    client: DataOpsCRMClient,
    hold_store: UnmatchedHoldStore,
    seen_store: SeenStore,
) -> Dict[str, Any]:
    """Hook for Source C: Parses human forwards to orin@.

    When clear: writes P1 note + creates org if missing.
    When unclear: sends to hold (TTL 14d) and returns Hub intake data.
    """
    subject = str(email_data.get("subject") or "").strip()
    text = str(email_data.get("text") or email_data.get("body") or "").strip()
    message_id = str(email_data.get("message_id") or email_data.get("id") or "").strip()
    received_at = str(email_data.get("received_at") or email_data.get("date") or datetime.now(timezone.utc).isoformat())

    evidence = str(email_data.get("url") or (f"email:{message_id}" if message_id else f"subject:{subject}"))

    # Determine kind
    lowered_corpus = f"{subject} {text}".lower()
    if any(k in lowered_corpus for k in ["sponsor", "sponsored", "partner", "ad slot"]):
        kind = "sponsored_elsewhere"
    else:
        kind = "funding"

    # Extract company, domain, amount, round
    company = email_data.get("company_name") or extract_company_from_title(subject)
    domain = normalize_domain(email_data.get("url") or "") or extract_company_domain_from_text(f"{subject} {text}")
    amount = extract_amount(f"{subject} {text}")
    round_name = extract_round(f"{subject} {text}")

    summary = subject if len(subject) > 10 else (text[:200] if text else "Forwarded company signal")

    item = SignalItem(
        kind=kind,
        source="email",
        observed_at=received_at,
        evidence_url=evidence,
        signal_summary=summary,
        amount=amount,
        round=round_name,
        company_name=company,
        domain=domain,
        raw_text=text,
        review="ok",
    )

    status = process_signal(item, config, client, hold_store, seen_store)
    res = {
        "status": status,
        "item": item,
        "source_key": item.source_key,
    }
    if status == "unmatched":
        res["hub_intake"] = hold_store.format_hub_intake({
            "id": f"unmatched-forward-{message_id or 'unknown'}",
            "reason": "unclear_company_identity_or_domain",
            "item": {
                "kind": item.kind,
                "source": item.source,
                "evidence_url": item.evidence_url,
                "signal_summary": item.signal_summary,
                "raw_text": item.raw_text,
            },
        })
    return res


# ---- Source B: Curated RSS feeds ----

def _clean_text(elem) -> str:
    if elem is None:
        return ""
    text = "".join(elem.itertext())
    return re.sub(r"\s+", " ", text).strip()


def _strip_html(text: str) -> str:
    clean = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(clean)).strip()


def parse_rss_xml(xml_bytes: bytes) -> List[Dict[str, str]]:
    """Parse RSS 2.0 or Atom XML feed entries into normalized dictionaries."""
    try:
        root = ET.fromstring(xml_bytes)
    except Exception as exc:
        logger.warning("Failed to parse RSS XML: %s", exc)
        return []

    entries = []
    # Both RSS items and Atom entries
    for node in root.iter():
        tag = node.tag.rsplit("}", 1)[-1].lower()
        if tag not in ("item", "entry"):
            continue

        children = {c.tag.rsplit("}", 1)[-1].lower(): c for c in node}
        title = _clean_text(children.get("title"))

        link = ""
        link_elem = children.get("link")
        if link_elem is not None:
            link = link_elem.get("href") or link_elem.text or ""
            link = link.strip()

        pub_date = (
            _clean_text(children.get("pubdate"))
            or _clean_text(children.get("published"))
            or _clean_text(children.get("updated"))
            or datetime.now(timezone.utc).isoformat()
        )

        desc = (
            _clean_text(children.get("description"))
            or _clean_text(children.get("summary"))
            or _clean_text(children.get("content"))
        )
        summary = _strip_html(desc)

        guid = _clean_text(children.get("guid")) or _clean_text(children.get("id")) or link

        if title or link:
            entries.append({
                "guid": guid,
                "title": title,
                "link": link,
                "published": pub_date,
                "summary": summary,
            })

    return entries


def fetch_feed_data(url: str, timeout: int = 15) -> bytes:
    """Fetch feed XML data via HTTP."""
    req = urllib.request.Request(url, headers={"User-Agent": RSS_USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def ingest_rss_feed(
    feed_cfg: Dict[str, Any],
    config: Dict[str, Any],
    client: DataOpsCRMClient,
    hold_store: UnmatchedHoldStore,
    seen_store: SeenStore,
    fetch_fn: Optional[Callable[[str], bytes]] = None,
) -> Dict[str, int]:
    """Pull one curated RSS feed and process its signals."""
    url = feed_cfg.get("url")
    if not url:
        return {"created": 0, "deduped": 0, "unmatched": 0, "filtered": 0, "total": 0}

    default_kind = feed_cfg.get("default_kind", "funding")

    fetcher = fetch_fn or fetch_feed_data
    try:
        raw_xml = fetcher(url)
        entries = parse_rss_xml(raw_xml)
    except Exception as exc:
        logger.error("RSS fetch error for %s: %s", url, exc)
        return {"created": 0, "deduped": 0, "unmatched": 0, "filtered": 0, "total": 0, "error": 1}

    counts = {"created": 0, "deduped": 0, "unmatched": 0, "filtered": 0, "total": len(entries)}

    for entry in entries:
        title = entry.get("title", "")
        summary = entry.get("summary", "")
        link = entry.get("link", "")
        published = entry.get("published", "")

        company = extract_company_from_title(title)
        domain = extract_company_domain_from_text(f"{title} {summary}")
        amount = extract_amount(f"{title} {summary}")
        round_name = extract_round(f"{title} {summary}")

        item = SignalItem(
            kind=default_kind,
            source="rss",
            observed_at=published,
            evidence_url=link,
            signal_summary=summary or title,
            amount=amount,
            round=round_name,
            company_name=company,
            domain=domain,
            raw_text=f"{title}\n{summary}",
            review="ok",
        )

        status = process_signal(item, config, client, hold_store, seen_store)
        if status in counts:
            counts[status] += 1

    return counts


# ---- Ingest Runner ----

def run_ingest(
    config_path: Optional[str] = None,
    client: Optional[DataOpsCRMClient] = None,
    hold_store: Optional[UnmatchedHoldStore] = None,
    seen_store: Optional[SeenStore] = None,
    fetch_fn: Optional[Callable[[str], bytes]] = None,
) -> Dict[str, Any]:
    """Execute the full company signal ingestion batch."""
    cfg = load_config(config_path)

    # Initialize dependencies
    settings = cfg.get("settings", {})
    hold = hold_store or UnmatchedHoldStore(
        file_path=settings.get("hold_file_path"),
        ttl_days=settings.get("unmatched_hold_ttl_days", 14),
    )
    seen = seen_store or SeenStore(file_path=settings.get("seen_store_path"))

    crm_client = client or DataOpsCliClient()

    totals = {"created": 0, "deduped": 0, "unmatched": 0, "filtered": 0, "total": 0}
    feed_results = {}

    feeds = cfg.get("feeds") or []
    for feed_info in feeds:
        if not feed_info.get("enabled", True):
            continue
        feed_name = feed_info.get("name", "feed")
        counts = ingest_rss_feed(feed_info, cfg, crm_client, hold, seen, fetch_fn=fetch_fn)
        feed_results[feed_name] = counts
        for key in ("created", "deduped", "unmatched", "filtered", "total"):
            totals[key] += counts.get(key, 0)

    return {
        "totals": totals,
        "feeds": feed_results,
        "hold_active_count": len(hold.list_items()),
        "seen_keys_count": seen.count(),
    }
