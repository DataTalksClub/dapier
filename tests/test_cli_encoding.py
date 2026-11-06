"""The CLI's redirected output must retain provider text on Windows."""

import json
import os
from pathlib import Path
import subprocess
import sys


def test_cli_outputs_utf8_with_legacy_pipe_encoding():
    # Synthetic text only: no provider connection, credentials or mail involved.
    script = r'''
import sys
from unittest.mock import patch
from dapier_cli.main import main
from dapier_cli.commands.shared import print_json

assert sys.stdout.encoding == "cp1252"
assert sys.stderr.encoding == "cp1252"

def emit(*args, **kwargs):
    print_json({"text": "\u2192 \u041f\u0440\u0438\u0432\u0435\u0442 \U0001f680"})
    print("diagnostic \u2192", file=sys.stderr)
    return 0

with patch("dapier_cli.main.dispatch", side_effect=emit):
    sys.exit(main(["auth", "status"]))
'''
    env = {**os.environ, "PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0"}
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        env=env, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert json.loads(result.stdout.decode("utf-8")) == {"text": "→ Привет 🚀"}
    assert result.stderr.decode("utf-8").strip() == "diagnostic →"
