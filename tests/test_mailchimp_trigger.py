"""Mailchimp trigger parity: chip, samples, and live intake agree.

The chip's declared events (connectors.triggers), the per-type sample
payloads (connectors.mailchimp) and the live intake's accepted types
(triggers.intake.mailchimp_webhooks) must name the same set, and a pulled
sample must carry the exact shape a real form-encoded delivery publishes —
otherwise the designer previews an event no delivery will ever match.
"""
from urllib.parse import urlencode

import pytest

from src.dapier.connectors import registry, trigger_discovery
import src.dapier.connectors  # noqa: F401  (import = registration)
from src.dapier.triggers.intake import mailchimp_webhooks


@pytest.fixture(autouse=True)
def no_recorded_history(monkeypatch):
    """Pin the samples to their documented payloads: history needs run tables."""
    monkeypatch.setattr(
        trigger_discovery, "history_sample",
        lambda connector, event=None: None)


def _chip():
    chips = {entry["name"]: entry for entry in registry.catalog()["connectors"]}
    return chips["mailchimp"]


def test_chip_declares_exactly_the_intake_event_types():
    events = _chip()["events"]
    # every webhook intake type is a chip event, and member.new is the
    # mailchimp.members poll source's event — the one non-intake extra
    assert list(mailchimp_webhooks.EVENT_TYPES) == [
        event for event in events if event != "member.new"]
    assert events.count("member.new") == 1
    # the registration ping is not a workflow event: intake answers it 200
    assert "ping" not in events


def test_every_declared_type_has_a_matching_sample_payload():
    for kind in mailchimp_webhooks.EVENT_TYPES:
        status, payload = trigger_discovery.api_discover(
            {"connector": "mailchimp", "event": kind})
        assert status == 200, payload
        sample = payload["sample"]
        assert sample["connector"] == "mailchimp"
        assert sample["event"] == kind
        assert sample["data"]["type"] == kind
        assert isinstance(sample["data"]["data"], dict) and sample["data"]["data"]
        # intake routes the source by list_id; every sample carries one
        assert sample["data"]["data"]["list_id"]


def test_a_real_delivery_matches_the_documented_sample_shape():
    """parse_form of a real subscribe POST yields the sample's data keys."""
    _, payload = trigger_discovery.api_discover(
        {"connector": "mailchimp", "event": "subscribe"})
    sample_data = payload["sample"]["data"]
    body = urlencode({
        "type": "subscribe",
        "data[list_id]": sample_data["data"]["list_id"],
        "data[email]": sample_data["data"]["email"],
        "data[merges][FNAME]": sample_data["data"]["merges"]["FNAME"],
    }).encode()

    published = []
    status, _ = mailchimp_webhooks.handle(
        body, hook="newsletter",
        publish=lambda *args, **kwargs: published.append((args, kwargs)))
    assert status == 200
    (connector, event, data), kwargs = published[0]
    assert (connector, event) == ("mailchimp", "subscribe")
    # The intake stamps the hook name beside the parsed body (the trigger's
    # matching filter); the sample documents the type-plus-data payload.
    assert set(data) == {"hook", "type", "data"}
    assert data["hook"] == "newsletter"
    assert set(sample_data) == {"type", "data"}
    for key in ("list_id", "email", "merges"):
        assert key in data["data"]
    assert kwargs["source"] == sample_data["data"]["list_id"]


def test_sample_event_defaults_to_subscribe():
    status, payload = trigger_discovery.api_discover({"connector": "mailchimp"})
    assert status == 200
    assert payload["sample"]["event"] == "subscribe"
    assert payload["sample"]["data"]["type"] == "subscribe"
