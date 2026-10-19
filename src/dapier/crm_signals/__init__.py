"""Dapier company signals to DataOps CRM notes package."""

from .contacts import ContactPolicyViolation, find_contacts_stub, store_contact_in_crm
from .dataops_client import DataOpsCRMClient, DataOpsCliClient, MockDataOpsClient
from .hold import UnmatchedHoldStore
from .icp import evaluate_icp
from .ingest import (
    format_signal_note_body,
    ingest_rss_feed,
    process_human_forward,
    process_signal,
    reuse_newsletter_sponsors,
    run_ingest,
)
from .models import ContactStub, SignalItem
from .normalizer import compute_source_key, normalize_domain
from .seen import SeenStore

__all__ = [
    "ContactPolicyViolation",
    "ContactStub",
    "DataOpsCRMClient",
    "DataOpsCliClient",
    "MockDataOpsClient",
    "SeenStore",
    "SignalItem",
    "UnmatchedHoldStore",
    "compute_source_key",
    "evaluate_icp",
    "find_contacts_stub",
    "format_signal_note_body",
    "ingest_rss_feed",
    "normalize_domain",
    "process_human_forward",
    "process_signal",
    "reuse_newsletter_sponsors",
    "run_ingest",
    "store_contact_in_crm",
]

