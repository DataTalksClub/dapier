"""Concrete Zapier mappings, provider requests, and checkpointed file chains.

Provider calls are replaced with fakes; no test uploads files or appends live rows.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import pytest
import yaml

from src.dapier.connectors import registry
from src.dapier.connectors import drive
from src.dapier.api.designer_store import parse_workflow
from src.dapier.engine import logic
from src.dapier.engine.actions import date_time, dropbox, s3, sheets, slack, dataops
from src.dapier.engine.actions.code import run_code
from src.dapier.engine.actions.templating import render
from src.dapier.triggers import poll_triggers

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "workflows"


def workflow(name):
    return yaml.safe_load((CONFIG / f"{name}.yaml").read_text())


def fixture(name):
    return json.loads((CONFIG / "zapier" / "fixtures" / f"{name}.json").read_text())


@pytest.mark.parametrize(
    "name",
    [
        "invoice-intake",
        "todo-intake",
        "telegram-todo",
        "youtube-slack",
        "dropbox_on_upload",
        "mailing-list-backup",
    ],
)
def test_migration_configs_validate_and_remain_inactive(name):
    text = (CONFIG / f"{name}.yaml").read_text()
    parsed = parse_workflow(text)
    assert parsed["id"] == name
    assert parsed["enabled"] is False
    assert any(
        tag.startswith("zapier-") and tag != "zapier-migration"
        for tag in parsed["tags"]
    )

    def validate(steps):
        for step in steps:
            if step["type"] == "condition":
                validate(step.get("then", []))
                validate(step.get("else", []))
            else:
                registry.validate_action_chain([step])

    validate(parsed["actions"])


@pytest.mark.parametrize("name", ["mailchimp-drive-backup", "invoice-landing"])
def test_poll_targets_survive_save_validation(name):
    body = json.loads((CONFIG / "zapier" / "triggers" / f"{name}.json").read_text())
    item = poll_triggers.build_item(body, "operator")
    assert not item["enabled"]
    assert item["expression"] == "rate(2 minutes)"
    view = poll_triggers.public_view(item)
    if name == "mailchimp-drive-backup":
        assert view["folder_id"] == "1-MAoAuQbny7FK9UQuk8800zT8M8TOQ59"
        assert view["drive_id"] == "0AJbu0ZbG97XkUk9PVA"
    else:
        assert view["path"] == "/_dtc_paperwork/invoices-landing"


def test_email_invoice_upload_keeps_bytes_and_utc_date_filename(monkeypatch):
    event = fixture("email")
    actions = workflow("invoice-intake")["actions"]
    date = date_time.run_date_time(actions[0], event)
    assert date["formatted"] == "2026-09-30"  # preceding UTC day
    monkeypatch.setattr(dropbox, "_dropbox_connection", lambda _id: {})
    monkeypatch.setattr(
        dropbox.tokens, "get_access_token", lambda *a, **kw: ("token", {})
    )
    monkeypatch.setattr(dropbox.base, "_s3_body", lambda ref: b"unchanged PNG bytes")
    calls = []

    def transport(method, url, **kw):
        calls.append(kw)
        return 200, b"{}"

    output = dropbox.run_dropbox_upload(
        actions[1], event, steps={"email-date": {"output": date}}, transport=transport
    )
    assert output["uploaded"] == ["/_dtc_paperwork/invoices/2026-09-30-Example.pdf"]
    assert calls[0]["body"] == b"unchanged PNG bytes"  # suffix is not a conversion
    arg = json.loads(calls[0]["headers"]["dropbox-api-arg"])
    assert arg["mode"] == "add" and arg["autorename"] is False


def test_ambiguous_email_attachments_fail_before_upload(monkeypatch):
    event = fixture("email")
    event["data"]["attachments"] *= 2
    monkeypatch.setattr(dropbox, "_dropbox_connection", lambda _id: {})
    monkeypatch.setattr(
        dropbox.tokens, "get_access_token", lambda *a, **kw: ("token", {})
    )
    with pytest.raises(ValueError, match="exactly one"):
        dropbox.run_dropbox_upload(workflow("invoice-intake")["actions"][1], event)


def test_upload_filename_override_cannot_collapse_multiple_files(monkeypatch):
    event = fixture("email")
    event["data"]["attachments"] *= 2
    monkeypatch.setattr(dropbox, "_dropbox_connection", lambda _id: {})
    monkeypatch.setattr(
        dropbox.tokens, "get_access_token", lambda *a, **kw: ("token", {})
    )
    with pytest.raises(ValueError, match="selecting a single"):
        dropbox.run_dropbox_upload(
            {
                "connection_id": "dropbox",
                "folder": "/archive",
                "filename": "single.pdf",
            },
            event,
        )


def test_overwrite_and_first_attachment_are_explicit(monkeypatch):
    event = fixture("email")
    event["data"]["attachments"] *= 2
    monkeypatch.setattr(dropbox, "_dropbox_connection", lambda _id: {})
    monkeypatch.setattr(
        dropbox.tokens, "get_access_token", lambda *a, **kw: ("token", {})
    )
    monkeypatch.setattr(dropbox.base, "_s3_body", lambda ref: b"bytes")
    args = []

    def transport(method, url, **kw):
        args.append(json.loads(kw["headers"]["dropbox-api-arg"]))
        return 200, b"{}"

    dropbox.run_dropbox_upload(
        {
            "connection_id": "dropbox",
            "folder": "/{subject}",
            "filename": "{steps.date.output.formatted}.pdf",
            "overwrite": "true",
            "autorename": "false",
            "attachment_selection": "first",
        },
        event,
        transport=transport,
        steps={"date": {"output": {"formatted": "2026-10-02"}}},
    )
    assert args == [
        {
            "path": "/Example/2026-10-02.pdf",
            "mode": "overwrite",
            "autorename": False,
            "mute": False,
        }
    ]


@pytest.mark.parametrize(
    "source,expected",
    [
        ("2026-09-30T23:30:00-05:00", "2026-10-01"),
        ("Thu, 01 Oct 2026 00:30:00 +0200", "2026-09-30"),
    ],
)
def test_offset_dates_convert_to_utc(source, expected):
    assert (
        date_time.run_date_time({"value": source, "timezone": "UTC"}, {})["formatted"]
        == expected
    )


@pytest.mark.parametrize(
    "instant,expected",
    [
        ("2026-09-30T04:30:00+00:00", "2026-09-29T23:30:00-05:00"),
        ("2026-12-01T05:30:00+00:00", "2026-11-30T23:30:00-06:00"),
    ],
)
def test_email_todo_uses_processing_clock_and_configurable_dst(instant, expected):
    event = fixture("email")
    event["data"]["route"] = "todo"
    clock = workflow("todo-intake")["actions"][0]["else"][0]
    with patch.object(date_time, "datetime") as cls:
        cls.now.return_value = datetime.fromisoformat(instant)
        output = date_time.run_date_time(clock, event)
    assert output["iso"] == expected
    normalize = run_code(workflow("todo-intake")["actions"][0]["else"][1], event)
    steps = {
        "append-task.else.processing-time": {"output": output},
        "append-task.else.email-fields": {"output": normalize},
    }
    row = sheets._rows_from_action(
        workflow("todo-intake")["actions"][0]["else"][2], event, steps
    )
    assert row == [
        [expected, 'Process email "Example" from Alice <alice@example.com>', "", "NEW"]
    ]


def test_empty_mapped_date_does_not_fall_back_to_today():
    with pytest.raises(ValueError, match="rendered empty"):
        date_time.run_date_time({"value": "{missing}"}, {"data": {}})


def test_naive_source_date_is_rejected():
    with pytest.raises(ValueError, match="timezone offset"):
        date_time.run_date_time({"value": "2026-10-02T12:00:00"}, {})


def test_original_telegram_bridge_preserves_date_text_and_blank_notes():
    event = fixture("telegram-webhook")
    event["occurred_at"] = "2027-01-01T00:00:00Z"
    row = sheets._rows_from_action(workflow("telegram-todo")["actions"][0], event)
    assert row == [["2026-10-02", "Example task", "", "NEW"]]


def test_numeric_worksheet_selection_survives_tab_rename(monkeypatch):
    action = workflow("telegram-todo")["actions"][0]
    monkeypatch.setattr(sheets, "_sheets_connection", lambda _id: {})
    monkeypatch.setattr(
        sheets.tokens, "get_access_token", lambda *a, **kw: ("token", {})
    )
    calls = []

    def transport(method, url, **kw):
        calls.append((method, url, kw))
        if method == "GET":
            return (
                200,
                json.dumps(
                    {"sheets": [{"properties": {"sheetId": 0, "title": "Tasks 'new'"}}]}
                ).encode(),
            )
        return 200, b'{"updates":{"updatedRows":1}}'

    sheets.run_sheets_append_row(
        action, fixture("telegram-webhook"), transport=transport
    )
    from urllib.parse import unquote

    assert unquote(calls[1][1]).endswith(
        "/values/'Tasks ''new''':append?valueInputOption=USER_ENTERED&insertDataOption=INSERT_ROWS"
    )
    assert json.loads(calls[1][2]["body"])["values"] == [
        ["2026-10-02", "Example task", "", "NEW"]
    ]


def test_shared_drive_request_includes_drive_scope_and_pagination():
    calls = []

    def transport(method, url, **kw):
        calls.append(parse_qs(urlparse(url).query))
        return (
            200,
            json.dumps(
                {
                    "files": [{"id": str(len(calls))}],
                    **({"nextPageToken": "next"} if len(calls) == 1 else {}),
                }
            ).encode(),
        )

    files = drive._list_folder_files(
        "token", "folder", drive_id="shared", transport=transport
    )
    assert len(files) == 2
    assert calls[0]["driveId"] == ["shared"] and calls[0]["corpora"] == ["drive"]
    assert calls[0]["includeItemsFromAllDrives"] == ["true"]
    assert "nextPageToken" in calls[0]["fields"][0]
    assert calls[1]["pageToken"] == ["next"]


def test_backup_preserves_title_bytes_and_explicit_mimetype(monkeypatch):
    event = fixture("drive")
    action = workflow("mailing-list-backup")["actions"][1]
    monkeypatch.setattr(s3, "_aws_config", lambda _action: {"access_key_id": "key", "secret_access_key": "secret"})
    monkeypatch.setattr(s3.base, "_s3_body", lambda ref: b"backup bytes")
    client = type(
        "FakeS3", (), {"put_object": lambda self, **kw: setattr(self, "request", kw)}
    )()
    steps = {"download-file": {"output": {"bucket": "staged", "key": "file"}}}
    s3.run_s3_upload(action, event, steps=steps, s3_client=client)
    assert client.request == {
        "Bucket": "datatalks-mailchimp-backup",
        "Key": "Mailchimp backup (October).zip",
        "Body": b"backup bytes",
        "ContentType": "none",
    }
    s3.run_s3_upload(
        {**action, "omit_content_type": True}, event, steps=steps, s3_client=client
    )
    assert "ContentType" not in client.request


def test_youtube_slack_request_preserves_channel_identity_and_message_order(
    monkeypatch,
):
    calls = []
    monkeypatch.setattr(slack, "_token_for", lambda _action: "token")

    def post(url, body, **kw):
        calls.append(body)
        return {"ok": True, "channel": body["channel"], "ts": "1"}

    monkeypatch.setattr(slack.base, "_json_request", post)
    slack.run_slack(workflow("youtube-slack")["actions"][0], fixture("youtube"))
    assert calls == [
        {
            "channel": "C01BQC114P2",
            "text": "New video on our YouTube channel!\n\nExample video\n\nLink: https://www.youtube.com/watch?v=example",
            "username": "YouTube",
            "unfurl_links": True,
            "unfurl_media": True,
            "link_names": True,
            "reply_broadcast": False,
        }
    ]


@pytest.mark.parametrize(
    "filename", ["deepseek.pdf", "image.png", "notes", "archive.tar.gz"]
)
def test_landing_chain_preserves_extension_once_and_reads_archived_path(
    filename, monkeypatch
):
    event = fixture("dropbox")
    event["data"]["path"] = f"/_dtc_paperwork/invoices-landing/{filename}"
    actions = workflow("dropbox_on_upload")["actions"]
    calls = []

    def runner(action, event, workflow_id, steps=None):
        if action["type"] == "date_time":
            return {"formatted": "2026-10-02"}
        if action["type"] == "dropbox_move":
            src = render(action["from_path"], event, steps)
            dst = render(action["to_path"], event, steps)
            calls.append((src, dst))
            return {"moved": dst, "item": {"path": dst}}
        calls.append(("dataops", render(action["path"], event, steps)))
        return {"ok": True}

    logic.run_chain("dropbox_on_upload", actions, event, runner)
    renamed = f"/_dtc_paperwork/invoices-landing/2026-10-02-{filename}"
    archived = f"/_dtc_paperwork/invoices/2026-10-02-{filename}"
    assert calls == [
        (event["data"]["path"], renamed),
        (renamed, archived),
        ("dataops", archived),
    ]


def test_dataops_downloads_the_rendered_archived_path(monkeypatch):
    event = fixture("dropbox")
    action = workflow("dropbox_on_upload")["actions"][-1]
    path = "/_dtc_paperwork/invoices/2026-10-02-deepseek.pdf"
    calls = []
    monkeypatch.setattr(dataops.dropbox, "_dropbox_connection", lambda _id: {})
    monkeypatch.setattr(
        dataops.tokens, "get_access_token", lambda *a, **kw: ("token", {})
    )
    monkeypatch.setattr(
        dataops.dropbox,
        "_dropbox_download",
        lambda token, path: (calls.append(path) or b"pdf"),
    )
    monkeypatch.setattr(dataops, "_stage_document", lambda *a: "s3://staged/file")
    body = dataops._intake_body(
        action, event, steps={"move-file": {"output": {"item": {"path": path}}}}
    )
    assert calls == [path]
    assert body["documents"][0]["filename"] == "2026-10-02-deepseek.pdf"
    assert body["recipientRoute"] == "invoice"
    assert body["documents"][0]["kind"] == "attachment"
    body = dataops._intake_body(
        {**action, "recipient_route": "receipts"}, event,
        steps={"move-file": {"output": {"item": {"path": path}}}},
    )
    assert body["recipientRoute"] == "receipts"


def test_empty_landing_folder_seed_allows_first_future_file(monkeypatch):
    from src.dapier.connectors import dropbox as source

    monkeypatch.setattr(source, "_dropbox_poll_files", lambda *a, **kw: [])
    item = {"connection_id": "dropbox", "path": "/_dtc_paperwork/invoices-landing"}
    files, cursor = source._dropbox_poll_fetch(item)
    assert not files and cursor is not None
    monkeypatch.setattr(
        source,
        "_dropbox_poll_files",
        lambda *a, **kw: [{"id": "new", "modified": "2026-10-02T12:00:00Z"}],
    )
    files, _ = source._dropbox_poll_fetch(item, cursor)
    assert [file["id"] for file in files] == ["new"]


def test_full_preview_formats_email_date_and_preserves_code_source():
    from src.dapier.engine import dryrun

    report = dryrun.test_run(workflow("invoice-intake"), fixture("email"), strict=True)
    assert all(step["ok"] for step in report["steps"])
    assert report["steps"][1]["rendered_input"]["filename"] == "2026-09-30-Example.pdf"
    code = {"id": "example", "type": "code", "code": "output = {'date': input['date']}"}
    assert (
        dryrun.test_run(
            {
                "id": "code-preview",
                "actions": [code],
                "trigger": {"connector": "email", "event": "message.received"},
            },
            fixture("email"),
        )["steps"][0]["rendered_input"]["code"]
        == code["code"]
    )


@pytest.mark.parametrize(
    "name",
    [
        "invoice-intake",
        "todo-intake",
        "telegram-todo",
        "youtube-slack",
        "dropbox_on_upload",
        "mailing-list-backup",
    ],
)
def test_cli_and_console_api_save_same_configuration_as_draft(name, monkeypatch):
    from dapier_cli import api
    from dapier_cli.commands.workflows import workflows_save
    from src.dapier.api import designer_store
    from src.dapier.triggers import published_workflows

    class Store:
        def __init__(self):
            self.items = {}

        def get_item(self, Key):
            return (
                {"Item": self.items[Key["workflow_id"]]}
                if Key["workflow_id"] in self.items
                else {}
            )

        def put_item(self, Item):
            self.items[Item["workflow_id"]] = Item

        def scan(self, **kw):
            return {"Items": list(self.items.values())}

    table = Store()
    monkeypatch.setenv(published_workflows.TABLE_ENV, "drafts-test")
    monkeypatch.setattr(published_workflows, "get_table", lambda *a, **kw: table)
    monkeypatch.setattr(
        designer_store,
        "_sync_youtube",
        lambda *a: pytest.fail("draft must not activate YouTube"),
    )
    text = (CONFIG / f"{name}.yaml").read_text()

    def call(url, method, path, body, **kw):
        assert method == "PUT" and path == "/api/agent/designer/workflows"
        assert body == {"yaml": text}
        status, response = designer_store.api_save(body, operator="operator")
        assert status == 200 and response["published"] is False
        return response

    monkeypatch.setattr(api, "call", call)
    assert (
        workflows_save("https://example.test", str(CONFIG / f"{name}.yaml"), None) == 0
    )
    assert published_workflows.get_item(name) is None
    assert published_workflows.get_draft(name)["workflow"] == parse_workflow(text)
    status, response = designer_store.api_save(
        {"yaml": text}, operator="console-operator"
    )
    assert status == 200 and response["published"] is False
    assert published_workflows.get_draft(name)["workflow"] == parse_workflow(text)


@pytest.mark.parametrize("native", [False, True])
def test_todo_chain_selects_email_intake_and_native_confirmation(native):
    event = fixture("email")
    event["data"]["route"] = "todo"
    if native:
        event["connector"] = "telegram"
        event["data"] = {"hook": "todo", "text": "/todo Example task"}
    calls = []

    def runner(action, event, workflow_id, steps=None):
        if action["type"] == "date_time":
            return {"iso": "2026-10-02T10:00:00-05:00"}
        if action["type"] == "code":
            return run_code(action, event)
        if action["type"] == "sheets_append_row":
            calls.append(("sheet", sheets._rows_from_action(action, event, steps)))
        elif action["type"] == "dataops":
            calls.append(("dataops", None))
        elif action["type"] == "telegram_send":
            calls.append(("confirmation", render(action["text"], event, steps)))
        return {"ok": True}

    logic.run_chain("todo-intake", workflow("todo-intake")["actions"], event, runner)
    if native:
        assert calls == [
            ("sheet", [["2026-09-30", "Example task", "", "NEW"]]),
            ("confirmation", 'Done! Saved "Example task" to the todo list.'),
        ]
    else:
        assert calls == [
            (
                "sheet",
                [
                    [
                        "2026-10-02T10:00:00-05:00",
                        'Process email "Example" from Alice <alice@example.com>',
                        "",
                        "NEW",
                    ]
                ],
            ),
            ("dataops", None),
        ]
