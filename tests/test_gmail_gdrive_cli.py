"""The `dapier gmail` / `dapier gdrive` CLI thin clients.

The CLI is a thin JSON client over /api/agent/gmail|gdrive/*: these tests
pin the path/verb/body shapes (account always explicit, attachments ride
base64, downloads land on disk) and that the CLI never talks to Google or
touches tokens itself.
"""

import base64
from urllib.parse import urlencode

ACCOUNT_A = "a@gmail.com"
ACCOUNT_B = "b@gmail.com"


def test_cli_nouns_register():
    from dapier_cli.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["gmail", "search", "from:x", "--account", ACCOUNT_A,
                              "--max", "5", "--label", "INBOX"])
    assert (args.group, args.command) == ("gmail", "search")
    assert args.account == ACCOUNT_A
    draft = parser.parse_args(["gmail", "draft", "send", "DRAFT1", "--account", ACCOUNT_A])
    assert draft.draft_command == "send" and draft.draft_id == "DRAFT1"
    up = parser.parse_args(["gdrive", "upload", "f.txt", "--name", "f.txt",
                            "--account", ACCOUNT_A])
    assert up.path == "f.txt"
    reply = parser.parse_args(["gmail", "reply", "M1", "--all", "--draft", "--file", "-", "--account", ACCOUNT_A])
    assert reply.reply_all and reply.draft and reply.file == "-"


def test_gmail_cli_calls_agent_api_without_google(monkeypatch, tmp_path):
    from dapier_cli.commands import gmail as cmd

    calls = []

    def fake_call(api_url, method, path, body=None, timeout=20, debug=False):
        calls.append({"method": method, "path": path, "body": body})
        return {"ok": True}

    monkeypatch.setattr(cmd.api, "call", fake_call)
    attachment = tmp_path / "a.pdf"
    attachment.write_bytes(b"%PDF")
    cmd.gmail_send("http://api", account=ACCOUNT_A, to="x@y.z", subject="s",
                   text="body", attach=[str(attachment) + ";application/pdf"])
    call = calls[0]
    assert call["method"] == "POST" and call["path"] == "/api/agent/gmail/send"
    assert call["body"]["account"] == ACCOUNT_A
    assert base64.b64decode(call["body"]["attachments"][0]["content_b64"]) == b"%PDF"
    assert call["body"]["attachments"][0]["mime_type"] == "application/pdf"

    cmd.gmail_reply("http://api", "M1", account=ACCOUNT_A, draft=True, text="t")
    assert calls[1]["path"] == "/api/agent/gmail/reply"
    assert calls[1]["body"]["draft"] is True

    cmd.gmail_draft_send("http://api", "D1", account=ACCOUNT_A)
    assert calls[2]["path"] == "/api/agent/gmail/drafts/D1/send"

    cmd.gmail_attachment("http://api", "M1", "ATT1", account=ACCOUNT_A,
                         out=str(tmp_path / "out.bin"))
    assert calls[3]["path"] == ("/api/agent/gmail/messages/M1/attachments/ATT1?"
                                + urlencode({"chunk_size": 1024 * 1024, "account": ACCOUNT_A}))


def test_gdrive_cli_calls_agent_api_without_google(monkeypatch, tmp_path):
    from dapier_cli.commands import gdrive as cmd

    calls = []

    def fake_call(api_url, method, path, body=None, timeout=20, debug=False):
        calls.append({"method": method, "path": path, "body": body})
        if "/content" in path:
            return {"name": "dl.bin", "mime_type": "application/octet-stream",
                    "data_b64": base64.b64encode(b"BYTES").decode()}
        return {"ok": True}

    monkeypatch.setattr(cmd.api, "call", fake_call)
    local = tmp_path / "up.txt"
    local.write_bytes(b"upload me")

    cmd.gdrive_upload("http://api", str(local), name="up.txt", account=ACCOUNT_A)
    assert calls[0]["path"] == "/api/agent/gdrive/files"
    assert base64.b64decode(calls[0]["body"]["content_b64"]) == b"upload me"

    cmd.gdrive_download("http://api", "F1", account=ACCOUNT_A,
                        out=str(tmp_path / "dl.bin"))
    assert calls[1]["path"] == ("/api/agent/gdrive/files/F1/content?"
                                + urlencode({"account": ACCOUNT_A}))
    assert (tmp_path / "dl.bin").read_bytes() == b"BYTES"

    cmd.gdrive_share("http://api", "F1", account=ACCOUNT_A, role="writer",
                     share_type="user", email="p@c.d")
    assert calls[2]["body"] == {"role": "writer", "share_type": "user",
                                "email_address": "p@c.d", "account": ACCOUNT_A}
    assert calls[2]["path"] == ("/api/agent/gdrive/files/F1/share?"
                                + urlencode({"account": ACCOUNT_A}))

    cmd.gdrive_trash("http://api", "F1", account=ACCOUNT_A, permanent=True)
    assert calls[3]["body"]["permanent"] is True




def test_gmail_urlsafe_attachment_download(monkeypatch, tmp_path):
    from dapier_cli.commands import gmail
    binary = b'\xfb\xff\xfe\x00'
    monkeypatch.setattr(gmail.api, 'call', lambda *a, **k: {
        'data_b64': base64.urlsafe_b64encode(binary).decode().rstrip('=')})
    target = tmp_path / 'attachment.bin'
    gmail.gmail_attachment('http://api', 'M', 'A', account=ACCOUNT_A, out=str(target))
    assert target.read_bytes() == binary


def test_gmail_search_json_and_draft_pagination(monkeypatch, capsys):
    import json
    from dapier_cli.commands import gmail
    calls = []
    def call(*args, **kwargs):
        calls.append(args)
        return {'messages': [], 'drafts': [], 'next_page_token': 'next'}
    monkeypatch.setattr(gmail.api, 'call', call)
    gmail.gmail_search('http://api', 'is:unread', account=ACCOUNT_A, as_json=True)
    assert json.loads(capsys.readouterr().out)['next_page_token'] == 'next'
    gmail.gmail_draft_list('http://api', account=ACCOUNT_A, max_results=10, page_token='p')
    assert 'max=10' in calls[-1][2] and 'page_token=p' in calls[-1][2]


def test_previews_include_all_write_fields_and_never_call_api(monkeypatch, tmp_path, capsys):
    import json
    from dapier_cli.commands import gmail, gdrive
    def unexpected(*args, **kwargs):
        raise AssertionError('preview must not call the API')
    monkeypatch.setattr(gmail.api, 'call', unexpected)
    body_file = tmp_path / 'body.txt'
    body_file.write_text('Body ü')
    gmail.gmail_send('http://api', account=ACCOUNT_A, to='peer@corp.com', cc='copy@corp.com',
                     subject='Subject', file=str(body_file), preview=True)
    body = json.loads(capsys.readouterr().out)['would_send']
    assert body['account'] == ACCOUNT_A and body['text'] == 'Body ü'
    assert body['cc'] == 'copy@corp.com'
    gdrive.gdrive_move('http://api', 'F', account=ACCOUNT_A, add_folder='NEW', remove_folder='OLD', preview=True)
    body = json.loads(capsys.readouterr().out)['would_send']
    assert body == {'account': ACCOUNT_A, 'add_parent': 'NEW', 'remove_parent': 'OLD'}
    gdrive.gdrive_update('http://api', 'F', account=ACCOUNT_A, name='new', file=str(body_file), preview=True)
    body = json.loads(capsys.readouterr().out)['would_send']
    assert body['name'] == 'new' and body['account'] == ACCOUNT_A
    assert body['content_b64'].endswith('bytes>')


def test_cli_requires_account_for_each_service():
    import pytest
    from dapier_cli.cli import build_parser
    for command in (['gmail', 'list'], ['gmail', 'draft', 'send', 'D'],
                    ['gdrive', 'list'], ['calendar', 'calendars']):
        with pytest.raises(SystemExit):
            build_parser().parse_args(command)


def test_mkdir_entrypoint_forwards_preview_without_api_call(monkeypatch, capsys):
    import json
    from dapier_cli.cli import build_parser, gdrive
    from dapier_cli import api
    def unexpected(*args, **kwargs):
        raise AssertionError('preview must never call the API')
    monkeypatch.setattr(api, 'call', unexpected)
    args = build_parser().parse_args(['gdrive', 'mkdir', 'Example', '--parent', 'PARENT',
                                      '--account', ACCOUNT_A, '--preview'])
    assert gdrive.run(args, 'http://api', False) == 0
    assert json.loads(capsys.readouterr().out)['would_send'] == {
        'account': ACCOUNT_A, 'name': 'Example', 'parent_id': 'PARENT'}


def test_draft_html_preview_and_stdin_validation(monkeypatch, tmp_path, capsys):
    import io
    import json
    import pytest
    from dapier_cli.cli import build_parser, gmail as cli
    from dapier_cli.commands import gmail as cmd

    monkeypatch.setattr(cmd.api, "call", lambda *a, **k: pytest.fail("preview cannot call API"))
    html_file = tmp_path / "body.html"
    html_file.write_text("<p>Hello</p>", encoding="utf-8")
    args = build_parser().parse_args(["gmail", "draft", "create", "--account", ACCOUNT_A,
        "--to", "peer@example.com", "--file", "-", "--html-file", str(html_file), "--preview"])
    monkeypatch.setattr(cmd.sys, "stdin", io.StringIO("Plain body"))
    cli.run(args, "http://api", False)
    body = json.loads(capsys.readouterr().out)["would_send"]
    assert body["text"] == "Plain body" and body["html"] == "<p>Hello</p>"
    args.file, args.html_file = None, "-"
    monkeypatch.setattr(cmd.sys, "stdin", io.StringIO("<p>stdin</p>"))
    cli.run(args, "http://api", False)
    assert json.loads(capsys.readouterr().out)["would_send"]["html"] == "<p>stdin</p>"
    args.file = "-"
    with pytest.raises(cmd.api.ApiError, match="cannot both read stdin"):
        cli.run(args, "http://api", False)


import pytest


@pytest.mark.parametrize("damage", ["changed", "truncated", "offset", "digest", "stalled", "protocol"])
def test_attachment_failure_keeps_existing_destination(monkeypatch, tmp_path, damage):
    import hashlib
    from dapier_cli.commands import gmail as cmd
    content = b"a" * 1048576 + b"last"
    expected = hashlib.sha256(content).hexdigest()
    calls = []
    def call(*a, **k):
        index = len(calls)
        calls.append(index)
        part = content[:1048576] if index == 0 else content[1048576:]
        blob = {"chunked": True, "offset": 0 if index == 0 else 1048576,
            "size": len(content), "sha256": expected, "chunk_size": len(part),
            "data_b64": base64.urlsafe_b64encode(part).decode(),
            "next_offset": 1048576 if index == 0 else None}
        if index == 1:
            if damage == "changed": blob["sha256"] = "0" * 64
            if damage == "offset": blob["offset"] = 0
            if damage == "digest": blob["data_b64"] = base64.b64encode(b"oops").decode()
            if damage == "truncated": blob["data_b64"] = ""
            if damage == "stalled": blob["next_offset"] = 1048576
            if damage == "protocol": blob = {"data_b64": base64.b64encode(part).decode()}
        return blob
    monkeypatch.setattr(cmd.api, "call", call)
    target = tmp_path / "existing.zip"
    target.write_bytes(b"keep")
    with pytest.raises(cmd.api.ApiError):
        cmd.gmail_attachment("http://api", "m", "a", account=ACCOUNT_A, out=str(target))
    assert target.read_bytes() == b"keep"
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize("uncertain", [False, True])
def test_large_drive_upload_no_binary_lambda_ingress(monkeypatch, tmp_path, uncertain, capsys):
    import hashlib
    import json
    from dapier_cli.commands import gdrive as cmd
    content = b"synthetic" * 570000
    target = tmp_path / "large.zip"
    target.write_bytes(content)
    md5 = hashlib.md5(content).hexdigest()
    capability = "https://www.googleapis.com/upload/drive/v3/files?upload_id=synthetic-private"
    native, chunks = [], []
    def api_call(api_url, method, path, body=None, **kwargs):
        native.append((method, path, body))
        if method == "POST":
            assert "content_b64" not in body and body["size"] == len(content)
            assert body["account"] == ACCOUNT_A and body["folder_id"] == "FOLDER"
            return {"upload_url": capability}
        assert "account=" in path
        return {"id": "file1", "name": "large.zip", "size": len(content), "md5_checksum": md5}
    def session(url, part, headers):
        assert url == capability and "Authorization" not in headers
        chunks.append((part, headers))
        if uncertain and len(chunks) == 1:
            return 503, b"", {}
        if headers["Content-Range"].startswith("bytes */"):
            return 308, b"", {"Range": "bytes=0-1048575"}
        end = int(headers["Content-Range"].split("-")[1].split("/")[0])
        if end + 1 == len(content):
            return 200, json.dumps({"id": "file1", "size": str(len(content)), "md5Checksum": md5}).encode(), {}
        return 308, b"", {"Range": f"bytes=0-{end}"}
    monkeypatch.setattr(cmd.api, "call", api_call)
    monkeypatch.setattr(cmd, "_session_request", session)
    cmd.gdrive_upload("http://api", str(target), account=ACCOUNT_A, folder="FOLDER", as_json=True)
    assert len(native) == 2 and native[0][0] == "POST" and native[1][0] == "GET"
    assert "upload_id" not in capsys.readouterr().out
    if uncertain:
        assert chunks[1][1]["Content-Range"] == f"bytes */{len(content)}"
    else:
        assert b"".join(c[0] for c in chunks) == content


@pytest.mark.parametrize("url", ["https://evil.example/upload?upload_id=x",
    "http://www.googleapis.com/upload/drive/v3/files?upload_id=x",
    "https://www.googleapis.com:443/upload/drive/v3/files?upload_id=x",
    "https://www.googleapis.com/other?upload_id=x"])
def test_drive_upload_refuses_foreign_session(monkeypatch, url):
    from dapier_cli.commands import gdrive as cmd
    monkeypatch.setattr(cmd, "_session_request", lambda *a: pytest.fail("foreign URL not contacted"))
    with pytest.raises(cmd.api.ApiError, match="Invalid Drive upload session"):
        cmd._upload_session(url, b"bytes", "application/zip")


@pytest.mark.parametrize("status,body,headers", [
    (410, b"expired", {}), (302, b"", {"Location": "https://evil.example"}),
    (200, b'{"id":"file1","size":"5","md5Checksum":"bad"}', {}),
    (308, b"", {"Range": "bytes=0-99999"})])
def test_drive_upload_failure_redacts_capability(monkeypatch, status, body, headers):
    from dapier_cli.commands import gdrive as cmd
    monkeypatch.setattr(cmd, "_session_request", lambda *a: (status, body, headers))
    with pytest.raises(cmd.api.ApiError) as error:
        cmd._upload_session("https://www.googleapis.com/upload/drive/v3/files?upload_id=private", b"bytes", "application/zip")
    assert "upload_id" not in str(error.value) and "private" not in str(error.value)
