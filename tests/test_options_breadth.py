"""Options breadth: every registry listing is reachable from POST /discover.

The connection-scoped listings (``registry.DISCOVERIES``, what GET
/connections/{id}/discover serves) and the trigger-discovery options
(``TRIGGER_DISCOVERIES``, what POST /api/{agent,admin}/discover serves with
``kind=options``) must not drift: every registry resource gets an options
entry registered by its connector module, delegating through
``options_from_registry``. The invariant test below fails the moment a new
registry listing lands without its trigger-side twin. Also here: the stored
trigger pickers (poll.triggers, schedule.triggers) and the payload-shape
contracts of the synthetic samples (webhook, email, dataops).
"""
import src.dapier.connectors  # noqa: F401  (import = registration)

from src.dapier.connectors import registry, trigger_discovery


# --- the invariant: registry listings and trigger options never drift --------


def test_every_registry_listing_is_trigger_discoverable():
    """Each registry Discovery resource has a (connector, "options",
    "connector.name") trigger discovery — options discovery is complete for
    all connectors."""
    for entry in registry.discoveries():
        resource = f"{entry.connector}.{entry.name}"
        discovered = trigger_discovery.TRIGGER_DISCOVERIES.get(
            (entry.connector, "options", resource))
        assert discovered is not None, f"{resource} has no kind=options trigger discovery"
        assert callable(discovered.fetch)


def test_options_catalog_lists_every_registry_resource():
    cat = trigger_discovery.trigger_discovery_catalog()
    for entry in registry.discoveries():
        resource = f"{entry.connector}.{entry.name}"
        assert resource in cat["options"].get(entry.connector, []), resource


# --- the event-slot params convention (see trigger_discovery.listing_params) --


def _delegate_fake(seen):
    def fake(resource, connection_id, limit, **kwargs):
        seen.append((resource, connection_id, limit, kwargs.get("params")))
        return {"options": [{"value": "opt-1", "label": "Opt 1"}],
                "connection_id": connection_id}
    return fake


def _options(connector, resource, event=None, connection_id="conn-1"):
    status, payload = trigger_discovery.api_discover(
        {"connector": connector, "kind": "options", "resource": resource,
         "event": event, "connection_id": connection_id})
    assert status == 200, payload
    return payload


def test_option_fetches_map_the_event_slot_to_the_listing_params(monkeypatch):
    """Param-free listings pass no params; one-param listings take the whole
    event; multi-param ones split it on "/". The single provider call per
    listing stays the registry entry's own."""
    seen = []
    monkeypatch.setattr(trigger_discovery, "options_from_registry", _delegate_fake(seen))

    # Param-free listings ride on the first connected connection.
    for connector, resource in (
            ("zoom", "zoom.recordings"),
            ("slack", "slack.users"),
            ("youtube", "youtube.channel"),
            ("youtube", "youtube.videos")):
        payload = _options(connector, resource)
        assert payload["options"] == [{"value": "opt-1", "label": "Opt 1"}]
        assert seen[-1] == (resource, "conn-1", 5, None)

    # One required param: the whole event is the value (slashes included).
    _options("slack", "slack.messages", event="C01BQC114P2")
    assert seen[-1] == ("slack.messages", "conn-1", 5, {"channel": "C01BQC114P2"})
    _options("dropbox", "dropbox.search", event="invoice 4137")
    assert seen[-1] == ("dropbox.search", "conn-1", 5, {"query": "invoice 4137"})
    _options("youtube", "youtube.playlist_items", event="UU1234-abcd")
    assert seen[-1] == ("youtube.playlist_items", "conn-1", 5, {"playlist_id": "UU1234-abcd"})

    # Two params: "<required>/<optional tail>" — a missing tail keeps the
    # provider default (worksheet defaults to Sheet1).
    _options("google-sheets", "google-sheets.rows", event="sheet-1/Tab 2")
    assert seen[-1] == ("google-sheets.rows", "conn-1", 5,
                        {"spreadsheet_id": "sheet-1", "worksheet": "Tab 2"})
    _options("google-sheets", "google-sheets.columns", event="sheet-1")
    assert seen[-1] == ("google-sheets.columns", "conn-1", 5, {"spreadsheet_id": "sheet-1"})

    # Optional param: only passed through when the event carries one.
    _options("dropbox", "dropbox.files", event="/Invoices/Sub")
    assert seen[-1] == ("dropbox.files", "conn-1", 5, {"path": "/Invoices/Sub"})
    _options("dropbox", "dropbox.files")
    assert seen[-1] == ("dropbox.files", "conn-1", 5, {})


def test_missing_required_param_is_a_404_not_a_listing(monkeypatch):
    """The s3.objects convention: a typo'd or absent selector stays a
    precise 404 — never a fabricated or silently-defaulted listing."""
    monkeypatch.setattr(trigger_discovery, "options_from_registry", _delegate_fake([]))
    for connector, resource, param in (
            ("youtube", "youtube.playlist_items", "playlist_id"),
            ("mailchimp", "mailchimp.members", "list_id"),
            ("slack", "slack.messages", "channel"),
            ("dropbox", "dropbox.search", "query"),
            ("google-sheets", "google-sheets.worksheets", "spreadsheet_id")):
        status, payload = trigger_discovery.api_discover(
            {"connector": connector, "kind": "options", "resource": resource})
        assert status == 404, (connector, resource, payload)
        assert param in payload["error"]


def test_member_options_delegate_like_the_audience_options(monkeypatch):
    """mailchimp imports its delegation by name; the pseudo-account fallback
    (``connection_id: "mailchimp"``) resolves through it the same way."""
    from src.dapier.connectors import mailchimp

    seen = []
    monkeypatch.setattr(mailchimp, "options_from_registry", _delegate_fake(seen))
    _options("mailchimp", "mailchimp.members", event="abc123", connection_id="mailchimp")
    assert seen[-1] == ("mailchimp.members", None, 5, {"list_id": "abc123"})


# --- stored-trigger pickers: poll.triggers and schedule.triggers --------------


def test_poll_and_schedule_trigger_options_list_stored_names(monkeypatch):
    """The operator picks the stored trigger's name for a live sample's
    event field instead of typing it; no connection is involved."""
    from src.dapier.triggers import poll_triggers, schedule_triggers

    monkeypatch.setattr(poll_triggers, "load_items", lambda table_ref=None: [
        {"poll_id": "blog", "url": "https://example.test/blog"},
        {"poll_id": "docs", "description": "Docs changes"}])
    monkeypatch.setattr(schedule_triggers, "load_items", lambda table_ref=None: [
        {"schedule_id": "weekdaily-digest", "expression": "rate(1 day)"},
        {"schedule_id": "nightly"}])

    status, payload = trigger_discovery.api_discover(
        {"connector": "poll", "kind": "options", "resource": "poll.triggers"})
    assert status == 200, payload
    assert payload["options"] == [
        {"value": "blog", "label": "blog — https://example.test/blog"},
        {"value": "docs", "label": "docs — Docs changes"}]
    assert payload["connection_id"] is None

    status, payload = trigger_discovery.api_discover(
        {"connector": "schedule", "kind": "options", "resource": "schedule.triggers"})
    assert status == 200, payload
    assert payload["options"] == [
        {"value": "weekdaily-digest", "label": "weekdaily-digest (rate(1 day))"},
        {"value": "nightly", "label": "nightly"}]


def test_empty_or_unconfigured_trigger_stores_are_empty_options(monkeypatch):
    from src.dapier.triggers import poll_triggers, schedule_triggers

    def unconfigured(table_ref=None):
        raise poll_triggers.TriggerError("poll triggers are not configured")

    for load in (lambda table_ref=None: [], unconfigured):
        monkeypatch.setattr(poll_triggers, "load_items", load)
        monkeypatch.setattr(schedule_triggers, "load_items", load)
        for connector in ("poll", "schedule"):
            status, payload = trigger_discovery.api_discover(
                {"connector": connector, "kind": "options",
                 "resource": f"{connector}.triggers"})
            assert status == 200, payload
            assert payload["options"] == []


# --- synthetic sample payload shapes: webhook, email, dataops ------------------


def _pin_synthetic(monkeypatch):
    """Pin the fallback chain to the documented example, like
    test_trigger_samples pins it — history depends on run tables."""
    monkeypatch.setattr(trigger_discovery, "history_sample",
                        lambda connector, event=None: None)


def _sample(connector):
    status, payload = trigger_discovery.api_discover({"connector": connector})
    assert status == 200, payload
    return payload["sample"]


def test_webhook_sample_keeps_the_delivery_shape(monkeypatch):
    _pin_synthetic(monkeypatch)
    sample = _sample("webhook")
    assert set(trigger_discovery.ENVELOPE_KEYS) <= set(sample)
    assert sample["connector"] == "webhook"
    assert sample["event"] == "request.received"
    data = sample["data"]
    assert data["hook"] == "orders"
    assert data["body"]["order_id"] and data["body"]["currency"]
    assert data["query"]
    assert data["content_type"] == "application/json"


def test_email_sample_keeps_the_inbound_contract(monkeypatch):
    _pin_synthetic(monkeypatch)
    sample = _sample("email")
    assert sample["event"] == "message.received"
    data = sample["data"]
    assert data["route"] == "todo"  # route is the local part, as in recorded deliveries
    assert data["message_id"].startswith("<")
    assert data["sender"]["addresses"] and data["sender"]["header"]
    assert data["recipients"]["matched"]
    assert data["subject"]
    assert data["body"]["html"]["value"]
    assert set(data["raw_mime"]) == {"bucket", "key"}


def test_dataops_sample_keeps_the_intake_document_contract(monkeypatch):
    _pin_synthetic(monkeypatch)
    sample = _sample("dataops")
    assert sample["event"] == "document.received"
    data = sample["data"]
    assert data["version"] and data["messageId"] and data["receivedAt"]
    document = data["documents"][0]
    assert document["kind"] and document["filename"]
    assert document["storageUri"].startswith("s3://")
    assert document["contentType"] == "application/pdf"
    assert document["checksum"].startswith("sha256:")
