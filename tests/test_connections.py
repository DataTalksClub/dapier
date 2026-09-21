import pytest

from src import connections
from src.connections import (
    BindingError,
    ConnectionError,
    build_item,
    check_binding,
    credential_id_for,
    mark_connected,
    public_view,
    validate_connection_id,
    validate_new_connection,
)


def body(**overrides):
    base = {
        "connection_id": "youtube-personal",
        "provider": "youtube",
        "display_name": "Personal YouTube",
        "client_id": "client-id",
        "client_secret": "client-secret",
        "scopes": ["https://www.googleapis.com/auth/youtube.upload"],
    }
    base.update(overrides)
    return base


def test_validate_connection_id_normalizes_case():
    assert validate_connection_id("Team-Dropbox") == "team-dropbox"


def test_validate_connection_id_rejects_bad_values():
    for bad in ("", "a", "-lead", "UPPER IS FINE".split()[0] + " space", "dots.bad"):
        with pytest.raises(ConnectionError):
            validate_connection_id(bad)


def test_validate_new_connection_keeps_current_error_messages():
    with pytest.raises(ConnectionError) as exc:
        validate_new_connection(body(connection_id="bad id!"))
    assert "lowercase letters" in str(exc.value)

    with pytest.raises(ConnectionError) as exc:
        validate_new_connection(body(provider="myspace"))
    assert "client ID, and client secret are required" in str(exc.value)

    with pytest.raises(ConnectionError) as exc:
        validate_new_connection(body(client_secret=""))
    assert "client ID, and client secret are required" in str(exc.value)


def test_validate_new_connection_accepts_space_separated_scopes():
    fields = validate_new_connection(body(scopes="a b a"))
    assert fields["scopes"] == ["a", "b"]


def test_validate_new_connection_rejects_youtube_without_scopes():
    with pytest.raises(ConnectionError):
        validate_new_connection(body(scopes=[]))


def test_build_item_create_defaults():
    item = build_item(validate_new_connection(body()), owner_subject="subject-1")
    assert item["connection_id"] == "youtube-personal"
    assert item["credential_id"] == "oauth#youtube-personal"
    assert item["status"] == "ready"
    assert item["version"] == 1
    assert item["owner_subject"] == "subject-1"
    assert item["verified_account_id"] is None
    assert item["expected_account_id"] is None
    assert "client_secret" not in item


def test_build_item_edit_preserves_binding_and_bumps_version():
    created = build_item(validate_new_connection(body()), owner_subject="subject-1")
    connected = mark_connected(
        created, verified_account_id="UC1", account_title="Ch",
        granted_scopes=created["scopes"], connected_by="subject-1",
    )
    edited = build_item(
        validate_new_connection(body(display_name="Renamed")),
        owner_subject="subject-1", previous=connected,
    )
    assert edited["display_name"] == "Renamed"
    assert edited["verified_account_id"] == "UC1"
    assert edited["version"] == connected["version"] + 1


def test_build_item_rejects_expected_account_change_after_binding():
    created = build_item(
        validate_new_connection(body(expected_account_id="UC1")), owner_subject="s",
    )
    connected = mark_connected(
        created, verified_account_id="UC1", account_title="Ch",
        granted_scopes=created["scopes"], connected_by="s",
    )
    with pytest.raises(BindingError):
        build_item(
            validate_new_connection(body(expected_account_id="UC2")),
            owner_subject="s", previous=connected,
        )


def test_check_binding_rejects_wrong_account():
    item = build_item(
        validate_new_connection(body(expected_account_id="UC-personal")), owner_subject="s",
    )
    with pytest.raises(BindingError):
        check_binding(item, "UCDvErgK0j5ur3aLgn6U-LqQ")


def test_check_binding_rejects_silent_account_switch():
    item = build_item(validate_new_connection(body()), owner_subject="s")
    connected = mark_connected(
        item, verified_account_id="UC1", account_title="Ch",
        granted_scopes=item["scopes"], connected_by="s",
    )
    with pytest.raises(BindingError):
        check_binding(connected, "UC2")


def test_mark_connected_sets_ready_fields():
    item = mark_connected(
        build_item(validate_new_connection(body()), owner_subject="s"),
        verified_account_id="UC1", account_title="Ch",
        granted_scopes=["scope-a"], connected_by="s",
    )
    assert item["status"] == "connected"
    assert item["connected_at"]
    assert item["granted_scopes"] == ["scope-a"]


def test_public_view_never_contains_secrets():
    item = build_item(validate_new_connection(body()), owner_subject="s")
    item["client_secret"] = "should-never-happen"
    view = public_view(item)
    assert "client_secret" not in view
    assert "credential_id" not in view
    assert view["connection_id"] == "youtube-personal"


def test_credential_id_scheme():
    assert credential_id_for("team-dropbox") == "oauth#team-dropbox"


class FakeTable:
    def __init__(self):
        self.items = {}

    def put_item(self, *, Item):
        self.items[Item["connection_id"]] = Item

    def get_item(self, *, Key):
        item = self.items.get(Key["connection_id"])
        return {"Item": item} if item else {}

    def scan(self, *, Limit):
        return {"Items": list(self.items.values())[:Limit]}


def test_table_roundtrip():
    table = FakeTable()
    item = build_item(validate_new_connection(body()), owner_subject="s")
    connections.put_connection(table, item)
    assert connections.get_connection(table, "youtube-personal")["version"] == 1
    assert len(connections.list_connections(table)) == 1
