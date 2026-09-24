import json

import pytest

from src import admin, credentials, oauth_clients, ingress
from src.oauth_clients import ClientConfigError


def request(method, path, body=None, cookies=None):
    return {
        "requestContext": {"http": {"method": method, "path": path}},
        "headers": {"host": "dapier.example.test"},
        "cookies": cookies or [],
        "body": json.dumps(body) if body is not None else None,
    }


@pytest.fixture(autouse=True)
def clean_cache():
    oauth_clients.invalidate_cache()
    yield
    oauth_clients.invalidate_cache()


class FakeTable:
    items = {}

    def get_item(self, **kwargs):
        return {"Item": self.items[kwargs["Key"]["credential_id"]]}


class FakeDynamo:
    def Table(self, _name):
        return FakeTable()


@pytest.fixture
def config_db(monkeypatch):
    FakeTable.items = {}
    monkeypatch.delenv("DAPIER_SKIP_CONFIG_DB", raising=False)
    monkeypatch.setenv("CREDENTIALS_TABLE", "credentials")
    monkeypatch.setattr("boto3.resource", lambda service: FakeDynamo())
    return FakeTable.items


def test_explicit_values_win_over_everything(monkeypatch, config_db):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "env-id")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "env-secret")
    config_db["oauth-client#google"] = {"value": {"client_id": "db-id", "client_secret": "db-secret"}}

    assert oauth_clients.get("google", client_id="explicit", client_secret="pair") == ("explicit", "pair")


def test_config_db_wins_over_environment(monkeypatch, config_db):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "env-id")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "env-secret")
    config_db["oauth-client#google"] = {"value": {"client_id": "db-id", "client_secret": "db-secret"}}

    assert oauth_clients.get("google") == ("db-id", "db-secret")


def test_environment_is_the_fallback_without_a_record(monkeypatch, config_db):
    monkeypatch.setenv("DROPBOX_OAUTH_CLIENT_ID", "env-id")
    monkeypatch.setenv("DROPBOX_OAUTH_CLIENT_SECRET", "env-secret")

    assert oauth_clients.get("dropbox") == ("env-id", "env-secret")


def test_mixed_sources_fill_each_other(monkeypatch, config_db):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "env-secret")
    config_db["oauth-client#google"] = {"value": {"client_id": "db-id"}}

    assert oauth_clients.get("google") == ("db-id", "env-secret")


def test_unconfigured_provider_names_the_console_path(config_db):
    with pytest.raises(ClientConfigError) as exc:
        oauth_clients.get("google")
    assert "Credentials" in str(exc.value)


def test_storage_failure_falls_back_to_environment(monkeypatch, config_db):
    monkeypatch.setenv("DROPBOX_OAUTH_CLIENT_ID", "env-id")
    monkeypatch.setenv("DROPBOX_OAUTH_CLIENT_SECRET", "env-secret")

    def broken(service):
        raise RuntimeError("dynamodb unavailable")

    monkeypatch.setattr("boto3.resource", broken)
    assert oauth_clients.get("dropbox") == ("env-id", "env-secret")


def test_skip_config_db_gate_keeps_tests_off_the_network(monkeypatch, config_db):
    monkeypatch.setenv("DAPIER_SKIP_CONFIG_DB", "1")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "env-id")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "env-secret")
    config_db["oauth-client#google"] = {"value": {"client_id": "db-id", "client_secret": "db-secret"}}

    assert oauth_clients.get("google") == ("env-id", "env-secret")


def test_youtube_shares_the_google_record(monkeypatch, config_db):
    config_db["oauth-client#google"] = {"value": {"client_id": "db-id", "client_secret": "db-secret"}}

    assert oauth_clients.get("youtube") == ("db-id", "db-secret")
    assert oauth_clients.canonical_provider("youtube") == "google"


def test_save_oauth_client_stores_write_only_record(monkeypatch, config_db):
    stored = []
    monkeypatch.setattr(credentials, "put_credential", lambda credential_id, value, **kwargs: stored.append((credential_id, value, kwargs)))
    monkeypatch.setattr(admin, "_session_subject", lambda event: "op")
    monkeypatch.setattr(admin, "_audit_event", lambda *args, **kwargs: None)

    response = admin.save_oauth_client("google", request("PUT", "/api/admin/oauth-clients/google", {
        "client_id": "gid", "client_secret": "gsec",
    }))

    assert response["statusCode"] == 200
    assert "gsec" not in response["body"]
    assert stored == [("oauth-client#google", {"client_id": "gid", "client_secret": "gsec"}, {"provider": "google"})]
    assert response["body"] and json.loads(response["body"])["source"] == "config"


def test_save_oauth_client_folds_youtube_onto_google(monkeypatch, config_db):
    stored = []
    monkeypatch.setattr(credentials, "put_credential", lambda credential_id, value, **kwargs: stored.append((credential_id, value, kwargs)))
    monkeypatch.setattr(admin, "_session_subject", lambda event: "op")
    monkeypatch.setattr(admin, "_audit_event", lambda *args, **kwargs: None)

    response = admin.save_oauth_client("youtube", request("PUT", "/api/admin/oauth-clients/youtube", {
        "client_id": "gid", "client_secret": "gsec",
    }))

    assert response["statusCode"] == 200
    assert stored[0][0] == "oauth-client#google"


def test_save_oauth_client_requires_both_values(monkeypatch, config_db):
    response = admin.save_oauth_client("google", request("PUT", "/api/admin/oauth-clients/google", {"client_id": "gid"}))
    assert response["statusCode"] == 400

    response = admin.save_oauth_client("mailchimp", request("PUT", "/api/admin/oauth-clients/mailchimp", {
        "client_id": "a", "client_secret": "b",
    }))
    assert response["statusCode"] == 404


def test_status_reports_config_source_and_never_the_secret(monkeypatch, config_db):
    monkeypatch.setenv("DROPBOX_OAUTH_CLIENT_ID", "env-id")
    monkeypatch.setenv("DROPBOX_OAUTH_CLIENT_SECRET", "env-secret")
    status = admin._oauth_client_status("dropbox")
    assert status == {"provider": "dropbox", "client_id": "env-id", "source": "deploy", "configured": True}

    config_db["oauth-client#dropbox"] = {"value": {"client_id": "db-id", "client_secret": "db-secret"}}
    oauth_clients.invalidate_cache()
    status = admin._oauth_client_status("dropbox")
    assert status["source"] == "config"
    assert status["client_id"] == "db-id"
    assert "db-secret" not in json.dumps(status)

    status = admin._oauth_client_status("google")
    assert status["configured"] is False and status["source"] == "none"


def test_ingress_accepts_config_db_secret_for_webhook(monkeypatch, config_db):
    config_db["oauth-client#dropbox"] = {"value": {"client_id": "db-id", "client_secret": "db-app-secret"}}
    body = b'{"list_folder": {"accounts": []}, "delta": {"accounts": []}}'
    import hashlib
    import hmac as hmac_mod

    signature = hmac_mod.new(b"db-app-secret", body, hashlib.sha256).hexdigest()
    event = {"requestContext": {"http": {"method": "POST", "path": "/webhooks/dropbox"}},
             "headers": {"x-dropbox-signature": signature, "content-type": "application/json"},
             "cookies": [], "body": body.decode()}

    assert ingress._dropbox_secrets() == ["db-app-secret"]
    assert ingress._verify_dropbox(event, body) is True
