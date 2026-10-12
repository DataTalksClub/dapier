"""dataops sponsors-leads intake body: contract 2026-10-10 envelope.

Dapier is only the pipe — one LLM extraction plus this formatter. Sponsor
records and cross-email dedup live in DataOps (POST /api/v1/intake/leads).
These tests pin the envelope without touching the network: run_dataops is
exercised with a stubbed _json_request, like the ai_complete transport seam.
The intake URL comes from the DataOps connection origin, not the job.
"""
import unittest
from copy import deepcopy
from unittest.mock import patch

from src.dapier.engine.actions import dataops as dataops_action


def email_event(**overrides):
    event = {
        "connector": "email",
        "event": "message.received",
        "id": "evt-news-1",
        "occurred_at": "2026-10-10T08:00:00+00:00",
        "data": {
            "message_id": "<abc123@example.test>",
            "route": "newsletter",
            "subject": "Data Bites Weekly #42",
            "date": "Fri, 10 Oct 2026 08:00:00 +0000",
            "sender": {"addresses": ["news@example.test"]},
        },
    }
    event["data"].update(overrides)
    return event


def extraction_steps(newsletter="Data Bites Weekly", sponsors=(), step="extract-sponsors"):
    return {step: {"status": "completed", "output": {
        "ok": True, "data": {"newsletter": newsletter, "sponsors": list(sponsors)},
        "model": "test-model", "usage": {}}}}


def leads_action(**overrides):
    action = {"type": "dataops", "auth_secret_id": "dapier/dataops",
              "connection_id": "dataops", "extraction_step": "extract-sponsors"}
    action.update(overrides)
    return action


def dataops_connection(url="https://ops.dtcdev.click"):
    return {"connection_id": "dataops", "provider": "dataops",
            "status": "connected", "url": url}


def patch_connection(url="https://ops.dtcdev.click"):
    return patch("src.dapier.engine.actions.dataops.base._connected_connection",
                 return_value=dataops_connection(url))


def body(action=None, event=None, steps=None):
    return dataops_action._leads_intake_body(
        action or leads_action(),
        event or email_event(),
        steps=steps if steps is not None else extraction_steps(sponsors=[
            {"name": "Acme Cloud", "url": "https://acme.example/",
             "category": "tool", "blurb": "Try Acme free",
             "context": "mid-roll"}]))


class RoutingTests(unittest.TestCase):
    def test_extraction_step_routes_to_the_leads_body(self):
        assert dataops_action._is_leads_action(leads_action()) is True

    def test_omitting_extraction_step_routes_to_documents(self):
        action = {"type": "dataops", "auth_secret_id": "dapier/dataops",
                  "connection_id": "dataops"}
        assert dataops_action._is_leads_action(action) is False

    def test_intake_body_dispatches_to_leads(self):
        envelope = dataops_action._intake_body(
            leads_action(), email_event(),
            steps=extraction_steps(sponsors=[{"name": "Acme"}]))
        assert envelope["version"] == "2026-10-10"
        assert envelope["sponsors"] == [{"name": "Acme"}]


class TargetUrlTests(unittest.TestCase):
    def test_leads_appends_the_leads_path(self):
        with patch_connection():
            assert dataops_action._target_url(leads_action()) == (
                "https://ops.dtcdev.click/api/v1/intake/leads")

    def test_documents_appends_the_documents_path(self):
        action = {"type": "dataops", "connection_id": "dataops",
                  "auth_secret_id": "dapier/dataops"}
        with patch_connection("https://intake.test"):
            assert dataops_action._target_url(action) == (
                "https://intake.test/api/v1/intake/email-documents")

    def test_missing_connection_url_fails_loudly(self):
        with patch_connection(""), self.assertRaisesRegex(ValueError, "no url"):
            dataops_action._target_url(leads_action())

    def test_missing_connection_id_fails_loudly(self):
        with self.assertRaisesRegex(ValueError, "connection_id"):
            dataops_action._target_url({"type": "dataops"})

    def test_non_dataops_connection_fails_loudly(self):
        with patch("src.dapier.engine.actions.dataops.base._connected_connection",
                   return_value={"connection_id": "dropbox", "provider": "dropbox",
                                 "status": "connected"}), \
             self.assertRaisesRegex(ValueError, "not a DataOps connection"):
            dataops_action._target_url(leads_action(connection_id="dropbox"))


class EnvelopeTests(unittest.TestCase):
    def test_happy_path_envelope(self):
        envelope = body()
        assert envelope == {
            "version": "2026-10-10",
            "messageId": "<abc123@example.test>",
            "newsletter": {
                "name": "Data Bites Weekly",
                "sender": "news@example.test",
                "subject": "Data Bites Weekly #42",
                "receivedAt": "2026-10-10T08:00:00Z",
            },
            "sponsors": [{"name": "Acme Cloud", "url": "https://acme.example",
                          "category": "tool", "blurb": "Try Acme free",
                          "context": "mid-roll"}],
        }

    def test_dict_newsletter_reads_title_then_name(self):
        steps = extraction_steps(
            newsletter={"title": "Data Bites", "issueDate": "2026-10-10"},
            sponsors=[{"name": "Acme"}])
        assert body(steps=steps)["newsletter"]["name"] == "Data Bites"
        steps = extraction_steps(newsletter={"name": "Morning Brew"},
                                 sponsors=[{"name": "Acme"}])
        assert body(steps=steps)["newsletter"]["name"] == "Morning Brew"

    def test_non_https_and_junk_urls_are_omitted_not_fatal(self):
        steps = extraction_steps(sponsors=[
            {"name": "A", "url": "http://plain.example/"},
            {"name": "B", "url": "not a url"},
            {"name": "C", "url": "https://user:pass@evil.example/"},
            {"name": "D"},
        ])
        sponsors = body(steps=steps)["sponsors"]
        assert sponsors == [{"name": "A"}, {"name": "B"}, {"name": "C"}, {"name": "D"}]

    def test_duplicates_collapse_case_insensitively_first_wins(self):
        steps = extraction_steps(sponsors=[
            {"name": "Acme Cloud", "url": "https://acme.example/", "blurb": "first"},
            {"name": "acme cloud", "url": "https://ACME.example", "blurb": "dup"},
            {"name": "Beta DB", "url": "", "context": "sidebar"},
        ])
        sponsors = body(steps=steps)["sponsors"]
        assert sponsors == [
            {"name": "Acme Cloud", "url": "https://acme.example", "blurb": "first"},
            {"name": "Beta DB", "context": "sidebar"},
        ]

    def test_same_link_under_different_name_readings_collapses(self):
        steps = extraction_steps(sponsors=[
            {"name": "Drive", "url": "https://acme.example/guide"},
            {"name": "Drive Fund Admin Data Quality Without Scrambling",
             "url": "https://acme.example/guide/"},
        ])
        assert body(steps=steps)["sponsors"] == [
            {"name": "Drive", "url": "https://acme.example/guide"}]

    def test_nameless_entries_drop_out(self):
        steps = extraction_steps(sponsors=[
            {"name": "", "url": "", "blurb": "footer link"},
            {"url": "https://gamma.example"},
            {"name": "Gamma", "url": "https://gamma.example"},
        ])
        assert body(steps=steps)["sponsors"] == [
            {"name": "Gamma", "url": "https://gamma.example"}]

    def test_empty_sponsors_fails_loudly(self):
        with self.assertRaisesRegex(ValueError, "no sponsors"):
            body(steps=extraction_steps(sponsors=[]))

    def test_more_than_twenty_sponsors_fails_loudly(self):
        sponsors = [{"name": f"Sponsor {i}"} for i in range(21)]
        with self.assertRaisesRegex(ValueError, "at most 20"):
            body(steps=extraction_steps(sponsors=sponsors))

    def test_missing_extraction_step_fails_loudly(self):
        with self.assertRaisesRegex(ValueError, "extract-sponsors"):
            dataops_action._leads_intake_body(
                leads_action(), email_event(), steps={})

    def test_ok_false_extraction_fails_loudly(self):
        steps = {"extract-sponsors": {"status": "completed", "output": {
            "ok": False, "error": "not JSON", "text": "sorry"}}}
        with self.assertRaisesRegex(ValueError, "extract-sponsors"):
            dataops_action._leads_intake_body(
                leads_action(), email_event(), steps=steps)

    def test_custom_extraction_step_is_honored(self):
        steps = extraction_steps(sponsors=[{"name": "Custom"}], step="my-extract")
        envelope = dataops_action._leads_intake_body(
            leads_action(extraction_step="my-extract"), email_event(), steps=steps)
        assert envelope["sponsors"] == [{"name": "Custom"}]

    def test_missing_message_id_fails_loudly(self):
        event = email_event()
        del event["data"]["message_id"]
        with self.assertRaisesRegex(ValueError, "message_id"):
            dataops_action._leads_intake_body(
                leads_action(), event,
                steps=extraction_steps(sponsors=[{"name": "Acme"}]))

    def test_unparseable_date_fails_loudly(self):
        event = email_event(date="not a date")
        event["occurred_at"] = "also not a date"
        with self.assertRaisesRegex(ValueError, "parseable email date"):
            dataops_action._leads_intake_body(
                leads_action(), event,
                steps=extraction_steps(sponsors=[{"name": "Acme"}]))


class PostTests(unittest.TestCase):
    def test_run_posts_the_envelope_with_the_intake_secret(self):
        seen = {}

        def fake_request(url, payload, headers=None, timeout=15):
            seen.update(url=url, payload=payload, headers=headers)
            return {"accepted": True}

        steps = extraction_steps(sponsors=[{"name": "Acme"}])
        with patch("src.dapier.engine.actions.base.secrets_value",
                   return_value='{"token": "tok-123"}'), \
             patch("src.dapier.engine.actions.dataops.base._json_request",
                   side_effect=fake_request), \
             patch_connection():
            result = dataops_action.run_dataops(leads_action(), email_event(), steps=steps)
        assert result == {"accepted": True}
        assert seen["url"] == "https://ops.dtcdev.click/api/v1/intake/leads"
        assert seen["headers"]["x-dataops-intake-secret"] == "tok-123"
        assert seen["payload"]["version"] == "2026-10-10"
        assert seen["payload"]["messageId"] == "<abc123@example.test>"

    def test_posts_to_the_connection_origin(self):
        seen = {}

        def fake_request(url, payload, headers=None, timeout=15):
            seen["url"] = url
            return {"accepted": True}

        with patch("src.dapier.engine.actions.base.secrets_value",
                   return_value="tok-123"), \
             patch("src.dapier.engine.actions.dataops.base._json_request",
                   side_effect=fake_request), \
             patch_connection("https://ops.example.test"):
            dataops_action.run_dataops(
                leads_action(), email_event(),
                steps=extraction_steps(sponsors=[{"name": "Acme"}]))
        assert seen["url"] == "https://ops.example.test/api/v1/intake/leads"

    def test_deepcopy_of_event_is_untouched(self):
        event = email_event()
        snapshot = deepcopy(event)
        body(steps=extraction_steps(sponsors=[{"name": "Acme"}]))
        assert event == snapshot


if __name__ == "__main__":
    unittest.main()
