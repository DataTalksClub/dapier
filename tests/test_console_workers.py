"""Console Workers page: how a worker row reads.

The row shaping in src/web/js/views/workers.js is pure; these tests run it
in a JS engine with the realistic long values that used to break the table
(FQDN hostnames, agent task keys, missing capability lists)."""

import pathlib

import pytest

SOURCE = pathlib.Path("src/web/js/views/workers.js").read_text()
INDEX = pathlib.Path("src/web/index.html").read_text()
NOW_MS = 1_760_000_000_000


@pytest.fixture(scope="module")
def js():
    from py_mini_racer import MiniRacer
    start = SOURCE.index("/* --- row shaping")
    end = SOURCE.index("/* --- end row shaping")
    ctx = MiniRacer()
    ctx.eval(SOURCE[start:end])
    return ctx


def test_identity_uses_short_hostname_and_pid(js):
    worker = {
        "worker_id": "ip-172-31-44-118.eu-west-1.compute.internal-48213-8ae4d1c2",
        "hostname": "ip-172-31-44-118.eu-west-1.compute.internal",
        "pid": 48213,
    }
    assert js.call("workerIdentity", worker) == {"name": "ip-172-31-44-118", "sub": "pid 48213 · 8ae4d1c2"}


def test_identity_falls_back_to_the_worker_id(js):
    assert js.call("workerIdentity", {"worker_id": "mini-1-77-0badf00d"}) == {"name": "mini-1", "sub": "pid 77 · 0badf00d"}
    assert js.call("workerIdentity", {"worker_id": "odd"}) == {"name": "odd", "sub": ""}


def test_task_shows_the_workflow_not_the_whole_key(js):
    running = js.call("workerTask", {"current_task_id": "agent:email-trigger-agents:8ae4d1c2-77aa:run"})
    assert running == {"name": "email-trigger-agents", "sub": "running now", "id": "agent:email-trigger-agents:8ae4d1c2-77aa:run"}
    last = js.call("workerTask", {"last_task_id": "agent:digest:e1:run", "last_status": "succeeded"})
    assert last["name"] == "digest" and last["sub"] == "last · succeeded"
    assert js.call("workerTask", {}) == {"name": "", "sub": "idle", "id": ""}


def test_capabilities_are_a_list_or_a_dash_never_none(js):
    assert js.call("capabilityText", {"capabilities": ["browser", "chrome"]}) == "browser, chrome"
    assert js.call("capabilityText", {"capabilities": []}) == "—"
    assert js.call("capabilityText", {"capabilities": None}) == "—"
    assert js.call("capabilityText", {}) == "—"


def test_relative_times(js):
    now_s = NOW_MS // 1000
    assert js.call("agoText", now_s - 20, NOW_MS) == "just now"
    assert js.call("agoText", now_s - 190, NOW_MS) == "3m ago"
    assert js.call("agoText", now_s - 5 * 3600, NOW_MS) == "5h ago"
    assert js.call("agoText", now_s - 3 * 86400, NOW_MS) == "3d ago"
    assert js.call("agoText", None, NOW_MS) == ""


def test_workspace_shows_its_tail(js):
    assert js.call("workspaceText", "/home/ubuntu/dapier-worker/workspaces/email-trigger-agents") == "…/email-trigger-agents"
    assert js.call("workspaceText", "/srv/w") == "/srv/w"
    assert js.call("workspaceText", "") == "—"


def test_engine_and_capabilities_render_as_separate_items():
    """The old cell glued capabilities and engine into one run of text
    ("Noneclaude"); the engine is now its own badge."""
    assert 'class="dk-badge worker-engine"' in SOURCE
    assert 'class="worker-capabilities"' in SOURCE
    assert "|| 'None'" not in SOURCE


def test_table_has_fixed_columns():
    css = pathlib.Path("src/web/app.css").read_text()
    assert ".workers-table table { table-layout: fixed; }" in css
    assert 'id="workers-wrap" class="table-wrap workers-table"' in INDEX
    assert "<th>Engine</th>" in INDEX
