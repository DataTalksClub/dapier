#!/usr/bin/env python3
"""Run the pinned, synthetic Dapier Sheets export -> DataOps dry-run contract.

This harness checks out DataOps PR 307 into a temporary directory, runs only
its synthetic CSV dry-run test, and then runs Dapier's exporter test against
that checkout. It never uses a Google token or production data and removes the
temporary checkout, npm cache, and generated CSVs on exit.
"""

import os
import shutil
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path


DATAOPS_PR307_HEAD = "5de43419c7e1ce890f4e11e083c68a9cf76e9ec6"
DATAOPS_REPO = "https://github.com/DataTalksClub/dataops.git"
PROJECT_ROOT = Path(__file__).resolve().parents[3]
DRY_RUN_TEST = (
    "CSV importer dry-run contract preserves raw amount precision and formatted dates"
)


def run(args, *, cwd, env=None):
    print("$ " + shlex.join(str(part) for part in args), flush=True)
    result = subprocess.run(
        args, cwd=cwd, env=env, check=False, text=True, capture_output=True,
    )
    if result.stdout:
        print(result.stdout, end="", flush=True)
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr, flush=True)
    if result.returncode:
        raise subprocess.CalledProcessError(
            result.returncode, args, output=result.stdout, stderr=result.stderr,
        )
    return result.stdout + result.stderr


def require_test_selected(output, expected_test):
    """Fail if Node only reports the test file and filtered out every test."""
    if expected_test not in output:
        raise RuntimeError(
            "DataOps dry-run test did not run; expected its test title in Node "
            f"output: {expected_test!r}"
        )


def main():
    missing = [name for name in ("git", "node", "npm", "pytest")
               if shutil.which(name) is None]
    if missing:
        raise SystemExit("missing required commands: " + ", ".join(missing))

    with tempfile.TemporaryDirectory(prefix="dapier-dataops-contract-") as temp:
        scratch = Path(temp)
        dataops = scratch / "dataops"
        run([
            "git", "clone", "--quiet", "--filter=blob:none", "--no-checkout",
            DATAOPS_REPO, str(dataops),
        ], cwd=PROJECT_ROOT)
        run([
            "git", "-C", str(dataops), "fetch", "--quiet", "--depth=1", "origin",
            "refs/pull/307/head:refs/remotes/origin/pr-307",
        ], cwd=PROJECT_ROOT)
        run([
            "git", "-C", str(dataops), "checkout", "--quiet", "--detach",
            "refs/remotes/origin/pr-307",
        ], cwd=PROJECT_ROOT)
        actual_head = subprocess.check_output(
            ["git", "-C", str(dataops), "rev-parse", "HEAD"], text=True,
        ).strip()
        print("DataOps PR 307 consumer head: " + actual_head, flush=True)
        if actual_head != DATAOPS_PR307_HEAD:
            raise SystemExit(
                f"PR 307 moved: expected {DATAOPS_PR307_HEAD}; review and update the pin before testing"
            )

        cli = dataops / "cli"
        npmrc = scratch / "npmrc"
        npmrc.write_text("audit=false\nfund=false\n", encoding="utf-8")
        npm_env = {
            key: value for key, value in os.environ.items()
            if key not in {"NODE_AUTH_TOKEN", "NPM_TOKEN", "npm_config_token"}
        }
        npm_env.update({
            "npm_config_cache": str(scratch / "npm-cache"),
            "npm_config_userconfig": str(npmrc),
        })
        run([
            "npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund",
        ], cwd=cli, env=npm_env)

        temp_preload = scratch / "node-tmpdir.mjs"
        temp_preload.write_text(
            "import os from 'node:os';\n"
            "import { syncBuiltinESMExports } from 'node:module';\n"
            "os.tmpdir = () => process.env.DAPIER_TEST_TMP;\n"
            "syncBuiltinESMExports();\n",
            encoding="utf-8",
        )
        node_env = dict(
            os.environ,
            NODE_OPTIONS=f"--import {temp_preload}",
            DAPIER_TEST_TMP=str(scratch),
        )
        node_output = run([
            "node", "--test", "--test-reporter=spec",
            f"--test-name-pattern={DRY_RUN_TEST}",
            "test/bookkeeping.test.mjs",
        ], cwd=cli, env=node_env)
        require_test_selected(node_output, DRY_RUN_TEST)

        pytest_env = dict(
            os.environ,
            PYTHONPATH=str(PROJECT_ROOT),
            DAPIER_DATAOPS_CLI_DIR=str(cli),
        )
        run([
            shutil.which("pytest"), "-q",
            "plugins/google/tests/test_google_data_pull.py",
            "--basetemp", str(scratch / "pytest-tmp"),
        ], cwd=PROJECT_ROOT, env=pytest_env)


if __name__ == "__main__":
    main()
