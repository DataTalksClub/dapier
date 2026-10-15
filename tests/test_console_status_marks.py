"""Status marks differ by shape as well as colour, and follow the theme.

The Latest run column (and every other status pill) once used an 8px green
or red circle — colours that collapse for colourblind operators — then
emoji, whose fixed artwork does not remap with the theme. Each state now
has its own small stroke glyph (check, cross, progress, warning, dashed
off), drawn as a CSS mask in the state's dakit text role.
"""
import re
from pathlib import Path

CSS = Path("src/web/app.css").read_text(encoding="utf-8")


def _block():
    match = re.search(r"/\* Status marks:.*?\.status\.off \{[^}]*\}", CSS, flags=re.S)
    assert match, "status mark block missing from app.css"
    return match.group(0)


def test_marks_are_theme_painted_svg_masks_not_emoji():
    block = _block()
    for emoji in ("✅", "❌", "🔵", "⚠", "○"):
        assert emoji not in CSS, emoji
    assert "mask: var(--status-mark)" in block and "background: currentColor" in block
    assert "content:" not in block
    for state, role in (("ok", "--dk-success-text"), ("err", "--dk-danger-text"),
                        ("run", "--dk-info-text"), ("warn", "--dk-warning-text")):
        rule = re.search(rf"\.status\.{state} \.status-dot \{{([^}}]*)\}}", block).group(1)
        assert "--status-mark: url(\"data:image/svg+xml," in rule, state
        assert f"color: var({role})" in rule, state


def test_each_state_has_its_own_shape():
    marks = re.findall(r"--status-mark: (url\(\"data:[^\"]+\"\))", _block())
    assert len(marks) == 5 and len(set(marks)) == 5


def test_format_js_still_emits_the_status_dot_span():
    """CSS paints the glyph onto .status-dot; the markup stays one span so
    every view (workflow list, runs, home, audit) picks the marks up."""
    js = Path("src/web/js/format.js").read_text(encoding="utf-8")
    assert "status-dot" in js
    assert "aria-hidden" in js
    assert "function statusDot" in js
    assert "function statusLine" in js
