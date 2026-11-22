"""Unit tests for `dapier slack post --image` and `dapier slack attach-image`:
the presigned files.uploadV2 flow, thread-reply attach, --replace, and loud
errors. Slack is mocked at slack_discovery._request / _slack_call."""

import json
from urllib.parse import parse_qs, urlsplit

import pytest

from src.dapier.api.agent import slack as slack_api
from src.dapier.api.agent import slack_ops

TOKEN = "xoxb-" + "c" * 40
UPLOAD_URL = "https://files.slack.com/upload/v1/presigned-abc"


class FakeSlack:
    """Routes slack_discovery._request by Slack method name."""

    def __init__(self, handlers, bot_id="B_BOT", user_id="U_BOT"):
        self.handlers = handlers
        self.calls = []
        self.bot_id = bot_id
        self.user_id = user_id

    def request(self, method, url, token, payload, transport=None, form=False):
        assert token == TOKEN
        parsed = urlsplit(url)
        name = parsed.path.rsplit("/", 1)[-1]
        params = dict(payload or {})
        if parsed.query:
            params.update({k: v[0] for k, v in parse_qs(parsed.query).items()})
        self.calls.append((name, method, params))
        handler = self.handlers.get(name)
        if handler is None:
            raise AssertionError(f"unexpected Slack call {name}")
        return handler(params) if callable(handler) else handler

    def slack_call(self, method, token, payload, transport=None):
        if method == "auth.test":
            return {"ok": True, "bot_id": self.bot_id, "user_id": self.user_id}
        raise AssertionError(method)

    def names(self):
        return [c[0] for c in self.calls]


@pytest.fixture
def slack(monkeypatch):
    monkeypatch.setattr(slack_ops.discovery_api, "_load",
                        lambda cid, table: ({"connection_id": cid, "provider": "slack"}, None))
    monkeypatch.setattr(slack_ops.tokens, "get_access_token",
                        lambda connection, **kw: (TOKEN, {}))

    def install(handlers, **kw):
        fake = FakeSlack(handlers, **kw)
        monkeypatch.setattr(slack_ops.slack_discovery, "_request", fake.request)
        monkeypatch.setattr(slack_ops.slack_discovery, "_slack_call", fake.slack_call)
        return fake

    return install


def _no_sleep(_delay):
    return None


def _complete_ok(params):
    files = json.loads(params["files"])
    return {"ok": True, "files": [{"id": files[0]["id"], "name": "pic.png",
                                   "title": files[0]["title"],
                                   "permalink": "https://x.slack.com/files/F1"}]}


# --- upload URL -------------------------------------------------------------


def test_image_upload_url_mints_presigned_target(slack):
    fake = slack({"files.getUploadURLExternal": {"ok": True, "upload_url": UPLOAD_URL,
                                                 "file_id": "F0IMG"}})
    res = slack_ops.image_upload_url("shots/pic.png", 1234)
    assert res["file_id"] == "F0IMG" and res["upload_url"] == UPLOAD_URL
    assert res["connection"] == "slack"
    name, method, params = fake.calls[0]
    assert method == "GET"
    assert params == {"filename": "pic.png", "length": "1234"}


def test_image_upload_url_missing_scope_names_needed_scope(slack):
    slack({"files.getUploadURLExternal": {"ok": False, "error": "missing_scope",
                                          "needed": "files:write",
                                          "provided": "chat:write,channels:read"}})
    with pytest.raises(slack_ops.SlackOpsError) as exc:
        slack_ops.image_upload_url("pic.png", 10)
    assert exc.value.status == 403
    assert "needed scope: files:write" in str(exc.value)
    assert "reinstall" in str(exc.value)


@pytest.mark.parametrize("name,length,needle", [
    ("notes.txt", 10, "not an image"),
    ("pic.png", 0, "empty"),
    ("pic.png", slack_ops.SLACK_MAX_FILE_BYTES + 1, "too big"),
])
def test_image_upload_url_rejects_bad_input_before_slack(slack, name, length, needle):
    fake = slack({})
    with pytest.raises(slack_ops.SlackOpsError, match=needle):
        slack_ops.image_upload_url(name, length)
    assert fake.calls == []


# --- post with image ----------------------------------------------------------


def test_post_image_completes_with_comment_and_finds_ts(slack):
    fake = slack({
        "files.completeUploadExternal": _complete_ok,
        "conversations.history": {"ok": True, "messages": [
            {"ts": "1800000000.000200", "files": [{"id": "F0IMG"}]}]},
    })
    res = slack_ops.post_image("C0TEST", "Hello *world*", "F0IMG", title="pic.png",
                               sleep=_no_sleep)
    assert res["ts"] == "1800000000.000200"
    assert res["channel"] == "C0TEST"
    assert res["file"]["id"] == "F0IMG"
    assert "ts_error" not in res
    _, method, params = fake.calls[0]
    assert method == "POST"
    assert params["initial_comment"] == "Hello *world*"
    assert params["channel_id"] == "C0TEST"
    assert "thread_ts" not in params
    assert json.loads(params["files"]) == [{"id": "F0IMG", "title": "pic.png"}]


def test_post_image_in_thread_looks_up_replies(slack):
    fake = slack({
        "files.completeUploadExternal": _complete_ok,
        "conversations.replies": {"ok": True, "messages": [
            {"ts": "1800000000.000100"},
            {"ts": "1800000000.000300", "files": [{"id": "F0IMG"}]}]},
    })
    res = slack_ops.post_image("C0TEST", "in thread", "F0IMG",
                               thread_ts="1800000000.000100", sleep=_no_sleep)
    assert res["ts"] == "1800000000.000300"
    assert fake.calls[0][2]["thread_ts"] == "1800000000.000100"
    assert fake.calls[1][2]["ts"] == "1800000000.000100"


@pytest.mark.parametrize("error,needle", [
    ("not_in_channel", "invite the bot"),
    ("channel_not_found", "channel not found"),
    ("file_too_large", "larger than Slack accepts"),
])
def test_post_image_fails_loudly(slack, error, needle):
    slack({"files.completeUploadExternal": {"ok": False, "error": error}})
    with pytest.raises(slack_ops.SlackOpsError) as exc:
        slack_ops.post_image("C0TEST", "x", "F0IMG", sleep=_no_sleep)
    assert error in str(exc.value) and needle in str(exc.value)


def test_post_image_reports_missing_ts_explicitly(slack):
    slack({"files.completeUploadExternal": _complete_ok,
           "conversations.history": {"ok": True, "messages": []}})
    res = slack_ops.post_image("C0TEST", "x", "F0IMG", sleep=_no_sleep)
    assert res["ts"] is None
    assert "did not appear" in res["ts_error"]


def test_post_image_rejects_bad_ids(slack):
    slack({})
    with pytest.raises(slack_ops.SlackOpsError, match="file_id"):
        slack_ops.post_image("C0TEST", "x", "not-a-file")
    with pytest.raises(slack_ops.SlackOpsError, match="thread_ts"):
        slack_ops.post_image("C0TEST", "x", "F0IMG", thread_ts="abc")


# --- attach-image -------------------------------------------------------------


def _history_with(message):
    def handler(params):
        if params.get("latest") == params.get("oldest"):
            return {"ok": True, "messages": [message] if message else []}
        return {"ok": True, "messages": [
            {"ts": "1800000001.000100", "files": [{"id": "F0IMG"}]}]}
    return handler


def test_attach_image_default_posts_thread_reply(slack):
    original = {"ts": "1800000000.000100", "text": "Original", "user": "U_SOMEONE"}
    fake = slack({
        "conversations.history": _history_with(original),
        "files.completeUploadExternal": _complete_ok,
        "conversations.replies": {"ok": True, "messages": [
            original, {"ts": "1800000002.000100", "files": [{"id": "F0IMG"}]}]},
    })
    res = slack_ops.attach_image("C0TEST", "1800000000.000100", "F0IMG", sleep=_no_sleep)
    assert res["mode"] == "thread_reply"
    assert res["ts"] == "1800000002.000100"
    assert res["thread_ts"] == "1800000000.000100"
    completion = [c for c in fake.calls if c[0] == "files.completeUploadExternal"][0][2]
    assert completion["thread_ts"] == "1800000000.000100"
    assert "initial_comment" not in completion
    assert "chat.delete" not in fake.names()


def test_attach_image_replace_reposts_then_deletes(slack):
    original = {"ts": "1800000000.000100", "text": "Announcement text", "bot_id": "B_BOT"}
    fake = slack({
        "conversations.history": _history_with(original),
        "files.completeUploadExternal": _complete_ok,
        "chat.delete": {"ok": True},
    })
    res = slack_ops.attach_image("C0TEST", "1800000000.000100", "F0IMG", replace=True,
                                 sleep=_no_sleep)
    assert res["mode"] == "replace"
    assert res["ts"] == "1800000001.000100"
    assert res["deleted_ts"] == "1800000000.000100"
    names = fake.names()
    assert names.index("files.completeUploadExternal") < names.index("chat.delete")
    completion = [c for c in fake.calls if c[0] == "files.completeUploadExternal"][0][2]
    assert completion["initial_comment"] == "Announcement text"
    delete = [c for c in fake.calls if c[0] == "chat.delete"][0][2]
    assert delete == {"channel": "C0TEST", "ts": "1800000000.000100"}


def test_attach_image_replace_refuses_foreign_message(slack):
    original = {"ts": "1800000000.000100", "text": "Not ours", "user": "U_HUMAN"}
    fake = slack({"conversations.history": _history_with(original)})
    with pytest.raises(slack_ops.SlackOpsError, match="not posted by this connection") as exc:
        slack_ops.attach_image("C0TEST", "1800000000.000100", "F0IMG", replace=True,
                               sleep=_no_sleep)
    assert exc.value.status == 403
    assert "files.completeUploadExternal" not in fake.names()


def test_attach_image_replace_refuses_message_with_replies(slack):
    original = {"ts": "1800000000.000100", "text": "Has thread", "bot_id": "B_BOT",
                "thread_ts": "1800000000.000100", "reply_count": 3}
    fake = slack({"conversations.history": _history_with(original)})
    with pytest.raises(slack_ops.SlackOpsError, match="thread replies"):
        slack_ops.attach_image("C0TEST", "1800000000.000100", "F0IMG", replace=True,
                               sleep=_no_sleep)
    assert "chat.delete" not in fake.names()


def test_attach_image_replace_keeps_old_when_new_ts_unconfirmed(slack):
    original = {"ts": "1800000000.000100", "text": "Text", "bot_id": "B_BOT"}

    def history(params):
        if params.get("latest") == params.get("oldest"):
            return {"ok": True, "messages": [original]}
        return {"ok": True, "messages": []}

    fake = slack({"conversations.history": history,
                  "files.completeUploadExternal": _complete_ok})
    with pytest.raises(slack_ops.SlackOpsError, match="NOT deleted"):
        slack_ops.attach_image("C0TEST", "1800000000.000100", "F0IMG", replace=True,
                               sleep=_no_sleep)
    assert "chat.delete" not in fake.names()


def test_attach_image_replace_reply_stays_in_thread(slack):
    original = {"ts": "1800000000.000500", "text": "A reply", "bot_id": "B_BOT",
                "thread_ts": "1800000000.000100"}
    fake = slack({
        "conversations.history": {"ok": True, "messages": []},
        "conversations.replies": lambda p: {"ok": True, "messages": (
            [{"ts": "1800000000.000100"}, original] if p.get("latest")
            else [{"ts": "1800000000.000100"},
                  {"ts": "1800000003.000100", "files": [{"id": "F0IMG"}]}])},
        "files.completeUploadExternal": _complete_ok,
        "chat.delete": {"ok": True},
    })
    res = slack_ops.attach_image("C0TEST", "1800000000.000500", "F0IMG", replace=True,
                                 text="Edited reply", sleep=_no_sleep)
    assert res["ts"] == "1800000003.000100"
    assert res["thread_ts"] == "1800000000.000100"
    completion = [c for c in fake.calls if c[0] == "files.completeUploadExternal"][0][2]
    assert completion["thread_ts"] == "1800000000.000100"
    assert completion["initial_comment"] == "Edited reply"


def test_attach_image_unknown_message_404(slack):
    slack({"conversations.history": {"ok": True, "messages": []},
           "conversations.replies": {"ok": False, "error": "thread_not_found"}})
    with pytest.raises(slack_ops.SlackOpsError, match="thread_not_found"):
        slack_ops.attach_image("C0TEST", "1800000000.000100", "F0IMG", sleep=_no_sleep)


# --- routes -------------------------------------------------------------------


@pytest.fixture
def routes(monkeypatch):
    calls = {}

    def require_operator(event, action):
        calls["action"] = action
        return "subject-1", None

    monkeypatch.setattr(slack_api, "require_operator", require_operator)
    monkeypatch.setattr(slack_api, "audit", type("A", (), {
        "emit": staticmethod(lambda *a, **k: calls.setdefault("audit", a))})())
    return calls


def test_route_image_upload_url(monkeypatch, routes):
    monkeypatch.setattr(slack_api.slack_ops, "image_upload_url",
                        lambda filename, length, connection_id: {
                            "connection": connection_id, "file_id": "F1",
                            "upload_url": UPLOAD_URL, "filename": filename, "length": length})
    resp = slack_api.route_slack(
        {"body": json.dumps({"filename": "a.png", "length": 5})},
        "POST", "/api/agent/slack/image-upload-url")
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["connection"] == "slack"
    assert routes["action"] == "slack.image_upload_url"


def test_route_post_with_file_id_uses_post_image(monkeypatch, routes):
    seen = {}

    def post_image(channel, text, file_id, **kw):
        seen.update(channel=channel, text=text, file_id=file_id, **kw)
        return {"ok": True, "connection": kw["connection_id"], "ts": "1.2"}

    monkeypatch.setattr(slack_api.slack_ops, "post_image", post_image)
    monkeypatch.setattr(slack_api.slack_ops, "post_message",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("text path")))
    resp = slack_api.route_slack(
        {"body": json.dumps({"channel": "C1", "text": "Hi", "file_id": "F1",
                             "thread_ts": "1.0"})},
        "POST", "/api/agent/slack/post")
    assert resp["statusCode"] == 200
    assert seen["file_id"] == "F1" and seen["thread_ts"] == "1.0"


def test_route_attach_image_maps_errors(monkeypatch, routes):
    def attach(*a, **kw):
        raise slack_ops.SlackOpsError("refusing --replace: not ours", status=403)

    monkeypatch.setattr(slack_api.slack_ops, "attach_image", attach)
    resp = slack_api.route_slack(
        {"body": json.dumps({"channel": "C1", "ts": "1.0", "file_id": "F1",
                             "replace": True})},
        "POST", "/api/agent/slack/attach-image")
    assert resp["statusCode"] == 403
    assert "refusing" in json.loads(resp["body"])["error"]
    assert routes["action"] == "slack.attach_image"


# --- CLI ----------------------------------------------------------------------


class FakeApi:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def call(self, api_url, method, path, body=None, timeout=20, debug=False):
        self.calls.append((method, path, body))
        return self.responses[path]


def _cli(monkeypatch, responses):
    from dapier_cli import commands

    fake = FakeApi(responses)
    uploads = []
    monkeypatch.setattr(commands.slack.api, "call", fake.call)
    monkeypatch.setattr(commands.slack, "_upload_bytes",
                        lambda url, content: uploads.append((url, content)))
    return commands, fake, uploads


def test_cli_parses_image_and_attach_flags():
    from dapier_cli.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["slack", "post", "--channel", "C1", "--text", "hi",
                              "--image", "a.png", "--thread", "1.0"])
    assert args.image == "a.png" and args.thread_ts == "1.0"
    args = parser.parse_args(["slack", "attach-image", "--channel", "C1", "--ts", "1.0",
                              "--image", "a.png", "--replace"])
    assert args.command == "attach-image" and args.replace is True
    assert args.connection == "slack"


def test_cli_post_image_uploads_then_posts(tmp_path, monkeypatch):
    img = tmp_path / "pic.png"
    img.write_bytes(b"\x89PNG fake")
    commands, fake, uploads = _cli(monkeypatch, {
        "/api/agent/slack/image-upload-url": {"file_id": "F1", "upload_url": UPLOAD_URL},
        "/api/agent/slack/post": {"ok": True, "ts": "1.5", "channel": "C1"},
    })
    rc = commands.slack_post("https://api", "C1", text="Caption", image=str(img),
                             thread_ts="1.0")
    assert rc == 0
    assert fake.calls[0][2] == {"filename": "pic.png", "length": 9, "connection_id": "slack"}
    assert uploads == [(UPLOAD_URL, b"\x89PNG fake")]
    post = fake.calls[1][2]
    assert post["file_id"] == "F1" and post["text"] == "Caption"
    assert post["thread_ts"] == "1.0"


def test_cli_post_image_missing_ts_is_nonzero(tmp_path, monkeypatch):
    img = tmp_path / "pic.png"
    img.write_bytes(b"x")
    commands, _fake, _uploads = _cli(monkeypatch, {
        "/api/agent/slack/image-upload-url": {"file_id": "F1", "upload_url": UPLOAD_URL},
        "/api/agent/slack/post": {"ok": True, "ts": None, "ts_error": "posted, but ..."},
    })
    assert commands.slack_post("https://api", "C1", text="x", image=str(img)) == 1


def test_cli_post_image_errors_loudly_on_bad_file(tmp_path, monkeypatch):
    from dapier_cli import api

    commands, fake, _uploads = _cli(monkeypatch, {})
    with pytest.raises(api.ApiError, match="cannot read image"):
        commands.slack_post("https://api", "C1", text="x", image=str(tmp_path / "nope.png"))
    empty = tmp_path / "empty.png"
    empty.write_bytes(b"")
    with pytest.raises(api.ApiError, match="empty"):
        commands.slack_post("https://api", "C1", text="x", image=str(empty))
    big = tmp_path / "big.png"
    big.write_bytes(b"xx")
    monkeypatch.setattr(commands.slack, "SLACK_MAX_FILE_BYTES", 1)
    with pytest.raises(api.ApiError, match="too big"):
        commands.slack_post("https://api", "C1", text="x", image=str(big))
    assert fake.calls == []


def test_cli_post_image_dry_run_flags_missing_files_write(tmp_path, monkeypatch):
    img = tmp_path / "pic.png"
    img.write_bytes(b"x")
    commands, fake, uploads = _cli(monkeypatch, {
        "/api/agent/slack/post": {"ok": True, "dry_run": True, "files_write": False},
    })
    rc = commands.slack_post("https://api", "C1", text="x", image=str(img), dry_run=True)
    assert rc == 1
    assert uploads == []
    assert fake.calls[0][2]["dry_run"] is True


def test_cli_attach_image_replace(tmp_path, monkeypatch):
    img = tmp_path / "pic.jpg"
    img.write_bytes(b"jpeg")
    commands, fake, uploads = _cli(monkeypatch, {
        "/api/agent/slack/image-upload-url": {"file_id": "F2", "upload_url": UPLOAD_URL},
        "/api/agent/slack/attach-image": {"ok": True, "mode": "replace", "ts": "2.0",
                                          "deleted_ts": "1.0"},
    })
    rc = commands.slack_attach_image("https://api", "C1", "1.0", str(img), replace=True)
    assert rc == 0
    body = fake.calls[1][2]
    assert body == {"channel": "C1", "ts": "1.0", "file_id": "F2", "replace": True,
                    "connection_id": "slack", "title": "pic.jpg"}


def test_cli_upload_bytes_refuses_foreign_host():
    from dapier_cli import api
    from dapier_cli.commands import slack as cmd

    with pytest.raises(api.ApiError, match="unexpected upload URL host"):
        cmd._upload_bytes("https://evil.example.com/upload", b"x")
    with pytest.raises(api.ApiError, match="unexpected upload URL host"):
        cmd._upload_bytes("http://files.slack.com/upload/v1/x", b"x")
