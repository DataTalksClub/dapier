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


def _api_role_policies(resources):
    """The API function's role policies: the function carries its execution
    role by reference (ApiExecutionRole — an explicit role, so its inline
    documents start on a fresh IAM size budget)."""
    return resources["ApiExecutionRole"]["Properties"]["Policies"]


def test_api_and_worker_can_send_attachment_email_and_api_can_stage_intake():
    resources = _resources()
    for name, policies in (("IngressFunction/ApiExecutionRole", _api_role_policies(resources)),
                           ("WorkerFunction",
                            resources["WorkerFunction"]["Properties"]["Policies"])):
        statements = [p["Statement"] for p in policies
                      if isinstance(p, dict) and isinstance(p.get("Statement"), dict)]
        assert any("ses:SendRawEmail" in s.get("Action", []) for s in statements), name
    ingress = _api_role_policies(resources)
    assert any(p.get("Statement", {}).get("Resource") ==
               "arn:aws:s3:::${DataOpsEmailDocumentsBucket}/transfer/*"
               and p["Statement"]["Action"] == "s3:PutObject"
               for p in ingress if isinstance(p, dict))
    assert any(p.get("Statement", {}).get("Resource") ==
               "arn:aws:secretsmanager:${AWS::Region}:${AWS::AccountId}:secret:dapier/dataops-*"
               and p["Statement"]["Action"] == "secretsmanager:GetSecretValue"
               for p in ingress if isinstance(p, dict))


def test_api_can_enqueue_agent_host_tasks():
    policies = _api_role_policies(_resources())
    grants = [p["Statement"] for p in policies if isinstance(p, dict)
              and isinstance(p.get("Statement"), dict)
              and p["Statement"].get("Resource") == "HostQueue.Arn"]
    actions = {action for grant in grants for action in grant["Action"]}
    assert "sqs:SendMessage" in actions


def test_hook_claims_and_poll_cleanup_have_scoped_cursor_write_permissions():
    policies = _api_role_policies(_resources())
    grants = [p["Statement"] for p in policies if isinstance(p, dict)
              and isinstance(p.get("Statement"), dict)
              and p["Statement"].get("Resource") == "CursorsTable.Arn"]
    assert len(grants) == 1
    grant = grants[0]
    assert set(grant["Action"]) == {"dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem"}
    assert set(grant["Condition"]["ForAllValues:StringLike"]["dynamodb:LeadingKeys"]) == {
        "poll#*", "seen#poll#*", "seen#hook#*"}


def test_api_role_reads_every_table_the_handlers_touch():
    """The consolidated role documents must still name every table the
    function's handlers touch — this is the regression guard for the drift
    that took the connections API down: the table grant existed in the
    template while the live role had lost it."""
    resources = _resources()
    crud = set()
    read = set()
    for policy in _api_role_policies(resources):
        if not isinstance(policy, dict) or not isinstance(policy.get("Statement"), dict):
            continue
        statement = policy["Statement"]
        actions = statement["Action"]
        if not isinstance(actions, list) or not all(a.startswith("dynamodb:") for a in actions):
            continue
        resource = statement.get("Resource")
        if isinstance(resource, str):
            resource = [resource]
        for arn in resource or []:
            (crud if "dynamodb:DeleteItem" in actions or "dynamodb:PutItem" in actions
             else read).add(arn)
    assert "ConnectionsTable.Arn" in crud
    assert "PublishedWorkflowsTable.Arn" in crud
    assert "CursorsTable.Arn" in read and "CursorsTable.Arn" in crud
    assert "TriggerInboxTable.Arn" in read
    assert "CredentialsTable.Arn" in read or "CredentialsTable.Arn" in crud


def test_api_function_uses_the_explicit_role():
    properties = _resources()["IngressFunction"]["Properties"]
    assert properties["Role"] == "ApiExecutionRole.Arn"
    assert "Policies" not in properties
