"""The console designer's Test run panel wires the API's ``strict`` flag.

``POST /api/admin/designer/workflows/test`` accepts ``strict`` — dry-run
rendered-input warnings fail their step instead of riding along (the CLI
exposes it as ``dapier workflows test --strict``) — but the panel's runTest
sent only ``{event, workflow, execute}``, leaving the flag unreachable from
the console. This pins the wiring in both the source (designer/src/App.tsx)
and the shipped bundle (src/web/designer.js, rebuilt by
``make designer-console``), so it cannot silently drop out of either.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_TSX = ROOT / "designer" / "src" / "App.tsx"
BUNDLE_JS = ROOT / "src" / "web" / "designer.js"

# runTest's request body: from its JSON.stringify open brace to the body's
# close (non-greedy: the strict spread's own braces sit inside).
RUN_TEST_BODY_RE = re.compile(
    r"async function runTest\(.*?JSON\.stringify\(\{(?P<body>.*?)\}\)", re.S)


def run_test_body(path):
    match = RUN_TEST_BODY_RE.search(path.read_text())
    assert match, f"runTest's request body not found in {path.name}"
    return match.group("body")


def test_the_test_run_panel_sends_the_strict_flag():
    for path in (APP_TSX, BUNDLE_JS):
        body = run_test_body(path)
        assert "event" in body and "workflow" in body and "execute" in body, (
            f"{path.name} runTest no longer sends the test-run request")
        assert "strict" in body, (
            f"{path.name} runTest dropped the strict flag — the Test run "
            "panel must keep reaching the API's strict dry-run")
