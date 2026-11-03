#!/usr/bin/env python3
"""Apply the template's renderer ECR policy before CloudFormation rollback."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml
from yaml.nodes import MappingNode, ScalarNode, SequenceNode


EXPECTED_ACCOUNT = "817685572750"
REPOSITORY = "dapier-html-renderer"
TEMPLATE = Path(__file__).resolve().parents[1] / "template.yaml"


class CloudFormationLoader(yaml.SafeLoader):
    """Load CloudFormation tags as their underlying YAML values."""


def _load_intrinsic(loader: CloudFormationLoader, _tag: str, node: Any) -> Any:
    if isinstance(node, ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, SequenceNode):
        return loader.construct_sequence(node)
    if isinstance(node, MappingNode):
        return loader.construct_mapping(node)
    raise TypeError(f"unsupported CloudFormation YAML node: {type(node).__name__}")


CloudFormationLoader.add_multi_constructor("!", _load_intrinsic)


def _account_id(override: str | None) -> str:
    if override:
        return override
    result = subprocess.run(
        ["aws", "sts", "get-caller-identity", "--query", "Account", "--output", "text"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _replace_parameters(value: Any, parameters: dict[str, str]) -> Any:
    if isinstance(value, str):
        for key, replacement in parameters.items():
            value = value.replace(f"${{{key}}}", replacement)
        return value
    if isinstance(value, list):
        return [_replace_parameters(item, parameters) for item in value]
    if isinstance(value, dict):
        return {key: _replace_parameters(item, parameters) for key, item in value.items()}
    return value


def _region(override: str | None) -> str:
    region = override or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    if not region:
        result = subprocess.run(
            ["aws", "configure", "get", "region"],
            check=False,
            capture_output=True,
            text=True,
        )
        region = result.stdout.strip()
    if not region:
        raise SystemExit("AWS region must be set with --region, AWS_REGION, or AWS_DEFAULT_REGION")
    return region


def _partition_for_region(region: str) -> str:
    for prefix, partition in (
        ("cn-", "aws-cn"),
        ("us-gov-", "aws-us-gov"),
        ("us-isob-", "aws-iso-b"),
        ("eu-isoe-", "aws-iso-e"),
        ("us-isof-", "aws-iso-f"),
        ("us-iso-", "aws-iso"),
    ):
        if region.startswith(prefix):
            return partition
    return "aws"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", help="Use this account instead of querying STS")
    parser.add_argument("--region", help="Use this AWS region instead of the configured region")
    parser.add_argument("--partition", help="Override the AWS partition derived from the region")
    parser.add_argument("--print-only", action="store_true", help="Print policy JSON without changing AWS")
    args = parser.parse_args()

    account_id = _account_id(args.account_id)
    if account_id != EXPECTED_ACCOUNT:
        raise SystemExit(f"refusing ECR policy update in unexpected account {account_id}")
    region = _region(args.region)
    partition = args.partition or _partition_for_region(region)

    template = yaml.load(TEMPLATE.read_text(encoding="utf-8"), Loader=CloudFormationLoader)
    policy = template["Resources"]["HtmlRendererRepository"]["Properties"]["RepositoryPolicyText"]
    policy = _replace_parameters(
        policy,
        {
            "AWS::AccountId": account_id,
            "AWS::Region": region,
            "AWS::Partition": partition,
        },
    )
    policy_text = json.dumps(policy, separators=(",", ":"))

    if args.print_only:
        print(policy_text)
        return

    result = subprocess.run(
        [
            "aws",
            "ecr",
            "set-repository-policy",
            "--registry-id",
            account_id,
            "--repository-name",
            REPOSITORY,
            "--policy-text",
            policy_text,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode:
        if "AccessDeniedException" in result.stderr and "ecr:SetRepositoryPolicy" in result.stderr:
            print(
                "GitHub OIDC role cannot set the ECR policy directly; continuing with "
                "CloudFormation rollback using the configured deployment role.",
                file=sys.stderr,
            )
            return
        raise SystemExit(result.stderr or f"aws ecr set-repository-policy failed ({result.returncode})")


if __name__ == "__main__":
    main()
