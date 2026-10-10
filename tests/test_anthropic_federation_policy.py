"""Keep outbound Anthropic identity confined to the workflow worker."""

from pathlib import Path

import yaml


def test_only_worker_can_mint_audience_and_lifetime_limited_tokens():
    class Loader(yaml.SafeLoader):
        pass

    def intrinsic(loader, tag, node):
        if isinstance(node, yaml.ScalarNode):
            return {tag: loader.construct_scalar(node)}
        if isinstance(node, yaml.SequenceNode):
            return {tag: loader.construct_sequence(node)}
        return {tag: loader.construct_mapping(node)}

    Loader.add_multi_constructor("!", intrinsic)
    template = yaml.load(Path("template.yaml").read_text(encoding="utf-8"), Loader=Loader)
    grants = []
    for name, resource in template["Resources"].items():
        for policy in resource.get("Properties", {}).get("Policies", []):
            if not isinstance(policy, dict):
                continue
            statements = policy.get("Statement", [])
            if isinstance(statements, dict):
                statements = [statements]
            for statement in statements:
                actions = statement.get("Action", [])
                if "sts:GetWebIdentityToken" in actions:
                    grants.append((name, statement))

    assert len(grants) == 1
    name, statement = grants[0]
    assert name == "WorkerFunction"
    assert statement["Effect"] == "Allow"
    conditions = statement["Condition"]
    assert conditions["ForAllValues:StringEquals"]["sts:IdentityTokenAudience"] == [
        "https://api.anthropic.com"
    ]
    assert conditions["Null"]["sts:IdentityTokenAudience"] == "false"
    assert conditions["StringEquals"]["sts:SigningAlgorithm"] == "RS256"
    assert conditions["NumericLessThanEquals"]["sts:DurationSeconds"] == 900
