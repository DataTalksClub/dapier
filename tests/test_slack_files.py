"""Read-only Slack attachment access: file metadata in history/replies/search,
`slack file-info` and `slack download`. Slack is mocked at slack_discovery
(_request/_slack_call) and slack_ops._http_get; all file content is synthetic."""

import base64
import hashlib
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from src.dapier.api.agent import slack as slack_api
from src.dapier.api.agent import slack_ops

TOKEN = "xoxb-" + "d" * 40
PDF = b"%PDF-1.4\n% synthetic test fixture\n%%EOF\n"
DL_URL = "https://files.slack.com/files-pri/T1-F0TEST/download/sample.pdf"


def _file(**overrides):
    item = {
        "id": "F0TEST", "name": "sample.pdf", "title": "Sample", "mimetype": "application/pdf",
        "filetype": "pdf", "pretty_type": "PDF", "size": len(PDF), "mode": "hosted",
        "is_external": False, "user": "U_UPLOADER",
        "url_private": "https://files.slack.com/files-pri/T1-F0TEST/sample.pdf",
        "url_private_download": DL_URL,
        "permalink": "https://example.slack.com/files/U1/F0TEST/sample.pdf",
        "thumb_pdf": "https://files.slack.com/files-tmb/T1-F0TEST/thumb.png",
    }
    item.update(overrides)
    return item


ROOT = {"ts": "1700000000.000100", "user": "U1", "text": "see attached", "files": [_file()]}
REPLY = {"ts": "1700000001.000200", "user": "U2", "text": "thanks",
         "thread_ts": "1700000000.000100"}


class FakeSlack:
    def __init__(self, handlers, scopes=("channels:history",)):
        self.handlers = handlers
        self.calls = []
        self.scopes = list(scopes)

    def request(self, method, url, token, payload, transport=None, form=False):
        assert token == TOKEN
        parsed = urlsplit(url)
        name = parsed.path.rsplit("/", 1)[-1]
        params = dict(payload or {})
        params.update({k: v[0] for k, v in parse_qs(parsed.query).items()})
        self.calls.append((name, params))
        handler = self.handlers.get(name)
        if handler is None:
            raise AssertionError(f"unexpected Slack call {name}")
        return handler(params) if callable(handler) else handler

    def slack_call(self, method, token, payload, transport=None):
        return self.request("POST", f"https://slack.com/api/{method}", token, payload)


@pytest.fixture
def slack(monkeypatch):
    monkeypatch.setattr(slack_ops.discovery_api, "_load",
                        lambda cid, table: ({"connection_id": cid, "provider": "slack"}, None))
    monkeypatch.setattr(slack_ops.tokens, "get_access_token",
                        lambda connection, **kw: (TOKEN, {}))

    def install(handlers, scopes=("channels:history",), http=None):
        fake = FakeSlack(handlers, scopes)
        monkeypatch.setattr(slack_ops.slack_discovery, "_request", fake.request)
        monkeypatch.setattr(slack_ops.slack_discovery, "_slack_call", fake.slack_call)
        monkeypatch.setattr(slack_ops, "_token_scopes", lambda token: list(fake.scopes))
        fake.http = []

        def http_get(url, headers, limit, timeout=20):
            fake.http.append((url, dict(headers)))
            return (http or {}).get(url, (404, {}, b""))

        monkeypatch.setattr(slack_ops, "_http_get", http_get)
        return fake

    return install


def _no_secrets(text):
    assert "://" not in text and "files-pri" not in text
    assert "xox" not in text
    assert "url_private" not in text and "permalink" not in text and "thumb" not in text


# --- metadata on history / replies / search ----------------------------------


def test_history_and_replies_expose_minimal_file_metadata(slack):
    slack({
        "conversations.history": {"ok": True, "messages": [ROOT]},
        "conversations.replies": {"ok": True, "messages": [ROOT, REPLY]},
    })
    hist = slack_ops.history("C1", connection_id="slack")
    msg = hist["messages"][0]
    assert msg["text"] == "see attached" and msg["user"] == "U1"  # existing fields kept
    assert msg["files"] == [{
        "id": "F0TEST", "name": "sample.pdf", "title": "Sample",
        "mimetype": "application/pdf", "filetype": "pdf", "pretty_type": "PDF",
        "size": len(PDF), "mode": "hosted", "is_external": False}]
    rep = slack_ops.replies("C1", "1700000000.000100", connection_id="slack")
    assert [len(m["files"]) for m in rep["messages"]] == [1, 0]  # attributed to the root
    _no_secrets(json.dumps(hist) + json.dumps(rep))


def test_image_dimensions_are_kept(slack):
    img = _file(id="F0IMG", mimetype="image/png", filetype="png", original_w=640,
                original_h=480)
    slack({"conversations.history": {"ok": True, "messages": [
        {"ts": "1.1", "files": [img, "junk", {"name": "no id"}]}]}})
    files = slack_ops.history("C1", connection_id="slack")["messages"][0]["files"]
    assert [(f["id"], f["original_w"], f["original_h"]) for f in files] == [("F0IMG", 640, 480)]


def test_search_items_carry_files(slack):
    slack({"search.messages": {"ok": True, "messages": {
        "matches": [{**ROOT, "channel": {"id": "C1", "name": "x"}}], "paging": {}}}})
    items = slack_ops.search("sample", connection_id="slack-alexey")["items"]
    assert items[0]["files"][0]["id"] == "F0TEST"


# --- file-info ------------------------------------------------------------------


def test_file_info_via_files_info(slack):
    fake = slack({"files.info": {"ok": True, "file": _file()}})
    out = slack_ops.file_info("F0TEST", connection_id="slack")
    assert out["downloadable"] is True and out["file"]["mimetype"] == "application/pdf"
    assert out["connection"] == "slack" and "channel" not in out
    assert fake.calls == [("files.info", {"file": "F0TEST"})]
    _no_secrets(json.dumps(out))


def test_file_info_missing_scope_is_actionable(slack):
    slack({"files.info": {"ok": False, "error": "missing_scope", "needed": "files:read",
                          "provided": "channels:history,chat:write"}})
    with pytest.raises(slack_ops.SlackOpsError) as exc:
        slack_ops.file_info("F0TEST", connection_id="slack")
    assert exc.value.status == 403
    message = str(exc.value)
    assert "needed scope: files:read" in message and "--channel/--ts" in message


def test_file_info_from_message_needs_no_files_read(slack):
    fake = slack({"conversations.history": {"ok": True, "messages": [ROOT]}})
    out = slack_ops.file_info("F0TEST", connection_id="slack", channel="C0ABC",
                              ts="1700000000.000100")
    assert out["channel"] == "C0ABC" and out["ts"] == "1700000000.000100"
    assert "files.info" not in [c[0] for c in fake.calls]


def test_file_not_on_message_lists_what_is_there(slack):
    slack({"conversations.history": {"ok": True, "messages": [ROOT]}})
    with pytest.raises(slack_ops.SlackOpsError) as exc:
        slack_ops.file_info("F0OTHER", connection_id="slack", channel="C0ABC",
                            ts="1700000000.000100")
    assert exc.value.status == 404 and "F0TEST" in str(exc.value)


@pytest.mark.parametrize("kwargs, message", [
    ({"file_id": "not-a-file"}, "file_id must be"),
    ({"file_id": "F0TEST", "channel": "C0ABC"}, "both channel and ts"),
    ({"file_id": "F0TEST", "channel": "C0ABC", "ts": "bad"}, "ts must be"),
])
def test_file_info_validates_input(slack, kwargs, message):
    slack({})
    with pytest.raises(slack_ops.SlackOpsError, match=message):
        slack_ops.file_info(connection_id="slack", **kwargs)


@pytest.mark.parametrize("overrides, reason", [
    ({"is_external": True, "mode": "external"}, "no Slack-hosted content"),
    ({"mode": "tombstone"}, "no Slack-hosted content"),
    ({"size": slack_ops.MAX_DOWNLOAD_BYTES + 1}, "download limit"),
])
def test_file_info_reports_not_downloadable(slack, overrides, reason):
    slack({"files.info": {"ok": True, "file": _file(**overrides)}})
    out = slack_ops.file_info("F0TEST", connection_id="slack")
    assert out["downloadable"] is False and reason in out["not_downloadable_reason"]


# --- download -------------------------------------------------------------------


def test_download_returns_bytes_from_files_slack_com_only(slack):
    fake = slack({"files.info": {"ok": True, "file": _file()}},
                 http={DL_URL: (200, {"Content-Type": "application/pdf"}, PDF)})
    out = slack_ops.file_download("F0TEST", connection_id="slack")
    assert base64.b64decode(out["content_base64"]) == PDF
    assert out["bytes"] == len(PDF) and out["sha256"] == hashlib.sha256(PDF).hexdigest()
    assert fake.http == [(DL_URL, {"authorization": f"Bearer {TOKEN}"})]
    _no_secrets(json.dumps({k: v for k, v in out.items() if k != "content_base64"}))


def test_download_follows_redirect_only_within_files_host(slack):
    moved = "https://files.slack.com/files-pri/T1-F0TEST/download/sample.pdf?origin=1"
    fake = slack({"files.info": {"ok": True, "file": _file()}}, http={
        DL_URL: (302, {"Location": moved}, b""),
        moved: (200, {"Content-Type": "application/pdf"}, PDF)})
    assert slack_ops.file_download("F0TEST", connection_id="slack")["bytes"] == len(PDF)
    assert [u for u, _h in fake.http] == [DL_URL, moved]


def test_download_refuses_redirect_to_sign_in_with_scope_hint(slack):
    fake = slack({"files.info": {"ok": True, "file": _file()}},
                 http={DL_URL: (302, {"Location": "https://example.slack.com/?redir=x"}, b"")})
    with pytest.raises(slack_ops.SlackOpsError) as exc:
        slack_ops.file_download("F0TEST", connection_id="slack")
    assert exc.value.status == 403
    assert "files:read: no" in str(exc.value)
    assert len(fake.http) == 1  # the token never went to the other host
    _no_secrets(str(exc.value))


def test_download_refuses_html_sign_in_page(slack):
    slack({"files.info": {"ok": True, "file": _file()}},
          http={DL_URL: (200, {"content-type": "text/html; charset=utf-8"}, b"<html>")})
    with pytest.raises(slack_ops.SlackOpsError, match="HTML page"):
        slack_ops.file_download("F0TEST", connection_id="slack")


def test_download_refuses_foreign_file_location(slack):
    slack({"files.info": {"ok": True, "file": _file(
        url_private_download="https://evil.example/sample.pdf")}})
    with pytest.raises(slack_ops.SlackOpsError, match="outside files.slack.com"):
        slack_ops.file_download("F0TEST", connection_id="slack")


def test_download_refuses_oversize_before_fetching(slack):
    fake = slack({"files.info": {"ok": True, "file": _file(
        size=slack_ops.MAX_DOWNLOAD_BYTES + 1)}})
    with pytest.raises(slack_ops.SlackOpsError) as exc:
        slack_ops.file_download("F0TEST", connection_id="slack")
    assert exc.value.status == 413 and fake.http == []


def test_download_refuses_oversize_body_and_size_mismatch(slack, monkeypatch):
    monkeypatch.setattr(slack_ops, "MAX_DOWNLOAD_BYTES", 8)
    slack({"files.info": {"ok": True, "file": _file(size=None)}},
          http={DL_URL: (200, {}, b"x" * 9)})
    with pytest.raises(slack_ops.SlackOpsError) as exc:
        slack_ops.file_download("F0TEST", connection_id="slack")
    assert exc.value.status == 413
    slack({"files.info": {"ok": True, "file": _file(size=5)}},
          http={DL_URL: (200, {}, b"abc")})
    with pytest.raises(slack_ops.SlackOpsError, match="metadata says 5"):
        slack_ops.file_download("F0TEST", connection_id="slack")


def test_download_via_message_with_user_connection(slack):
    slack({"conversations.history": {"ok": True, "messages": [ROOT]}},
          http={DL_URL: (200, {}, PDF)})
    out = slack_ops.file_download("F0TEST", connection_id="slack-alexey", channel="C0ABC",
                                  ts="1700000000.000100")
    assert out["connection"] == "slack-alexey" and out["ts"] == "1700000000.000100"


# --- routes ---------------------------------------------------------------------


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


def test_routes_default_to_bot_connection_and_audit(monkeypatch, routes):
    seen = {}

    def file_download(**kw):
        seen.update(kw)
        return {"connection": kw["connection_id"], "bytes": 1}

    monkeypatch.setattr(slack_api.slack_ops, "file_download", file_download)
    resp = slack_api.route_slack(
        {"queryStringParameters": {"file_id": "F0TEST"}}, "GET",
        "/api/agent/slack/file/content")
    assert resp["statusCode"] == 200
    assert seen == {"file_id": "F0TEST", "connection_id": "slack", "channel": None,
                    "ts": None}
    assert routes["action"] == "slack.file_download"
    assert routes["audit"][:2] == ("slack", "slack.file_download")


def test_route_file_info_maps_errors(monkeypatch, routes):
    def file_info(**kw):
        raise slack_ops.SlackOpsError("Slack rejected files.info: missing_scope", status=403)

    monkeypatch.setattr(slack_api.slack_ops, "file_info", file_info)
    resp = slack_api.route_slack(
        {"queryStringParameters": {"file_id": "F0TEST", "connection_id": "Slack-Alexey",
                                   "channel": "C1", "ts": "1.2"}},
        "GET", "/api/agent/slack/file")
    assert resp["statusCode"] == 403 and routes["action"] == "slack.file_info"


# --- CLI ------------------------------------------------------------------------


def _cli(monkeypatch, response):
    from dapier_cli import commands

    calls = []

    def call(api_url, method, path, body=None, timeout=20, debug=False):
        calls.append((method, path, timeout))
        return response

    monkeypatch.setattr(commands.slack.api, "call", call)
    return commands, calls


def _payload(**overrides):
    data = {"connection": "slack", "file": {"id": "F0TEST", "name": "../sample.pdf",
                                            "mimetype": "application/pdf"},
            "bytes": len(PDF), "sha256": hashlib.sha256(PDF).hexdigest(),
            "content_base64": base64.b64encode(PDF).decode()}
    data.update(overrides)
    return data


def test_cli_parses_file_commands():
    from dapier_cli.cli import build_parser

    args = build_parser().parse_args(["slack", "download", "F0TEST", "--channel", "C1",
                                      "--ts", "1.2", "-o", "out"])
    assert (args.command, args.file_id, args.connection, args.output) == (
        "download", "F0TEST", "slack", "out")
    args = build_parser().parse_args(["slack", "file-info", "F0TEST", "--connection",
                                      "slack-alexey"])
    assert args.command == "file-info" and args.connection == "slack-alexey"


def test_cli_download_writes_file_and_never_prints_content(tmp_path, monkeypatch, capsys):
    commands, calls = _cli(monkeypatch, _payload())
    rc = commands.slack_download("https://api", "F0TEST", output=str(tmp_path),
                                 channel="C1", ts="1.2")
    assert rc == 0
    assert (tmp_path / "sample.pdf").read_bytes() == PDF  # basename only, no traversal
    method, path, timeout = calls[0]
    assert path.startswith("/api/agent/slack/file/content?") and "channel=C1" in path
    assert method == "GET" and timeout >= 60
    out = capsys.readouterr().out
    assert "content_base64" not in out and "synthetic" not in out
    assert json.loads(out)["path"] == str(tmp_path / "sample.pdf")


def test_cli_download_refuses_overwrite_without_force(tmp_path, monkeypatch):
    target = tmp_path / "x.pdf"
    target.write_bytes(b"keep")
    commands, _calls = _cli(monkeypatch, _payload())
    assert commands.slack_download("https://api", "F0TEST", output=str(target)) == 1
    assert target.read_bytes() == b"keep"
    assert commands.slack_download("https://api", "F0TEST", output=str(target),
                                   force=True) == 0
    assert target.read_bytes() == PDF


def test_cli_download_checks_integrity(tmp_path, monkeypatch):
    from dapier_cli import api

    commands, _calls = _cli(monkeypatch, _payload(sha256="0" * 64))
    with pytest.raises(api.ApiError, match="sha256"):
        commands.slack_download("https://api", "F0TEST", output=str(tmp_path / "x.pdf"))
    assert not (tmp_path / "x.pdf").exists()


def test_cli_file_info_prints_metadata(monkeypatch, capsys):
    commands, calls = _cli(monkeypatch, {"connection": "slack", "file": {"id": "F0TEST"},
                                         "downloadable": True})
    assert commands.slack_file_info("https://api", "F0TEST") == 0
    assert calls[0][1] == "/api/agent/slack/file?file_id=F0TEST&connection_id=slack"
    assert json.loads(capsys.readouterr().out)["downloadable"] is True
