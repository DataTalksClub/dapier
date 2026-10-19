"""Tests for company-signal CRM notes ingestion, dedupe, org matching, and unmatched hold."""

import json
import os
import tempfile
import time
import unittest

from src.dapier.crm_signals.contacts import CONTACT_FIELDS, ROLE_PRIORITY, find_contacts_stub
from src.dapier.crm_signals.dataops_client import MockDataOpsClient
from src.dapier.crm_signals.hold import UnmatchedHoldStore
from src.dapier.crm_signals.icp import evaluate_icp
from src.dapier.crm_signals.ingest import (
    format_signal_note_body,
    ingest_rss_feed,
    process_human_forward,
    process_signal,
    reuse_newsletter_sponsors,
    run_ingest,
)
from src.dapier.crm_signals.models import ContactStub, SignalItem
from src.dapier.crm_signals.normalizer import (
    compute_source_key,
    extract_amount,
    extract_company_from_title,
    extract_round,
    normalize_domain,
)
from src.dapier.crm_signals.seen import SeenStore

SAMPLE_RSS_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>TechCrunch</title>
    <link>https://techcrunch.com</link>
    <item>
      <title>Datapine raises $20M in Series A for real-time analytics</title>
      <link>https://techcrunch.com/2026/10/19/datapine-series-a/</link>
      <pubDate>Mon, 19 Oct 2026 10:00:00 GMT</pubDate>
      <description>Datapine, at https://datapine.io, secures $20M Series A funding to expand its data engineering platform.</description>
    </item>
    <item>
      <title>GamblingCo secures $50M for casino platform</title>
      <link>https://techcrunch.com/2026/10/19/gamblingco-raises/</link>
      <pubDate>Mon, 19 Oct 2026 11:00:00 GMT</pubDate>
      <description>GamblingCo, at https://gamblingco.com, raises $50M to build online casino betting games.</description>
    </item>
    <item>
      <title>AI &amp; Data Roundup: 10 startups to watch</title>
      <link>https://techcrunch.com/2026/10/19/ai-data-roundup/</link>
      <pubDate>Mon, 19 Oct 2026 12:00:00 GMT</pubDate>
      <description>A general editorial summary of diverse AI and machine learning startups without a single focal company domain.</description>
    </item>
  </channel>
</rss>
"""


class NormalizerTests(unittest.TestCase):
    def test_normalize_domain_strips_protocol_www_and_path(self):
        self.assertEqual(normalize_domain("https://www.Pinecone.io/docs/quickstart?ref=1"), "pinecone.io")
        self.assertEqual(normalize_domain("http://Databricks.com:443/"), "databricks.com")
        self.assertEqual(normalize_domain("cohere.ai"), "cohere.ai")

    def test_normalize_domain_filters_media_and_aggregators(self):
        self.assertIsNone(normalize_domain("https://techcrunch.com/category/startups/"))
        self.assertIsNone(normalize_domain("https://www.eu-startups.com/2026/10/post/"))
        self.assertIsNone(normalize_domain("https://substack.com/@curator/post"))
        self.assertIsNone(normalize_domain("https://twitter.com/startup/status/123"))

    def test_normalize_domain_keeps_media_when_flag_false(self):
        self.assertEqual(normalize_domain("https://techcrunch.com/page", filter_media=False), "techcrunch.com")

    def test_compute_source_key_format(self):
        key = compute_source_key("funding", "pinecone.io", "https://news.example/pinecone")
        self.assertTrue(key.startswith("signal:funding:pinecone.io:"))
        parts = key.split(":")
        self.assertEqual(len(parts), 4)
        self.assertEqual(parts[0], "signal")
        self.assertEqual(parts[1], "funding")
        self.assertEqual(parts[2], "pinecone.io")
        self.assertEqual(len(parts[3]), 16)

    def test_extract_funding_details(self):
        text = "Acme Corp raises $15M in Series A funding"
        self.assertEqual(extract_amount(text), "$15M")
        self.assertEqual(extract_round(text), "Series A")

        company = extract_company_from_title(text)
        self.assertEqual(company, "Acme Corp")


class ICPEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.icp_config = {
            "allow_keywords": ["data", "machine learning", "ml", "ai", "analytics", "data engineering"],
            "deny_keywords": ["gambling", "casino", "fashion", "apparel"],
            "allowed_stages": ["seed", "series-a", "series-b"],
        }

    def test_allows_valid_data_ml_buyer(self):
        item = SignalItem(
            kind="funding",
            source="rss",
            observed_at="2026-10-19T10:00:00Z",
            evidence_url="https://example.com/item1",
            signal_summary="Acme provides real-time data engineering and machine learning pipelines",
            company_name="Acme",
            domain="acme.ai",
            round="Series A",
        )
        passed, reason = evaluate_icp(item, self.icp_config)
        self.assertTrue(passed)
        self.assertEqual(reason, "ok")

    def test_denies_prohibited_industry(self):
        item = SignalItem(
            kind="funding",
            source="rss",
            observed_at="2026-10-19T10:00:00Z",
            evidence_url="https://example.com/item2",
            signal_summary="BetCo raises $10M for online casino and sports betting platform",
            company_name="BetCo",
            domain="betco.com",
            round="Seed",
        )
        passed, reason = evaluate_icp(item, self.icp_config)
        self.assertFalse(passed)
        self.assertIn("casino", reason)

    def test_filters_non_matching_industry(self):
        item = SignalItem(
            kind="funding",
            source="rss",
            observed_at="2026-10-19T10:00:00Z",
            evidence_url="https://example.com/item3",
            signal_summary="FoodCo raises $5M for organic dog treats bakery",
            company_name="FoodCo",
            domain="foodco.com",
            round="Seed",
        )
        passed, reason = evaluate_icp(item, self.icp_config)
        self.assertFalse(passed)
        self.assertIn("Did not match ICP allow keywords", reason)


class DedupeAndOrgMatchingTests(unittest.TestCase):
    def setUp(self):
        self.client = MockDataOpsClient()
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.hold_file = os.path.join(self.tmp_dir.name, "hold.json")
        self.seen_file = os.path.join(self.tmp_dir.name, "seen.json")
        self.hold_store = UnmatchedHoldStore(self.hold_file, ttl_days=14)
        self.seen_store = SeenStore(self.seen_file)
        self.config = {
            "icp": {
                "allow_keywords": ["data", "ml", "ai", "analytics"],
                "deny_keywords": ["gambling"],
            },
            "settings": {
                "auto_signal_kinds": ["funding", "sponsored_elsewhere"],
            },
        }

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_clear_company_creates_org_and_appends_p1_note(self):
        item = SignalItem(
            kind="funding",
            source="rss",
            observed_at="2026-10-19T12:00:00Z",
            evidence_url="https://news.example/acme-seed",
            signal_summary="Acme raises $5M Seed for vector search and data pipelines",
            amount="$5M",
            round="Seed",
            company_name="Acme Data",
            domain="acme.ai",
        )

        status = process_signal(item, self.config, self.client, self.hold_store, self.seen_store)
        self.assertEqual(status, "created")

        # Verify org was created in CRM
        org = self.client.find_organization_by_domain("acme.ai")
        self.assertIsNotNone(org)
        self.assertEqual(org["displayName"], "Acme Data")
        self.assertEqual(org["domain"], "acme.ai")

        # Verify P1 note was added with sourceKey
        notes = self.client.list_organization_notes(org["id"])
        self.assertEqual(len(notes), 1)
        note = notes[0]
        self.assertEqual(note["sourceKey"], item.source_key)
        self.assertIn("[P1 Signal]", note["body"])
        self.assertIn("Acme Data (acme.ai)", note["body"])
        self.assertIn("Amount: $5M", note["body"])
        self.assertIn("Round: Seed", note["body"])
        self.assertIn(f"Source-Key: {item.source_key}", note["body"])

    def test_clear_company_matches_existing_org_without_duplicate_org(self):
        # Pre-seed an existing org in CRM
        existing = self.client.create_organization("Existing Acme Corp", "acme.ai")

        item = SignalItem(
            kind="funding",
            source="rss",
            observed_at="2026-10-19T12:00:00Z",
            evidence_url="https://news.example/acme-series-a",
            signal_summary="Acme raises $20M Series A for data lakehouse",
            amount="$20M",
            round="Series A",
            company_name="Acme",
            domain="acme.ai",
        )

        status = process_signal(item, self.config, self.client, self.hold_store, self.seen_store)
        self.assertEqual(status, "created")

        # Only one org exists
        self.assertEqual(len(self.client.organizations), 1)

        # Note is attached to the existing org
        notes = self.client.list_organization_notes(existing["id"])
        self.assertEqual(len(notes), 1)
        self.assertIn("Series A", notes[0]["body"])

    def test_dedupe_prevents_duplicate_notes(self):
        item = SignalItem(
            kind="funding",
            source="rss",
            observed_at="2026-10-19T12:00:00Z",
            evidence_url="https://news.example/acme-seed",
            signal_summary="Acme raises $5M Seed for vector search and data pipelines",
            amount="$5M",
            round="Seed",
            company_name="Acme Data",
            domain="acme.ai",
        )

        status1 = process_signal(item, self.config, self.client, self.hold_store, self.seen_store)
        self.assertEqual(status1, "created")

        # Second attempt with identical signal -> deduped
        status2 = process_signal(item, self.config, self.client, self.hold_store, self.seen_store)
        self.assertEqual(status2, "deduped")

        # Verify only 1 note exists in CRM
        org = self.client.find_organization_by_domain("acme.ai")
        notes = self.client.list_organization_notes(org["id"])
        self.assertEqual(len(notes), 1)

    def test_unclear_company_routes_to_unmatched_hold(self):
        # Missing domain -> unclear
        item = SignalItem(
            kind="funding",
            source="rss",
            observed_at="2026-10-19T12:00:00Z",
            evidence_url="https://news.example/unclear-item",
            signal_summary="A stealth data startup raises $3M",
            company_name="Stealth AI",
            domain=None,  # No clear domain
        )

        status = process_signal(item, self.config, self.client, self.hold_store, self.seen_store)
        self.assertEqual(status, "unmatched")

        # Org was NOT created
        self.assertEqual(len(self.client.organizations), 0)

        # Stored in unmatched hold store
        hold_items = self.hold_store.list_items()
        self.assertEqual(len(hold_items), 1)
        self.assertEqual(hold_items[0]["item"]["company_name"], "Stealth AI")
        self.assertEqual(hold_items[0]["reason"], "unclear_company_identity_or_domain")


class UnmatchedHoldStoreTTLTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.hold_file = os.path.join(self.tmp_dir.name, "hold.json")
        self.hold_store = UnmatchedHoldStore(self.hold_file, ttl_days=14)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_ttl_expiry_14d_purges_old_entries(self):
        base_time = 1792400000.0  # arbitrary epoch

        item_old = SignalItem(
            kind="funding",
            source="rss",
            observed_at="2026-10-01T00:00:00Z",
            evidence_url="https://example.com/old-item",
            signal_summary="Old unclear signal",
        )
        # Added 15 days in the past
        self.hold_store.add_unmatched(item_old, now=base_time - (15 * 86400))

        item_fresh = SignalItem(
            kind="funding",
            source="rss",
            observed_at="2026-10-15T00:00:00Z",
            evidence_url="https://example.com/fresh-item",
            signal_summary="Fresh unclear signal",
        )
        # Added 2 days in the past
        self.hold_store.add_unmatched(item_fresh, now=base_time - (2 * 86400))

        # At base_time: item_old has expired (>14 days), item_fresh is active (<14 days)
        active_items = self.hold_store.list_items(now=base_time)
        self.assertEqual(len(active_items), 1)
        self.assertEqual(active_items[0]["item"]["evidence_url"], "https://example.com/fresh-item")

    def test_format_hub_intake_targets_project_orin(self):
        item = SignalItem(
            kind="funding",
            source="rss",
            observed_at="2026-10-19T00:00:00Z",
            evidence_url="https://example.com/unmatched-hub",
            signal_summary="Unclear ML tool funding",
            company_name="AmbiguousML",
        )
        record = self.hold_store.add_unmatched(item)
        payload = self.hold_store.format_hub_intake(record)

        self.assertEqual(payload["project"], "orin")
        self.assertIn("AmbiguousML", payload["title"])
        self.assertEqual(payload["key"], record["id"])
        self.assertIn("hub intake create --project orin", payload["cli_command"])


class SourceHooksTests(unittest.TestCase):
    def setUp(self):
        self.client = MockDataOpsClient()
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.hold_store = UnmatchedHoldStore(os.path.join(self.tmp_dir.name, "h.json"))
        self.seen_store = SeenStore(os.path.join(self.tmp_dir.name, "s.json"))
        self.config = {
            "icp": {"allow_keywords": ["data", "ml", "ai", "vector"], "deny_keywords": ["casino"]},
            "settings": {"auto_signal_kinds": ["funding", "sponsored_elsewhere"]},
        }

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_source_a_newsletter_reuse(self):
        newsletter_envelope = {
            "newsletter": {"name": "The Batch", "receivedAt": "2026-10-15T12:00:00Z"},
            "messageId": "<batch123@deeplearning.ai>",
            "sponsors": [
                {
                    "name": "Qdrant",
                    "url": "https://qdrant.tech",
                    "blurb": "Vector database for high-performance AI search",
                },
                {
                    "name": "CasinoRoyale",
                    "url": "https://casino.example",
                    "blurb": "Play casino poker online",
                },
            ],
        }

        counts = reuse_newsletter_sponsors(
            newsletter_envelope, self.config, self.client, self.hold_store, self.seen_store
        )
        self.assertEqual(counts["total"], 2)
        self.assertEqual(counts["created"], 1)  # Qdrant
        self.assertEqual(counts["filtered"], 1)  # CasinoRoyale (denied by ICP)

        # Qdrant org created
        org = self.client.find_organization_by_domain("qdrant.tech")
        self.assertIsNotNone(org)
        notes = self.client.list_organization_notes(org["id"])
        self.assertEqual(len(notes), 1)
        self.assertIn("[P1 Signal]", notes[0]["body"])
        self.assertIn("kind: sponsored_elsewhere", notes[0]["body"])
        self.assertIn("source: newsletter", notes[0]["body"])

    def test_source_b_rss_feed_ingest(self):
        feed_cfg = {
            "name": "techcrunch-startups",
            "url": "https://techcrunch.com/category/startups/feed/",
            "default_kind": "funding",
        }

        counts = ingest_rss_feed(
            feed_cfg,
            self.config,
            self.client,
            self.hold_store,
            self.seen_store,
            fetch_fn=lambda url: SAMPLE_RSS_XML,
        )

        self.assertEqual(counts["total"], 3)
        self.assertEqual(counts["created"], 1)   # Datapine (clear, passes ICP)
        self.assertEqual(counts["filtered"], 1)  # GamblingCo (denied by ICP)
        self.assertEqual(counts["unmatched"], 1) # Venture Roundup (unclear domain, into hold)

        # Datapine org created
        org = self.client.find_organization_by_domain("datapine.io")
        self.assertIsNotNone(org)

    def test_source_c_human_forward(self):
        email_data = {
            "subject": "Fwd: Modal raises $20M Series A for serverless AI compute",
            "text": "Check out Modal at https://modal.com - they just raised $20M Series A for serverless AI python infra.",
            "message_id": "fwd-modal-001",
            "received_at": "2026-10-19T14:00:00Z",
        }

        res = process_human_forward(
            email_data, self.config, self.client, self.hold_store, self.seen_store
        )
        self.assertEqual(res["status"], "created")

        org = self.client.find_organization_by_domain("modal.com")
        self.assertIsNotNone(org)
        notes = self.client.list_organization_notes(org["id"])
        self.assertEqual(len(notes), 1)
        self.assertIn("source: email", notes[0]["body"])
        self.assertIn("Modal", notes[0]["body"])


class ContactPolicyStubTests(unittest.TestCase):
    def test_contact_policy_enforcement(self):
        stub = find_contacts_stub("org_123")
        self.assertEqual(stub["status"], "TODO_HUMAN_PROMOTE")
        self.assertFalse(stub["policy"]["auto_leads"])
        self.assertFalse(stub["policy"]["silent_send"])
        self.assertTrue(stub["policy"]["human_promote_required"])

        # Check required contact fields from AC
        for field in ("name", "title", "work_email", "linkedin", "source_url", "source_key", "confidence", "dnc"):
            self.assertIn(field, stub["schema_fields"])

    def test_store_contact_in_crm_with_linkedin_only(self):
        from src.dapier.crm_signals.contacts import store_contact_in_crm

        client = MockDataOpsClient()
        org = client.create_organization("Cohere", "cohere.com")

        contact = ContactStub(
            name="Alice Smith",
            role="Head of Partnerships",
            linkedin="https://linkedin.com/in/alicesmith",
            work_email="alice@cohere.com",  # Reference only
            outreach_channel="linkedin",
        )

        res = store_contact_in_crm(org["id"], contact, client)
        self.assertEqual(res["name"], "Alice Smith")
        self.assertEqual(res["organizationId"], org["id"])
        self.assertEqual(res["outreachChannel"], "linkedin")
        self.assertEqual(res["linkedin"], "https://linkedin.com/in/alicesmith")
        self.assertIn("alice@cohere.com", res["emails"])
        self.assertIn("Outreach channel: LinkedIn only", res["notes"])
        self.assertIn("No cold email from datatalks.club", res["notes"])

        # Verify stored in client
        stored = client.list_organization_contacts(org["id"])
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["id"], res["id"])

    def test_cold_email_rejected_by_policy(self):
        from src.dapier.crm_signals.contacts import ContactPolicyViolation, store_contact_in_crm

        client = MockDataOpsClient()
        org = client.create_organization("Pinecone", "pinecone.io")

        contact = ContactStub(
            name="Bob Jones",
            role="DevRel",
            linkedin="https://linkedin.com/in/bobjones",
            work_email="bob@pinecone.io",
            outreach_channel="email",  # Prohibited!
        )

        with self.assertRaises(ContactPolicyViolation):
            store_contact_in_crm(org["id"], contact, client)


class CLITests(unittest.TestCase):
    def test_cli_contacts_command(self):
        from src.dapier.crm_signals import cli
        import io
        from contextlib import redirect_stdout

        out = io.StringIO()
        with redirect_stdout(out):
            ret = cli.main(["contacts", "org_999", "--json"])
        self.assertEqual(ret, 0)
        data = json.loads(out.getvalue())
        self.assertEqual(data["status"], "TODO_HUMAN_PROMOTE")
        self.assertEqual(data["org_id"], "org_999")

    def test_cli_store_contact(self):
        from src.dapier.crm_signals import cli
        import io
        from contextlib import redirect_stdout
        from unittest.mock import patch

        mock_client = MockDataOpsClient()
        mock_client.create_organization("Datapine", "datapine.io")

        out = io.StringIO()
        with redirect_stdout(out), patch("src.dapier.crm_signals.cli.DataOpsCliClient", return_value=mock_client):
            ret = cli.main([
                "contacts", "org_1", "--store",
                "--name", "Carol Danvers",
                "--linkedin", "https://linkedin.com/in/carol",
                "--work-email", "carol@datapine.io",
                "--role", "VP Marketing",
                "--json",
            ])
        self.assertEqual(ret, 0)
        data = json.loads(out.getvalue())
        self.assertEqual(data["name"], "Carol Danvers")
        self.assertEqual(data["outreachChannel"], "linkedin")
        self.assertIn("carol@datapine.io", data["emails"])

    def test_cli_hold_command(self):
        from src.dapier.crm_signals import cli
        import io
        from contextlib import redirect_stdout

        out = io.StringIO()
        with redirect_stdout(out):
            ret = cli.main(["hold", "list", "--json"])
        self.assertEqual(ret, 0)
        data = json.loads(out.getvalue())
        self.assertIsInstance(data, list)



if __name__ == "__main__":
    unittest.main()


