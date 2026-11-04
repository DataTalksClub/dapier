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
                                + urlencode({"account": ACCOUNT_A}))


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
