"""Contact finding policy enforcement and stub for future human promote workflow."""

from typing import Any, Dict, List, Optional

from .dataops_client import DataOpsCRMClient
from .models import ContactStub

# Contact fields schema as locked in AC
CONTACT_FIELDS = (
    "name",
    "title",
    "work_email",
    "linkedin",
    "source_url",
    "source_key",
    "confidence",
    "dnc",
)

# Role priority order as locked in AC
ROLE_PRIORITY = (
    "Partnerships",
    "Marketing/Brand",
    "Founder",
    "DevRel",
)


class ContactPolicyViolation(Exception):
    """Raised when an automated script or process attempts auto-lead creation or silent outreach."""
    pass


def find_contacts_stub(org_id: str, client: Optional[DataOpsCRMClient] = None) -> Dict[str, Any]:
    """Stub for contact discovery on an organization with signal notes.

    Enforces Locked AC Rules:
    1. No auto-leads / no silent send. Contacts created ONLY after human promote.
    2. Order of triage:
       a. Existing CRM contacts for this org
       b. Named people on press/funding evidence
       c. Manual LinkedIn search -> URL + title (LinkedIn URL without email = enough)
    3. Role priority: Partnerships > Marketing/Brand > Founder > DevRel.
    4. Log touches on real outreach only; never auto-send.
    5. Deal only when closed. No DTC deliverables from posts.
    """
    notes: List[Dict[str, Any]] = []
    if client:
        try:
            notes = client.list_organization_notes(org_id)
        except Exception:
            notes = []

    has_signal_note = any(
        "[P1 Signal]" in str(n.get("body", "")) or bool(n.get("sourceKey"))
        for n in notes
    )

    return {
        "status": "TODO_HUMAN_PROMOTE",
        "org_id": org_id,
        "has_signal_note": has_signal_note,
        "policy": {
            "auto_leads": False,
            "silent_send": False,
            "human_promote_required": True,
            "rule": "Contacts created only after human promote. No auto sequences or scrapers.",
        },
        "role_priority": list(ROLE_PRIORITY),
        "schema_fields": list(CONTACT_FIELDS),
        "sop_steps": [
            "1. Check if org note exists and passes DTC ICP.",
            "2. Check existing CRM contacts on this organization.",
            "3. Identify named people on the press release / evidence URL.",
            "4. Manual LinkedIn search -> find profile URL + title (LinkedIn URL is sufficient).",
            "5. Role priority: Partnerships > Marketing/Brand > Founder > DevRel.",
            "6. Log touches on real outreach only; never auto-send.",
        ],
    }


def store_contact_in_crm(
    org_id: str,
    contact: ContactStub,
    client: DataOpsCRMClient,
) -> Dict[str, Any]:
    """Store a contact in DataOps CRM linked to an organization.

    Locked Policy (Alexey via Orin):
    - Outreach channel = LinkedIn only (Orin messages later).
    - No cold email from datatalks.club.
    - Work email may be stored for reference only.
    """
    if str(contact.outreach_channel or "").lower() == "email":
        raise ContactPolicyViolation(
            "No cold email from datatalks.club permitted. Outreach channel must be LinkedIn only."
        )

    clean_channel = "linkedin"

    note_lines = [
        "Outreach channel: LinkedIn only (Orin messages later).",
        "Policy: No cold email from datatalks.club. Work email stored for reference only.",
    ]
    if contact.notes:
        note_lines.append(contact.notes.strip())

    emails = [contact.work_email.strip()] if contact.work_email and contact.work_email.strip() else []

    contact_payload = {
        "name": contact.name.strip(),
        "role": contact.role or contact.title or "Target Contact",
        "linkedin": (contact.linkedin or "").strip(),
        "emails": emails,
        "primary": False,
        "outreachChannel": clean_channel,
        "notes": "\n".join(note_lines),
    }
    if contact.source_key:
        contact_payload["sourceKey"] = contact.source_key
    if contact.source_url:
        contact_payload["sourceUrl"] = contact.source_url
    if contact.confidence is not None:
        contact_payload["confidence"] = contact.confidence
    if contact.dnc:
        contact_payload["dnc"] = True

    return client.create_contact(org_id, contact_payload)


def enforce_no_auto_outreach(channel: str = "email"):
    """Safety guard function to verify no automated email sequences run."""
    if channel.lower() == "email":
        raise ContactPolicyViolation("Cold email from datatalks.club is strictly prohibited.")
