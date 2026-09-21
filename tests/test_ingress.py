import hashlib
import hmac

from src import ingress


def test_youtube_signature_header_is_case_insensitive(monkeypatch):
    secret = "websub-secret"
    body = b"<feed />"
    signature = hmac.new(secret.encode(), body, hashlib.sha1).hexdigest()

    monkeypatch.setenv("YOUTUBE_WEBHOOK_SECRET_ID", "dapier/youtube-websub")
    monkeypatch.setattr(ingress, "_youtube_secret", secret)

    assert ingress._verify_youtube(
        {"headers": {"X-Hub-Signature": f"sha1={signature}"}}, body
    )


def test_youtube_signature_rejects_missing_or_invalid_value(monkeypatch):
    monkeypatch.setenv("YOUTUBE_WEBHOOK_SECRET_ID", "dapier/youtube-websub")
    monkeypatch.setattr(ingress, "_youtube_secret", "websub-secret")

    assert not ingress._verify_youtube({"headers": {}}, b"<feed />")
    assert not ingress._verify_youtube(
        {"headers": {"x-hub-signature": "sha1=invalid"}}, b"<feed />"
    )


def test_publish_adds_version_and_correlation_id(monkeypatch):
    sent = []
    monkeypatch.setenv("EVENT_QUEUE_URL", "https://sqs.example.test/events")
    monkeypatch.setattr(
        ingress.queue,
        "send_message",
        lambda **kwargs: sent.append(kwargs),
    )

    ingress._publish("custom", "received", {"ok": True}, event_id="event-1")

    import json

    envelope = json.loads(sent[0]["MessageBody"])
    assert envelope["schema_version"] == "1.0"
    assert envelope["id"] == "event-1"
    assert envelope["correlation_id"] == "event-1"


def test_agent_paths_reach_the_api_router(monkeypatch):
    import json

    monkeypatch.setenv("AUTH_CLI_CLIENT_ID", "")
    response = ingress.handler(
        {
            "requestContext": {"http": {"method": "GET", "path": "/api/agent/config"}},
            "headers": {"host": "dapier.example.test"},
            "cookies": [],
        },
        None,
    )
    assert response["statusCode"] == 200
    assert "cli_client_id" in json.loads(response["body"])
