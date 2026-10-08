"""Human workflow names (engine.naming): the generated "<trigger> → <actions>"
name, the ``name:`` override, the API fields, and ids assigned from names."""

import pytest

from src.dapier.api import designer_store
from src.dapier.api import overview
from src.dapier.engine import naming
from src.dapier.triggers import published_workflows


def _flow(trigger, actions, **extra):
    return {"id": "x", "trigger": trigger, "actions": actions, **extra}


AGENT_RELAY = _flow(
    {"connector": "webhook", "event": "request.received",
     "filters": {"hook": {"equals": "agent-redo"}}},
    [{"id": "run", "type": "agent", "prompt": "{body.prompt}"}])

DROPBOX_ON_UPLOAD = _flow(
    {"connector": "dropbox", "event": "file.created",
     "filters": {"path": {"prefix": "/_dtc_paperwork/income-invoices/"}}},
    [{"type": "dataops"}, {"type": "dropbox_delete"}])

MAILING_LIST_BACKUP = _flow(
    {"connector": "google-drive", "event": "file.created",
     "filters": {"mimeType": {"not_equals": "application/vnd.google-apps.folder"},
                 "poll": {"equals": "mailchimp-drive-backup"}}},
    [{"type": "drive_read_file"}, {"type": "s3_upload"}])


@pytest.mark.parametrize("workflow, expected", [
    (AGENT_RELAY, "Webhook agent-redo → Agent"),
    (_flow({"connector": "zoom", "event": "recording.completed"}, [{"type": "agent"}]),
     "Zoom recording completed → Agent"),
    (DROPBOX_ON_UPLOAD,
     "Dropbox file created in income-invoices → DataOps, delete from Dropbox"),
    (MAILING_LIST_BACKUP, "Drive file created → Upload to S3"),
    (_flow({"connector": "email", "event": "message.received",
            "filters": {"route": {"equals": "dropbox-inbox"}}},
           [{"type": "dropbox_upload"}]),
     "Email to dropbox-inbox → Upload to Dropbox"),
    (_flow({"connector": "email", "event": "message.received",
            "filters": {"from": {"equals": "billing@example.com"}}},
           [{"type": "dataops"}]),
     "Email from billing@example.com → DataOps"),
    # An opaque channel id is not a name: skipped.
    (_flow({"connector": "youtube", "event": "video.published",
            "filters": {"channel_id": {"equals": "UCDvErgK0j5ur3aLgn6U-LqQ"}}},
           [{"type": "slack"}]),
     "YouTube video published → Post to Slack"),
])
def test_generated_names(workflow, expected):
    assert naming.generated_name(workflow) == expected


def test_plumbing_is_skipped_and_long_chains_collapse():
    workflow = _flow(
        {"connector": "email", "event": "message.received",
         "filters": {"route": {"equals": "invoice"}}},
        [{"type": "code"},
         {"type": "condition", "then": [
             {"type": "date_time"}, {"type": "code"},
             {"type": "dropbox_upload"}, {"type": "dataops"}],
          "else": [{"type": "code"}, {"type": "render_html_to_pdf"}]}])
    assert naming.generated_name(workflow) == \
        "Email to invoice → Upload to Dropbox, DataOps +1 more"


def test_only_plumbing_still_names_the_steps():
    workflow = _flow({"connector": "webhook", "event": "request.received"},
                     [{"type": "code"}, {"type": "code"}])
    assert naming.generated_name(workflow) == "Webhook → Run code"


def test_duplicate_actions_collapse_and_same_connector_triggers_merge():
    hook = {"filters": {"hook": {"equals": "automator-telegram"}}, "connector": "telegram"}
    workflow = {"id": "x", "triggers": [
        {**hook, "event": "message.received"},
        {**hook, "event": "channel_post.received"}],
        "actions": [{"type": "condition", "then": [{"type": "slack"}]},
                    {"type": "condition", "then": [{"type": "slack"}]}]}
    assert naming.generated_name(workflow) == "Telegram automator-telegram → Post to Slack"


def test_mixed_connectors_share_a_filter_value():
    workflow = {"id": "x", "triggers": [
        {"connector": "email", "event": "message.received",
         "filters": {"route": {"equals": "todo"}}},
        {"connector": "telegram", "event": "message.received",
         "filters": {"hook": {"equals": "todo"}}}],
        "actions": [{"type": "sheets_append_row"}]}
    assert naming.generated_name(workflow) == "Email or Telegram todo → Add row to Sheets"


def test_names_are_deterministic_and_bounded():
    workflow = _flow({"connector": "email", "event": "message.received",
                      "filters": {"route": {"equals": "r" * 60}}},
                     [{"type": "dataops"}])
    first = naming.generated_name(workflow)
    assert first == naming.generated_name(dict(workflow))
    assert len(first) <= naming.MAX_NAME_LENGTH


def test_override_wins():
    assert naming.display_name({**AGENT_RELAY, "name": "  Redo agent tasks "}) == \
        ("Redo agent tasks", "custom")
    assert naming.display_name({**AGENT_RELAY, "name": ""}) == \
        ("Webhook agent-redo → Agent", "auto")
    assert naming.name_fields(AGENT_RELAY) == {
        "name": "Webhook agent-redo → Agent", "name_source": "auto"}


YAML = """\
{id_line}name: {name}
trigger:
  connector: webhook
  event: request.received
  filters:
    hook:
      equals: agent-redo
actions:
  - id: run
    type: agent
    prompt: hi
"""


def test_parse_validates_and_normalizes_the_name():
    parsed = designer_store.parse_workflow(YAML.format(id_line="id: relay\n", name="'  Redo   it '"))
    assert parsed["name"] == "Redo it"
    parsed = designer_store.parse_workflow(YAML.format(id_line="id: relay\n", name="''"))
    assert "name" not in parsed
    with pytest.raises(designer_store.WorkflowError, match="at most 80"):
        designer_store.parse_workflow(YAML.format(id_line="id: relay\n", name="n" * 81))
    with pytest.raises(designer_store.WorkflowError, match="name must be a string"):
        designer_store.parse_workflow(YAML.format(id_line="id: relay\n", name="[1, 2]"))
    # name sorts right after id in the canonical dump.
    assert list(designer_store.ordered_workflow(
        {"enabled": True, "name": "n", "id": "i"}))[:2] == ["id", "name"]


class StubTable:
    def __init__(self):
        self.items = {}

    def put_item(self, Item, **_):
        self.items[Item["workflow_id"]] = Item

    def get_item(self, Key):
        item = self.items.get(Key["workflow_id"])
        return {"Item": item} if item else {}

    def delete_item(self, Key):
        self.items.pop(Key["workflow_id"], None)

    def scan(self, **_):
        return {"Items": list(self.items.values())}


@pytest.fixture
def store(monkeypatch):
    monkeypatch.setenv(published_workflows.TABLE_ENV, "published-test")
    table = StubTable()
    monkeypatch.setattr(published_workflows, "get_table", lambda table_ref=None: table)
    return table


def test_save_without_an_id_slugs_the_generated_name(store):
    body = {"yaml": YAML.format(id_line="", name="''")}
    status, payload = designer_store.api_save(body)
    assert status == 200, payload
    assert payload["file"] == "webhook-agent-redo-agent.yaml"
    assert payload["name"] == "Webhook agent-redo → Agent"
    assert payload["name_source"] == "auto"
    # A second unnamed save of the same flow does not clobber the first.
    status, payload = designer_store.api_save(body)
    assert status == 200 and payload["file"] == "webhook-agent-redo-agent-2.yaml"
    status, payload = designer_store.api_save(body)
    assert payload["file"] == "webhook-agent-redo-agent-3.yaml"


def test_save_without_an_id_uses_the_override(store):
    status, payload = designer_store.api_save(
        {"yaml": YAML.format(id_line="", name="Redo agent tasks")})
    assert status == 200, payload
    assert payload["file"] == "redo-agent-tasks.yaml"
    assert payload["name"] == "Redo agent tasks" and payload["name_source"] == "custom"
    draft = published_workflows.get_draft("redo-agent-tasks")
    assert draft["workflow"]["id"] == "redo-agent-tasks"


def test_explicit_id_is_kept(store):
    status, payload = designer_store.api_save(
        {"yaml": YAML.format(id_line="id: agent-relay\n", name="Redo agent tasks")})
    assert status == 200 and payload["file"] == "agent-relay.yaml"


def test_list_get_and_overview_carry_name_fields(store):
    workflow = designer_store.parse_workflow(YAML.format(id_line="id: agent-relay\n", name="''"))
    published_workflows.publish(workflow)
    status, payload = designer_store.api_list()
    assert status == 200
    row = payload["workflows"][0]
    assert row["id"] == "agent-relay"
    assert row["name"] == "Webhook agent-redo → Agent" and row["name_source"] == "auto"
    # ?q= searches the name too.
    assert designer_store.api_list(q="agent-redo →")[1]["workflows"]
    status, payload = designer_store.api_get("agent-relay.yaml")
    assert payload["name"] == "Webhook agent-redo → Agent"
    assert payload["name_source"] == "auto"
    view = overview._workflow_view({**workflow, "name": "Redo"}, "agent-relay.yaml",
                                   published=True)
    assert view["name"] == "Redo" and view["name_source"] == "custom"
    assert view["id"] == "agent-relay"


# ---- CLI ----

from dapier_cli import commands as cli_commands  # noqa: E402
from dapier_cli import main as cli_main  # noqa: E402

LISTED = {"workflows": [
    {"id": "agent-relay", "source": "agent-relay.yaml", "enabled": True,
     "name": "Webhook agent-redo → Agent", "name_source": "auto"},
    {"id": "twin-a", "source": "twin-a.yaml", "enabled": False,
     "name": "Twin", "name_source": "custom"},
    {"id": "twin-b", "source": "twin-b.yaml", "enabled": True,
     "name": "twin", "name_source": "custom"},
], "git_sync": {"configured": False}}


@pytest.fixture
def isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("DAPIER_API_URL", raising=False)
    return tmp_path


def test_cli_list_shows_name_and_id_columns(monkeypatch, capsys):
    monkeypatch.setattr(cli_commands.api, "call", lambda *args, **kwargs: LISTED)
    assert cli_commands.workflows_list("https://api.example.test") == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == ["NAME", "ID", "STATE"]
    row = next(line for line in lines if "agent-relay" in line)
    assert row.startswith("Webhook agent-redo → Agent")
    assert row.index("Webhook") < row.index("agent-relay") < row.index("On")


def test_cli_resolves_ids_names_and_files(monkeypatch):
    calls = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        calls.append(path)
        return LISTED

    monkeypatch.setattr(cli_commands.api, "call", fake_call)
    resolve = cli_commands.resolve_workflow_ref
    assert resolve("u", "agent-relay.yaml") == "agent-relay.yaml"
    assert calls == []  # a file name needs no lookup
    assert resolve("u", "agent-relay") == "agent-relay.yaml"
    assert resolve("u", "webhook AGENT-redo → agent") == "agent-relay.yaml"
    with pytest.raises(cli_commands.WorkflowRefError, match="twin-a, twin-b"):
        resolve("u", "TWIN")
    with pytest.raises(cli_commands.WorkflowRefError, match="No workflow"):
        resolve("u", "nope")


def test_cli_commands_accept_a_name(isolated_home, monkeypatch, capsys):
    calls = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        calls.append((method, path))
        if path == "/api/agent/designer/workflows":
            return LISTED
        return {"workflow": {"id": "agent-relay"}, "name": "Webhook agent-redo → Agent",
                "name_source": "auto"}

    monkeypatch.setattr(cli_commands.api, "call", fake_call)
    assert cli_main.main(["workflows", "show", "Webhook agent-redo → Agent"]) == 0
    assert calls[-1] == ("GET", "/api/agent/designer/workflows/agent-relay.yaml")
    out = capsys.readouterr().out
    assert "Name: Webhook agent-redo → Agent (auto" in out
    assert "ID:   agent-relay" in out
    assert cli_main.main(["workflows", "show", "twin"]) == 4
    assert "pass the id instead" in capsys.readouterr().out


def test_cli_show_reports_a_custom_name(monkeypatch, capsys):
    monkeypatch.setattr(cli_commands.api, "call", lambda *args, **kwargs: {
        "workflow": {"id": "twin-a", "name": "Twin"}, "name": "Twin",
        "name_source": "custom"})
    assert cli_commands.workflows_show("u", "twin-a.yaml") == 0
    assert "Name: Twin (custom" in capsys.readouterr().out
