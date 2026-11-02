#!/usr/bin/env python3
"""Apply the template's renderer ECR policy before CloudFormation rollback."""

from __future__ import annotations

import argparse
import json
import subprocess
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


def _replace_account(value: Any, account_id: str) -> Any:
    if isinstance(value, str):
        return value.replace("${AWS::AccountId}", account_id)
    if isinstance(value, list):
        return [_replace_account(item, account_id) for item in value]
    if isinstance(value, dict):
        return {key: _replace_account(item, account_id) for key, item in value.items()}
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", help="Use this account instead of querying STS")
    parser.add_argument("--print-only", action="store_true", help="Print policy JSON without changing AWS")
    args = parser.parse_args()

    account_id = _account_id(args.account_id)
    if account_id != EXPECTED_ACCOUNT:
        raise SystemExit(f"refusing ECR policy update in unexpected account {account_id}")

    template = yaml.load(TEMPLATE.read_text(encoding="utf-8"), Loader=CloudFormationLoader)
    policy = template["Resources"]["HtmlRendererRepository"]["Properties"]["RepositoryPolicyText"]
    policy = _replace_account(policy, account_id)
    policy_text = json.dumps(policy, separators=(",", ":"))

    if args.print_only:
        print(policy_text)
        return

    subprocess.run(
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
        check=True,
    )


if __name__ == "__main__":
    main()
