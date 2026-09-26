import pytest
import io
import urllib.error

from src.dapier.connections.providers import oauth_providers
from src.dapier.connections.providers.oauth_providers import (
    ProviderError,
    UnknownProviderError,
    authorization_url,
    exchange_code,
    get,
    is_expired,
    normalize_scopes,
    normalize_token_data,
    redact,
    refresh_access_token,
    revoke_token,
    verify_account,
)


def test_unknown_provider():
    with pytest.raises(UnknownProviderError):
        get("myspace")


def test_normalize_scopes_sorts_and_dedupes():
    assert normalize_scopes("dropbox", ["b", "a", "b", " "]) == ["a", "b"]


def test_normalize_scopes_accepts_space_separated_string_split_by_caller():
    assert normalize_scopes("dropbox", []) == []


def test_youtube_requires_a_scope():
    with pytest.raises(ProviderError):
        normalize_scopes("youtube", [])


def test_authorization_url_youtube_pkce():
    url = authorization_url(
        "youtube",
        client_id="cid",
        redirect_uri="https://dapier.example.test/oauth/callback",
        scopes=["https://www.googleapis.com/auth/youtube.readonly"],
        state="state-token",
        code_challenge="challenge",
    )
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "code_challenge=challenge" in url
    assert "code_challenge_method=S256" in url
    assert "access_type=offline" in url
    assert "prompt=consent" in url


def test_authorization_url_dropbox_offline():
    url = authorization_url(
        "dropbox",
        client_id="cid",
        redirect_uri="https://dapier.example.test/oauth/callback",
        scopes=[],
        state="state-token",
    )
    assert url.startswith("https://www.dropbox.com/oauth2/authorize?")
    assert "token_access_type=offline" in url
    assert "code_challenge" not in url


def fake_transport(calls, status=200, payload=None):
    def transport(method, url, *, headers=None, body=None, timeout=15):
        calls.append({"method": method, "url": url, "headers": headers, "body": body})
        import json as _json

        return status, _json.dumps(payload if payload is not None else {}).encode()

    return transport


def test_exchange_code_posts_pkce_verifier():
    calls = []
    data = exchange_code(
        "youtube", code="auth-code", client_id="cid", client_secret="secret",
        redirect_uri="https://dapier.example.test/oauth/callback",
        code_verifier="verifier", transport=fake_transport(calls, payload={"access_token": "at"}),
    )
    assert data["access_token"] == "at"
    body = calls[0]["body"].decode()
    assert "code_verifier=verifier" in body
    assert "auth-code" not in body or "code=auth-code" in body


def test_exchange_code_without_access_token_fails():
    with pytest.raises(ProviderError):
        exchange_code(
            "youtube", code="c", client_id="cid", client_secret="s",
            redirect_uri="https://x.example.test/cb",
            transport=fake_transport([], payload={"unexpected": True}),
        )


def test_exchange_code_provider_error_is_redacted():
    calls = []
    with pytest.raises(ProviderError) as exc:
        exchange_code(
            "youtube", code="c", client_id="cid", client_secret="super-secret-value",
            redirect_uri="https://x.example.test/cb",
            transport=fake_transport(calls, status=400, payload={"error": "invalid_grant"}),
        )
    assert "super-secret-value" not in str(exc.value)


def test_http_error_response_reports_safe_oauth_error(monkeypatch):
    def rejected(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url, 400, "Bad Request", {},
            io.BytesIO(b'{"error":"invalid_grant","error_description":"code=secret-code"}'),
        )

    monkeypatch.setattr(oauth_providers.urllib.request, "urlopen", rejected)
    with pytest.raises(ProviderError, match=r"token endpoint returned HTTP 400 \(invalid_grant\)") as exc:
        exchange_code(
            "dropbox", code="secret-code", client_id="cid", client_secret="secret",
            redirect_uri="https://dapier.example.test/oauth/callback",
        )
    assert "secret-code" not in str(exc.value)


def test_http_error_response_hides_unrecognized_provider_text(monkeypatch):
    def rejected(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url, 400, "Bad Request", {},
            io.BytesIO(b'{"error":"bad-code-secret-code","error_description":"secret-code"}'),
        )

    monkeypatch.setattr(oauth_providers.urllib.request, "urlopen", rejected)
    with pytest.raises(ProviderError, match="token endpoint returned HTTP 400") as exc:
        exchange_code(
            "dropbox", code="secret-code", client_id="cid", client_secret="secret",
            redirect_uri="https://dapier.example.test/oauth/callback",
        )
    assert "secret-code" not in str(exc.value)


def test_http_error_response_reports_code_prefixed_with_description(monkeypatch):
    # Dropbox returns {"error": "invalid_client: Invalid client_id or
    # client_secret"} — one string, code and description together.
    def rejected(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url, 400, "Bad Request", {},
            io.BytesIO(b'{"error":"invalid_client: app secret secret-code"}'),
        )

    monkeypatch.setattr(oauth_providers.urllib.request, "urlopen", rejected)
    with pytest.raises(ProviderError, match=r"token endpoint returned HTTP 400 \(invalid_client\)") as exc:
        exchange_code(
            "dropbox", code="secret-code", client_id="cid", client_secret="secret",
            redirect_uri="https://dapier.example.test/oauth/callback",
        )
    assert "secret-code" not in str(exc.value)


def test_refresh_preserves_omitted_refresh_token():
    data = refresh_access_token(
        "youtube", refresh_token="old-refresh", client_id="cid", client_secret="s",
        transport=fake_transport([], payload={"access_token": "new-at", "expires_in": 3600}),
    )
    normalized = normalize_token_data(data, previous_refresh_token="old-refresh", now=1_000_000)
    assert normalized["access_token"] == "new-at"
    assert normalized["refresh_token"] == "old-refresh"
    assert normalized["expires_at"] == 1_000_000 + 3600 - 60


def test_refresh_rotation_uses_new_refresh_token():
    normalized = normalize_token_data(
        {"access_token": "at", "refresh_token": "new-refresh", "expires_in": 100},
        previous_refresh_token="old-refresh", now=0,
    )
    assert normalized["refresh_token"] == "new-refresh"
    assert normalized["expires_at"] == 40


def test_is_expired_with_skew():
    assert is_expired({"expires_at": 1000}, now=900) is True
    assert is_expired({"expires_at": 1000}, now=800) is False
    assert is_expired({}, now=0) is True


def test_verify_youtube_channel():
    calls = []
    account_id, title = verify_account(
        "youtube", "valid-token",
        transport=fake_transport(calls, payload={
            "items": [{"id": "UC123", "snippet": {"title": "DataTalksClub"}}],
        }),
    )
    assert (account_id, title) == ("UC123", "DataTalksClub")
    assert "mine=true" in calls[0]["url"]
    assert calls[0]["headers"]["authorization"] == "Bearer valid-token"


def test_verify_youtube_no_channel_fails_closed():
    with pytest.raises(ProviderError):
        verify_account("youtube", "token", transport=fake_transport([], payload={"items": []}))


def test_verify_dropbox_account():
    account_id, title = verify_account(
        "dropbox", "token",
        transport=fake_transport([], payload={
            "account_id": "dbid:123", "name": {"display_name": "Op"}, "email": "op@example.test",
        }),
    )
    assert (account_id, title) == ("dbid:123", "Op")


def test_revoke_token_success_and_failure():
    assert revoke_token("youtube", "t", transport=fake_transport([], status=200)) is True
    assert revoke_token("youtube", "t", transport=fake_transport([], status=400)) is False

    def boom(method, url, **kwargs):
        raise OSError("down")

    assert revoke_token("dropbox", "t", transport=boom) is False


def test_redact_removes_secrets():
    redacted = redact({
        "client_secret": "shh-secret-value", "access_token": "tok-value-123",
        "nested": {"refresh_token": "refresh-value-456"}, "ok": "fine",
    })
    assert redacted == {
        "client_secret": "[redacted]", "access_token": "[redacted]",
        "nested": {"refresh_token": "[redacted]"}, "ok": "fine",
    }
    assert "shh-secret-value" not in str(redacted)
    assert "tok-value-123" not in str(redacted)
    assert "refresh-value-456" not in str(redacted)


def test_google_requires_a_scope():
    with pytest.raises(ProviderError):
        normalize_scopes("google", [])


def test_authorization_url_google_offline_consent():
    url = authorization_url(
        "google",
        client_id="cid",
        redirect_uri="https://dapier.example.test/oauth/callback",
        scopes=["https://www.googleapis.com/auth/calendar.events.owned"],
        state="state-token",
    )
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "access_type=offline" in url
    assert "prompt=consent" in url
    assert "calendar.events.owned" in url


def test_verify_google_account_email():
    calls = []
    transport = fake_transport(calls, payload={
        "email": "alexey@datatalks.club", "email_verified": True,
        "name": "Alexey Grigorev"})
    account_id, title = verify_account("google", "token", transport=transport)
    assert account_id == "alexey@datatalks.club"
    assert title == "Alexey Grigorev"
    assert calls[0]["url"] == "https://www.googleapis.com/oauth2/v3/userinfo"


def test_verify_google_unverified_email_fails_closed():
    transport = fake_transport([], payload={"email": "a@b.c", "email_verified": False})
    with pytest.raises(ProviderError):
        verify_account("google", "token", transport=transport)


def test_verify_google_missing_email_fails_closed():
    transport = fake_transport([], payload={"name": "No Email"})
    with pytest.raises(ProviderError):
        verify_account("google", "token", transport=transport)


def test_revoke_google_posts_to_google_revoke():
    calls = []
    assert revoke_token("google", "t", transport=fake_transport(calls, status=200)) is True
    assert calls[0]["url"] == "https://oauth2.googleapis.com/revoke"


def test_google_requires_a_scope():
    with pytest.raises(ProviderError):
        normalize_scopes("google", [])


def test_authorization_url_google_offline_consent():
    url = authorization_url(
        "google",
        client_id="cid",
        redirect_uri="https://dapier.example.test/oauth/callback",
        scopes=["https://www.googleapis.com/auth/calendar.events.owned"],
        state="state-token",
    )
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "access_type=offline" in url
    assert "prompt=consent" in url
    assert "calendar.events.owned" in url


def test_verify_google_account_email():
    calls = []
    transport = fake_transport(calls, payload={
        "email": "alexey@datatalks.club", "email_verified": True,
        "name": "Alexey Grigorev"})
    account_id, title = verify_account("google", "token", transport=transport)
    assert account_id == "alexey@datatalks.club"
    assert title == "Alexey Grigorev"
    assert calls[0]["url"] == "https://www.googleapis.com/oauth2/v3/userinfo"


def test_verify_google_unverified_email_fails_closed():
    transport = fake_transport([], payload={"email": "a@b.c", "email_verified": False})
    with pytest.raises(ProviderError):
        verify_account("google", "token", transport=transport)


def test_verify_google_missing_email_fails_closed():
    transport = fake_transport([], payload={"name": "No Email"})
    with pytest.raises(ProviderError):
        verify_account("google", "token", transport=transport)


def test_revoke_google_posts_to_google_revoke():
    calls = []
    assert revoke_token("google", "t", transport=fake_transport(calls, status=200)) is True
    assert calls[0]["url"] == "https://oauth2.googleapis.com/revoke"
