"""Console Agents page: compact run head and independently scrolling panes.

The selected run reads like an Emails message: a heading-size title held to
two lines, ONE meta line (status · workflow · when · duration), and a
Details disclosure for the exact times, engine and ids. On desktop the list
and the detail each scroll inside a viewport-tall workspace."""

import pathlib
import re

WEB = pathlib.Path("src/web")
JS = (WEB / "js/views/agents.js").read_text()
CSS = (WEB / "app.css").read_text()


def _rule(selector, css=CSS):
    match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert match, selector
    return match.group(1)


def _desktop_block():
    start = CSS.index("@media (min-width: 861px) {\n  /* Two columns that can never overlap")
    return CSS[start:CSS.index("\n}\n", start)]


def test_detail_head_is_title_meta_line_then_details():
    head = JS[JS.index('<div class="agent-detail-head">'):JS.index('<section class="agent-result"')]
    assert '<h2 tabindex="-1"' in head
    assert head.count('<p class="agent-detail-meta">') == 1
    meta = head[head.index('agent-detail-meta'):head.index('<details')]
    assert "${badge(task)}" in meta
    assert 'href="/workflows/${encodeURIComponent(task.workflow' in meta
    assert "duration(task)" in meta
    # Everything else sits behind one Details disclosure, before the result.
    assert '<details class="agent-technical"><summary>Details</summary>' in head
    for label in ("Finished", "Duration", "Engine", "Task ID", "Required capabilities",
                  "Workspace", "Exit code", "Completion email sent"):
        assert f"<dt>{label}</dt>" in head, label
    # No second "Run details" block and no label/value summary rows left over.
    assert "Run details" not in JS
    assert "run-summary" not in JS
    # Back to runs, Logs and the stream switch are still there.
    assert JS.count('class="agent-back') == 2
    assert 'class="agent-log-section"' in JS and "data-agent-stream" in JS


def test_title_is_clamped_heading_size():
    h2 = _rule(".agent-detail-head h2")
    assert "-webkit-line-clamp: 2" in h2
    assert "var(--dk-text-lg)" in h2
    row_title = _rule(".agent-run-line strong")
    assert "text-overflow: ellipsis" in row_title and "white-space: nowrap" in row_title


def test_desktop_panes_scroll_independently_in_a_viewport_frame():
    block = _desktop_block()
    assert re.search(r'body\[data-view="agents"\] main \{[^}]*height: 100dvh', block)
    assert re.search(r'section\.view\[data-page="agents"\]\.active \{[^}]*flex: 1; min-height: 0', block)
    assert re.search(r"\.agent-workspace \{[^}]*flex: 1", block)
    assert re.search(r"#agent-run-detail \{[^}]*overflow-y: auto", block)
    assert "overflow-y: auto" in _rule(".agent-run-list")
    # The old sticky list with a viewport max-height is gone: the frame owns height.
    assert "position: sticky" not in _rule(".agent-run-sidebar")


def test_toolbar_status_cannot_overlap_the_chips():
    block = _desktop_block()
    assert re.search(r"\.agent-toolbar \{[^}]*display: grid; grid-template-columns: minmax\(0, 1fr\) auto", block)
    strip = _rule(".page-tools .agent-workers-strip, .agent-workers-strip")
    assert "min-width: 0" in strip and "white-space: normal" in strip


def test_phone_keeps_one_pane_at_a_time():
    start = CSS.index("/* One pane at a time: the list, or the selected run with Back. */")
    phone = CSS[start:CSS.index("\n}\n", start)]
    assert ".agent-workspace.has-selection .agent-run-sidebar { display: none; }" in phone
    assert ".agent-back { display: inline-flex;" in phone
