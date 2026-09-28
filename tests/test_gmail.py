"""The Gmail chip: the gmail.messages poll source, the gmail_send action,
the chip's sample/label discoveries, and the connection test it rides.

Zapier's "New Email" / "Send Email" over a Google connection: a stored
poll trigger with the chip's source reads the mailbox on its schedule
(OAuth token refreshed through poll_triggers._bearer_token) and publishes
gmail/message.received, scoped per trigger through the poll-name filter.
Covered here: save-time validation, the seeded first fire (enabling must
not fire the mailbox's history), strictly-new fetches keyed on
internalDate, end-to-end fires on fake tables, the send action's raw MIME,
the sample pulls (live/history/synthetic), the label options, and the
connection test.

There is deliberately no gmail-keyed ConnectionTest: Gmail rides the
shared "google" provider (like Sheets, Drive and Calendar), whose
registration already answers with the account's email as identity — a
test under "gmail" would be unreachable (no connection carries provider
"gmail") and one under "google" would clobber the shared check.
"""
import base64
import json
import urllib.parse
from unittest.mock import patch

import pytest

from src.dapier.connectors import gmail, registry  # noqa: F401  (import = registration)
from src.dapier.connectors import trigger_discovery
from src.dapier.connections import discovery as provider
from src.dapier.connections import tokens
from src.dapier.engine.actions.gmail import run_gmail_send
from src.dapier.triggers import poll_sources, poll_triggers
from src.dapier.triggers.email_triggers import TriggerError


# --- fakes -----------------------------------------------------------------------


class MailboxTransport:
    """Routes the poll's two call shapes by URL: messages.list pages in
    order, then one messages.get per stub from the mailbox, recording every
    call. A non-200 ``status`` fails every call with Gmail's error shape."""

    def __init__(self, pages=(), messages=(), *, status=200):
        self.pages = list(pages)
        self.messages = {message["id"]: message for message in messages}
        self.status = status
        self.calls = []

    def __call__(self, method, url, *, headers=None, body=None, timeout=15):
        self.calls.append({"method": method, "url": url, "headers": headers})
        if self.status >= 300:
            return self.status, json.dumps(
                {"error": {"message": "mailbox exploded"}}).encode()
        if "/users/me/messages?" in url:
            page = self.pages.pop(0) if self.pages else {"messages": []}
            return 200, json.dumps(page).encode()
        message_id = urllib.parse.unquote(url.rsplit("/", 1)[-1].split("?")[0])
        return 200, json.dumps(self.messages.get(message_id)
                               or {"id": message_id, "internalDate": "0"}).encode()


class ApiTransport:
    """Route provider calls by URL substring to canned JSON responses
    (the send action and the label listing's seam)."""

    def __init__(self, *routes):
        self.routes = routes  # (substring, status, payload) tuples
        self.calls = []

    def __call__(self, method, url, *, headers=None, body=None, timeout=15):
        self.calls.append({"method": method, "url": url, "headers": headers,
                           "body": body})
        for substring, status, payload in self.routes:
            if substring in url:
                return status, json.dumps(payload).encode()
        raise AssertionError(f"unexpected provider call: {method} {url}")


class FakePollTable:
    def __init__(self):
        self.items = {}

    def scan(self, **_kwargs):
        return {"Items": [dict(item) for item in self.items.values()]}

    def get_item(self, Key):
        item = self.items.get(Key["poll_id"])
        return {"Item": dict(item)} if item else {}

    def put_item(self, Item):
        self.items[Item["poll_id"]] = dict(Item)

    def delete_item(self, Key):
        self.items.pop(Key["poll_id"], None)


class FakeCursorTable:
    """DynamoDB stand-in keyed by cursor_id (poll cursors) or scope_id
    (the seen store's per-trigger sets)."""

    def __init__(self):
        self.items = {}

    @staticmethod
    def _partition(key_or_item):
        return (key_or_item.get("cursor_id")
                if "cursor_id" in key_or_item else key_or_item.get("scope_id"))

    def get_item(self, Key):
        item = self.items.get(self._partition(Key))
        return {"Item": dict(item)} if item else {}

    def put_item(self, Item):
        self.items[self._partition(Item)] = dict(Item)

    def delete_item(self, Key):
        self.items.pop(self._partition(Key), None)


class FakeEvents:
    def put_rule(self, **_kwargs):
        pass

    def put_targets(self, **_kwargs):
        pass

    def remove_targets(self, Rule, Ids, **_kwargs):
        pass

    def delete_rule(self, Name, **_kwargs):
        pass


def gmail_stub(message_id):
    """A messages.list entry: bare ids, the API's listing shape."""
    return {"id": message_id, "threadId": message_id}


def gmail_message(message_id, internal_date, text="Invoice #4137 is attached.",
                  **extra):
    """One messages.get resource: the addressing headers, snippet,
    internalDate, and one text/plain part stored base64url-unpadded."""
    data = base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")
    payload = {
        "mimeType": "multipart/alternative",
        "headers": [
            {"name": "From", "value": "Acme Billing <billing@example.test>"},
            {"name": "To", "value": "todo@dtcdev.click"},
            {"name": "Subject", "value": "Invoice #4137 - September"},
            {"name": "Date", "value": "Mon, 28 Sep 2026 09:14:03 +0000"},
        ],
        "parts": [{"mimeType": "text/plain", "body": {"data": data}}],
    }
    return {"id": message_id, "threadId": message_id,
            "snippet": "Invoice #4137 for September is attached.",
            "internalDate": internal_date, "payload": payload, **extra}


def poll_body(**overrides):
    body = {
        "name": "gmail-news",
        "expression": "rate(5 minutes)",
        "source": "gmail.messages",
        "connection_id": "google",
        "actions": [{"type": "email_send", "to": "ops@example.test"}],
    }
    body.update(overrides)
    return body


def stored(body):
    """The stored item build_item would persist for ``body``."""
    return poll_triggers.build_item(body, "op@example.test")


@pytest.fixture
def google_transport(monkeypatch):
    """Install a canned transport at the shared provider seam and pin the
    OAuth token refresh; returns the installed transport."""
    def install(transport):
        monkeypatch.setattr(provider, "_default_transport", transport)
        monkeypatch.setattr(poll_triggers, "_bearer_token",
                            lambda connection_id: "fresh-token")
        return transport
    return install


@pytest.fixture(autouse=True)
def fixed_seed(monkeypatch):
    # Before every fixture stamp, so existing mail is history and the fire
    # test's new message is strictly after the seed.
    monkeypatch.setattr(gmail, "_gmail_now", lambda: "1790500000000")


@pytest.fixture
def no_history(monkeypatch):
    monkeypatch.setattr(trigger_discovery, "history_sample",
                        lambda connector, event=None: None)


# --- registration -----------------------------------------------------------------


def test_the_source_resolves_through_the_builtin_module_seam():
    spec = poll_sources.resolve("gmail.messages")
    assert (spec.connector, spec.event) == ("gmail", "message.received")
    with pytest.raises(ValueError, match="must be one of"):
        poll_sources.resolve("gmail.nope")


def test_the_chip_matches_its_poll_source():
    chip = registry.CONNECTORS["gmail"]
    source = poll_sources.resolve("gmail.messages")
    assert chip.label == "Gmail"
    assert source.event in chip.events
    assert source.label == chip.label
    assert "gmail.messages" in poll_sources.source_names()


def test_public_view_names_the_query():
    view = poll_triggers.public_view(stored(poll_body(query="label:INBOX")))

    assert view["source"] == "gmail.messages"
    assert view["query"] == "label:INBOX"


# --- save validation --------------------------------------------------------------


def test_save_stores_the_fetch_spec():
    item = stored(poll_body(query="label:Newsletters"))

    assert item["source"] == "gmail.messages"
    assert item["query"] == "label:Newsletters"
    assert item["connection_id"] == "google"
    assert item["cursor_mode"] == "next_cursor"
    assert item["id_path"] == "id"
    assert item["url"] == ""


def test_save_defaults_the_query_to_a_whole_mailbox_watch():
    assert stored(poll_body())["query"] == ""


def test_save_requires_a_connection():
    with pytest.raises(TriggerError, match="connection_id"):
        stored(poll_body(connection_id=""))


# --- fetch: seed at now, then strictly-newer mail ----------------------------------


def test_first_fetch_seeds_at_now_without_emitting_or_expanding(google_transport):
    transport = google_transport(MailboxTransport(
        pages=[{"messages": [gmail_stub("m-1")]}]))
    item = stored(poll_body())

    items, next_cursor = gmail._gmail_poll_fetch(item, None)

    assert items == []
    assert next_cursor == "1790500000000"
    assert transport.calls[0]["url"].startswith(
        "https://gmail.googleapis.com/gmail/v1/users/me/messages?")
    assert transport.calls[0]["headers"]["authorization"] == "Bearer fresh-token"
    # the seed only lists: no messages.get per existing stub
    assert len(transport.calls) == 1


def test_next_fetch_returns_only_newer_mail_oldest_first(google_transport):
    google_transport(MailboxTransport(
        pages=[{"messages": [gmail_stub("m-3"), gmail_stub("m-1"),
                             gmail_stub("m-2")]}],
        messages=[gmail_message("m-1", "1790584441000"),
                  gmail_message("m-2", "1790584442000"),
                  gmail_message("m-3", "1790584443000")]))
    item = stored(poll_body())

    items, next_cursor = gmail._gmail_poll_fetch(item, "1790584441500")

    assert [entry["id"] for entry in items] == ["m-2", "m-3"]
    assert next_cursor == "1790584443000"


def test_no_new_mail_keeps_the_cursor(google_transport):
    google_transport(MailboxTransport(
        pages=[{"messages": [gmail_stub("m-1")]}],
        messages=[gmail_message("m-1", "1790584441000")]))
    item = stored(poll_body())

    items, next_cursor = gmail._gmail_poll_fetch(item, "1790584442000")

    assert items == []
    assert next_cursor == "1790584442000"


def test_items_carry_the_headers_snippet_and_decoded_body(google_transport):
    google_transport(MailboxTransport(
        pages=[{"messages": [gmail_stub("m-1")]}],
        messages=[gmail_message("m-1", "1790584443000",
                                text="Invoice #4137 for September is attached.")]))
    item = stored(poll_body())

    items, _next = gmail._gmail_poll_fetch(item, "1790584442000")

    assert items == [{
        "id": "m-1", "thread_id": "m-1",
        "from": "Acme Billing <billing@example.test>",
        "to": "todo@dtcdev.click", "cc": None,
        "subject": "Invoice #4137 - September",
        "date": "Mon, 28 Sep 2026 09:14:03 +0000",
        "internal_date": "1790584443000",
        "snippet": "Invoice #4137 for September is attached.",
        "text": "Invoice #4137 for September is attached.",
    }]


def test_a_message_without_a_stamp_never_fires(google_transport):
    google_transport(MailboxTransport(
        pages=[{"messages": [gmail_stub("m-1")]}],
        messages=[{"id": "m-1", "threadId": "m-1", "payload": {}}]))
    item = stored(poll_body())

    items, next_cursor = gmail._gmail_poll_fetch(item, "1790584442000")

    assert items == []
    assert next_cursor == "1790584442000"


def test_the_query_rides_the_listing_and_pages_follow(google_transport):
    transport = google_transport(MailboxTransport(
        pages=[{"messages": [gmail_stub("m-1")], "nextPageToken": "page-2"},
               {"messages": [gmail_stub("m-2")]}],
        messages=[gmail_message("m-2", "1790584443000")]))
    item = stored(poll_body(query="label:Newsletters"))

    items, next_cursor = gmail._gmail_poll_fetch(item, "1790584442000")

    assert "q=label%3ANewsletters" in transport.calls[0]["url"]
    assert "pageToken=page-2" in transport.calls[1]["url"]
    assert [entry["id"] for entry in items] == ["m-2"]
    assert next_cursor == "1790584443000"


def test_failed_fetch_raises_runtimeerror(google_transport):
    google_transport(MailboxTransport(status=502))
    item = stored(poll_body())

    with pytest.raises(RuntimeError, match="mailbox exploded"):
        gmail._gmail_poll_fetch(item, "1790584442000")


# --- end-to-end fires on fake tables ------------------------------------------------


def saved_trigger(body):
    polls, cursors, events = FakePollTable(), FakeCursorTable(), FakeEvents()
    poll_triggers.api_save(body, "op@example.test", table_ref=polls,
                           cursor_table_ref=cursors, events_client=events,
                           target_arn="arn:worker")
    return polls, cursors


def run_fire(name, polls, cursors, transport):
    """One scheduled fire against a canned mailbox."""
    fired = []
    with patch.object(provider, "_default_transport", transport), \
         patch.object(poll_triggers, "_bearer_token", return_value="fresh-token"), \
         patch("src.dapier.engine.execute",
               side_effect=lambda event, **_kwargs: fired.append(event)), \
         patch("src.dapier.engine.notify.notify_failure"):
        result = poll_triggers.fire(name, table_ref=polls, cursor_table_ref=cursors)
    return result, fired


def test_fire_seeds_then_emits_only_new_mail_without_duplicates():
    polls, cursors = saved_trigger(poll_body())
    mailbox = MailboxTransport(
        pages=[{"messages": [gmail_stub("m-1")]}],
        messages=[gmail_message("m-1", "1790058441000")])

    result, fired = run_fire("gmail-news", polls, cursors, mailbox)
    assert result == {"poll": "gmail-news", "fired": 0}
    assert poll_triggers.get_cursor("gmail-news", table=cursors) == "1790500000000"

    mailbox = MailboxTransport(
        pages=[{"messages": [gmail_stub("m-2"), gmail_stub("m-1")]}],
        messages=[gmail_message("m-1", "1790058441000"),
                  gmail_message("m-2", "1790584442000")])
    result, fired = run_fire("gmail-news", polls, cursors, mailbox)

    assert result == {"poll": "gmail-news", "fired": 1}
    assert len(fired) == 1
    event = fired[0]
    assert (event["connector"], event["event"]) == ("gmail", "message.received")
    assert event["source"] == "gmail-news"
    assert event["data"]["poll"] == "gmail-news"
    assert event["data"]["item_id"] == "m-2"
    assert event["data"]["subject"] == "Invoice #4137 - September"
    assert poll_triggers.get_cursor("gmail-news", table=cursors) == "1790584442000"

    # the recycled page re-lists m-1, now behind the parked cursor: nothing
    # fires again and the cursor stays put
    result, fired = run_fire("gmail-news", polls, cursors, mailbox)
    assert result == {"poll": "gmail-news", "fired": 0}
    assert fired == []
    assert poll_triggers.get_cursor("gmail-news", table=cursors) == "1790584442000"


def test_the_budget_parks_no_cursor_and_the_seen_set_dedupes_the_refetch():
    """max_items 1 over a page of two fresh messages: the budget fires the
    older one and leaves the cursor parked below the undrained page, the
    refetch recognizes it in the seen-set and fires only the second, and
    the drained page finally parks the cursor at the newest stamp."""
    polls, cursors = saved_trigger(poll_body(max_items=1))

    def mailbox():
        """A fresh canned page per fire: the provider recycles the listing."""
        return MailboxTransport(
            pages=[{"messages": [gmail_stub("m-2"), gmail_stub("m-1")]}],
            messages=[gmail_message("m-1", "1790584441000"),
                      gmail_message("m-2", "1790584442000")])

    result, fired = run_fire("gmail-news", polls, cursors, mailbox())  # seed
    assert result["fired"] == 0

    result, fired = run_fire("gmail-news", polls, cursors, mailbox())
    assert [event["data"]["id"] for event in fired] == ["m-1"]
    # the page still had a fresh item: the fetch's next cursor is not parked
    assert poll_triggers.get_cursor("gmail-news", table=cursors) is not None

    result, fired = run_fire("gmail-news", polls, cursors, mailbox())
    assert result.get("skipped_seen") == 1
    assert [event["data"]["id"] for event in fired] == ["m-2"]
    assert poll_triggers.get_cursor("gmail-news", table=cursors) == "1790584442000"

    result, fired = run_fire("gmail-news", polls, cursors, mailbox())
    assert result["fired"] == 0 and fired == []


# --- the gmail_send action ----------------------------------------------------------


def send_connection(monkeypatch):
    monkeypatch.setattr("src.dapier.engine.actions.gmail._gmail_connection",
                        lambda connection_id: {
                            "connection_id": connection_id, "provider": "google",
                            "status": "connected", "credential_id": "oauth#google"})
    monkeypatch.setattr(tokens, "get_access_token",
                        lambda connection, transport=None: ("tok", {}))


def test_send_builds_raw_mime_and_posts_it(monkeypatch):
    send_connection(monkeypatch)
    transport = ApiTransport(
        ("users/me/profile", 200, {"emailAddress": "me@gmail.test"}),
        ("messages/send", 200, {"id": "sent-1", "threadId": "th-1"}))

    output = run_gmail_send(
        {"type": "gmail_send", "connection_id": "google",
         "to": "ops@example.test, other@example.test",
         "cc": "copy@example.test", "bcc": "hidden@example.test",
         "subject": "Weekly digest", "text": "Here is the digest.",
         "html": "<p>Here is the digest.</p>"},
        {"data": {}}, transport=transport)

    assert output == {"message_id": "sent-1", "thread_id": "th-1",
                      "from": "me@gmail.test",
                      "to": ["ops@example.test", "other@example.test"],
                      "subject": "Weekly digest",
                      "cc": ["copy@example.test"],
                      "bcc": ["hidden@example.test"]}
    send_call = transport.calls[1]
    assert send_call["method"] == "POST"
    assert send_call["url"] == "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
    assert send_call["headers"]["authorization"] == "Bearer tok"
    import email as email_parser

    message = email_parser.message_from_string(base64.urlsafe_b64decode(
        json.loads(send_call["body"])["raw"]).decode())
    assert message["From"] == "me@gmail.test"
    assert message["To"] == "ops@example.test, other@example.test"
    assert message["Cc"] == "copy@example.test"
    assert message["Subject"] == "Weekly digest"
    assert message["Message-ID"]  # the raw MIME carries an explicit id
    plain = next(part for part in message.walk()
                 if part.get_content_type() == "text/plain")
    assert plain.get_payload(decode=True).decode().rstrip("\n") == "Here is the digest."
    html = next(part for part in message.walk()
                if part.get_content_type() == "text/html")
    assert html.get_payload(decode=True).decode().rstrip("\n") == "<p>Here is the digest.</p>"


def test_send_renders_templates_from_the_event(monkeypatch):
    send_connection(monkeypatch)
    transport = ApiTransport(
        ("users/me/profile", 200, {"emailAddress": "me@gmail.test"}),
        ("messages/send", 200, {"id": "sent-2", "threadId": "th-2"}))

    output = run_gmail_send(
        {"type": "gmail_send", "connection_id": "google", "to": "{sender}",
         "subject": "Re: {subject}", "text": "Got it."},
        {"data": {"sender": "ann@example.test", "subject": "Invoice"}},
        transport=transport)

    assert output["to"] == ["ann@example.test"]
    assert output["subject"] == "Re: Invoice"


def test_send_needs_a_to_and_a_body(monkeypatch):
    send_connection(monkeypatch)
    transport = ApiTransport()

    with pytest.raises(ValueError, match="to address"):
        run_gmail_send({"type": "gmail_send", "connection_id": "google",
                        "text": "hi"}, {"data": {}}, transport=transport)
    with pytest.raises(ValueError, match="text or html"):
        run_gmail_send({"type": "gmail_send", "connection_id": "google",
                        "to": "ops@example.test"}, {"data": {}}, transport=transport)
    assert transport.calls == []


def test_send_provider_error_raises(monkeypatch):
    send_connection(monkeypatch)
    transport = ApiTransport(
        ("users/me/profile", 200, {"emailAddress": "me@gmail.test"}),
        ("messages/send", 403, {"error": {"message": "Send permission denied"}}))

    with pytest.raises(RuntimeError) as excinfo:
        run_gmail_send({"type": "gmail_send", "connection_id": "google",
                        "to": "ops@example.test", "text": "hi"},
                       {"data": {}}, transport=transport)
    assert "HTTP 403" in str(excinfo.value)
    assert "Send permission denied" in str(excinfo.value)


def test_send_dispatches_through_the_registry(monkeypatch):
    send_connection(monkeypatch)
    transport = ApiTransport(
        ("users/me/profile", 200, {"emailAddress": "me@gmail.test"}),
        ("messages/send", 200, {"id": "sent-3", "threadId": "th-3"}))

    with patch("src.dapier.engine.actions.base._default_transport", transport):
        output = registry.ACTIONS["gmail_send"].run(
            {"type": "gmail_send", "connection_id": "google",
             "to": "ops@example.test", "subject": "{subject}", "text": "hi"},
            {"data": {"subject": "Dispatched"}}, "wf-1")

    assert output["message_id"] == "sent-3"
    assert output["subject"] == "Dispatched"


def test_send_chain_validates_against_the_registry():
    registry.validate_action_chain([
        {"type": "gmail_send", "connection_id": "google", "to": "{sender}",
         "subject": "Welcome", "text": "Hello {name}"},
    ])
    with pytest.raises(registry.ActionError, match="missing: to"):
        registry.validate_action_chain([
            {"type": "gmail_send", "connection_id": "google"}])
    with pytest.raises(registry.ActionError, match="unknown keys: sender"):
        registry.validate_action_chain([
            {"type": "gmail_send", "connection_id": "google", "to": "a@b.test",
             "text": "hi", "sender": "nope@example.test"}])


# --- the connection test the chip rides ---------------------------------------------


def test_the_shared_google_test_answers_for_gmail_connections(monkeypatch):
    monkeypatch.setattr(tokens, "get_access_token",
                        lambda connection, transport=None: ("tok", {}))

    def verify(provider_name, token, *, transport=None):
        assert provider_name == "google" and token == "tok"
        return "me@gmail.test", "Me"

    monkeypatch.setattr(provider.oauth_providers, "verify_account", verify)
    verdict = registry.connection_test_for("google").run(
        {"connection_id": "google", "provider": "google", "status": "connected",
         "credential_id": "oauth#google"})

    assert verdict["ok"] is True
    assert verdict["identity"] == {"id": "me@gmail.test", "name": "Me"}


# --- the chip's sample pull ----------------------------------------------------------

def discover(**body):
    status, payload = trigger_discovery.api_discover({"kind": "sample", **body})
    return status, payload


def test_sample_pulls_the_polls_newest_message_live(monkeypatch, google_transport,
                                                    no_history):
    google_transport(MailboxTransport(
        pages=[{"messages": [gmail_stub("m-1"), gmail_stub("m-2")]}],
        messages=[gmail_message("m-1", "1790584441000"),
                  gmail_message("m-2", "1790584442000")]))
    item = stored(poll_body())
    monkeypatch.setattr(poll_triggers, "get_item",
                        lambda name, table_ref=None: item)

    status, payload = discover(connector="gmail", event="gmail-news")

    assert status == 200, payload
    assert payload["source"] == "live"
    assert payload["sample"]["event"] == "message.received"
    assert payload["sample"]["data"]["id"] == "m-2"  # the newest message
    assert payload["sample"]["data"]["poll"] == "gmail-news"
    assert payload["connection_id"] == "google"


def test_sample_falls_through_to_history(monkeypatch, no_history):
    monkeypatch.setattr(poll_triggers, "get_item",
                        lambda name, table_ref=None: None)
    monkeypatch.setattr(trigger_discovery, "history_sample",
                        lambda connector, event=None: {
                            "connector": connector, "event": event,
                            "data": {"id": "rec-1"}, "id": "run-1",
                            "source": "run", "occurred_at": "2026-09-28T09:00:00Z"})

    status, payload = discover(connector="gmail")

    assert status == 200, payload
    assert payload["source"] == "history"
    assert payload["sample"]["event"] == "message.received"


def test_sample_without_a_poll_or_history_is_synthetic(monkeypatch, no_history):
    monkeypatch.setattr(poll_triggers, "get_item",
                        lambda name, table_ref=None: None)

    status, payload = discover(connector="gmail")

    assert status == 200, payload
    assert payload["source"] == "synthetic"
    assert payload["sample"]["event"] == "message.received"
    assert payload["sample"]["data"]["id"] == "18c1a2b3c4d5e6f7"
    assert payload["sample"]["data"]["subject"] == "Invoice #4137 - September"
    assert payload["sample"]["data"]["text"]


# --- the label options ---------------------------------------------------------------


def configure_connections(monkeypatch, connections):
    """A fake connections table, so the options wrapper resolves its account."""
    tables = {"connections": FakePollTable()}
    tables["connections"].items = {c["connection_id"]: c for c in connections}

    class Dynamo:
        def Table(self, name):
            return tables[name]

    import boto3

    monkeypatch.setenv("CONNECTIONS_TABLE", "connections")
    monkeypatch.setattr(boto3, "resource", lambda service: Dynamo())


def test_label_options_map_to_query_terms(monkeypatch):
    configure_connections(monkeypatch, [
        {"connection_id": "google", "provider": "google", "status": "connected",
         "credential_id": "oauth#google"}])
    monkeypatch.setattr(provider, "_default_transport", ApiTransport(
        ("users/me/labels", 200, {"labels": [
            {"id": "Label_1", "name": "Newsletters", "type": "user"},
            {"id": "INBOX", "name": "INBOX", "type": "system"}]})))
    monkeypatch.setattr(tokens, "get_access_token",
                        lambda connection, transport=None: ("tok", {}))

    status, payload = trigger_discovery.api_discover(
        {"connector": "gmail", "kind": "options", "resource": "gmail.labels"})

    assert status == 200, payload
    assert payload["resource"] == "gmail.labels"
    assert payload["options"] == [
        {"value": "label:Newsletters", "label": "Newsletters"},
        {"value": "label:INBOX", "label": "INBOX"},
    ]


# --- the Gmail scope declaration -------------------------------------------------
#
# GMAIL_SCOPES is the one declaration of what the chip's calls answer to;
# the OAuth flow derives a Gmail connection's grant from it
# (connection_grant_scopes), and the GMAIL_SCOPES environment variable
# extends the declaration with live-use extras at consent and verification
# time.

GMAIL_READONLY = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_SEND = "https://www.googleapis.com/auth/gmail.send"


def test_effective_gmail_scopes_without_env_is_the_declaration(monkeypatch):
    monkeypatch.delenv("GMAIL_SCOPES", raising=False)
    assert gmail.effective_gmail_scopes() == (GMAIL_READONLY, GMAIL_SEND)


def test_effective_gmail_scopes_env_extends_the_declaration(monkeypatch):
    monkeypatch.setenv("GMAIL_SCOPES",
                       "https://www.googleapis.com/auth/gmail.modify")
    assert gmail.effective_gmail_scopes() == (
        GMAIL_READONLY, GMAIL_SEND,
        "https://www.googleapis.com/auth/gmail.modify")


def test_effective_gmail_scopes_parses_commas_whitespace_and_short_names(monkeypatch):
    monkeypatch.setenv(
        "GMAIL_SCOPES",
        "gmail.modify, https://www.googleapis.com/auth/gmail.settings\n"
        "\tgmail.labels gmail.delegates,gmail.filters")
    assert gmail.effective_gmail_scopes() == (
        GMAIL_READONLY, GMAIL_SEND,
        "https://www.googleapis.com/auth/gmail.modify",
        "https://www.googleapis.com/auth/gmail.settings",
        "https://www.googleapis.com/auth/gmail.labels",
        "https://www.googleapis.com/auth/gmail.delegates",
        "https://www.googleapis.com/auth/gmail.filters")


def test_effective_gmail_scopes_dedupes_and_keeps_the_declaration_first(monkeypatch):
    monkeypatch.setenv("GMAIL_SCOPES",
                       "gmail.send, " + GMAIL_SEND + ", gmail.modify, gmail.modify")
    assert gmail.effective_gmail_scopes() == (
        GMAIL_READONLY, GMAIL_SEND,
        "https://www.googleapis.com/auth/gmail.modify")


def test_grant_scopes_hold_a_gmail_connection_to_the_whole_declaration(monkeypatch):
    monkeypatch.delenv("GMAIL_SCOPES", raising=False)
    assert gmail.connection_grant_scopes("google", [GMAIL_READONLY]) == \
        sorted(gmail.GMAIL_SCOPES)
    assert gmail.connection_grant_scopes("google", [GMAIL_SEND, GMAIL_SEND]) == \
        sorted(gmail.GMAIL_SCOPES)


def test_grant_scopes_carry_env_extras_for_gmail_connections(monkeypatch):
    monkeypatch.setenv("GMAIL_SCOPES", "gmail.modify, gmail.settings")
    assert gmail.connection_grant_scopes("google", [GMAIL_SEND]) == sorted({
        GMAIL_READONLY, GMAIL_SEND,
        "https://www.googleapis.com/auth/gmail.modify",
        "https://www.googleapis.com/auth/gmail.settings"})


def test_grant_scopes_leave_non_gmail_connections_alone(monkeypatch):
    monkeypatch.setenv("GMAIL_SCOPES", "gmail.modify")
    calendar_scope = "https://www.googleapis.com/auth/calendar.readonly"
    youtube_scope = "https://www.googleapis.com/auth/youtube.readonly"
    assert gmail.connection_grant_scopes("google", [calendar_scope]) == \
        [calendar_scope]
    assert gmail.connection_grant_scopes("youtube", [youtube_scope]) == \
        [youtube_scope]
