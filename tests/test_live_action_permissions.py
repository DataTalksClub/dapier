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


def _api_role_statements(resources):
    """Every permission statement of the API function's execution role.

    The role is explicit (ApiExecutionRole — a fresh role sidesteps the
    inline-policy size cap the previous implicit role filled up), so its
    Policies are named documents whose Statement may be one dict or a
    list; conditional entries (!If) parse to lists and are skipped.
    """
    statements = []
    for policy in resources["ApiExecutionRole"]["Properties"]["Policies"]:
        if not isinstance(policy, dict) or not isinstance(policy.get("PolicyDocument"), dict):
            continue
        doc = policy["PolicyDocument"].get("Statement")
        statements.extend(doc if isinstance(doc, list) else [doc])
    return statements


def test_api_and_worker_can_send_attachment_email_and_api_can_stage_intake():
    resources = _resources()
    api_statements = _api_role_statements(resources)
    worker_statements = [
        p["Statement"] for p in resources["WorkerFunction"]["Properties"]["Policies"]
        if isinstance(p, dict) and isinstance(p.get("Statement"), dict)]
    for name, statements in (("IngressFunction/ApiExecutionRole", api_statements),
                             ("WorkerFunction", worker_statements)):
        assert any("ses:SendRawEmail" in s.get("Action", []) for s in statements), name
    def names_resource(statement, resource):
        value = statement.get("Resource")
        value = [value] if isinstance(value, str) else (value or [])
        return resource in value

    assert any(s.get("Action") == "s3:PutObject" and
               names_resource(s, "arn:aws:s3:::${DataOpsEmailDocumentsBucket}/transfer/*")
               for s in api_statements)
    assert any(s.get("Action") == "secretsmanager:GetSecretValue" and
               names_resource(s, "arn:aws:secretsmanager:${AWS::Region}:${AWS::AccountId}:secret:dapier/dataops-*")
               for s in api_statements)


def test_api_can_enqueue_agent_host_tasks():
    grants = [s for s in _api_role_statements(_resources())
              if s.get("Resource") == "HostQueue.Arn"]
    actions = {action for grant in grants for action in grant["Action"]}
    assert "sqs:SendMessage" in actions


def test_hook_claims_and_poll_cleanup_have_scoped_cursor_write_permissions():
    grants = [s for s in _api_role_statements(_resources())
              if s.get("Resource") == "CursorsTable.Arn"
              and "Condition" in s]
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
    crud, read = set(), set()
    for statement in _api_role_statements(_resources()):
        actions = statement.get("Action")
        if not isinstance(actions, list) or not all(a.startswith("dynamodb:") for a in actions):
            continue
        resource = statement.get("Resource")
        if isinstance(resource, str):
            resource = [resource]
        bucket = crud if ("dynamodb:DeleteItem" in actions
                          or "dynamodb:PutItem" in actions) else read
        bucket.update(resource or [])
    assert "ConnectionsTable.Arn" in crud
    assert "PublishedWorkflowsTable.Arn" in crud
    assert "CursorsTable.Arn" in crud and "CursorsTable.Arn" in read
    assert "TriggerInboxTable.Arn" in read
    assert "CredentialsTable.Arn" in crud
    assert "TaskUsageTable.Arn" in read


def test_api_function_uses_the_explicit_role():
    properties = _resources()["IngressFunction"]["Properties"]
    assert properties["Role"] == "ApiExecutionRole.Arn"
    assert "Policies" not in properties
