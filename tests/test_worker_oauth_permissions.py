"""Keep unattended provider refresh authorized without credential admin access."""
from pathlib import Path

import yaml


class CfnLoader(yaml.SafeLoader):
    pass


def intrinsic(loader, tag, node):
    value = (loader.construct_scalar(node) if isinstance(node, yaml.ScalarNode)
             else loader.construct_sequence(node, deep=True))
    return {tag: value}


CfnLoader.add_multi_constructor("!", intrinsic)


def test_worker_can_persist_only_oauth_refresh_records():
    template = yaml.load(Path("template.yaml").read_text(), Loader=CfnLoader)
    policies = template["Resources"]["WorkerFunction"]["Properties"]["Policies"]
    statements = [policy["Statement"] for policy in policies if "Statement" in policy]
    credential_statements = [statement for statement in statements
                             if statement.get("Resource") == {"GetAtt": "CredentialsTable.Arn"}]
    assert len(credential_statements) == 2
    read, refresh = credential_statements
    assert read["Effect"] == "Allow"
    assert read["Action"] == ["dynamodb:GetItem"]
    assert refresh["Effect"] == "Allow"
    assert refresh["Action"] == ["dynamodb:PutItem"]
    assert refresh["Condition"] == {
        "ForAllValues:StringLike": {"dynamodb:LeadingKeys": ["oauth#*"]},
    }
    assert not any(policy.get("DynamoDBCrudPolicy", {}).get("TableName")
                   == {"Ref": "CredentialsTable"} for policy in policies)
