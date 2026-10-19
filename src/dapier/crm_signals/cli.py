"""Command-line interface for company signal CRM ingestion and contact policy."""

import argparse
import json
import sys
from typing import List, Optional

from .contacts import find_contacts_stub, store_contact_in_crm
from .dataops_client import DataOpsCliClient
from .hold import UnmatchedHoldStore
from .ingest import load_config, run_ingest
from .models import ContactStub


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="crm-signals",
        description="Company signal ingestion into DataOps CRM notes",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # Ingest command
    ingest_p = sub.add_parser("ingest", help="Run the company signal ingest job")
    ingest_p.add_argument("--config", default=None, help="Path to crm-signal-feeds.yaml")
    ingest_p.add_argument("--json", action="store_true", help="Output summary as JSON")

    # Contacts command (enforces policy stub or stores contact in CRM)
    contacts_p = sub.add_parser("contacts", help="Contact policy, human promote stub, or store contact in CRM")
    contacts_p.add_argument("org_id", help="Organization ID to inspect or add contact to")
    contacts_p.add_argument("--store", action="store_true", help="Store a promoted contact in CRM (LinkedIn only)")
    contacts_p.add_argument("--name", help="Contact name (required with --store)")
    contacts_p.add_argument("--linkedin", help="Contact LinkedIn URL (required with --store)")
    contacts_p.add_argument("--role", default=None, help="Contact role / title")
    contacts_p.add_argument("--work-email", default=None, help="Work email (reference only; no cold email)")
    contacts_p.add_argument("--json", action="store_true", help="Output as JSON")

    # Hold command
    hold_p = sub.add_parser("hold", help="Inspect or purge unmatched hold items (14d TTL)")
    hold_p.add_argument("action", choices=["list", "purge"], nargs="?", default="list", help="Hold action")
    hold_p.add_argument("--config", default=None, help="Path to crm-signal-feeds.yaml")
    hold_p.add_argument("--json", action="store_true", help="Output as JSON")

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "ingest":
        results = run_ingest(config_path=args.config)
        if args.json:
            print(json.dumps(results, indent=2))
        else:
            totals = results.get("totals", {})
            print("=== Company Signal Ingest Complete ===")
            print(f"Total entries processed: {totals.get('total', 0)}")
            print(f"Created P1 org notes:    {totals.get('created', 0)}")
            print(f"Deduped (already seen):  {totals.get('deduped', 0)}")
            print(f"Unmatched (hold TTL 14d):{totals.get('unmatched', 0)}")
            print(f"Filtered (non-ICP):      {totals.get('filtered', 0)}")
            print(f"Active in hold store:    {results.get('hold_active_count', 0)}")
        return 0

    if args.command == "contacts":
        if args.store:
            if not args.name or not args.linkedin:
                print("Error: --name and --linkedin are required when using --store.", file=sys.stderr)
                return 2
            contact = ContactStub(
                name=args.name,
                role=args.role,
                linkedin=args.linkedin,
                work_email=args.work_email,
                outreach_channel="linkedin",
            )
            client = DataOpsCliClient()
            try:
                res = store_contact_in_crm(args.org_id, contact, client)
                if args.json:
                    print(json.dumps(res, indent=2))
                else:
                    print(f"Stored contact in CRM for org {args.org_id}: {contact.name} (LinkedIn: {contact.linkedin})")
                    print("Policy: Outreach channel = LinkedIn only (Orin messages later). Work email stored for reference only.")
                return 0
            except Exception as exc:
                print(f"Error storing contact: {exc}", file=sys.stderr)
                return 1

        res = find_contacts_stub(args.org_id)
        if args.json:
            print(json.dumps(res, indent=2))
        else:
            print(f"=== Contact Stub for Org: {args.org_id} ===")
            print("Status: Human Promote Required (No auto-leads / no silent send)")
            print("SOP Steps:")
            for step in res.get("sop_steps", []):
                print(f"  {step}")
            print("\nRole Priority Order:")
            for r in res.get("role_priority", []):
                print(f"  - {r}")
            print("\nTarget Fields:")
            print("  " + ", ".join(res.get("schema_fields", [])))
            print("\nOutreach Policy:")
            print("  Outreach channel = LinkedIn only (Orin messages later).")
            print("  No cold email from datatalks.club. Work email for reference only.")
        return 0

    if args.command == "hold":
        cfg = load_config(args.config)
        settings = cfg.get("settings", {})
        hold_store = UnmatchedHoldStore(
            file_path=settings.get("hold_file_path"),
            ttl_days=settings.get("unmatched_hold_ttl_days", 14),
        )
        if args.action == "purge":
            purged = hold_store.purge_expired()
            print(f"Purged {purged} expired items (TTL > 14 days).")
            return 0
        else:
            items = hold_store.list_items()
            if args.json:
                print(json.dumps(items, indent=2))
            else:
                print(f"=== Unmatched Hold Store ({len(items)} items) ===")
                for item in items:
                    it = item.get("item", {})
                    print(f"- [{item.get('id')}] {it.get('company_name') or it.get('evidence_url')} (expires: {item.get('expires_at')})")
            return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
