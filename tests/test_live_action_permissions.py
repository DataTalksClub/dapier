"""Live execution uses the same provider capabilities in API and worker."""
from pathlib import Path

import yaml


def _resources():
    class Loader(yaml.SafeLoader):
        pass

    Loader.add_multi_constructor(
        "!", lambda loader, tag, node: loader.construct_scalar(node)
        if isinstance(node, yaml.ScalarNode)
        else loader.construct_sequence(node, deep=True)
        if isinstance(node, yaml.SequenceNode)
        else loader.construct_mapping(node, deep=True),
    )
    return yaml.load(Path("template.yaml").read_text(), Loader=Loader)["Resources"]


def test_api_and_worker_can_send_attachment_email_and_api_can_stage_intake():
    resources = _resources()
    for name in ("IngressFunction", "WorkerFunction"):
        statements = [p["Statement"] for p in resources[name]["Properties"]["Policies"]
                      if isinstance(p, dict) and isinstance(p.get("Statement"), dict)]
        assert any("ses:SendRawEmail" in s.get("Action", []) for s in statements), name
    ingress = resources["IngressFunction"]["Properties"]["Policies"]
    assert any(p.get("Statement", {}).get("Resource") ==
               "arn:aws:s3:::${DataOpsEmailDocumentsBucket}/transfer/*"
               and p["Statement"]["Action"] == "s3:PutObject"
               for p in ingress if isinstance(p, dict))
    assert any(p.get("Statement", {}).get("Resource") ==
               "arn:aws:secretsmanager:${AWS::Region}:${AWS::AccountId}:secret:dapier/dataops-*"
               and p["Statement"]["Action"] == "secretsmanager:GetSecretValue"
               for p in ingress if isinstance(p, dict))


def test_api_can_enqueue_agent_host_tasks():
    policies = _resources()["IngressFunction"]["Properties"]["Policies"]
    grants = [p["Statement"] for p in policies if isinstance(p, dict)
              and isinstance(p.get("Statement"), dict)
              and p["Statement"].get("Resource") == "HostQueue.Arn"]
    actions = {action for grant in grants for action in grant["Action"]}
    assert "sqs:SendMessage" in actions


def test_hook_claims_and_poll_cleanup_have_scoped_cursor_write_permissions():
    policies = _resources()["IngressFunction"]["Properties"]["Policies"]
    grants = [p["Statement"] for p in policies if isinstance(p, dict)
               and isinstance(p.get("Statement"), dict)
               and p["Statement"].get("Resource") == "CursorsTable.Arn"]
    assert len(grants) == 1
    grant = grants[0]
    assert set(grant["Action"]) == {"dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem"}
    assert set(grant["Condition"]["ForAllValues:StringLike"]["dynamodb:LeadingKeys"]) == {
        "poll#*", "seen#poll#*", "seen#hook#*"}
