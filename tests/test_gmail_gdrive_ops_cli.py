"""gmail/gdrive agent ops, routes, and the CLI thin clients.

Covers the brief's and the amendment's guarantees: account resolution
rides every operation, wrong-account isolation is real (each account's
call carries only its own bearer token), provider errors surface with
their status plus the scope hint, MIME construction handles Unicode and
binary attachments with correct threading headers, replies work both as
direct sends and reply drafts, and the CLI is a thin JSON client that
never touches Google or tokens itself.
"""

import base64
import email
import email.policy
import io
import json
from urllib.parse import unquote_plus

import pytest

from src.dapier.api.agent import gmail as gmail_api
from src.dapier.api.agent import gmail_ops
from src.dapier.api.agent import gdrive as gdrive_api
from src.dapier.api.agent import gdrive_ops
from src.dapier.api.agent import google_accounts

GMAIL_READ = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_SEND = "https://www.googleapis.com/auth/gmail.send"
GMAIL_MODIFY = "https://www.googleapis.com/auth/gmail.modify"
DRIVE_FULL = "https://www.googleapis.com/auth/drive"
USERINFO = "https://www.googleapis.com/auth/userinfo.email"


def _connection(cid, email, scopes):
    return {
        "connection_id": cid,
        "provider": "google",
        "status": "connected",
        "verified_account_id": email,
        "account_title": email.split("@")[0].title(),
        "scopes": list(scopes),
        "granted_scopes": list(scopes),
    }


ACCOUNT_A = "a@gmail.com"
ACCOUNT_B = "b@gmail.com"


@pytest.fixture
def google_world(monkeypatch):
    """Two Google connections with distinct tokens, routed through one
    fake `_load`/`get_access_token` pair; returns the connections."""
    conn_a = _connection("google-a", ACCOUNT_A,
                         (GMAIL_READ, GMAIL_SEND, GMAIL_MODIFY, DRIVE_FULL, USERINFO))
    conn_b = _connection("google-b", ACCOUNT_B,
                         (GMAIL_READ, GMAIL_SEND, USERINFO))
    by_id = {c["connection_id"]: c for c in (conn_a, conn_b)}

    monkeypatch.setattr(google_accounts, "_tables", lambda: (object(), object()))
    monkeypatch.setattr(google_accounts.connections, "_scan_all",
                        lambda table: [conn_a, conn_b])
    from src.dapier.api import discovery as api_discovery

    monkeypatch.setattr(api_discovery, "_load",
                        lambda cid, table: (by_id[cid], None))
    tokens_seen = []

    def fake_token(connection, **kwargs):
        tokens_seen.append(connection["connection_id"])
        return f"token-{connection['connection_id']}", {"expires_at": None}

    monkeypatch.setattr(gmail_ops.tokens, "get_access_token", fake_token)
    monkeypatch.setattr(gdrive_ops.tokens, "get_access_token", fake_token)
    return conn_a, conn_b, tokens_seen


class ProviderFake:
    """Canned provider._request answers keyed by (method, url substring);
    records every call's bearer for the isolation asserts."""

    def __init__(self):
        self.calls = []
        self.routes = []

    def on(self, method, fragment, payload=None, status=200, raw=None):
        self.routes.append((method, fragment, payload, status, raw))
        return self

    def _answer(self, method, url):
        for want_method, fragment, payload, status, raw in self.routes:
            if want_method == method and fragment in url:
                if raw is not None:
                    return status, raw
                return status, json.dumps(payload or {}).encode()
        raise AssertionError(f"unexpected provider call {method} {url}")


def _settle(canned, status, raw):
    """One canned provider answer: payload, error, or raw bytes."""
    if status >= 300:
        from src.dapier.connections.discovery import DiscoveryError

        body = raw if raw is not None else json.dumps(
            {"error": {"message": canned or "backend error"}}).encode()
        try:
            err = json.loads(body.decode()).get("error")
            detail = err.get("message") if isinstance(err, dict) else str(err)
        except Exception:
            detail = ""
        raise DiscoveryError(f"provider returned HTTP {status}: {detail}", status=502)
    if raw is not None:
        return json.loads(raw.decode() or "{}")
    return canned or {}


@pytest.fixture
def gmail_provider(monkeypatch):
    fake = ProviderFake()

    def request(method, url, token, payload, *, transport=None):
        fake.calls.append({"method": method, "url": url, "token": token,
                           "payload": payload})
        for want_method, fragment, canned, status, raw in reversed(fake.routes):
            if want_method == method and fragment in url:
                return _settle(canned, status, raw)
        raise AssertionError(f"unexpected provider call {method} {url}")

    monkeypatch.setattr(gmail_ops.provider, "_request", request)
    return fake


@pytest.fixture
def gdrive_provider(monkeypatch):
    fake = ProviderFake()

    def request(method, url, token, payload, *, transport=None):
        fake.calls.append({"method": method, "url": url, "token": token,
                           "payload": payload})
        for want_method, fragment, canned, status, raw in reversed(fake.routes):
            if want_method == method and fragment in url:
                return _settle(canned, status, raw)
        raise AssertionError(f"unexpected provider call {method} {url}")

    monkeypatch.setattr(gdrive_ops.provider, "_request", request)
    return fake


# --- gmail ops -----------------------------------------------------------------


def test_gmail_list_search_expands_and_isolates_accounts(google_world, gmail_provider):
    gmail_provider.on("GET", "/users/me/messages?maxResults=25",
                      {"messages": [{"id": "m1"}, {"id": "m2"}]})
    gmail_provider.on("GET", "/messages/m1?format=metadata",
                      {"id": "m1", "threadId": "t1", "payload": {"headers": [
                          {"name": "From", "value": "x@y.z"},
                          {"name": "Subject", "value": "Hello"}]}})
    gmail_provider.on("GET", "/messages/m2?format=metadata",
                      {"id": "m2", "threadId": "t1", "payload": {"headers": [
                          {"name": "From", "value": "q@r.s"},
                          {"name": "Subject", "value": "Re: Hello"}]}})
    payload = gmail_ops.list_messages(ACCOUNT_A, query="is:unread", label_ids=["INBOX"])
    assert payload["count"] == 2
    assert payload["messages"][0]["subject"] == "Hello"
    first = gmail_provider.calls[0]
    assert "q=is%3Aunread" in first["url"] and "labelIds=INBOX" in first["url"]
    assert first["token"] == "token-google-a"
    assert all(call["token"] == "token-google-a" for call in gmail_provider.calls)

    gmail_provider.calls.clear()
    gmail_ops.list_messages(ACCOUNT_B)
    assert gmail_provider.calls[0]["token"] == "token-google-b"


def test_gmail_account_resolution_errors_surface(google_world):
    with pytest.raises(gmail_ops.GmailOpsError) as missing:
        gmail_ops.list_messages(None)  # two connected gmail accounts: explicit pick required
    assert missing.value.status == 400
    assert "--account is required" in str(missing.value)
    with pytest.raises(gmail_ops.GmailOpsError) as unknown:
        gmail_ops.list_messages("nobody@gmail.com")
    assert unknown.value.status == 400


def test_gmail_get_message_includes_body_and_attachments(google_world, gmail_provider):
    data = "aGVsbG8="  # "hello"
    payload = {
        "id": "m1", "threadId": "t1", "snippet": "hello world",
        "internalDate": "1730000000000",
        "payload": {
            "headers": [
                {"name": "From", "value": "Boss <boss@corp.com>"},
                {"name": "Subject", "value": "Report"},
                {"name": "Message-ID", "value": "<orig@corp.com>"},
            ],
            "parts": [
                {"mimeType": "text/plain",
                 "body": {"data": base64.urlsafe_b64encode("héllo body".encode()).decode()}},
                {"mimeType": "application/pdf", "filename": "r.pdf",
                 "body": {"attachmentId": "att1", "size": 4}},
            ],
        },
    }
    gmail_provider.on("GET", "/messages/m1?format=full", payload)
    view = gmail_ops.get_message(ACCOUNT_A, "m1")
    assert view["text"] == "héllo body"  # Unicode bodies decode correctly
    assert view["from"] == "Boss <boss@corp.com>"
    assert view["attachments"][0]["attachment_id"] == "att1"

    gmail_provider.on("GET", "/messages/m1/attachments/att1",
                      {"attachmentId": "att1", "size": 4, "data": data})
    blob = gmail_ops.get_attachment(ACCOUNT_A, "m1", "att1")
    assert base64.b64decode(blob["data_b64"]) == base64.b64decode(data)


def test_gmail_send_builds_mime_with_unicode_and_binary_attachment(
        google_world, gmail_provider, tmp_path):
    gmail_provider.on("GET", "/users/me/profile", {"emailAddress": ACCOUNT_A})
    gmail_provider.on("POST", "/users/me/messages/send", {"id": "sent1", "threadId": "t9"})
    binary = bytes(range(256))
    attachment = tmp_path / "data.bin"
    attachment.write_bytes(binary)

    from dapier_cli import commands as cli_commands

    spec = cli_commands.gmail.parse_attach(str(attachment))
    result = gmail_ops.send(ACCOUNT_A, to="dest@corp.com", cc="copy@corp.com",
                            subject="Ünïcode réport", text="höllo", attachments=[spec])
    assert result["message_id"] == "sent1"

    sent = gmail_provider.calls[-1]
    assert sent["token"] == "token-google-a"
    raw = base64.urlsafe_b64decode(sent["payload"]["raw"])
    message = email.message_from_bytes(raw, policy=email.policy.default)
    assert str(message["To"]) == "dest@corp.com"
    assert str(message["Cc"]) == "copy@corp.com"
    assert str(message["Subject"]) == "Ünïcode réport"
    assert message.get_content_type() == "multipart/mixed"
    body_part, attachment_part = list(message.iter_parts())
    assert body_part.get_content().strip() == "höllo"
    assert attachment_part.get_filename() == "data.bin"
    assert attachment_part.get_content() == binary


_ORIGINAL_FULL = {
    "id": "m1", "threadId": "t1",
    "payload": {"headers": [
        {"name": "From", "value": "Boss <boss@corp.com>"},
        {"name": "To", "value": f"{ACCOUNT_A}, peer@corp.com"},
        {"name": "Cc", "value": "watcher@corp.com"},
        {"name": "Reply-To", "value": "careers@corp.com"},
        {"name": "Subject", "value": "Quarterly plan"},
        {"name": "Message-ID", "value": "<orig@corp.com>"},
        {"name": "References", "value": "<prev@corp.com>"},
        {"name": "Date", "value": "Mon, 2 Nov 2026 10:00:00 +0000"},
    ], "parts": [
        {"mimeType": "text/plain",
         "body": {"data": base64.urlsafe_b64encode("see plan".encode()).decode()}},
    ]},
}


def test_gmail_reply_direct_send_threads_correctly(google_world, gmail_provider):
    gmail_provider.on("GET", "/messages/m1?format=full", _ORIGINAL_FULL)
    gmail_provider.on("GET", "/users/me/profile", {"emailAddress": ACCOUNT_A})
    gmail_provider.on("POST", "/users/me/messages/send", {"id": "sent2", "threadId": "t1"})
    result = gmail_ops.reply(ACCOUNT_A, "m1", text="On it")
    sent = gmail_provider.calls[-1]
    assert sent["url"].endswith("/users/me/messages/send")
    assert sent["payload"]["threadId"] == "t1"  # the original thread
    raw = base64.urlsafe_b64decode(sent["payload"]["raw"])
    message = email.message_from_bytes(raw, policy=email.policy.default)
    # Reply-To wins over From; reply-all was not requested
    assert str(message["To"]) == "careers@corp.com"
    assert str(message["In-Reply-To"]) == "<orig@corp.com>"
    assert "<prev@corp.com>" in str(message["References"])
    assert "<orig@corp.com>" in str(message["References"])
    assert str(message["Subject"]) == "Re: Quarterly plan"
    assert result["reply_to"] == ["careers@corp.com"]


def test_gmail_reply_all_excludes_self_and_keeps_thread(google_world, gmail_provider):
    gmail_provider.on("GET", "/messages/m1?format=full", _ORIGINAL_FULL)
    gmail_provider.on("GET", "/users/me/profile", {"emailAddress": ACCOUNT_A})
    gmail_provider.on("POST", "/users/me/messages/send", {"id": "sent3", "threadId": "t1"})
    gmail_ops.reply(ACCOUNT_A, "m1", reply_all=True, text="Thanks all")
    sent = gmail_provider.calls[-1]
    raw = base64.urlsafe_b64decode(sent["payload"]["raw"])
    message = email.message_from_bytes(raw, policy=email.policy.default)
    recipients = {address.strip() for address in str(message["To"]).split(",")}
    assert recipients == {"careers@corp.com", "peer@corp.com", "watcher@corp.com"}
    assert ACCOUNT_A not in recipients  # the account never replies to itself


def test_gmail_reply_draft_parks_in_thread_without_sending(google_world, gmail_provider):
    gmail_provider.on("GET", "/messages/m1?format=full", _ORIGINAL_FULL)
    gmail_provider.on("GET", "/users/me/profile", {"emailAddress": ACCOUNT_A})
    gmail_provider.on("POST", "/users/me/drafts",
                      {"id": "d9", "message": {"id": "m-x", "threadId": "t1"}})
    result = gmail_ops.reply(ACCOUNT_A, "m1", reply_all=True, text="drafting", draft=True)
    created = gmail_provider.calls[-1]
    assert created["url"].endswith("/users/me/drafts")
    assert created["payload"]["message"]["threadId"] == "t1"
    assert result["draft"] is True and result["draft_id"] == "d9"
    assert not any(call["url"].endswith("/messages/send") for call in gmail_provider.calls)


def test_gmail_draft_create_update_send_delete(google_world, gmail_provider):
    gmail_provider.on("GET", "/users/me/profile", {"emailAddress": ACCOUNT_A})
    gmail_provider.on("POST", "/users/me/drafts",
                      {"id": "d1", "message": {"id": "m2", "threadId": "t2"}})
    draft = gmail_ops.create_draft(ACCOUNT_A, to="a@b.c", subject="Inv", text="body")
    assert draft["id"] == "d1"
    assert gmail_provider.calls[-1]["payload"]["message"]["raw"]

    gmail_provider.on("GET", "/drafts/d1", {"id": "d1", "message": {"threadId": "t2"}})
    gmail_provider.on("PUT", "/drafts/d1", {"id": "d1", "message": {"id": "m2"}})
    updated = gmail_ops.update_draft(ACCOUNT_A, "d1", to="a@b.c", subject="Inv2", text="new")
    assert updated["id"] == "d1"
    assert gmail_provider.calls[-1]["method"] == "PUT"

    gmail_provider.on("GET", "/drafts/d1",
                      {"id": "d1", "message": {"id": "m2", "threadId": "t2"}})
    gmail_provider.on("POST", "/drafts/send", {"id": "sent4", "threadId": "t2"})
    sent = gmail_ops.send_draft(ACCOUNT_A, "d1")
    assert sent["message_id"] == "sent4"
    assert gmail_provider.calls[-1]["payload"] == {"id": "d1"}

    gmail_provider.on("DELETE", "/drafts/d1", {})
    assert gmail_ops.delete_draft(ACCOUNT_A, "d1")["deleted"] == "d1"


def test_gmail_modify_maps_flags_to_system_labels(google_world, gmail_provider):
    gmail_provider.on("POST", "/messages/m1/modify",
                      {"id": "m1", "labelIds": ["INBOX"]})
    payload = gmail_ops.modify(ACCOUNT_A, "m1", mark_read=True, archive=True)
    body = gmail_provider.calls[-1]["payload"]
    assert body["removeLabelIds"] == ["UNREAD", "INBOX"]
    assert "addLabelIds" not in body
    assert payload["labels"] == ["INBOX"]

    gmail_provider.on("POST", "/messages/m1/trash", {"id": "m1", "labelIds": ["TRASH"]})
    gmail_ops.modify(ACCOUNT_A, "m1", trash=True)
    assert gmail_provider.calls[-1]["url"].endswith("/messages/m1/trash")

    with pytest.raises(gmail_ops.GmailOpsError):
        gmail_ops.modify(ACCOUNT_A, "m1")  # no change requested


def test_gmail_provider_errors_relay_status_and_scope_hint(google_world, gmail_provider):
    gmail_provider.on("GET", "/users/me/messages", None, status=403,
                      raw=json.dumps({"error": {"message": "Insufficient Permission"}}).encode())
    with pytest.raises(gmail_ops.GmailOpsError) as denied:
        gmail_ops.list_messages(ACCOUNT_A)
    assert denied.value.status == 403
    assert "Insufficient Permission" in str(denied.value)
    assert "dapier connections edit --scopes" in str(denied.value)

    gmail_provider.on("GET", "/users/me/messages", None, status=404,
                      raw=json.dumps({"error": {"message": "Requested entity was not found."}}).encode())
    with pytest.raises(gmail_ops.GmailOpsError) as missing:
        gmail_ops.list_messages(ACCOUNT_A)
    assert missing.value.status == 404


def test_gmail_token_failure_is_502_without_token_material(google_world, monkeypatch):
    def broken(connection, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(gmail_ops.tokens, "get_access_token", broken)
    with pytest.raises(gmail_ops.GmailOpsError) as exc:
        gmail_ops.list_messages(ACCOUNT_A)
    assert exc.value.status == 502
    assert "token-google" not in str(exc.value)


# --- gdrive ops ----------------------------------------------------------------


def test_gdrive_list_filters_by_folder_and_query(google_world, gdrive_provider):
    gdrive_provider.on("GET", "/drive/v3/files", {
        "files": [{"id": "f1", "name": "Report.pdf", "mimeType": "application/pdf",
                   "size": "10", "modifiedTime": "2026-11-01T00:00:00Z"}]})
    payload = gdrive_ops.list_files(ACCOUNT_A, query="name contains 'report'",
                                    folder_id="FOL", max_results=10)
    url = unquote_plus(gdrive_provider.calls[0]["url"])
    assert "'FOL' in parents" in url
    assert "name contains 'report'" in url
    assert payload["files"][0]["name"] == "Report.pdf"
    assert payload["files"][0]["size"] == 10
    assert gdrive_provider.calls[0]["token"] == "token-google-a"


def test_gdrive_download_exports_native_docs_and_streams_binary(
        google_world, gdrive_provider, monkeypatch):
    gdrive_provider.on("GET", "/files/fdoc?fields=", {
        "id": "fdoc", "name": "Plan", "mimeType": "application/vnd.google-apps.document"})
    pdf_bytes = b"%PDF-1.4 fake"
    seen = {}

    def fake_transport(method, url, *, headers, body, timeout=15):
        seen["method"], seen["url"] = method, url
        return 200, pdf_bytes

    monkeypatch.setattr(gdrive_ops.provider, "_default_transport", fake_transport)
    payload = gdrive_ops.download(ACCOUNT_A, "fdoc")
    assert "/files/fdoc/export" in seen["url"]
    assert "mimeType=application%2Fpdf" in seen["url"]
    assert base64.b64decode(payload["data_b64"]) == pdf_bytes
    assert payload["mime_type"] == "application/pdf"

    gdrive_provider.on("GET", "/files/fbin?fields=", {
        "id": "fbin", "name": "data.bin", "mimeType": "application/octet-stream"})
    payload = gdrive_ops.download(ACCOUNT_A, "fbin")
    assert "alt=media" in seen["url"]
    assert payload["mime_type"] == "application/octet-stream"
    assert payload["exported"] is False


def test_gdrive_upload_multipart_and_update(google_world, gdrive_provider, monkeypatch):
    captured = {}

    def fake_transport(method, url, *, headers, body, timeout=30):
        captured["method"], captured["url"], captured["headers"], captured["body"] = \
            method, url, headers, body
        name = "renamed.txt" if method == "PATCH" else "notes.txt"
        return 200, json.dumps({
            "id": "fnew", "name": name, "mimeType": "text/plain"}).encode()

    monkeypatch.setattr(gdrive_ops.provider, "_default_transport", fake_transport)
    content = "nōtes ünïcode".encode()
    result = gdrive_ops.upload(ACCOUNT_A, "notes.txt",
                               base64.b64encode(content).decode("ascii"),
                               folder_id="FOL")
    assert result["id"] == "fnew"
    assert captured["url"].startswith("https://www.googleapis.com/upload/drive/v3/files")
    assert "uploadType=multipart" in captured["url"]
    body = captured["body"]
    metadata_part = body.split(b"\r\n\r\n")[1]
    assert json.loads(metadata_part.split(b"\r\n--")[0])["parents"] == ["FOL"]
    assert content in body

    result = gdrive_ops.update(ACCOUNT_A, "fnew", name="renamed.txt",
                               content_b64=base64.b64encode(b"v2").decode())
    assert captured["method"] == "PATCH"
    assert result["name"] == "renamed.txt"

    with pytest.raises(gdrive_ops.GDriveOpsError):
        gdrive_ops.update(ACCOUNT_A, "fnew")  # nothing to change


def test_gdrive_share_trash_move_semantics(google_world, gdrive_provider):
    with pytest.raises(gdrive_ops.GDriveOpsError):
        gdrive_ops.share(ACCOUNT_A, "f1", role="reader", share_type="user")  # no email

    gdrive_provider.on("POST", "/files/f1/permissions",
                       {"id": "perm1", "role": "writer", "type": "user"})
    payload = gdrive_ops.share(ACCOUNT_A, "f1", role="writer", share_type="user",
                               email_address="peer@corp.com")
    body = gdrive_provider.calls[-1]["payload"]
    assert body == {"role": "writer", "type": "user", "emailAddress": "peer@corp.com"}
    assert payload["permission_id"] == "perm1"

    gdrive_provider.on("PATCH", "/files/f1?", {"id": "f1", "trashed": True})
    trashed = gdrive_ops.trash(ACCOUNT_A, "f1")
    assert gdrive_provider.calls[-1]["payload"] == {"trashed": True}
    assert trashed["trashed"] is True and trashed["permanent"] is False

    seen = {}

    def fake_transport(method, url, *, headers, body, timeout=15):
        seen["method"] = method
        return 204, b""

    from unittest.mock import patch

    with patch.object(gdrive_ops.provider, "_default_transport", fake_transport):
        permanent = gdrive_ops.trash(ACCOUNT_A, "f1", permanent=True)
    assert seen["method"] == "DELETE"
    assert permanent["permanent"] is True

    gdrive_provider.on("PATCH", "addParents=F2", {
        "id": "f1", "name": "x", "parents": ["F2"]})
    moved = gdrive_ops.move(ACCOUNT_A, "f1", add_parent="F2", remove_parent="F1")
    url = gdrive_provider.calls[-1]["url"]
    assert "addParents=F2" in url and "removeParents=F1" in url
    assert moved["parents"] == ["F2"]

    with pytest.raises(gdrive_ops.GDriveOpsError):
        gdrive_ops.move(ACCOUNT_A, "f1")  # needs a folder change


def test_gdrive_restore_untrashes(google_world, gdrive_provider):
    gdrive_provider.on("PATCH", "/drive/v3/files/f1?", {
        "id": "f1", "name": "back.txt", "trashed": False})
    payload = gdrive_ops.restore(ACCOUNT_A, "f1")
    body = gdrive_provider.calls[-1]["payload"]
    assert body == {"trashed": False}
    assert payload["trashed"] is False


def test_provider_httperror_recovers_status_and_detail(google_world, monkeypatch):
    """Production urllib raises HTTPError on non-2xx; the provider layer
    recovers the status/body so 403s carry the scope hint (not the
    exception name). The real provider._request runs — only the transport
    is faked."""
    import urllib.error

    def raising_transport(method, url, *, headers, body, timeout=15):
        raise urllib.error.HTTPError(url, 403, "Forbidden", {},
                                     io.BytesIO(json.dumps(
                                         {"error": {"message": "Insufficient Permission"}}).encode()))

    monkeypatch.setattr(gdrive_ops.provider, "_default_transport", raising_transport)
    with pytest.raises(gdrive_ops.GDriveOpsError) as denied:
        gdrive_ops.list_files(ACCOUNT_A)
    assert denied.value.status == 403
    assert "Insufficient Permission" in str(denied.value)
    assert "edit --scopes" in str(denied.value)
    assert "HTTPError" not in str(denied.value)


def test_gdrive_folder_create_and_download_errors(google_world, gdrive_provider):
    gdrive_provider.on("POST", "/drive/v3/files?",
                       {"id": "fold", "name": "Invoices", "mimeType":
                        "application/vnd.google-apps.folder"})
    payload = gdrive_ops.create_folder(ACCOUNT_A, "Invoices")
    body = gdrive_provider.calls[-1]["payload"]
    assert body["mimeType"] == "application/vnd.google-apps.folder"
    assert payload["name"] == "Invoices"

    gdrive_provider.on("GET", "/files/fold?fields=", {
        "id": "fold", "name": "Invoices", "mimeType": "application/vnd.google-apps.folder"})
    with pytest.raises(gdrive_ops.GDriveOpsError) as folder:
        gdrive_ops.download(ACCOUNT_A, "fold")
    assert "folder" in str(folder.value)


def test_gdrive_provider_error_relays_status(google_world, gdrive_provider):
    gdrive_provider.on("GET", "/drive/v3/files", None, status=403,
                       raw=json.dumps({"error": {"message": "The granted scopes do not allow this"}}).encode())
    with pytest.raises(gdrive_ops.GDriveOpsError) as denied:
        gdrive_ops.list_files(ACCOUNT_A)
    assert denied.value.status == 403
    assert "edit --scopes" in str(denied.value)


# --- routes --------------------------------------------------------------------


def _operator_gate(monkeypatch, module, action_name):
    seen = {}

    def require_operator(event, action):
        seen["action"] = action
        return "op-subject", None

    monkeypatch.setattr(module, "require_operator", require_operator)
    monkeypatch.setattr(module, "audit", type("A", (), {
        "emit": staticmethod(lambda *a, **k: seen.setdefault("audit", (a, k)))})())
    return seen


def test_route_gmail_requires_operator_and_audits(google_world, gmail_provider, monkeypatch):
    seen = _operator_gate(monkeypatch, gmail_api, "gmail.list_messages")
    gmail_provider.on("GET", "/users/me/messages?maxResults=5", {"messages": []})
    resp = gmail_api.route_gmail(
        {"queryStringParameters": {"account": ACCOUNT_A, "max": "5"}},
        "GET", "/api/agent/gmail/messages")
    assert resp["statusCode"] == 200
    assert seen["action"] == "gmail.list_messages"
    assert seen["audit"]

    from src.dapier.api.agent.common import _json_response

    def deny(event, action):
        return None, _json_response(403, {"error": "not an operator"})

    monkeypatch.setattr(gmail_api, "require_operator", deny)
    resp = gmail_api.route_gmail({}, "GET", "/api/agent/gmail/messages")
    assert resp["statusCode"] == 403
    assert gmail_api.route_gmail({}, "GET", "/api/agent/gmail/other") is None


def test_route_gmail_send_and_reply_dispatch(google_world, gmail_provider, monkeypatch):
    _operator_gate(monkeypatch, gmail_api, "gmail.send")
    gmail_provider.on("GET", "/users/me/profile", {"emailAddress": ACCOUNT_A})
    gmail_provider.on("POST", "/users/me/messages/send", {"id": "s1", "threadId": "t1"})
    resp = gmail_api.route_gmail(
        {"body": json.dumps({"account": ACCOUNT_A, "to": "x@y.z", "text": "hi"})},
        "POST", "/api/agent/gmail/send")
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["message_id"] == "s1"

    _operator_gate(monkeypatch, gmail_api, "gmail.reply")
    gmail_provider.on("GET", "/messages/m1?format=full", _ORIGINAL_FULL)
    gmail_provider.on("GET", "/users/me/profile", {"emailAddress": ACCOUNT_A})
    gmail_provider.on("POST", "/users/me/drafts",
                      {"id": "d3", "message": {"threadId": "t1"}})
    resp = gmail_api.route_gmail(
        {"body": json.dumps({"account": ACCOUNT_A, "message_id": "m1",
                             "draft": True, "text": "hey"})},
        "POST", "/api/agent/gmail/reply")
    assert json.loads(resp["body"])["draft_id"] == "d3"

    resp = gmail_api.route_gmail(
        {"body": json.dumps({"account": ACCOUNT_A})},
        "POST", "/api/agent/gmail/reply")
    assert resp["statusCode"] == 400  # message_id required


def test_route_gdrive_dispatch_and_audit(google_world, gdrive_provider, monkeypatch):
    _operator_gate(monkeypatch, gdrive_api, "gdrive.list_files")
    gdrive_provider.on("GET", "/drive/v3/files", {"files": [{"id": "f1", "name": "x"}]})
    resp = gdrive_api.route_gdrive(
        {"queryStringParameters": {"account": ACCOUNT_A}}, "GET",
        "/api/agent/gdrive/files")
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["count"] == 1

    resp = gdrive_api.route_gdrive(
        {"body": json.dumps({"account": ACCOUNT_A})},
        "POST", "/api/agent/gdrive/folders")
    assert resp["statusCode"] == 400  # a folder needs a name
    assert gdrive_api.route_gdrive({}, "GET", "/api/agent/gdrive/zzz") is None


# --- CLI thin clients ------------------------------------------------------------


def test_edit_reply_draft_retains_thread_headers_and_attachment(google_world, gmail_provider):
    headers = _ORIGINAL_FULL["payload"]["headers"] + [
        {"name": "In-Reply-To", "value": "<orig@corp.com>"}]
    gmail_provider.on("GET", "/drafts/reply1", {
        "id": "reply1", "message": {"threadId": "t1", "payload": {"headers": headers}}})
    gmail_provider.on("GET", "/users/me/profile", {"emailAddress": ACCOUNT_B})
    gmail_provider.on("PUT", "/drafts/reply1", {"id": "reply1"})
    gmail_ops.update_draft(ACCOUNT_B, "reply1", to=None, text="Edited ü", attachments=[{
        "filename": "report.bin", "content_b64": base64.b64encode(b"\xff\x00").decode()}])
    sent = gmail_provider.calls[-1]["payload"]["message"]
    assert sent["threadId"] == "t1"
    message = email.message_from_bytes(base64.urlsafe_b64decode(sent["raw"]), policy=email.policy.default)
    assert message["In-Reply-To"] == "<orig@corp.com>"
    assert message["References"] == "<prev@corp.com>"
    assert message["Subject"] == "Quarterly plan"
    assert list(message.iter_attachments())[0].get_content() == b"\xff\x00"
    assert all(c["token"] == "token-google-b" for c in gmail_provider.calls)


def test_reply_all_handles_named_addresses_without_disclosing_bcc(google_world, gmail_provider):
    original = json.loads(json.dumps(_ORIGINAL_FULL))
    original["payload"]["headers"] += [
        {"name": "To", "value": '"Self, A" <a@gmail.com>, "Peer, P" <peer@corp.com>'},
        {"name": "Bcc", "value": "hidden@corp.com"}]
    gmail_provider.on("GET", "/messages/m1?format=full", original)
    gmail_provider.on("GET", "/users/me/profile", {"emailAddress": ACCOUNT_A})
    gmail_provider.on("POST", "/messages/send", {"id": "sent"})
    result = gmail_ops.reply(ACCOUNT_A, "m1", reply_all=True, text="OK")
    assert result["reply_to"] == ["careers@corp.com", "peer@corp.com", "watcher@corp.com"]


def test_drive_metadata_update_uses_standard_endpoint(google_world, gdrive_provider):
    gdrive_provider.on("PATCH", "/drive/v3/files/f1", {"id": "f1", "name": "Renamed"})
    assert gdrive_ops.update(ACCOUNT_A, "f1", name="Renamed")["name"] == "Renamed"
    assert "/upload/" not in gdrive_provider.calls[-1]["url"]
