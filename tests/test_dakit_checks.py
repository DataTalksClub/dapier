"""dakit's checkers over the console (dakit docs/adoption.md): the vendored
copy matches its lock, no accent borders, no raw colours, and spacing/type on
the scales with a shrink-only baseline. Same commands as `make check-dakit`."""
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENDOR = "src/web/vendor"


def _check_paths():
    makefile = (ROOT / "Makefile").read_text()
    block = re.search(r"^DAKIT_CHECK_PATHS = (.*?)\n\n", makefile, re.S | re.M).group(1)
    return block.replace("\\\n", " ").split()


def _run(*args):
    r = subprocess.run(["node", *args], cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_vendored_dakit_matches_its_lock():
    _run(f"{VENDOR}/sync.mjs", "--check", "--dest", VENDOR)


def test_console_has_no_accent_borders():
    _run(f"{VENDOR}/check-borders.mjs", "src/web", "designer/src")


def test_console_has_no_raw_colours():
    _run(f"{VENDOR}/check-colors.mjs", *_check_paths())


def test_console_spacing_and_type_stay_on_scale():
    _run(f"{VENDOR}/check-scale.mjs", "--baseline", "dakit-scale-baseline.json", *_check_paths())
