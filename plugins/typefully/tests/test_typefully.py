import io
import json
import sys
import pytest
from plugins.typefully import plugin
from src.dapier.connections import credentials
from dapier_cli.commands.credentials import credentials_set
from dapier_cli import api


def test_save_is_write_only_and_rejects_bad_keys(monkeypatch):
    saved = []
    monkeypatch.setattr(credentials, "put_credential", lambda *args, **kwargs: saved.append((args, kwargs)))
    key = "test-key-" + "x" * 40
    status, body = credentials.api_save_credential("typefully", {"api_key": key})
    assert status == 200 and body == {"provider": "typefully", "configured": True}
    assert saved[0][0] == ("typefully", {"api_key": key})
    for invalid in [None, {}, "short", "x" * 25 + "\nheader: injected", "x" * 2049]:
        assert credentials.api_save_credential("typefully", {"api_key": invalid})[0] == 400
    assert len(saved) == 1


def test_cli_uses_shared_api_without_printing_secret(monkeypatch, capsys):
    key = "test-key-" + "z" * 40
    monkeypatch.setattr(sys, "stdin", io.StringIO(key))
    calls = []
    monkeypatch.setattr(api, "call", lambda *args, **kwargs: calls.append(args) or {"provider": "typefully"})
    assert credentials_set("https://example.test", "typefully", "-") == 0
    assert calls[0][1:] == ("PUT", "/api/agent/credentials/typefully", {"api_key": key})
    assert key not in capsys.readouterr().out


def test_request_scopes_bearer_to_fixed_provider(monkeypatch):
    monkeypatch.setattr(credentials, "get_credential", lambda _: {"api_key": "secret"})
    calls = []
    def transport(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return 200, b'{"id": 7}'
    assert plugin.request("/me", transport=transport) == {"id": 7}
    assert calls[0][0:2] == ("GET", "https://api.typefully.com/v2/me")
    assert calls[0][2]["headers"]["authorization"] == "Bearer secret"


def test_discovery_paginates_without_trusting_next_url(monkeypatch):
    seen = []
    def request(path, connection):
        seen.append(path)
        return {"results": [{"id": len(seen), "name": "Brand"}], "next": "https://evil.test" if len(seen) == 1 else None}
    monkeypatch.setattr(plugin, "request", request)
    assert [i["id"] for i in plugin.social_sets({}, {})] == ["1", "2"]
    assert seen == ["/social-sets?limit=50&offset=0", "/social-sets?limit=50&offset=50"]


def test_health_never_echoes_transport_secrets(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("credential-value")
    monkeypatch.setattr(plugin, "request", fail)
    assert plugin.health({})["ok"] is False
    assert "credential-value" not in str(plugin.health({}))


def test_discovery_and_console_share_registered_provider():
    from src.dapier.api.discovery import _pseudo_connection
    assert _pseudo_connection("typefully")["provider"] == "typefully"


def test_list_drafts_maps_items_and_defaults_social_set(monkeypatch):
    seen = []
    def request(path, connection, **kwargs):
        seen.append(path)
        return {"results": [{"id": 9, "preview": "Hello", "status": "draft"}], "next": None}
    monkeypatch.setattr(plugin, "request", request)
    items = plugin.list_drafts({}, {})
    assert items[0]["id"] == "9" and items[0]["name"] == "Hello"
    assert "/social-sets/182343/drafts?" in seen[0]


def test_create_unscheduled_rejects_publish(monkeypatch):
    monkeypatch.setattr(plugin, "request", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no call")))
    try:
        plugin.create_unscheduled_draft({}, {"body": {"platforms": {"x": {"enabled": True, "posts": [{"text": "hi"}]}}, "publish_at": "now"}})
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "publish_at" in str(exc)


def test_create_unscheduled_posts(monkeypatch):
    calls = []
    def request(path, connection, **kwargs):
        calls.append((path, kwargs))
        return {"id": 1, "status": "draft"}
    monkeypatch.setattr(plugin, "request", request)
    body = {"platforms": {"x": {"enabled": True, "posts": [{"text": "hi"}]}}}
    assert plugin.create_unscheduled_draft({}, {"body": body})["id"] == 1
    assert calls[0][0] == "/social-sets/182343/drafts"
    assert calls[0][1]["method"] == "POST"


PLATFORMS = {"x": {"enabled": True, "posts": [{"text": "hi"}]}}


def test_create_scheduled_requires_exactly_one_schedule_field(monkeypatch):
    monkeypatch.setattr(plugin, "request", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no call")))
    with pytest.raises(ValueError):
        plugin.create_scheduled_draft({}, {"body": {"platforms": PLATFORMS}})
    with pytest.raises(ValueError) as exc:
        plugin.create_scheduled_draft({}, {"body": {"platforms": PLATFORMS,
            "publish_at": "2030-01-01T10:00:00+00:00", "plan_at": "2030-01-02T10:00:00+00:00"}})
    assert "mutually exclusive" in str(exc.value)


def test_create_scheduled_validates_values(monkeypatch):
    monkeypatch.setattr(plugin, "request", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no call")))
    for bad in ["now", "", "tomorrow", "2030-01-01T10:00:00"]:
        with pytest.raises(ValueError):
            plugin.create_scheduled_draft({}, {"body": {"platforms": PLATFORMS, "plan_at": bad}})
    with pytest.raises(ValueError):
        plugin.create_scheduled_draft({}, {"body": {"platforms": PLATFORMS, "publish_at": "2030-01-01T10:00:00"}})


def test_create_scheduled_posts_with_schedule(monkeypatch):
    calls = []
    def request(path, connection, **kwargs):
        calls.append((path, kwargs))
        return {"id": 3, "status": "scheduled"}
    monkeypatch.setattr(plugin, "request", request)
    body = {"platforms": PLATFORMS, "publish_at": "2030-01-01T10:00:00+00:00"}
    assert plugin.create_scheduled_draft({}, {"body": body})["status"] == "scheduled"
    assert calls[0][0] == "/social-sets/182343/drafts"
    assert calls[0][1]["method"] == "POST"
    assert calls[0][1]["body"]["publish_at"] == "2030-01-01T10:00:00+00:00"


def test_patch_draft_uses_patch_and_rejects_both_schedule_fields(monkeypatch):
    calls = []
    def request(path, connection, **kwargs):
        calls.append((path, kwargs))
        return {"id": 9, "status": "draft"}
    monkeypatch.setattr(plugin, "request", request)
    body = {"platforms": PLATFORMS, "publish_at": "next-free-slot"}
    assert plugin.patch_draft({}, {"draft_id": 9, "body": body})["id"] == 9
    assert calls[0][0] == "/social-sets/182343/drafts/9"
    assert calls[0][1]["method"] == "PATCH"
    with pytest.raises(ValueError):
        plugin.patch_draft({}, {"draft_id": 9, "body": {"publish_at": "now", "plan_at": "now"}})
    with pytest.raises(ValueError):
        plugin.patch_draft({}, {"draft_id": 9, "body": {}})
    with pytest.raises(ValueError):
        plugin.patch_draft({}, {"draft_id": "x", "body": body})


def _upload_stubs(monkeypatch, statuses, put_status=200):
    seen = []
    polls = []
    def request(path, connection, **kwargs):
        seen.append(path)
        if path.endswith("/media/upload"):
            return {"media_id": "mid-1", "upload_url": "https://signed.example.test/put?sig=SECRET"}
        polls.append(path)
        return {"media_id": "mid-1", "status": statuses[min(len(polls) - 1, len(statuses) - 1)]}
    puts = []
    monkeypatch.setattr(plugin, "request", request)
    monkeypatch.setattr(plugin, "_raw_put", lambda url, content: puts.append((url, content)) or put_status)
    return seen, puts


def test_upload_media_full_flow(monkeypatch):
    seen, puts = _upload_stubs(monkeypatch, ["processing", "ready"])
    result = plugin.upload_media({}, {"file_name": "pic.png", "content": b"\x89PNG-data",
                                       "alt_text": "a plot"}, sleep=lambda s: None)
    assert result == {"media_id": "mid-1", "file_name": "pic.png", "status": "ready"}
    assert seen[0] == "/social-sets/182343/media/upload"
    assert puts[0] == ("https://signed.example.test/put?sig=SECRET", b"\x89PNG-data")
    assert "SECRET" not in str(result)


def test_upload_media_reports_failed_with_reason(monkeypatch):
    _upload_stubs(monkeypatch, ["failed"])
    monkeypatch.setattr(plugin, "request", lambda path, connection, **k:
        {"media_id": "mid-1", "upload_url": "https://signed.example.test/u"}
        if path.endswith("/media/upload")
        else {"media_id": "mid-1", "status": "failed", "error_reason": "bad pixels"})
    result = plugin.upload_media({}, {"file_name": "a.jpg", "content": b"bytes"}, sleep=lambda s: None)
    assert result["status"] == "failed" and result["error_reason"] == "bad pixels"


def test_upload_media_rejects_bad_inputs(monkeypatch):
    monkeypatch.setattr(plugin, "request", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no call")))
    monkeypatch.setattr(plugin, "_raw_put", lambda *a: (_ for _ in ()).throw(AssertionError("no put")))
    for params in [{"file_name": "pic.exe", "content": b"x"},
                   {"file_name": "noext", "content": b"x"},
                   {"file_name": "pic.png", "content": b""},
                   {"file_name": "pic.png", "content": "not-bytes"},
                   {"file_name": "../pic.png", "content": b"x"}]:
        with pytest.raises(ValueError):
            plugin.upload_media({}, params, sleep=lambda s: None)


def test_upload_media_put_failure_stays_generic(monkeypatch):
    def request(path, connection, **kwargs):
        if path.endswith("/media/upload"):
            return {"media_id": "mid-1", "upload_url": "https://signed.example.test/u?sig=TOPSECRET"}
        return {"status": "ready"}
    monkeypatch.setattr(plugin, "request", request)
    def fail(url, content):
        raise ValueError("media upload failed")
    monkeypatch.setattr(plugin, "_raw_put", fail)
    with pytest.raises(ValueError) as exc:
        plugin.upload_media({}, {"file_name": "pic.png", "content": b"x"}, sleep=lambda s: None)
    assert "TOPSECRET" not in str(exc.value) and "signed.example.test" not in str(exc.value)


def test_raw_put_sends_no_credential_headers(monkeypatch):
    """Regression: urllib Request(data=...) re-adds Content-Type on send.

    _raw_put must use http.client with only Content-Length so the Typefully
    S3 signature (calculated without Content-Type) still matches.
    """
    captured = {}

    class FakeResponse:
        status = 200
        def read(self):
            return b""

    class FakeConn:
        def __init__(self, host, port=None, timeout=60):
            captured["host"] = host
            captured["port"] = port
            captured["timeout"] = timeout
        def request(self, method, path, body=None, headers=None):
            captured["method"] = method
            captured["path"] = path
            captured["body"] = body
            captured["headers"] = dict(headers or {})
        def getresponse(self):
            return FakeResponse()
        def close(self):
            captured["closed"] = True

    import http.client as http_client_mod
    monkeypatch.setattr(http_client_mod, "HTTPSConnection", FakeConn)
    assert plugin._raw_put("https://signed.example.test/upload?sig=TOPSECRET", b"bytes") == 200
    assert captured["method"] == "PUT" and captured["body"] == b"bytes"
    assert captured["path"].startswith("/upload?")
    assert "TOPSECRET" in captured["path"]  # query preserved
    assert captured["headers"] == {"Content-Length": "5"}
    assert "Content-Type" not in captured["headers"]
    assert "Authorization" not in captured["headers"]
    assert captured.get("closed") is True



def test_media_status_validates_id(monkeypatch):
    monkeypatch.setattr(plugin, "request", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no call")))
    with pytest.raises(ValueError):
        plugin.media_status({}, {"media_id": "../../etc"})
    calls = []
    monkeypatch.setattr(plugin, "request", lambda p, c, **k: calls.append(p) or {"status": "ready"})
    assert plugin.media_status({}, {"media_id": "550e8400-e29b-41d4-a716-446655440000"})["status"] == "ready"
    assert calls[0] == "/social-sets/182343/media/550e8400-e29b-41d4-a716-446655440000"


def test_new_paths_never_echo_credential(monkeypatch):
    secret = "Bearer-" + "k" * 48
    monkeypatch.setattr(credentials, "get_credential", lambda _: {"api_key": secret})
    def fail(*args, **kwargs):
        raise RuntimeError("boom-" + secret)
    monkeypatch.setattr(plugin.base, "_default_transport", fail)
    for call in [lambda: plugin.upload_media({}, {"file_name": "a.png", "content": b"x"}),
                 lambda: plugin.media_status({}, {"media_id": "m-1"}),
                 lambda: plugin.patch_draft({}, {"draft_id": 1, "body": {"publish_at": "now"}}),
                 lambda: plugin.create_scheduled_draft(
                     {}, {"body": {"platforms": PLATFORMS, "publish_at": "now"}})]:
        try:
            call()
            assert False, "expected ValueError"
        except ValueError as exc:
            assert secret not in str(exc)


def _agent_event(method, path, body=None, query=None):
    return {"requestContext": {"http": {"method": method, "path": path}},
            "queryStringParameters": query, "body": body}


def _operator(monkeypatch):
    from src.dapier.api import agent as agent_api
    monkeypatch.setattr(agent_api, "require_operator", lambda event, action: ("tester", None))
    monkeypatch.setattr(agent_api, "audit", type("A", (), {"emit": staticmethod(lambda *a, **k: None)}))


def test_agent_routes_cover_media_and_patch(monkeypatch):
    from src.dapier.api.agent import typefully as agent_typefully
    _operator(monkeypatch)
    calls = []
    def upload(connection, params):
        calls.append(("upload", params))
        return {"media_id": "m-9", "file_name": "a.png", "status": "ready",
                "upload_url": "https://signed.example.test/u?sig=HIDDEN"}
    import base64 as _b64
    monkeypatch.setattr(agent_typefully._plugin(), "upload_media", upload)
    event = _agent_event("POST", "/api/agent/typefully/social-sets/182343/media",
                         json.dumps({"file_name": "a.png",
                                     "content_base64": _b64.b64encode(b"img").decode()}))
    response = agent_typefully.route_typefully(event, "POST", "/api/agent/typefully/social-sets/182343/media")
    assert response["statusCode"] == 201
    payload = json.loads(response["body"])
    assert payload["media_id"] == "m-9" and payload["status"] == "ready"
    assert "upload_url" not in payload and "HIDDEN" not in response["body"]
    assert calls[0][1]["content"] == b"img"

    monkeypatch.setattr(agent_typefully._plugin(), "media_status", lambda c, p: {"status": "processing"})
    event = _agent_event("GET", "/api/agent/typefully/social-sets/182343/media/m-9")
    response = agent_typefully.route_typefully(
        event, "GET", "/api/agent/typefully/social-sets/182343/media/m-9")
    assert response["statusCode"] == 200

    patched = []
    monkeypatch.setattr(agent_typefully._plugin(), "patch_draft",
                        lambda c, p: patched.append(p) or {"id": 5})
    event = _agent_event("PATCH", "/api/agent/typefully/social-sets/182343/drafts/5",
                         json.dumps({"publish_at": "now"}))
    response = agent_typefully.route_typefully(
        event, "PATCH", "/api/agent/typefully/social-sets/182343/drafts/5")
    assert response["statusCode"] == 200 and patched[0]["draft_id"] == "5"

    scheduled = []
    monkeypatch.setattr(agent_typefully._plugin(), "create_scheduled_draft",
                        lambda c, p: scheduled.append(p) or {"id": 6})
    monkeypatch.setattr(agent_typefully._plugin(), "create_unscheduled_draft",
                        lambda c, p: (_ for _ in ()).throw(AssertionError("wrong path")))
    event = _agent_event("POST", "/api/agent/typefully/social-sets/182343/drafts",
                         json.dumps({"platforms": PLATFORMS, "publish_at": "now"}),
                         query={"allow_schedule": "true"})
    response = agent_typefully.route_typefully(
        event, "POST", "/api/agent/typefully/social-sets/182343/drafts")
    assert response["statusCode"] == 201 and scheduled


def test_agent_upload_rejects_bad_base64(monkeypatch):
    from src.dapier.api.agent import typefully as agent_typefully
    _operator(monkeypatch)
    event = _agent_event("POST", "/api/agent/typefully/social-sets/182343/media",
                         json.dumps({"file_name": "a.png", "content_base64": "!!!"}))
    response = agent_typefully.route_typefully(
        event, "POST", "/api/agent/typefully/social-sets/182343/media")
    assert response["statusCode"] == 400


def test_cli_upload_media_prints_ids_only(monkeypatch, capsys, tmp_path):
    from dapier_cli.commands import typefully as cli_typefully
    target = tmp_path / "pic.png"
    target.write_bytes(b"\x89PNG-bytes")
    calls = []
    def fake_call(api_url, method, path, body=None, debug=False):
        calls.append((method, path, body))
        return {"media_id": "m-1", "file_name": "pic.png", "status": "ready",
                "upload_url": "https://signed.example.test/u?sig=HIDDEN"}
    monkeypatch.setattr(cli_typefully.api, "call", fake_call)
    assert cli_typefully.typefully_upload_media("https://api.test", str(target)) == 0
    out = capsys.readouterr().out
    assert "m-1" in out and "HIDDEN" not in out and "signed.example.test" not in out
    import base64 as _b64
    assert "PNG-bytes" not in out and _b64.b64encode(b"\x89PNG-bytes").decode() not in out
    assert calls[0][0] == "POST" and calls[0][1].endswith("/social-sets/182343/media")
    assert calls[0][2]["file_name"] == "pic.png"


def test_cli_create_draft_schedule_opt_in(monkeypatch, capsys, tmp_path):
    from dapier_cli.commands import typefully as cli_typefully
    draft = tmp_path / "draft.json"
    draft.write_text(json.dumps({"platforms": PLATFORMS, "publish_at": "now"}), encoding="utf-8")
    monkeypatch.setattr(cli_typefully.api, "call", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("must reject before HTTP")))
    assert cli_typefully.typefully_create_draft("https://api.test", str(draft)) == 2
    assert "allow-schedule" in capsys.readouterr().err
    calls = []
    monkeypatch.setattr(cli_typefully.api, "call",
                        lambda *a, **k: calls.append((a[1], a[2])) or {"id": 1})
    assert cli_typefully.typefully_create_draft(
        "https://api.test", str(draft), allow_schedule=True) == 0
    assert calls[0][1].endswith("/drafts?allow_schedule=true")


def test_cli_patch_draft_and_media_status(monkeypatch, capsys, tmp_path):
    from dapier_cli.commands import typefully as cli_typefully
    patch = tmp_path / "patch.json"
    patch.write_text(json.dumps({"publish_at": "now"}), encoding="utf-8")
    calls = []
    monkeypatch.setattr(cli_typefully.api, "call",
                        lambda *a, **k: calls.append((a[1], a[2], k.get("body"))) or {"id": 5})
    assert cli_typefully.typefully_patch_draft("https://api.test", 5, str(patch)) == 0
    assert calls[0][0] == "PATCH" and calls[0][1].endswith("/drafts/5")
    assert calls[0][2] == {"publish_at": "now"}
    assert cli_typefully.typefully_media_status("https://api.test", "m-1") == 0
    assert calls[1][0] == "GET" and calls[1][1].endswith("/media/m-1")
