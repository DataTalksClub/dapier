"""One timestamp family: absolute Europe/Berlin, relative wording only in tooltips."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
node = shutil.which("node")


def run_js(expr):
    script = (f"import('{(ROOT / 'src/web/js/format.js').as_uri()}')"
              f".then((m) => console.log(JSON.stringify({expr})))")
    out = subprocess.run([node, "--input-type=commonjs", "-e", script],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


@pytest.mark.skipif(node is None, reason="node not installed")
def test_timestamps_are_berlin_wall_clock_in_summer_and_winter():
    # CEST (UTC+2) and CET (UTC+1) both render as Berlin wall time.
    assert run_js("m.formatTimestamp('2026-07-01T10:00:00Z')") == "2026-07-01 12:00"
    assert run_js("m.formatTimestamp('2026-11-08T10:00:00Z')") == "2026-11-08 11:00"
    assert run_js("[m.formatDay('2026-11-08T23:30:00Z'), m.formatClock('2026-11-08T23:30:00Z')]") == ["2026-11-09", "00:30"]


@pytest.mark.skipif(node is None, reason="node not installed")
def test_tooltip_carries_relative_time_and_zone():
    title = run_js("m.timeTitle(new Date(Date.now() - 21 * 3600 * 1000).toISOString())")
    assert title.startswith("21 h ago · ") and title.endswith(" Europe/Berlin")
