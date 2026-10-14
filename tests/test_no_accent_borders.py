"""dakit rule: no colored edge accents in the console (see AGENTS.md / dakit docs)."""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_console_has_no_accent_borders():
    r = subprocess.run(
        ["node", "src/web/vendor/check-borders.mjs", "src/web"],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr
