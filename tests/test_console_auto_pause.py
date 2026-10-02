"""The auto-pause trip wire stays visible — and undoable — on the surfaces.

The engine's pause (tests/test_auto_pause.py pins the domain) is only as
good as the operator's ability to see it and undo it: the overview payload
carries ``auto_paused`` plus the live streak, the console's Workflows rows
show the paused state with a Resume button, and the CLI's list and overview
mark paused workflows. The resume verb is re-enabling on every surface —
the console's Resume posts the same toggle endpoint `dapier workflows on`
drives (designer_store.api_toggle clears the flag and zeroes the streak),
so there is no second route to keep in sync.
"""
import re
from pathlib import Path

from dapier_cli import commands

WEB = Path(__file__).resolve().parents[1] / "src" / "web"


def _read(*parts):
    return Path(*parts).read_text()


# ---- CLI ----


def test_cli_list_marks_auto_paused_workflows(monkeypatch, capsys):
    def fake_call(api_url, method, path, body=None, **kwargs):
        assert (method, path) == ("GET", "/api/agent/designer/workflows")
        return {"workflows": [
            {"id": "flaky", "source": "flaky.yaml", "enabled": True,
             "auto_paused": True, "connector": "email", "event": "message.received"},
            {"id": "healthy", "source": "healthy.yaml", "enabled": True,
             "connector": "email", "event": "message.received"},
        ]}

    monkeypatch.setattr(commands.api, "call", fake_call)
    assert commands.workflows_list("https://api.example.test") == 0
    out, _ = capsys.readouterr()
    assert "flaky.yaml" in out and "(auto-paused)" in out
    # The healthy row stays a plain On — the marker rides the state column.
    healthy_row = next(line for line in out.splitlines() if "healthy.yaml" in line)
    assert "auto-paused" not in healthy_row
    assert "(auto-paused)" not in out.split("healthy.yaml")[1]


def test_cli_overview_marks_auto_paused_workflows(capsys):
    commands.print_overview({
        "service": "dapier",
        "region": "eu-west-1",
        "workflows": [
            {"id": "flaky", "enabled": True, "auto_paused": True},
            {"id": "healthy", "enabled": True},
        ],
        "connections": [],
    })
    out, _ = capsys.readouterr()
    assert "flaky" in out and "(auto-paused)" in out


# ---- Console ----


def test_workflows_rows_show_the_paused_state():
    js = _read(WEB, "js", "views", "overview.js")
    assert "auto_paused" in js, \
        "the Workflows rows must read the overview payload's auto_paused flag"
    assert "'auto-paused'" in js, \
        "a paused workflow's State cell must say Auto-paused, not On"
    assert re.search(r'button[^>]+class="dk-button dk-button--secondary workflow-resume"', js), \
        "each paused row needs a Resume button"


def test_resume_drives_the_same_toggle_endpoint_as_the_cli():
    js = _read(WEB, "js", "views", "overview.js")
    # The Resume handler PUTs the workflow resource with enabled:true — the
    # exact call the console toggle and `dapier workflows on` make; api_toggle
    # is the resume verb (clears the flag, zeroes the streak).
    resume_block = js[js.index("workflow-resume\""):] if "workflow-resume\"" in js else ""
    assert resume_block, "the Resume click handler is missing"
    assert "designer/workflows/${encodeURIComponent(button.dataset.file)}" in resume_block, \
        "Resume must call PUT /api/admin/designer/workflows/<file>"
    assert "{ enabled: true }" in resume_block, \
        "Resume must re-enable — api_toggle on enable is the resume verb"


def test_row_click_guard_ignores_the_resume_button():
    js = _read(WEB, "js", "views", "overview.js")
    assert ".workflow-resume" in js, "the guard must keep row-open off the Resume button"


def test_status_line_knows_the_auto_paused_kind():
    js = _read(WEB, "js", "format.js")
    assert "'auto-paused'" in js, \
        "statusLine must render the paused state as an attention (err) pill"
