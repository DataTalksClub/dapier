"""Data models for company signals and contact stubs."""

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class SignalItem:
    """A single company signal from RSS, newsletter reuse, or forwarded email."""

    kind: str  # "funding" | "sponsored_elsewhere"
    source: str  # "newsletter" | "rss" | "email"
    observed_at: str  # ISO-8601 timestamp string
    evidence_url: str  # URL or source ref
    signal_summary: str  # Summary of the signal
    amount: Optional[str] = None  # e.g. "$10M", "€5M"
    round: Optional[str] = None  # e.g. "Series A", "Seed"
    review: str = "ok"  # "ok" | "needed"
    company_name: Optional[str] = None
    domain: Optional[str] = None  # Normalized company domain
    raw_text: Optional[str] = None
    source_key: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def is_clear(self) -> bool:
        """A company is clear when it has both a non-empty name and a normalized domain.

        Unmatched only if unclear.
        """
        name = str(self.company_name or "").strip()
        domain = str(self.domain or "").strip()
        return bool(name and domain)


@dataclass
class ContactStub:
    """Contact record schema stored in CRM.

    Policy (Alexey via Orin):
    - Outreach channel = LinkedIn only (Orin messages later).
    - No cold email from datatalks.club.
    - Work email may be stored for reference only.
    """

    name: str
    organization_id: Optional[str] = None
    title: Optional[str] = None
    role: Optional[str] = None
    work_email: Optional[str] = None  # Reference only! No cold email
    linkedin: Optional[str] = None  # Canonical outreach channel
    source_url: Optional[str] = None
    source_key: Optional[str] = None
    confidence: Optional[float] = None
    dnc: bool = False  # Do Not Contact flag
    outreach_channel: str = "linkedin"  # Locked: LinkedIn only
    notes: Optional[str] = None
