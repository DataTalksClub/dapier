import json

import pytest

from src.dapier.connections import tokens
from src.dapier.connections.records import BindingError
from src.dapier.connections.credentials import VersionConflict
from src.dapier.connections.tokens import TokenError, get_access_token, refresh_and_store, revoke_connection

YOUTUBE = "youtube"


def configure_client(monkeypatch, **overrides):
    values = {
        "GOOGLE_OAUTH_CLIENT_ID": "shared-client-id",
        "GOOGLE_OAUTH_CLIENT_SECRET": "shared-client-secret",
        "DROPBOX_OAUTH_CLIENT_ID": "shared-dropbox-id",
        "DROPBOX_OAUTH_CLIENT_SECRET": "shared-dropbox-secret",
    }
    values.update(overrides)
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def connection(**overrides):
    item = {
        "connection_id": "youtube-personal",
        "provider": YOUTUBE,
        "scopes": ["https://www.googleapis.com/auth/youtube.readonly"],
        "granted_scopes": [],
        "expected_account_id": None,
        "verified_account_id": None,
        "status": "connected",
        "version": 3,
    }
    item.update(overrides)
    return item


def stored_token(**overrides):
    value = {
        "access_token": "fresh-access",
        "refresh_token": "refresh-1",
        "expires_at": 9_000_000_000,
        "token_type": "Bearer",
        "scope": "scope-a",
        "obtained_at": 1_000_000,
    }
    value.update(overrides)
    return {"credential_id": "oauth#youtube-personal", "value": value, "version": 4}


def configure_store(monkeypatch, record, writes=None, conflicts=0):
    state = {"record": record, "conflicts": conflicts}

    def fake_get(credential_id):
        assert credential_id == "oauth#youtube-personal"
        return state["record"]

    def fake_put(credential_id, value, *, provider, expected_version):
        if state["conflicts"]:
            state["conflicts"] -= 1
            raise VersionConflict(credential_id)
        assert expected_version == state["record"].get("version", 0)
        state["record"] = {"credential_id": credential_id, "value": value,
                           "version": expected_version + 1}
        if writes is not None:
            writes.append(value)
        return "now"

    monkeypatch.setattr(tokens, "get_credential_record", fake_get)
    monkeypatch.setattr(tokens, "put_credential_if_version", fake_put)
    return state


def transport_for(monkeypatch, *, token_response=None, verify_payload=None):
    calls = []

    def fake(method, url, *, headers=None, body=None, timeout=15):
        calls.append({"method": method, "url": url, "headers": headers, "body": body})
        if "oauth2.googleapis.com/token" in url or "api.dropboxapi.com/oauth2/token" in url:
            return 200, json.dumps(token_response or {}).encode()
        if "googleapis.com/youtube" in url:
            return 200, json.dumps(verify_payload or {}).encode()
        raise AssertionError(f"unexpected url {url}")

    import src.dapier.connections.providers.oauth_providers as providers

    monkeypatch.setattr(providers, "_default_transport", fake)
    return calls


def sent_fields(call):
    import urllib.parse

    return urllib.parse.parse_qs(call["body"].decode())


def test_cached_token_verifies_binding(monkeypatch):
    configure_store(monkeypatch, stored_token())
    transport_for(monkeypatch, verify_payload={
        "items": [{"id": "UC1", "snippet": {"title": "Ch"}}],
    })
    token, info = get_access_token(connection())
    assert token == "fresh-access"
    assert info["refreshed"] is False
    assert info["provider_account_id"] == "UC1"


def test_cached_token_wrong_account_fails_closed(monkeypatch):
    configure_store(monkeypatch, stored_token())
    transport_for(monkeypatch, verify_payload={
        "items": [{"id": "UC-other", "snippet": {"title": "Evil"}}],
    })
    item = connection(expected_account_id="UC1")
    with pytest.raises(BindingError):
        get_access_token(item)


def test_expired_token_refreshes_and_preserves_refresh(monkeypatch):
    writes = []
    configure_client(monkeypatch)
    configure_store(monkeypatch, stored_token(expires_at=100), writes=writes)
    transport_for(
        monkeypatch,
        token_response={"access_token": "new-access", "expires_in": 3600},
        verify_payload={"items": [{"id": "UC1", "snippet": {"title": "Ch"}}]},
    )
    token, info = get_access_token(connection(), transport=None)
    assert token == "new-access"
    assert info["refreshed"] is True
    assert writes[0]["refresh_token"] == "refresh-1"
    assert writes[0]["access_token"] == "new-access"


def test_refresh_uses_shared_client_credentials(monkeypatch):
    configure_client(monkeypatch)
    configure_store(monkeypatch, stored_token(expires_at=100))
    calls = transport_for(
        monkeypatch,
        token_response={"access_token": "a2", "expires_in": 3600},
        verify_payload={"items": [{"id": "UC1", "snippet": {}}]},
    )
    get_access_token(connection())
    fields = sent_fields(calls[0])
    assert fields["client_id"] == ["shared-client-id"]
    assert fields["client_secret"] == ["shared-client-secret"]


def test_refresh_prefers_record_client_override(monkeypatch):
    configure_client(monkeypatch)
    writes = []
    configure_store(monkeypatch, stored_token(
        expires_at=100, client_id="legacy-id", client_secret="legacy-secret",
    ), writes=writes)
    calls = transport_for(
        monkeypatch,
        token_response={"access_token": "a2", "expires_in": 3600},
        verify_payload={"items": [{"id": "UC1", "snippet": {}}]},
    )
    get_access_token(connection())
    fields = sent_fields(calls[0])
    assert fields["client_id"] == ["legacy-id"]
    assert fields["client_secret"] == ["legacy-secret"]
    # The override travels with the record so the next refresh still works.
    assert writes[0]["client_id"] == "legacy-id"
    assert writes[0]["client_secret"] == "legacy-secret"


def test_missing_shared_client_fails_closed(monkeypatch):
    configure_store(monkeypatch, stored_token(expires_at=100))
    transport_for(
        monkeypatch,
        token_response={"access_token": "a2", "expires_in": 3600},
        verify_payload={"items": [{"id": "UC1", "snippet": {}}]},
    )
    with pytest.raises(TokenError) as exc:
        get_access_token(connection())
    assert "GOOGLE_OAUTH_CLIENT_ID" in str(exc.value)


def test_refresh_rotation_adopts_new_refresh_token(monkeypatch):
    writes = []
    configure_client(monkeypatch)
    configure_store(monkeypatch, stored_token(expires_at=100), writes=writes)
    transport_for(
        monkeypatch,
        token_response={"access_token": "a2", "refresh_token": "refresh-2", "expires_in": 3600},
        verify_payload={"items": [{"id": "UC1", "snippet": {}}]},
    )
    _, info = get_access_token(connection())
    assert info["provider_account_id"] == "UC1"
    assert writes[0]["refresh_token"] == "refresh-2"


def test_refresh_binding_mismatch_stores_nothing(monkeypatch):
    writes = []
    configure_client(monkeypatch)
    configure_store(monkeypatch, stored_token(expires_at=100), writes=writes)
    transport_for(
        monkeypatch,
        token_response={"access_token": "a2", "expires_in": 3600},
        verify_payload={"items": [{"id": "UC-HIJACK", "snippet": {}}]},
    )
    with pytest.raises(BindingError):
        refresh_and_store(
            connection(expected_account_id="UC1"), stored_token(expires_at=100),
        )
    assert writes == []


def test_version_conflict_retries_with_fresh_read(monkeypatch):
    configure_client(monkeypatch)
    configure_store(monkeypatch, stored_token(expires_at=100), conflicts=1)
    transport_for(
        monkeypatch,
        token_response={"access_token": "a2", "expires_in": 3600},
        verify_payload={"items": [{"id": "UC1", "snippet": {}}]},
    )
    # After the conflict the re-read still shows an expired token, so the
    # second refresh attempt (conflicts exhausted) succeeds.
    token, info = get_access_token(connection())
    assert token == "a2"
    assert info["refreshed"] is True


def test_persistent_conflict_raises_token_error(monkeypatch):
    configure_client(monkeypatch)
    configure_store(monkeypatch, stored_token(expires_at=100), conflicts=99)
    transport_for(
        monkeypatch,
        token_response={"access_token": "a2", "expires_in": 3600},
        verify_payload={"items": [{"id": "UC1", "snippet": {}}]},
    )
    with pytest.raises(TokenError):
        get_access_token(connection())


def test_missing_refresh_token_fails(monkeypatch):
    record = stored_token()
    record["value"] = {k: v for k, v in record["value"].items() if k != "refresh_token"}
    configure_store(monkeypatch, record)
    with pytest.raises(TokenError):
        get_access_token(connection())


def test_revoked_connection_never_issues(monkeypatch):
    configure_store(monkeypatch, stored_token())
    with pytest.raises(TokenError):
        get_access_token(connection(status="revoked"))


def test_revoke_clears_tokens_and_marks_revoked(monkeypatch):
    writes = []
    configure_client(monkeypatch)
    configure_store(monkeypatch, stored_token(), writes=writes)
    calls = transport_for(monkeypatch)
    updated = revoke_connection(connection())
    assert updated["status"] == "revoked"
    assert writes[0] == {}
    assert any("revoke" in call["url"] for call in calls)
    with pytest.raises(TokenError):
        get_access_token(updated)


def test_revoke_preserves_record_client_override(monkeypatch):
    writes = []
    configure_client(monkeypatch)
    configure_store(monkeypatch, stored_token(client_id="legacy-id", client_secret="legacy-secret"), writes=writes)
    transport_for(monkeypatch)
    revoke_connection(connection())
    assert writes[0] == {"client_id": "legacy-id", "client_secret": "legacy-secret"}


def test_revoke_direct_token_connection_clears_local_secret(monkeypatch):
    writes = []
    configure_store(monkeypatch, {
        "credential_id": "oauth#youtube-personal",
        "value": {"token": "xoxb-example"},
        "version": 1,
    }, writes=writes)

    updated = revoke_connection(connection(provider="slack"))

    assert updated["status"] == "revoked"
    assert writes == [{}]
