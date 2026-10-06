import json
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml

from dapier_cli import commands
from src.dapier.api import admin
from src.dapier.connections import aws, credentials
import plugins.aws.plugin as s3
from plugins.aws.runners import s3 as actions

ROLE = "arn:aws:iam::387546586013:role/dapier-mailchimp-backup"
CONFIG = {
    "role_arn": ROLE,
    "region": "eu-west-1",
    "buckets": ["datatalks-mailchimp-backup"],
}


def test_api_can_stage_file_bytes_and_assume_only_configured_roles():
    source = Path(__file__).resolve().parents[3] / "template.yaml"
    document = yaml.load(source.read_text(), Loader=yaml.BaseLoader)
    resources = document["Resources"]
    # The API's execution role is explicit (ApiExecutionRole — a fresh role
    # sidesteps the inline-policy size cap the implicit role filled up);
    # its Policies are named documents whose Statement may be one dict or
    # a list.
    def statements_of(policies):
        flat = []
        for policy in policies:
            if not isinstance(policy, dict):
                continue
            document = policy.get("PolicyDocument", policy)
            statement = document.get("Statement") if isinstance(document, dict) else None
            if statement is None:
                continue
            flat.extend(statement if isinstance(statement, list) else [statement])
        return flat

    api_statements = statements_of(
        resources["ApiExecutionRole"]["Properties"]["Policies"])
    assert any(
        statement == {
            "Effect": "Allow",
            "Action": ["s3:GetObject", "s3:PutObject"],
            "Resource": "${RenderArtifactsBucket.Arn}/*",
        }
        for statement in api_statements
    )
    worker_statements = statements_of(
        resources["WorkerFunction"]["Properties"]["Policies"])
    for statements in (api_statements, worker_statements):
        assert any(
            statement == {
                "Effect": "Allow", "Action": "sts:AssumeRole",
                "Resource": "AwsAssumableRoleArns",
            }
            for statement in statements
        )


@pytest.fixture
def role_config(monkeypatch):
    monkeypatch.setenv("AWS_ASSUMABLE_ROLE_ARNS", ROLE)
    monkeypatch.setattr(credentials, "get_credential", lambda credential_id: CONFIG)


def test_assume_role_uses_runtime_identity_and_session_token(monkeypatch, role_config):
    sts = Mock()
    sts.assume_role.return_value = {
        "Credentials": {
            "AccessKeyId": "temporary-access",
            "SecretAccessKey": "temporary-secret",
            "SessionToken": "temporary-session",
        }
    }
    calls = []
    target = object()

    def client(service, **kwargs):
        calls.append((service, kwargs))
        return sts if len(calls) == 1 else target

    monkeypatch.setattr(aws.boto3, "client", client)
    assert aws.client("s3", {**CONFIG, "external_id": "customer-id"}) is target
    assert calls == [
        ("sts", {"region_name": "eu-west-1"}),
        (
            "s3",
            {
                "region_name": "eu-west-1",
                "aws_access_key_id": "temporary-access",
                "aws_secret_access_key": "temporary-secret",
                "aws_session_token": "temporary-session",
            },
        ),
    ]
    sts.assume_role.assert_called_once_with(
        RoleArn=ROLE,
        RoleSessionName="dapier-provider",
        DurationSeconds=900,
        ExternalId="customer-id",
    )


def test_unapproved_role_is_rejected_before_aws_call(monkeypatch, role_config):
    monkeypatch.setenv(
        "AWS_ASSUMABLE_ROLE_ARNS", "arn:aws:iam::111111111111:role/other"
    )
    with pytest.raises(ValueError, match="allowed role list"):
        aws.stored_config()


def test_failed_assumption_does_not_fall_back_to_keys(monkeypatch, role_config):
    sts = Mock()
    sts.assume_role.side_effect = RuntimeError("role access denied")
    factory = Mock(return_value=sts)
    monkeypatch.setattr(aws.boto3, "client", factory)
    with pytest.raises(RuntimeError, match="role access denied"):
        actions.run_s3_find({"bucket": CONFIG["buckets"][0], "pattern": "export.zip"}, {})
    factory.assert_called_once_with("sts", region_name="eu-west-1")


def test_role_credentials_save_and_reject_mixed_or_unapproved(monkeypatch, role_config):
    writes = []
    monkeypatch.setattr(
        credentials,
        "put_credential",
        lambda *args, **kwargs: writes.append((args, kwargs)),
    )
    status, result = credentials.api_save_credential("aws", CONFIG)
    assert status == 200
    assert result == {"provider": "aws", "configured": True}
    assert writes == [(("aws", CONFIG), {"provider": "aws"})]
    assert (
        credentials.api_save_credential("aws", {**CONFIG, "access_key_id": "key"})[0]
        == 400
    )
    assert (
        credentials.api_save_credential(
            "aws", {"role_arn": "arn:aws:iam::111111111111:role/other"}
        )[0]
        == 400
    )
    assert (
        credentials.api_save_credential("aws", {**CONFIG, "buckets": "bucket"})[0]
        == 400
    )
    assert (
        credentials.api_save_credential("aws", {**CONFIG, "external_id": 5})[0] == 400
    )
    assert len(writes) == 1


def test_cli_passes_role_configuration_to_shared_api(monkeypatch, tmp_path):
    path = tmp_path / "aws.json"
    path.write_text(json.dumps(CONFIG))
    calls = []
    monkeypatch.setattr(
        commands.api,
        "call",
        lambda *args, **kwargs: calls.append((args, kwargs)) or {"provider": "aws"},
    )
    assert commands.credentials_set("https://api.example", "aws", str(path)) == 0
    assert calls[0][0] == (
        "https://api.example",
        "PUT",
        "/api/agent/credentials/aws",
        CONFIG,
    )


def test_console_api_stores_role_config(monkeypatch, role_config):
    writes = []
    monkeypatch.setattr(
        credentials,
        "put_credential",
        lambda *args, **kwargs: writes.append((args, kwargs)),
    )
    response = admin.save_credential("aws", {"body": json.dumps(CONFIG)})
    assert response["statusCode"] == 200
    assert writes[0][0] == ("aws", CONFIG)


def test_cli_endpoint_uses_same_role_validation(monkeypatch, role_config):
    from src.dapier.api.agent import access

    monkeypatch.setattr(
        access, "require_operator", lambda event, operation: ("operator", None)
    )
    monkeypatch.setattr(access.audit, "emit", lambda *args, **kwargs: None)
    writes = []
    monkeypatch.setattr(
        credentials,
        "put_credential",
        lambda *args, **kwargs: writes.append((args, kwargs)),
    )
    response = access.credentials_api({"body": json.dumps(CONFIG)}, "aws")
    assert response["statusCode"] == 200
    assert writes[0][0] == ("aws", CONFIG)


def test_actions_poll_discovery_and_health_use_shared_role_client(
    monkeypatch, role_config
):
    target = Mock()
    target.list_objects_v2.return_value = {"Contents": []}
    target.get_caller_identity.return_value = {
        "Arn": f"arn:aws:sts::387546586013:assumed-role/dapier-mailchimp-backup/dapier-provider",
        "Account": "387546586013",
    }
    calls = []
    monkeypatch.setattr(
        aws, "client", lambda service, config: calls.append((service, config)) or target
    )
    monkeypatch.setattr(
        actions, "_source_body", lambda *args, **kwargs: b"original-zip-bytes"
    )
    upload = actions.run_s3_upload(
        {"bucket": CONFIG["buckets"][0], "key": "original.zip"}, {}
    )
    assert upload["bytes"] == len(b"original-zip-bytes")
    target.put_object.assert_called_once_with(
        Bucket=CONFIG["buckets"][0],
        Key="original.zip",
        Body=b"original-zip-bytes",
        ContentType="application/octet-stream",
    )
    assert s3._s3_poll_client({}) is target
    assert s3._run_objects({}, {"bucket": CONFIG["buckets"][0]}) == []
    assert s3._run_test({})["ok"]
    assert calls == [("s3", CONFIG), ("s3", CONFIG), ("s3", CONFIG), ("sts", CONFIG)]
    assert s3._run_buckets({}, {}) == [
        {"id": CONFIG["buckets"][0], "name": CONFIG["buckets"][0]}
    ]
    target.list_buckets.assert_not_called()
