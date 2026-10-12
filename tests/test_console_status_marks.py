"""Ok/fail status marks are emoji shapes, not color-only dots.

The Latest run column (and every other status pill) used an 8px green or
red circle. Those two colors collapse for colorblind operators; a check
versus a cross does not.
"""
import re
from pathlib import Path

CSS = Path("src/web/app.css").read_text(encoding="utf-8")


def test_ok_fail_status_marks_are_larger_emojis():
    match = re.search(
        r"/\* --- status .*?---.*?\*/\n(.*?)(?=\n/\* ---)", CSS, flags=re.S)
    assert match, "status mark block missing from app.css"
    block = match.group(1)
    assert 'content: "✅"' in block
    assert 'content: "❌"' in block
    assert "font-size: 1.35em" in block
    assert "width: 8px" not in block
    assert "border-radius: var(--dk-radius-full)" not in block


def test_format_js_still_emits_the_status_dot_span():
    """CSS paints the glyph onto .status-dot; the markup stays one span so
    every view (workflow list, runs, home, audit) picks the marks up."""
    js = Path("src/web/js/format.js").read_text(encoding="utf-8")
    assert "status-dot" in js
    assert "function statusDot" in js
    assert "function statusLine" in js
