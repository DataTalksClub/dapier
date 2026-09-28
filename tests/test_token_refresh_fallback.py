"""Refresh fallback when a locally-fresh token is rejected by the provider.

A stored access token can pass its local expiry check yet fail the
provider's verify (revoked or rotated server-side). Before the fallback,
``get_access_token`` raised TokenError straight away — wedging workflows
until the stored expiry passed despite a valid refresh token. Now that
failure triggers one ``refresh_and_store`` attempt (which re-verifies and
bind-checks the replacement); a second consecutive verify failure still
fails loudly, and binding is enforced exactly as before.

Mirrors the fixture style of tests/test_tokens.py.
"""

import json

import pytest

from src.dapier.connections import tokens
from src.dapier.connections.credentials import VersionConflict
from src.dapier.connections.records import BindingError
from src.dapier.connections.tokens import TokenError, get_access_token

YOUTUBE = "youtube"


def configure_client(monkeypatch, **overrides):
    values = {
        "GOOGLE_OAUTH_CLIENT_ID": "shared-client-id",
        "GOOGLE_OAUTH_CLIENT_SECRET": "shared-client-secret",
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
        "access_token": "revoked-server-side",
        "refresh_token": "refresh-1",
        # Locally fresh (far future); the provider disagrees in these tests.
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


def transport_for(monkeypatch, *, verify_failures=1, verify_payload=None):
    """The account-verify endpoint rejects the first ``verify_failures``
    calls with HTTP 401 (the revoked-token shape) before returning the
    channel payload; the token endpoint always issues ``new-access``."""
    calls = []
    remaining = {"verify": verify_failures}

    def fake(method, url, *, headers=None, body=None, timeout=15):
        calls.append({"method": method, "url": url})
        if "oauth2.googleapis.com/token" in url:
            return 200, json.dumps({"access_token": "new-access",
                                    "expires_in": 3600}).encode()
        if "googleapis.com/youtube" in url:
            if remaining["verify"] > 0:
                remaining["verify"] -= 1
                return 401, b"{}"
            return 200, json.dumps(verify_payload or {
                "items": [{"id": "UC1", "snippet": {"title": "Ch"}}]}).encode()
        raise AssertionError(f"unexpected url {url}")

    import src.dapier.connections.providers.oauth_providers as providers

    monkeypatch.setattr(providers, "_default_transport", fake)
    return calls


def test_rejected_fresh_token_refreshes_instead_of_failing(monkeypatch):
    writes = []
    configure_client(monkeypatch)
    configure_store(monkeypatch, stored_token(), writes=writes)
    calls = transport_for(monkeypatch, verify_failures=1)

    token, info = get_access_token(connection())

    assert token == "new-access"
    assert info["refreshed"] is True
    assert info["provider_account_id"] == "UC1"
    # Exactly one refresh POST carried the fallback, and the replacement
    # token replaced the provider-revoked one in the store.
    assert len([c for c in calls if "oauth2.googleapis.com/token" in c["url"]]) == 1
    assert writes[0]["access_token"] == "new-access"


def test_second_consecutive_verify_failure_still_fails(monkeypatch):
    configure_client(monkeypatch)
    configure_store(monkeypatch, stored_token())
    calls = transport_for(monkeypatch, verify_failures=99)

    with pytest.raises(TokenError, match="HTTP 401"):
        get_access_token(connection())
    # The fallback was tried: two rejected verify calls bracket one refresh.
    verify_calls = [c for c in calls if "googleapis.com/youtube" in c["url"]]
    refresh_posts = [c for c in calls if "oauth2.googleapis.com/token" in c["url"]]
    assert len(verify_calls) == 2
    assert len(refresh_posts) == 1


def test_rejected_fresh_token_without_refresh_token_reports_the_verify_failure(monkeypatch):
    record = stored_token()
    record["value"] = {k: v for k, v in record["value"].items() if k != "refresh_token"}
    configure_store(monkeypatch, record)
    transport_for(monkeypatch, verify_failures=99)

    # No way out exists, but the operator sees why the token was rejected,
    # not the later "no refresh token" complaint.
    with pytest.raises(TokenError, match="HTTP 401"):
        get_access_token(connection())


def test_refreshed_account_binding_is_still_enforced(monkeypatch):
    writes = []
    configure_client(monkeypatch)
    configure_store(monkeypatch, stored_token(), writes=writes)
    transport_for(monkeypatch, verify_failures=1, verify_payload={
        "items": [{"id": "UC-HIJACK", "snippet": {"title": "Evil"}}],
    })

    with pytest.raises(BindingError):
        get_access_token(connection(expected_account_id="UC1"))
    assert writes == []  # a mismatched refresh stores nothing


def test_refresh_conflict_during_the_fallback_raises_token_error(monkeypatch):
    configure_client(monkeypatch)
    configure_store(monkeypatch, stored_token(), conflicts=1)
    transport_for(monkeypatch, verify_failures=1)

    with pytest.raises(TokenError, match="Concurrent refresh conflict"):
        get_access_token(connection())


if __name__ == "__main__":
    pytest.main([__file__])
