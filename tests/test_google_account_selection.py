"""Account selection for the gmail/gdrive agent ops (google_accounts).

The resolver is the single gate every /api/agent/gmail|gdrive operation
passes: these tests pin the brief's guarantees — accounts are Google
connections granting the service, listing carries verified email plus
connection id, `--account` matches id/email/unique fragment, an omitted
selection resolves only a single connected account, and ambiguous or
missing matches are rejected naming the candidates (never a silent switch).
"""

import pytest

from src.dapier.api.agent import google_accounts


def _google_connection(cid, email, scope_urls):
    return {
        "connection_id": cid,
        "provider": "google",
        "status": "connected",
        "verified_account_id": email,
        "account_title": email.split("@")[0].title(),
        "scopes": list(scope_urls),
        "granted_scopes": list(scope_urls),
    }


GMAIL_ONLY_SCOPES = (
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/userinfo.email",
)
FULL_SCOPES = GMAIL_ONLY_SCOPES + (
    "https://www.googleapis.com/auth/drive.readonly",
)
DISCONNECTED = "disconnected"


@pytest.fixture
def two_accounts(monkeypatch):
    """gmail-only account b@gmail.com and gmail+drive account a@gmail.com;
    `_scan_all` serves them, `_tables` serves a dummy pair."""
    conns = [
        _google_connection("google-a", "a@gmail.com", FULL_SCOPES),
        _google_connection("google-b", "b@gmail.com", GMAIL_ONLY_SCOPES),
        {"connection_id": "slack-1", "provider": "slack", "status": "connected",
         "verified_account_id": "T1", "scopes": ["xoxp"]},
        _google_connection("google-old", "old@gmail.com", GMAIL_ONLY_SCOPES)
        | {"status": DISCONNECTED},
    ]
    monkeypatch.setattr(google_accounts, "_tables", lambda: (object(), object()))
    monkeypatch.setattr(google_accounts.connections, "_scan_all",
                        lambda table: conns)
    return conns


def test_list_accounts_filters_provider_and_service(two_accounts):
    gmail_accounts = google_accounts.list_accounts("gmail")
    ids = [item["connection_id"] for item in gmail_accounts]
    assert ids == ["google-a", "google-b", "google-old"]  # slack never, order stable
    drive_accounts = google_accounts.list_accounts("drive")
    assert [item["connection_id"] for item in drive_accounts] == ["google-a"]
    view = gmail_accounts[0]
    assert view["email"] == "a@gmail.com"
    assert view["connection_id"] == "google-a"
    assert view["refs"]  # human references ride along


def test_resolve_by_id_email_and_fragment(two_accounts):
    assert google_accounts.resolve_account("gmail", "google-a") == "google-a"
    assert google_accounts.resolve_account("gmail", "A@GMAIL.COM") == "google-a"
    assert google_accounts.resolve_account("gmail", "b@gmail") == "google-b"
    # a gmail-only fragment never resolves for drive: wrong-service is a miss
    with pytest.raises(google_accounts.GoogleAccountError) as exc:
        google_accounts.resolve_account("drive", "b@gmail.com")
    assert "b@gmail.com" in str(exc.value)  # the error names what exists


def test_omitted_account_single_connected_resolves(two_accounts):
    # drive has exactly one connected account: no --account needed
    assert google_accounts.resolve_account("drive", None) == "google-a"


def test_omitted_account_multiple_is_rejected(two_accounts):
    with pytest.raises(google_accounts.GoogleAccountError) as exc:
        google_accounts.resolve_account("gmail", None)
    message = str(exc.value)
    assert "--account is required" in message
    assert "a@gmail.com" in message and "b@gmail.com" in message
    assert exc.value.accounts  # candidates ride the error for the API


def test_ambiguous_fragment_is_rejected(monkeypatch):
    conns = [
        _google_connection("google-a", "alexey@gmail.com", GMAIL_ONLY_SCOPES),
        _google_connection("google-b", "alexey@datatalks.club", GMAIL_ONLY_SCOPES),
    ]
    monkeypatch.setattr(google_accounts, "_tables", lambda: (object(), object()))
    monkeypatch.setattr(google_accounts.connections, "_scan_all", lambda table: conns)
    with pytest.raises(google_accounts.GoogleAccountError) as exc:
        google_accounts.resolve_account("gmail", "alexey")
    assert "Ambiguous" in str(exc.value)
    assert "alexey@gmail.com" in str(exc.value)
    assert "alexey@datatalks.club" in str(exc.value)


def test_disconnected_duplicate_never_shadows_connected(monkeypatch):
    conns = [
        _google_connection("google-a", "alexey@gmail.com", GMAIL_ONLY_SCOPES)
        | {"status": DISCONNECTED},
        _google_connection("google-b", "alexey@gmail.com", GMAIL_ONLY_SCOPES),
    ]
    monkeypatch.setattr(google_accounts, "_tables", lambda: (object(), object()))
    monkeypatch.setattr(google_accounts.connections, "_scan_all", lambda table: conns)
    # the connected sign-in wins when it is the only connected match
    assert google_accounts.resolve_account("gmail", None) == "google-b"


def test_no_accounts_is_a_clear_error(monkeypatch):
    monkeypatch.setattr(google_accounts, "_tables", lambda: (object(), object()))
    monkeypatch.setattr(google_accounts.connections, "_scan_all", lambda table: [])
    with pytest.raises(google_accounts.GoogleAccountError) as exc:
        google_accounts.resolve_account("gmail", None)
    assert "No Google connection grants gmail" in str(exc.value)
