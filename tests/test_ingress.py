import hashlib
import hmac
import json
import re

from src.dapier.api import router as ingress


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


def test_dropbox_challenge_is_answered():
    response = ingress.handler(
        {
            "requestContext": {"http": {"method": "GET", "path": "/hooks/dropbox"}},
            "queryStringParameters": {"challenge": "ping"},
        },
        None,
    )
    assert response["statusCode"] == 200
    assert response["body"] == "ping"


def test_dropbox_accounts_deduplicates_across_sections():
    payload = {
        "list_folder": {"accounts": ["dbid:a", "dbid:b"]},
        "delta": {"accounts": ["dbid:b", "dbid:c"]},
    }
    assert ingress._dropbox_accounts(payload) == ["dbid:a", "dbid:b", "dbid:c"]


def test_dropbox_secrets_come_from_the_deploy_time_app_secret(monkeypatch):
    monkeypatch.setenv("DROPBOX_OAUTH_CLIENT_SECRET", "app-secret")
    assert ingress._dropbox_secrets() == ["app-secret"]

    monkeypatch.delenv("DROPBOX_OAUTH_CLIENT_SECRET")
    assert ingress._dropbox_secrets() == []


def test_dropbox_webhook_queues_one_message_per_account(monkeypatch):
    import json

    body = json.dumps({
        "list_folder": {"accounts": ["dbid:a"]},
        "delta": {"accounts": ["dbid:a", "dbid:b"]},
    }).encode()
    signature = hmac.new(b"app-secret", body, hashlib.sha256).hexdigest()
    monkeypatch.setattr(ingress, "_dropbox_secrets", lambda: ["app-secret"])
    monkeypatch.setenv("DROPBOX_QUEUE_URL", "https://sqs.example.test/dropbox")
    sent = []
    monkeypatch.setattr(ingress.queue, "send_message", lambda **kwargs: sent.append(kwargs))

    response = ingress.handler(
        {
            "requestContext": {"http": {"method": "POST", "path": "/hooks/dropbox"}},
            "headers": {"X-Dropbox-Signature": signature},
            "body": body.decode(),
        },
        None,
    )

    assert response["statusCode"] == 202
    envelopes = [json.loads(message["MessageBody"]) for message in sent]
    assert [envelope["data"]["account_id"] for envelope in envelopes] == ["dbid:a", "dbid:b"]
    assert all(message["QueueUrl"] == "https://sqs.example.test/dropbox" for message in sent)
    assert len({envelope["correlation_id"] for envelope in envelopes}) == 1
    assert all(envelope["event"] == "account.changed" for envelope in envelopes)


def test_dropbox_webhook_rejects_missing_or_invalid_signature(monkeypatch):
    import json

    body = json.dumps({"delta": {"accounts": ["dbid:a"]}}).encode()
    bad_signature = hmac.new(b"other-secret", body, hashlib.sha256).hexdigest()
    monkeypatch.setattr(ingress, "_dropbox_secrets", lambda: ["app-secret"])
    monkeypatch.setenv("DROPBOX_QUEUE_URL", "https://sqs.example.test/dropbox")
    sent = []
    monkeypatch.setattr(ingress.queue, "send_message", lambda **kwargs: sent.append(kwargs))

    def post(headers):
        return ingress.handler(
            {
                "requestContext": {"http": {"method": "POST", "path": "/hooks/dropbox"}},
                "headers": headers,
                "body": body.decode(),
            },
            None,
        )

    assert post({"x-dropbox-signature": bad_signature})["statusCode"] == 401
    assert post({})["statusCode"] == 401
    assert sent == []


def test_dropbox_webhook_fails_closed_without_configured_connections(monkeypatch):
    monkeypatch.setattr(ingress, "_dropbox_secrets", lambda: [])
    response = ingress.handler(
        {
            "requestContext": {"http": {"method": "POST", "path": "/hooks/dropbox"}},
            "headers": {},
            "body": "{}",
        },
        None,
    )
    assert response["statusCode"] == 401


def test_console_views_serve_index_html():
    for path in ingress.CONSOLE_VIEWS:
        response = ingress._static(path)
        assert response["statusCode"] == 200, path
        assert "forbidden-view" in response["body"], path
        assert response["headers"]["cache-control"] == "no-store", path


def test_every_nav_link_resolves_to_a_served_page():
    index = ingress._static("/")
    nav_links = re.findall(r'class="nav-item[^"]*" href="([^"]+)"', index["body"])
    assert len(nav_links) >= 7, nav_links
    for path in nav_links:
        response = ingress._static(path)
        assert response is not None and response["statusCode"] == 200, path


def test_console_assets_are_self_hosted():
    lucide = ingress._static("/assets/lucide.min.js")
    assert lucide["statusCode"] == 200
    assert "unpkg.com" not in lucide["headers"]["content-security-policy"]
    assert "sourceMappingURL" not in lucide["body"]

    index = ingress._static("/")
    assert "https://unpkg.com" not in index["body"]
    assert "forbidden-view" in index["body"]


# --- Webhook and Telegram trigger hooks -------------------------------------

def _hook_stub(hook_id="orders", kind="webhook", token="tok-123", enabled=True):
    item = {"hook_id": hook_id, "kind": kind, "url": f"u-{hook_id}", "token": token,
            "actions": [], "enabled": enabled}
    return type("T", (), {
        "scan": lambda self, Limit=200: {"Items": [dict(item)]},
        "get_item": lambda self, Key: {"Item": dict(item)} if Key["hook_id"] == hook_id else {},
        "put_item": lambda self, Item: None,
        "delete_item": lambda self, Key: None,
    })()


def _post(path, body=b'{"hello":"world"}', headers=None, query=None):
    return ingress.handler({
        "requestContext": {"http": {"method": "POST", "path": path}},
        "headers": headers or {},
        "queryStringParameters": query,
        "body": body.decode(),
    }, None)


def test_webhook_hook_accepts_bearer_token_and_publishes(monkeypatch):
    sent = []
    monkeypatch.setenv("EVENT_QUEUE_URL", "https://sqs.example.test/events")
    monkeypatch.setenv("HOOK_TRIGGERS_TABLE", "hooks")
    monkeypatch.setattr(ingress.queue, "send_message", lambda **kwargs: sent.append(kwargs))
    monkeypatch.setattr("src.dapier.triggers.hook_triggers.get_table", lambda *a, **k: _hook_stub())

    response = _post("/hooks/webhook/orders",
                     headers={"authorization": "Bearer tok-123",
                              "content-type": "application/json"},
                     query={"src": "ci"})

    assert response["statusCode"] == 202
    envelope = json.loads(sent[0]["MessageBody"])
    assert envelope["connector"] == "webhook"
    assert envelope["event"] == "request.received"
    assert envelope["source"] == "orders"
    assert envelope["data"]["hook"] == "orders"
    assert envelope["data"]["body"] == {"hello": "world"}
    assert envelope["data"]["query"] == {"src": "ci"}


def test_webhook_hook_rejects_bad_or_missing_tokens(monkeypatch):
    sent = []
    monkeypatch.setenv("EVENT_QUEUE_URL", "https://sqs.example.test/events")
    monkeypatch.setenv("HOOK_TRIGGERS_TABLE", "hooks")
    monkeypatch.setattr(ingress.queue, "send_message", lambda **kwargs: sent.append(kwargs))
    monkeypatch.setattr("src.dapier.triggers.hook_triggers.get_table", lambda *a, **k: _hook_stub())

    assert _post("/hooks/webhook/orders")["statusCode"] == 401
    assert _post("/hooks/webhook/orders",
                 headers={"authorization": "Bearer wrong"})["statusCode"] == 401
    assert sent == []


def test_webhook_hook_unknown_or_disabled_is_404(monkeypatch):
    monkeypatch.setenv("HOOK_TRIGGERS_TABLE", "hooks")
    monkeypatch.setattr("src.dapier.triggers.hook_triggers.get_table", lambda *a, **k: _hook_stub())
    assert _post("/hooks/webhook/stranger",
                 headers={"authorization": "Bearer tok-123"})["statusCode"] == 404
    monkeypatch.setattr("src.dapier.triggers.hook_triggers.get_table",
                        lambda *a, **k: _hook_stub(enabled=False))
    assert _post("/hooks/webhook/orders",
                 headers={"authorization": "Bearer tok-123"})["statusCode"] == 404


def test_webhook_hook_wraps_non_json_bodies(monkeypatch):
    sent = []
    monkeypatch.setenv("EVENT_QUEUE_URL", "https://sqs.example.test/events")
    monkeypatch.setenv("HOOK_TRIGGERS_TABLE", "hooks")
    monkeypatch.setattr(ingress.queue, "send_message", lambda **kwargs: sent.append(kwargs))
    monkeypatch.setattr("src.dapier.triggers.hook_triggers.get_table", lambda *a, **k: _hook_stub())

    response = _post("/hooks/webhook/orders", body=b"plain text",
                     headers={"authorization": "Bearer tok-123",
                              "content-type": "text/plain"})
    assert response["statusCode"] == 202
    assert json.loads(sent[0]["MessageBody"])["data"]["body"] == {"raw": "plain text"}


def test_telegram_hook_verifies_secret_header_and_extracts_fields(monkeypatch):
    sent = []
    monkeypatch.setenv("EVENT_QUEUE_URL", "https://sqs.example.test/events")
    monkeypatch.setenv("HOOK_TRIGGERS_TABLE", "hooks")
    monkeypatch.setattr(ingress.queue, "send_message", lambda **kwargs: sent.append(kwargs))
    monkeypatch.setattr(
        "src.dapier.triggers.hook_triggers.get_table",
        lambda *a, **k: _hook_stub(hook_id="bot-inbox", kind="telegram", token="tg-secret"))

    update = {"update_id": 91, "message": {
        "message_id": 7, "text": "hello dapier",
        "chat": {"id": 555, "type": "private"},
        "from": {"id": 9, "first_name": "Ada"},
    }}
    response = _post("/hooks/telegram/bot-inbox",
                     body=json.dumps(update).encode(),
                     headers={"x-telegram-bot-api-secret-token": "tg-secret"})

    assert response["statusCode"] == 200
    envelope = json.loads(sent[0]["MessageBody"])
    assert envelope["connector"] == "telegram"
    assert envelope["event"] == "message.received"
    data = envelope["data"]
    assert data["hook"] == "bot-inbox"
    assert data["text"] == "hello dapier"
    assert data["chat_id"] == 555
    assert data["update_id"] == 91
    assert data["update"]["message"]["from"]["first_name"] == "Ada"


def test_telegram_hook_rejects_wrong_or_missing_secret(monkeypatch):
    sent = []
    monkeypatch.setenv("EVENT_QUEUE_URL", "https://sqs.example.test/events")
    monkeypatch.setenv("HOOK_TRIGGERS_TABLE", "hooks")
    monkeypatch.setattr(ingress.queue, "send_message", lambda **kwargs: sent.append(kwargs))
    monkeypatch.setattr(
        "src.dapier.triggers.hook_triggers.get_table",
        lambda *a, **k: _hook_stub(hook_id="bot-inbox", kind="telegram", token="tg-secret"))

    assert _post("/hooks/telegram/bot-inbox")["statusCode"] == 401
    assert _post("/hooks/telegram/bot-inbox",
                 headers={"x-telegram-bot-api-secret-token": "nope"})["statusCode"] == 401
    assert sent == []


# --- Webhook and Telegram trigger hooks -------------------------------------

def _hook_stub(hook_id="orders", kind="webhook", token="tok-123", enabled=True):
    item = {"hook_id": hook_id, "kind": kind, "url": f"u-{hook_id}", "token": token,
            "actions": [], "enabled": enabled}
    return type("T", (), {
        "scan": lambda self, Limit=200: {"Items": [dict(item)]},
        "get_item": lambda self, Key: {"Item": dict(item)} if Key["hook_id"] == hook_id else {},
        "put_item": lambda self, Item: None,
        "delete_item": lambda self, Key: None,
    })()


def _post(path, body=b'{"hello":"world"}', headers=None, query=None):
    return ingress.handler({
        "requestContext": {"http": {"method": "POST", "path": path}},
        "headers": headers or {},
        "queryStringParameters": query,
        "body": body.decode(),
    }, None)


def test_webhook_hook_accepts_bearer_token_and_publishes(monkeypatch):
    sent = []
    monkeypatch.setenv("EVENT_QUEUE_URL", "https://sqs.example.test/events")
    monkeypatch.setenv("HOOK_TRIGGERS_TABLE", "hooks")
    monkeypatch.setattr(ingress.queue, "send_message", lambda **kwargs: sent.append(kwargs))
    monkeypatch.setattr("src.dapier.triggers.hook_triggers.get_table", lambda *a, **k: _hook_stub())

    response = _post("/hooks/webhook/orders",
                     headers={"authorization": "Bearer tok-123",
                              "content-type": "application/json"},
                     query={"src": "ci"})

    assert response["statusCode"] == 202
    envelope = json.loads(sent[0]["MessageBody"])
    assert envelope["connector"] == "webhook"
    assert envelope["event"] == "request.received"
    assert envelope["source"] == "orders"
    assert envelope["data"]["hook"] == "orders"
    assert envelope["data"]["body"] == {"hello": "world"}
    assert envelope["data"]["query"] == {"src": "ci"}


def test_webhook_hook_rejects_bad_or_missing_tokens(monkeypatch):
    sent = []
    monkeypatch.setenv("EVENT_QUEUE_URL", "https://sqs.example.test/events")
    monkeypatch.setenv("HOOK_TRIGGERS_TABLE", "hooks")
    monkeypatch.setattr(ingress.queue, "send_message", lambda **kwargs: sent.append(kwargs))
    monkeypatch.setattr("src.dapier.triggers.hook_triggers.get_table", lambda *a, **k: _hook_stub())

    assert _post("/hooks/webhook/orders")["statusCode"] == 401
    assert _post("/hooks/webhook/orders",
                 headers={"authorization": "Bearer wrong"})["statusCode"] == 401
    assert sent == []


def test_webhook_hook_unknown_or_disabled_is_404(monkeypatch):
    monkeypatch.setenv("HOOK_TRIGGERS_TABLE", "hooks")
    monkeypatch.setattr("src.dapier.triggers.hook_triggers.get_table", lambda *a, **k: _hook_stub())
    assert _post("/hooks/webhook/stranger",
                 headers={"authorization": "Bearer tok-123"})["statusCode"] == 404
    monkeypatch.setattr("src.dapier.triggers.hook_triggers.get_table",
                        lambda *a, **k: _hook_stub(enabled=False))
    assert _post("/hooks/webhook/orders",
                 headers={"authorization": "Bearer tok-123"})["statusCode"] == 404


def test_webhook_hook_wraps_non_json_bodies(monkeypatch):
    sent = []
    monkeypatch.setenv("EVENT_QUEUE_URL", "https://sqs.example.test/events")
    monkeypatch.setenv("HOOK_TRIGGERS_TABLE", "hooks")
    monkeypatch.setattr(ingress.queue, "send_message", lambda **kwargs: sent.append(kwargs))
    monkeypatch.setattr("src.dapier.triggers.hook_triggers.get_table", lambda *a, **k: _hook_stub())

    response = _post("/hooks/webhook/orders", body=b"plain text",
                     headers={"authorization": "Bearer tok-123",
                              "content-type": "text/plain"})
    assert response["statusCode"] == 202
    assert json.loads(sent[0]["MessageBody"])["data"]["body"] == {"raw": "plain text"}


def test_telegram_hook_verifies_secret_header_and_extracts_fields(monkeypatch):
    sent = []
    monkeypatch.setenv("EVENT_QUEUE_URL", "https://sqs.example.test/events")
    monkeypatch.setenv("HOOK_TRIGGERS_TABLE", "hooks")
    monkeypatch.setattr(ingress.queue, "send_message", lambda **kwargs: sent.append(kwargs))
    monkeypatch.setattr(
        "src.dapier.triggers.hook_triggers.get_table",
        lambda *a, **k: _hook_stub(hook_id="bot-inbox", kind="telegram", token="tg-secret"))

    update = {"update_id": 91, "message": {
        "message_id": 7, "text": "hello dapier",
        "chat": {"id": 555, "type": "private"},
        "from": {"id": 9, "first_name": "Ada"},
    }}
    response = _post("/hooks/telegram/bot-inbox",
                     body=json.dumps(update).encode(),
                     headers={"x-telegram-bot-api-secret-token": "tg-secret"})

    assert response["statusCode"] == 200
    envelope = json.loads(sent[0]["MessageBody"])
    assert envelope["connector"] == "telegram"
    assert envelope["event"] == "message.received"
    data = envelope["data"]
    assert data["hook"] == "bot-inbox"
    assert data["text"] == "hello dapier"
    assert data["chat_id"] == 555
    assert data["update_id"] == 91
    assert data["update"]["message"]["from"]["first_name"] == "Ada"


def test_telegram_hook_rejects_wrong_or_missing_secret(monkeypatch):
    sent = []
    monkeypatch.setenv("EVENT_QUEUE_URL", "https://sqs.example.test/events")
    monkeypatch.setenv("HOOK_TRIGGERS_TABLE", "hooks")
    monkeypatch.setattr(ingress.queue, "send_message", lambda **kwargs: sent.append(kwargs))
    monkeypatch.setattr(
        "src.dapier.triggers.hook_triggers.get_table",
        lambda *a, **k: _hook_stub(hook_id="bot-inbox", kind="telegram", token="tg-secret"))

    assert _post("/hooks/telegram/bot-inbox")["statusCode"] == 401
    assert _post("/hooks/telegram/bot-inbox",
                 headers={"x-telegram-bot-api-secret-token": "nope"})["statusCode"] == 401
    assert sent == []
