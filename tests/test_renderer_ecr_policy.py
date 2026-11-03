"""Regression tests for the Lambda image pull repository policy."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import yaml
from yaml.nodes import MappingNode, ScalarNode, SequenceNode


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "apply-renderer-ecr-policy.py"
ACCESS_TEMPLATE = ROOT / "infra" / "renderer-ecr-access.yaml"


class CloudFormationLoader(yaml.SafeLoader):
    """Load CloudFormation tags as their underlying YAML values."""


def _load_intrinsic(loader: CloudFormationLoader, _tag: str, node: object) -> object:
    if isinstance(node, ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, SequenceNode):
        return loader.construct_sequence(node)
    if isinstance(node, MappingNode):
        return loader.construct_mapping(node)
    raise TypeError(f"unsupported CloudFormation YAML node: {type(node).__name__}")


CloudFormationLoader.add_multi_constructor("!", _load_intrinsic)


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


def test_deploy_role_can_read_only_the_renderer_function_configuration() -> None:
    template = yaml.load(ACCESS_TEMPLATE.read_text(encoding="utf-8"), Loader=CloudFormationLoader)
    statements = template["Resources"]["GithubDeployRendererImageAccess"]["Properties"][
        "PolicyDocument"
    ]["Statement"]

    permission = next(item for item in statements if "lambda:GetFunction" in item["Action"])
    assert permission["Resource"] == (
        "arn:${AWS::Partition}:lambda:${AWS::Region}:${AWS::AccountId}:"
        "function:dapier-HtmlRendererFunction-*"
    )
