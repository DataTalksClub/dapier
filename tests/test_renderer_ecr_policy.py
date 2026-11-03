"""Regression tests for the Lambda image pull repository policy."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "apply-renderer-ecr-policy.py"


def test_print_only_resolves_a_dapier_scoped_lambda_pull_statement() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--account-id",
            "817685572750",
            "--region",
            "eu-west-1",
            "--print-only",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    policy = json.loads(result.stdout)
    statement = next(
        item for item in policy["Statement"] if item["Sid"] == "AllowDapierLambdaImagePull"
    )
    assert statement["Principal"] == {"Service": "lambda.amazonaws.com"}
    assert statement["Action"] == ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]
    assert statement["Condition"] == {
        "ArnLike": {
            "aws:SourceArn": "arn:aws:lambda:eu-west-1:817685572750:function:dapier-*"
        }
    }


def test_print_only_resolves_the_partition_for_govcloud_regions() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--account-id",
            "817685572750",
            "--region",
            "us-gov-west-1",
            "--print-only",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    policy = json.loads(result.stdout)
    statement = next(
        item for item in policy["Statement"] if item["Sid"] == "AllowDapierLambdaImagePull"
    )
    assert statement["Condition"]["ArnLike"]["aws:SourceArn"] == (
        "arn:aws-us-gov:lambda:us-gov-west-1:817685572750:function:dapier-*"
    )
