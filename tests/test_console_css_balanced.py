"""app.css must close every block it opens.

Several tabs append their CSS at the end of app.css, and a rebase that
drops one closing brace silently swallows every rule after it into the
previous @media block — the Hooks and Polls tabs rendered unstyled on
desktop when the Schedules phone query lost its `}`.
"""
import re
from pathlib import Path

CSS = (Path(__file__).resolve().parents[1] / "src" / "web" / "app.css").read_text()


def test_app_css_braces_balance_at_every_point():
    text = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
    text = re.sub(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', '""', text)
    depth = 0
    for number, line in enumerate(text.splitlines(), 1):
        depth += line.count("{") - line.count("}")
        assert depth >= 0, f"app.css closes a block it never opened near line {number}"
    assert depth == 0, f"app.css leaves {depth} block(s) open at the end"
