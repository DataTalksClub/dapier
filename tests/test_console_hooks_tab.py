"""The Hooks tab is a webhook console: recent deliveries first, endpoint
cards beside them, a delivery detail dialog with Replay, and Send test
request — every action on an endpoint the CLI also drives (UI/CLI parity)."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src" / "web"
HTML = (WEB / "index.html").read_text()
HOOKS_JS = (WEB / "js" / "views" / "hooks.js").read_text()
TRIGGERS_JS = (WEB / "js" / "views" / "triggers.js").read_text()
CLI = (ROOT / "dapier_cli" / "commands" / "hooks.py").read_text()


def test_hooks_panel_has_deliveries_endpoints_and_dialogs():
    for marker in ('id="hook-deliveries"', 'id="hook-endpoints"', 'id="hooks-outcome-chips"',
                   'id="hook-delivery-dialog"', 'id="hook-delivery-replay"',
                   'id="hook-test-dialog"', 'id="hook-send-test"', 'id="new-hook"',
                   'id="hook-dialog"'):
        assert marker in HTML, marker
    assert 'id="hook-table"' not in HTML  # the old definitions-only table is gone


def test_hooks_view_owns_the_hook_actions():
    assert "hook-triggers" not in TRIGGERS_JS
    assert "renderHooks" in TRIGGERS_JS


def test_every_hooks_tab_endpoint_has_a_cli_counterpart():
    calls = {
        "/api/admin/hook-triggers/deliveries?": "/api/agent/hook-triggers/deliveries?",
        "/api/admin/hook-triggers/deliveries/${": "/api/agent/hook-triggers/deliveries/",
        "/api/admin/hook-triggers/test": "/api/agent/hook-triggers/test",
        "'/api/admin/hook-triggers'": "/api/agent/hook-triggers",
    }
    for console_path, agent_path in calls.items():
        assert console_path in HOOKS_JS, console_path
        assert agent_path in CLI, agent_path
    # Replay reuses the trigger inbox endpoint, which `dapier hooks replay` wraps.
    assert "/api/admin/triggers/inbox/" in HOOKS_JS
    assert "inbox_replay" in (ROOT / "dapier_cli" / "cli" / "hooks.py").read_text()
