"""Mailchimp member actions: remove and tag.

Zapier's "Remove Subscriber" and the tag half of its Member Tags support:
unit tests drive each registered runner with a fake transport and the
stored credential patched, asserting method, URL (the email's md5 path),
request body and the step-output shape. The registry half checks the new
types are registered with their field sets and that a valid chain
validates while missing fields and unknown keys are rejected.
"""
import hashlib
import json
import unittest.mock as mock

import pytest

from src.dapier.connectors import registry
from src.dapier.connections import credentials
from src.dapier.engine.actions import base
from plugins.mailchimp.runners.mailchimp import (
    run_mailchimp_find_member,
    run_mailchimp_remove_member,
    run_mailchimp_tag_member,
    run_mailchimp_unsubscribe_member,
)

import src.dapier.connectors  # noqa: F401  (import = registration)

LIST_ID = "abc123"
EMAIL = "reader@example.test"
EVENT = {"connector": "mailchimp", "event": "subscribe",
         "occurred_at": "2026-09-28T09:00:00+00:00",
         "data": {"email": EMAIL, "list_id": LIST_ID}}


class Transport:
    """One canned Marketing API response, recording every call."""

    def __init__(self, status=204, payload=None):
        self.status = status
        self.payload = payload
        self.calls = []

    def __call__(self, method, url, *, headers=None, body=None, timeout=15):
        self.calls.append({"method": method, "url": url, "body": body})
        raw = json.dumps(self.payload).encode() if self.payload is not None else b""
        return self.status, raw


@pytest.fixture(autouse=True)
def stored_key(monkeypatch):
    """The shared ``mailchimp`` credential resolves without DynamoDB."""
    monkeypatch.setattr(credentials, "get_credential",
                        lambda credential_id: {"apiKey": "key123-us21",
                                               "server": "us21"})


def member_path(email=EMAIL, suffix=""):
    digest = hashlib.md5(email.strip().lower().encode()).hexdigest()
    return f"/lists/{LIST_ID}/members/{digest}{suffix}"


# --- mailchimp_remove_member -------------------------------------------------------


def test_remove_deletes_the_members_md5_path():
    transport = Transport(status=204)

    output = run_mailchimp_remove_member(
        {"type": "mailchimp_remove_member", "list_id": LIST_ID, "email": EMAIL},
        EVENT, transport=transport)

    assert output == {"removed": True, "email": EMAIL}
    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call["method"] == "DELETE"
    assert call["url"].endswith(f"us21.api.mailchimp.com/3.0{member_path()}")
    assert call["body"] is None  # a delete carries no payload


def test_remove_renders_the_templates():
    transport = Transport(status=204)

    output = run_mailchimp_remove_member(
        {"type": "mailchimp_remove_member", "list_id": "{list_id}",
         "email": "{email}"}, EVENT, transport=transport)

    assert output == {"removed": True, "email": EMAIL}
    assert transport.calls[0]["url"].endswith(member_path())


def test_remove_a_missing_member_is_removed_false_not_an_error():
    transport = Transport(status=404, payload={"title": "Resource Not Found"})

    output = run_mailchimp_remove_member(
        {"type": "mailchimp_remove_member", "list_id": LIST_ID, "email": EMAIL},
        EVENT, transport=transport)

    assert output == {"removed": False, "email": EMAIL}


def test_remove_a_rejection_raises_with_the_detail():
    transport = Transport(status=400, payload={"detail": "list does not exist"})

    with pytest.raises(RuntimeError, match="HTTP 400: list does not exist"):
        run_mailchimp_remove_member(
            {"type": "mailchimp_remove_member", "list_id": LIST_ID, "email": EMAIL},
            EVENT, transport=transport)


def test_remove_requires_list_id_and_email():
    with pytest.raises(ValueError, match="requires list_id and email"):
        run_mailchimp_remove_member(
            {"type": "mailchimp_remove_member", "list_id": LIST_ID}, EVENT)


# --- mailchimp_tag_member -------------------------------------------------------


def test_tag_add_posts_the_active_tag():
    transport = Transport(status=204)

    output = run_mailchimp_tag_member(
        {"type": "mailchimp_tag_member", "list_id": LIST_ID, "email": EMAIL,
         "tag": "digest-readers"}, EVENT, transport=transport)

    assert output == {"tagged": True, "email": EMAIL,
                      "tag": "digest-readers", "tag_status": "active"}
    call = transport.calls[0]
    assert call["method"] == "POST"
    assert call["url"].endswith(f"us21.api.mailchimp.com/3.0{member_path(suffix='/tags')}")
    assert json.loads(call["body"]) == {
        "tags": [{"name": "digest-readers", "status": "active"}]}


def test_tag_remove_sets_the_tag_inactive():
    transport = Transport(status=204)

    output = run_mailchimp_tag_member(
        {"type": "mailchimp_tag_member", "list_id": LIST_ID, "email": EMAIL,
         "tag": "digest-readers", "tag_action": "remove"},
        EVENT, transport=transport)

    assert output["tag_status"] == "inactive"
    assert json.loads(transport.calls[0]["body"]) == {
        "tags": [{"name": "digest-readers", "status": "inactive"}]}


def test_tag_action_defaults_to_add():
    transport = Transport(status=204)

    run_mailchimp_tag_member(
        {"type": "mailchimp_tag_member", "list_id": LIST_ID, "email": EMAIL,
         "tag": "vip", "tag_action": ""}, EVENT, transport=transport)

    assert json.loads(transport.calls[0]["body"])["tags"][0]["status"] == "active"


def test_tag_rejects_an_unknown_operation():
    with pytest.raises(ValueError, match="tag_action must be one of"):
        run_mailchimp_tag_member(
            {"type": "mailchimp_tag_member", "list_id": LIST_ID, "email": EMAIL,
             "tag": "vip", "tag_action": "toggle"}, EVENT)


def test_tag_requires_list_id_email_and_tag():
    with pytest.raises(ValueError, match="requires list_id, email and tag"):
        run_mailchimp_tag_member(
            {"type": "mailchimp_tag_member", "list_id": LIST_ID, "email": EMAIL},
            EVENT)


def test_tag_a_missing_member_raises():
    transport = Transport(status=404, payload={"title": "Resource Not Found"})

    with pytest.raises(RuntimeError, match="HTTP 404"):
        run_mailchimp_tag_member(
            {"type": "mailchimp_tag_member", "list_id": LIST_ID, "email": EMAIL,
             "tag": "vip"}, EVENT, transport=transport)


# --- registry wiring ----------------------------------------------------------


def test_new_actions_are_registered():
    assert {"mailchimp_remove_member", "mailchimp_tag_member"} <= set(registry.ACTIONS)
    specs = registry.action_specs()
    assert specs["mailchimp_remove_member"] == (
        {"list_id", "email"}, {"credential_id", "connection_id"})
    assert specs["mailchimp_tag_member"] == (
        {"list_id", "email", "tag"},
        {"tag_action", "credential_id", "connection_id"})


def test_remove_member_dispatches_through_the_registry():
    transport = Transport(status=204)
    with mock.patch.object(base, "_default_transport", transport):
        output = registry.ACTIONS["mailchimp_remove_member"].run(
            {"type": "mailchimp_remove_member", "list_id": LIST_ID,
             "email": "{email}"}, EVENT, "wf-1")

    assert output["removed"] is True
    assert transport.calls[0]["method"] == "DELETE"


def test_validate_action_chain_accepts_and_rejects_the_new_actions():
    registry.validate_action_chain([
        {"type": "mailchimp_tag_member", "list_id": LIST_ID,
         "email": "{email}", "tag": "welcome"},
        {"type": "mailchimp_remove_member", "list_id": LIST_ID,
         "email": "{email}"},
    ])
    with pytest.raises(registry.ActionError, match="missing: tag"):
        registry.validate_action_chain([
            {"type": "mailchimp_tag_member", "list_id": LIST_ID,
             "email": "{email}"}])
    with pytest.raises(registry.ActionError, match="unknown keys: tag_status"):
        registry.validate_action_chain([
            {"type": "mailchimp_tag_member", "list_id": LIST_ID,
             "email": "{email}", "tag": "vip", "tag_status": "active"}])


# --- mailchimp_unsubscribe_member --------------------------------------------------


def test_unsubscribe_patches_the_status():
    transport = Transport(status=200, payload={"status": "unsubscribed",
                                               "email_address": EMAIL})

    output = run_mailchimp_unsubscribe_member(
        {"type": "mailchimp_unsubscribe_member", "list_id": LIST_ID, "email": EMAIL},
        EVENT, transport=transport)

    assert output["unsubscribed"] is True
    assert output["email"] == EMAIL
    assert output["member"]["status"] == "unsubscribed"
    call = transport.calls[0]
    assert call["method"] == "PATCH"
    assert call["url"].endswith(f"us21.api.mailchimp.com/3.0{member_path()}")
    assert json.loads(call["body"]) == {"status": "unsubscribed"}


def test_unsubscribe_renders_the_templates():
    transport = Transport(status=200, payload={"status": "unsubscribed"})

    output = run_mailchimp_unsubscribe_member(
        {"type": "mailchimp_unsubscribe_member", "list_id": "{list_id}",
         "email": "{email}"}, EVENT, transport=transport)

    assert output["unsubscribed"] is True
    assert transport.calls[0]["url"].endswith(member_path())


def test_unsubscribe_a_missing_member_is_unsubscribed_false_not_an_error():
    transport = Transport(status=404, payload={"title": "Resource Not Found"})

    output = run_mailchimp_unsubscribe_member(
        {"type": "mailchimp_unsubscribe_member", "list_id": LIST_ID, "email": EMAIL},
        EVENT, transport=transport)

    assert output == {"unsubscribed": False, "email": EMAIL}


def test_unsubscribe_a_rejection_raises_with_the_detail():
    transport = Transport(status=400, payload={"detail": "list does not exist"})

    with pytest.raises(RuntimeError, match="HTTP 400: list does not exist"):
        run_mailchimp_unsubscribe_member(
            {"type": "mailchimp_unsubscribe_member", "list_id": LIST_ID, "email": EMAIL},
            EVENT, transport=transport)


def test_unsubscribe_requires_list_id_and_email():
    with pytest.raises(ValueError, match="requires list_id and email"):
        run_mailchimp_unsubscribe_member(
            {"type": "mailchimp_unsubscribe_member", "list_id": LIST_ID}, EVENT)


def test_unsubscribe_dispatches_through_the_registry():
    transport = Transport(status=200, payload={"status": "unsubscribed"})
    with mock.patch.object(base, "_default_transport", transport):
        output = registry.ACTIONS["mailchimp_unsubscribe_member"].run(
            {"type": "mailchimp_unsubscribe_member", "list_id": LIST_ID,
             "email": "{email}"}, EVENT, "wf-1")

    assert output["unsubscribed"] is True
    assert transport.calls[0]["method"] == "PATCH"


# --- find member with create_if_missing ---------------------------------------------


class MethodTransport:
    """Answers per HTTP method (GET misses, PUT creates) and records calls."""

    def __init__(self, get=(404, None), put=(200, None)):
        self.responses = {"GET": get, "PUT": put}
        self.calls = []

    def __call__(self, method, url, *, headers=None, body=None, timeout=15):
        self.calls.append({"method": method, "url": url, "body": body})
        status, payload = self.responses.get(method, (404, None))
        raw = json.dumps(payload).encode() if payload is not None else b""
        return status, raw


def test_find_create_if_missing_upserts_on_a_miss():
    transport = MethodTransport(put=(200, {"status": "subscribed",
                                           "email_address": EMAIL}))

    output = run_mailchimp_find_member(
        {"type": "mailchimp_find_member", "list_id": LIST_ID, "email": EMAIL,
         "create_if_missing": "true", "status": "pending",
         "merge_fields": '{"FNAME": "{name}"}'},
        dict(EVENT, data={"name": "Reader"}), transport=transport)

    assert output["found"] is True
    assert output["created"] is True
    assert output["member"]["email"] == EMAIL
    create = transport.calls[1]
    assert create["method"] == "PUT"
    assert create["url"].endswith(member_path())
    assert json.loads(create["body"]) == {
        "email_address": EMAIL, "status_if_new": "pending",
        "merge_fields": {"FNAME": "Reader"}}


def test_find_create_if_missing_defaults_to_subscribed():
    transport = MethodTransport(put=(200, {"status": "subscribed"}))

    run_mailchimp_find_member(
        {"type": "mailchimp_find_member", "list_id": LIST_ID, "email": EMAIL,
         "create_if_missing": True}, EVENT, transport=transport)

    create = transport.calls[1]
    assert create["method"] == "PUT"
    assert json.loads(create["body"]) == {"email_address": EMAIL,
                                          "status_if_new": "subscribed"}


def test_find_miss_without_the_flag_stays_a_miss():
    transport = Transport(status=404, payload={"title": "Resource Not Found"})

    output = run_mailchimp_find_member(
        {"type": "mailchimp_find_member", "list_id": LIST_ID, "email": EMAIL},
        EVENT, transport=transport)

    assert output == {"found": False, "member": None}
    assert len(transport.calls) == 1  # the GET; no create


def test_find_hit_never_creates():
    transport = Transport(status=200, payload={"status": "subscribed",
                                               "email_address": EMAIL})

    output = run_mailchimp_find_member(
        {"type": "mailchimp_find_member", "list_id": LIST_ID, "email": EMAIL,
         "create_if_missing": "true"}, EVENT, transport=transport)

    assert output["found"] is True
    assert "created" not in output
    assert len(transport.calls) == 1  # the GET only


def test_find_create_rejects_an_unknown_status():
    transport = Transport(status=404)

    with pytest.raises(ValueError, match="status must be one of"):
        run_mailchimp_find_member(
            {"type": "mailchimp_find_member", "list_id": LIST_ID, "email": EMAIL,
             "create_if_missing": "true", "status": "maybe"},
            EVENT, transport=transport)


def test_find_create_rejects_bad_merge_fields():
    transport = Transport(status=404)

    with pytest.raises(ValueError, match="merge_fields must be a JSON object"):
        run_mailchimp_find_member(
            {"type": "mailchimp_find_member", "list_id": LIST_ID, "email": EMAIL,
             "create_if_missing": "true", "merge_fields": "{oops"},
            EVENT, transport=transport)


def test_find_create_failure_raises():
    def reject(method, url, *, headers=None, body=None, timeout=15):
        if method == "PUT":
            return 400, json.dumps({"detail": "nope"}).encode()
        return 404, b""

    with pytest.raises(RuntimeError, match="member create returned HTTP 400"):
        run_mailchimp_find_member(
            {"type": "mailchimp_find_member", "list_id": LIST_ID, "email": EMAIL,
             "create_if_missing": "true"}, EVENT, transport=reject)


# --- registry wiring for the new fields -------------------------------------------


def test_unsubscribe_and_find_fields_are_registered():
    assert {"mailchimp_remove_member", "mailchimp_tag_member",
            "mailchimp_unsubscribe_member"} <= set(registry.ACTIONS)
    specs = registry.action_specs()
    assert specs["mailchimp_unsubscribe_member"] == (
        {"list_id", "email"}, {"credential_id", "connection_id"})
    assert specs["mailchimp_find_member"] == (
        {"list_id", "email"},
        {"create_if_missing", "status", "merge_fields",
         "credential_id", "connection_id"})


if __name__ == "__main__":
    pytest.main([__file__])
