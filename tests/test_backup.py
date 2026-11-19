import gzip
import json
import tracemalloc
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal

import pytest

import src.dapier.backup as backup


def test_backup_memory_is_bounded_across_scan_pages():
    class PagedDynamo:
        def get_paginator(self, operation):
            assert operation == "scan"
            return self

        def paginate(self, **kwargs):
            # 64 MiB of distinct items, generated lazily like DynamoDB pages.
            for page in range(64):
                yield {"Items": [{"payload": {"S": f"{page}:{item}:" + "x" * 65536}}
                                 for item in range(16)]}

    class StreamingS3:
        def put_object(self, **kwargs):
            assert kwargs["ContentEncoding"] == "gzip"
            # Consume the seekable file exactly as the SDK does, without
            # retaining the uploaded object in this memory measurement.
            body = kwargs["Body"]
            with gzip.GzipFile(fileobj=body, mode="rb") as snapshot:
                self.items = sum(1 for _ in snapshot)

    s3 = StreamingS3()
    tracemalloc.start()
    try:
        stats = backup.backup_table(PagedDynamo(), s3, "table", "bucket", "day")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert stats["items"] == s3.items == 1024
    assert stats["bytes"] > 0
    # The old whole-table implementation exceeds 128 MiB on this fixture.
    assert peak < 12 * 1024 * 1024


def test_backup_has_explicit_memory_headroom():
    import yaml

    class TemplateLoader(yaml.SafeLoader):
        pass

    TemplateLoader.add_multi_constructor("!", lambda loader, suffix, node: None)
    template = yaml.load(Path("template.yaml").read_text(), Loader=TemplateLoader)
    # 1 GB: the first full runs of the streaming fix peaked at ~497 MB of
    # the previous 512 (2026-11-19), too close to the OOM kill.
    assert template["Resources"]["BackupFunction"]["Properties"]["MemorySize"] >= 1024


def test_backup_has_timeout_and_tmp_headroom_for_slow_scans():
    """The 2026-11-17/18 alarm fired because full scans of the 3.7 GB inbox
    — throttled by its fresh GSI backfill — outgrew the 300 s timeout and
    the invocation was killed before manifest and failure email. The
    function must carry explicit headroom against both."""
    template = _load_yaml("template.yaml")
    fn = template["Resources"]["BackupFunction"]["Properties"]
    assert fn["Timeout"] >= 900
    assert fn["EphemeralStorage"]["Size"] >= 4096


def test_backup_alarm_notifies_on_recovery():
    """A fired backup alarm only emails on the way up; without OKActions the
    operator never learns that a scheduled backup went green again."""
    template = _load_yaml("template.yaml")
    alarm = template["Resources"]["BackupErrorAlarm"]["Properties"]
    topic = [{"Ref": "AlarmTopic"}]
    assert alarm["AlarmActions"] == topic
    assert alarm["OKActions"] == topic


class MultipartS3:
    """Reassembles multipart uploads byte-for-byte, like S3 does."""

    def __init__(self):
        self.objects = {}
        self.uploads = []
        self.aborted = []
        self.put_keys = []

    def _by_id(self, upload_id):
        return next(u for u in self.uploads if u["id"] == upload_id)

    def put_object(self, Bucket, Key, Body, **kwargs):
        self.put_keys.append(Key)
        self.objects[Key] = Body.read() if hasattr(Body, "read") else Body

    def create_multipart_upload(self, Bucket, Key, **kwargs):
        upload = {"id": f"upload-{len(self.uploads)}", "key": Key, "parts": {}}
        self.uploads.append(upload)
        return {"UploadId": upload["id"]}

    def upload_part(self, Bucket, Key, UploadId, PartNumber, Body, **kwargs):
        body = Body.read() if hasattr(Body, "read") else Body
        self._by_id(UploadId)["parts"][PartNumber] = body
        return {"ETag": f"etag-{PartNumber}"}

    def complete_multipart_upload(self, Bucket, Key, UploadId, MultipartUpload):
        upload = self._by_id(UploadId)
        self.objects[Key] = b"".join(
            upload["parts"][part["PartNumber"]] for part in MultipartUpload["Parts"])

    def abort_multipart_upload(self, Bucket, Key, UploadId, **kwargs):
        self.aborted.append(UploadId)


def _blob(n):
    """Random text: gzip can't crush it, so sizes stay near n/1.33."""
    import base64
    import os

    return base64.b64encode(os.urandom(n)).decode("ascii")


def test_backup_streams_big_tables_as_multipart_parts(monkeypatch):
    """A multi-GB snapshot must not have to fit in Lambda's /tmp: once the
    gzip stream outgrows one part size, finished chunks upload as S3
    multipart parts and the object reassembles byte-for-byte."""
    monkeypatch.setattr(backup, "MULTIPART_PART_SIZE", 64 * 1024)
    items = [{"id": {"S": f"item-{i}"}, "blob": {"S": _blob(4096)}}
             for i in range(64)]  # distinct random blobs: ~350 KB compressed
    s3 = MultipartS3()
    stats = backup.backup_table(FakeDynamo({"big": items}), s3, "big", "bucket", "day")

    assert len(s3.uploads) == 1
    parts = s3.uploads[0]["parts"]
    assert len(parts) >= 2
    # All but the final part ride the part-size minimum.
    assert all(len(body) == backup.MULTIPART_PART_SIZE
               for number, body in parts.items() if number < len(parts))
    assert s3.objects[stats["key"]] == b"".join(
        parts[number] for number in sorted(parts))
    assert stats["bytes"] == len(s3.objects[stats["key"]])
    snapshot = gzip.decompress(s3.objects[stats["key"]])
    assert [json.loads(line) for line in snapshot.decode().splitlines()] == items
    assert s3.aborted == []


def test_backup_small_tables_skip_multipart(monkeypatch):
    monkeypatch.setattr(backup, "MULTIPART_PART_SIZE", 64 * 1024)
    s3 = MultipartS3()
    stats = backup.backup_table(
        FakeDynamo({"tiny": [{"id": {"S": "one"}}]}), s3, "tiny", "bucket", "day")
    assert s3.uploads == []
    assert stats["key"] in s3.put_keys


def test_backup_aborts_the_upload_when_a_scan_dies_mid_table(monkeypatch):
    monkeypatch.setattr(backup, "MULTIPART_PART_SIZE", 64 * 1024)

    class DiesOnSecondPage:
        def get_paginator(self, operation):
            return self

        def paginate(self, **kwargs):
            yield {"Items": [{"id": {"S": f"i-{i}"}, "blob": {"S": _blob(4096)}}
                             for i in range(32)]}  # > one part size
            raise RuntimeError("scan died")

    s3 = MultipartS3()
    with pytest.raises(RuntimeError, match="scan died"):
        backup.backup_table(DiesOnSecondPage(), s3, "big", "bucket", "day")
    assert len(s3.aborted) == 1  # no orphaned upload accruing storage cost
    assert s3.objects == {}


def _load_yaml(path):
    """Load a YAML file, mapping !Ref values to {'Ref': name} and dropping
    every other tag's value."""
    import yaml

    class Loader(yaml.SafeLoader):
        pass

    def construct(loader, suffix, node):
        if suffix == "Ref":
            return {"Ref": node.value}
        if suffix == "Sub":
            return {"Fn::Sub": node.value}
        return None

    Loader.add_multi_constructor("!", construct)
    return yaml.load(Path(path).read_text(), Loader=Loader)


INSPECTION_GRANTS = {
    ("cloudformation", "describe_stack_resource"): "cloudformation:DescribeStackResource",
    ("lambda", "get_function_configuration"): "lambda:GetFunctionConfiguration",
    ("lambda", "invoke"): "lambda:InvokeFunction",
    ("cloudwatch", "describe_alarms"): "cloudwatch:DescribeAlarms",
    ("cloudwatch", "get_metric_statistics"): "cloudwatch:GetMetricStatistics",
    ("logs", "filter_log_events"): "logs:FilterLogEvents",
    ("dynamodb", "describe_table"): "dynamodb:DescribeTable",
    ("s3", "get_object"): "s3:GetObject",
    ("s3", "head_object"): "s3:GetObject",
    ("s3", "list_objects_v2"): "s3:ListBucket",
}


def _inspection_step_index(deploy_workflow):
    steps = deploy_workflow["jobs"]["deploy"]["steps"]
    named = {step.get("name"): i for i, step in enumerate(steps) if step.get("name")}
    i = named["Inspect DynamoDB backup health (read-only)"]
    j = named["Deploy backup inspection access policy stack"]
    assert j < i, "the access policy stack must deploy before the inspection step"
    return i


def test_backup_inspection_grants_match_inspection_script():
    """The backup health checks can only verify while their IAM grants cover
    every boto3 call scripts/inspect_backup.py makes — until 2026-11-12 they
    did not, so a failing backup's only evidence line was BACKUP_UNVERIFIED.
    This fails when the script and infra/backup-inspection-access.yaml drift
    apart. The deploy workflow and the dispatchable backup-inspection
    workflow both run the same script, so one check covers both."""
    import re

    deploy_workflow = _load_yaml(".github/workflows/deploy.yml")
    step = deploy_workflow["jobs"]["deploy"]["steps"][_inspection_step_index(deploy_workflow)]
    # The exact command, one line: when the step moved from an inline heredoc
    # to this script, a leftover "PY" terminator line survived the edit and
    # the script died on the stray argument — silently, behind
    # continue-on-error (2026-11-19). Pin the whole run string.
    assert step["run"].strip() == "uv run --with boto3 python scripts/inspect_backup.py"
    dispatch = _load_yaml(".github/workflows/backup-inspection.yml")
    dispatch_runs = [s.get("run", "") for s in dispatch["jobs"]["inspect"]["steps"]]
    assert any("scripts/inspect_backup.py" in run for run in dispatch_runs)
    assert any("--invoke" in run for run in dispatch_runs)

    script = Path("scripts/inspect_backup.py").read_text()
    # Every client creation maps its service; methods come from the vars
    # named at creation. A plain assignment ends right after the quotes; a
    # configured one (the invoke client's read timeout) carries kwargs, so
    # only match its first line — `config`-style locals that merely hold a
    # call result are deliberately not client vars.
    plain = dict(re.findall(
        r"^\s*(\w+) = boto3\.client\('([^']+)'\)\s*$", script, re.MULTILINE))
    configured = dict(re.findall(
        r"^\s*(\w+) = boto3\.client\('([^']+)',", script, re.MULTILINE))
    calls = set(re.findall(r"boto3\.client\('([^']+)'\)\.(\w+)\(", script))
    for var, service in {**plain, **configured}.items():
        calls.update((service, method) for method in re.findall(rf"\b{var}\.(\w+)\(", script))

    infra = _load_yaml("infra/backup-inspection-access.yaml")
    policy = infra["Resources"]["GithubDeployBackupInspectionAccess"]["Properties"]
    assert policy["Roles"] == ["dapier-github-actions-deploy"]
    granted = {action
               for statement in policy["PolicyDocument"]["Statement"]
               for action in statement["Action"]}

    used = set()
    for service, method in sorted(calls):
        assert (service, method) in INSPECTION_GRANTS, \
            f"boto3 call {service}.{method} is not mapped — extend the map and the grants"
        action = INSPECTION_GRANTS[service, method]
        assert action in granted, f"{action} missing from backup inspection grants"
        used.add(action)

    unneeded = granted - used
    assert not unneeded, f"grants the inspection step never uses: {sorted(unneeded)}"
    # Snapshot reads stay scoped to the backup bucket's backups/ prefix; the
    # function-config, invoke and log grants name the backup function only.
    def arns(actions):
        found = []
        for statement in policy["PolicyDocument"]["Statement"]:
            if any(action in statement["Action"] for action in actions):
                for resource in statement["Resource"] if isinstance(
                        statement["Resource"], list) else [statement["Resource"]]:
                    found.append(resource["Fn::Sub"])
        return found

    assert arns(["s3:GetObject"]) == [
        "arn:${AWS::Partition}:s3:::dapier-backups-${AWS::AccountId}/backups/*"]
    function_arn = ("arn:${AWS::Partition}:lambda:${AWS::Region}:"
                    "${AWS::AccountId}:function:dapier-BackupFunction-*")
    assert arns(["lambda:GetFunctionConfiguration"]) == [function_arn]
    assert arns(["lambda:InvokeFunction"]) == [function_arn]
    # The physical function/log-group names carry a random CloudFormation
    # suffix (dapier-BackupFunction-XXXXXXXX), so every name pattern must
    # wildcard inside the name. A pattern ending "...BackupFunction:*" only
    # matches a function literally named dapier-BackupFunction, and the live
    # calls stay AccessDenied — that is exactly how 2026-11-12's first
    # version of the log grant failed.
    (log_group_arn,) = arns(["logs:FilterLogEvents"])
    name = log_group_arn.split("log-group:", 1)[1]
    assert name == "/aws/lambda/dapier-BackupFunction*"


def test_backup_file_body_round_trips_through_s3():
    import boto3
    from moto import mock_aws

    with mock_aws():
        s3 = boto3.client("s3", region_name="eu-west-1")
        s3.create_bucket(Bucket="backup-test", CreateBucketConfiguration={
            "LocationConstraint": "eu-west-1"})
        items = [{"id": {"S": "first"}}, {"id": {"S": "second"}}]
        stats = backup.backup_table(FakeDynamo({"table": items}), s3,
                                    "table", "backup-test", "day")
        obj = s3.get_object(Bucket="backup-test", Key=stats["key"])
        assert obj["ContentLength"] == stats["bytes"]
        assert obj["ContentEncoding"] == "gzip"
        assert [json.loads(line) for line in gzip.decompress(obj["Body"].read()).splitlines()] == items


class FakeDynamo:
    def __init__(self, tables):
        self._tables = tables

    def get_paginator(self, operation):
        assert operation == "scan"
        return self

    def paginate(self, TableName=None, **_):
        if TableName not in self._tables:
            raise RuntimeError(f"scan failed for {TableName}")
        return [{"Items": self._tables[TableName]}]


class FakeS3:
    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body, **kwargs):
        self.objects[Key] = Body.read() if hasattr(Body, "read") else Body


class BrokenManifestS3(FakeS3):
    """The bucket itself rejects writes — the S3-side catastrophe case."""

    def put_object(self, Bucket, Key, Body, **kwargs):
        if "manifest.json" in Key:
            raise RuntimeError("The specified bucket does not exist")
        super().put_object(Bucket, Key, Body, **kwargs)


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("BACKUP_BUCKET", "dapier-backups-test")
    monkeypatch.setenv("CONNECTIONS_TABLE", "dapier-ConnectionsTable-x")
    monkeypatch.setenv("CREDENTIALS_TABLE", "dapier-CredentialsTable-x")
    monkeypatch.setattr(backup, "_claim_email", lambda day: True)
    monkeypatch.setattr(backup, "_release_email", lambda day: None)
    monkeypatch.delenv("BACKUP_ALERT_EMAIL", raising=False)


def test_backup_writes_ndjson_and_manifest(env, monkeypatch):
    items = [
        {"connection_id": "c1", "scopes": {"read", "write"}, "count": Decimal("3"),
         "ratio": Decimal("2.5")},
        {"connection_id": "c2", "count": Decimal("0")},
    ]
    fake_s3 = FakeS3()
    monkeypatch.setattr(backup, "_dynamodb", lambda: FakeDynamo({
        "dapier-ConnectionsTable-x": items,
        "dapier-CredentialsTable-x": [],
    }))
    monkeypatch.setattr(backup, "_s3", lambda: fake_s3)

    manifest = backup.handler({}, {})

    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert manifest["tables"]["dapier-ConnectionsTable-x"]["items"] == 2
    assert manifest["tables"]["dapier-CredentialsTable-x"]["items"] == 0

    snapshot = gzip.decompress(
        fake_s3.objects[f"backups/{day}/dapier-ConnectionsTable-x.json.gz"]
    )
    restored = [json.loads(line) for line in snapshot.decode().splitlines()]
    assert restored[0]["connection_id"] == "c1"
    assert restored[0]["count"] == 3
    assert restored[0]["ratio"] == 2.5
    assert set(restored[0]["scopes"]) == {"read", "write"}
    # An empty table still gets a (valid, empty) snapshot file.
    empty = gzip.decompress(
        fake_s3.objects[f"backups/{day}/dapier-CredentialsTable-x.json.gz"]
    )
    assert empty == b""
    assert manifest["date"] == json.loads(
        fake_s3.objects[f"backups/{day}/manifest.json"]
    )["date"]


def test_backup_raises_after_attempting_every_table(env, monkeypatch):
    posts = []
    monkeypatch.setattr(
        backup, "_send_email",
        lambda payload: posts.append(payload),
    )
    monkeypatch.setenv("BACKUP_ALERT_EMAIL", "ops@example.com")
    fake_s3 = FakeS3()
    monkeypatch.setattr(backup, "_dynamodb", lambda: FakeDynamo({
        "dapier-ConnectionsTable-x": [{"connection_id": "c1"}],
        "dapier-CredentialsTable-x": None,  # paginate raises
    }))
    monkeypatch.setattr(backup, "_s3", lambda: fake_s3)

    with pytest.raises(RuntimeError, match="Credentials"):
        backup.handler({}, {})

    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # The healthy table was still backed up before the raise.
    assert f"backups/{day}/dapier-ConnectionsTable-x.json.gz" in fake_s3.objects
    manifest = json.loads(fake_s3.objects[f"backups/{day}/manifest.json"])
    assert "error" in manifest["tables"]["dapier-CredentialsTable-x"]

    # The failure was reported by email, with the failed table and a per-day
    # idempotency key so re-invocations don't spam.
    assert len(posts) == 1
    payload = posts[0]
    assert payload["email"] == "ops@example.com"
    assert payload["template_key"] == "cli-message"
    assert payload["idempotency_key"] == f"dapier-backup-failure-{day}"
    assert "1 of 2 tables" in payload["context"]["subject"]
    assert "dapier-CredentialsTable-x" in payload["context"]["body"]


def test_successful_backup_sends_no_email(env, monkeypatch):
    posts = []
    monkeypatch.setattr(
        backup, "_send_email",
        lambda payload: posts.append(payload),
    )
    monkeypatch.setenv("BACKUP_ALERT_EMAIL", "ops@example.com")
    monkeypatch.setattr(backup, "_dynamodb", lambda: FakeDynamo({
        "dapier-ConnectionsTable-x": [{"connection_id": "c1"}],
        "dapier-CredentialsTable-x": [],
    }))
    monkeypatch.setattr(backup, "_s3", lambda: FakeS3())

    backup.handler({}, {})

    assert posts == []  # failure-only, like rds-export's notify_run.sh


def test_failure_email_is_skipped_when_unconfigured_and_never_raises(env, monkeypatch):
    def broken_post(payload):
        raise OSError("no network")

    monkeypatch.setattr(backup, "_send_email", broken_post)
    monkeypatch.setenv("BACKUP_ALERT_EMAIL", "ops@example.com")
    monkeypatch.setattr(backup, "_dynamodb", lambda: FakeDynamo({
        "dapier-ConnectionsTable-x": None,
    }))
    monkeypatch.setattr(backup, "_s3", lambda: FakeS3())

    # A broken mailer must not mask the original backup error...
    with pytest.raises(RuntimeError, match="Connections"):
        backup.handler({}, {})


def test_failure_email_skipped_without_config(env, monkeypatch):
    posts = []
    monkeypatch.setattr(
        backup, "_send_email",
        lambda payload: posts.append(payload),
    )
    # env fixture removes BACKUP_ALERT_EMAIL.
    monkeypatch.setattr(backup, "_dynamodb", lambda: FakeDynamo({
        "dapier-ConnectionsTable-x": None,
    }))
    monkeypatch.setattr(backup, "_s3", lambda: FakeS3())

    with pytest.raises(RuntimeError):
        backup.handler({}, {})

    assert posts == []  # only the CloudWatch alarm remains


def test_manifest_failure_also_triggers_the_email(env, monkeypatch):
    """If even the manifest can't be written (bucket gone), the email must
    still go out — this is exactly the all-bets-are-off case."""
    posts = []
    monkeypatch.setattr(
        backup, "_send_email",
        lambda payload: posts.append(payload),
    )
    monkeypatch.setenv("BACKUP_ALERT_EMAIL", "ops@example.com")
    monkeypatch.setattr(backup, "_dynamodb", lambda: FakeDynamo({
        "dapier-ConnectionsTable-x": [],
        "dapier-CredentialsTable-x": [],
    }))
    monkeypatch.setattr(backup, "_s3", lambda: BrokenManifestS3())

    with pytest.raises(RuntimeError, match="manifest.json"):
        backup.handler({}, {})

    assert len(posts) == 1
    payload = posts[0]
    assert "0 of 2 tables" in payload["context"]["subject"]
    assert "manifest.json" in payload["context"]["body"]
    assert "The specified bucket does not exist" in payload["context"]["body"]


def test_failure_email_does_not_send_when_already_claimed(env, monkeypatch):
    posts = []
    monkeypatch.setenv("BACKUP_ALERT_EMAIL", "ops@example.com")
    monkeypatch.setattr(backup, "_claim_email", lambda day: False)
    monkeypatch.setattr(backup, "_send_email", posts.append)
    backup.notify_failure("2026-10-13", {}, {"table": "failed"})
    assert posts == []


def test_failed_email_releases_the_daily_claim(env, monkeypatch):
    released = []
    monkeypatch.setenv("BACKUP_ALERT_EMAIL", "ops@example.com")
    monkeypatch.setattr(backup, "_release_email", released.append)
    monkeypatch.setattr(backup, "_send_email", lambda payload: (_ for _ in ()).throw(OSError("SES unavailable")))
    backup.notify_failure("2026-10-13", {}, {"table": "failed"})
    assert released == ["2026-10-13"]


def test_backup_email_uses_iam_ses_without_a_mailer_key(monkeypatch):
    from src.dapier.engine import notify
    calls = []
    class Ses:
        def send_email(self, **kwargs):
            calls.append(kwargs)
            return {"MessageId": "sent"}
    monkeypatch.setattr(notify, "ses_client", lambda: Ses())
    monkeypatch.setenv("DAPIER_EMAIL_SENDER", "dapier@example.com")
    backup._send_email({"email": "ops@example.com", "context": {"subject": "failure", "body": "details"}})
    assert calls[0]["Source"] == "dapier@example.com"
    assert calls[0]["Destination"] == {"ToAddresses": ["ops@example.com"]}
    assert calls[0]["Message"]["Body"]["Text"]["Data"] == "details"

    monkeypatch.setenv("DAPIER_ALERT_SENDER", "DTC Alerts <alerts@dtcdev.click>")
    backup._send_email({"email": "ops@example.com", "context": {"subject": "failure", "body": "details"}})
    assert calls[1]["Source"] == "DTC Alerts <alerts@dtcdev.click>"


def test_daily_claim_is_atomic_and_duplicate_is_skipped(monkeypatch):
    from botocore.exceptions import ClientError
    calls = []
    class Table:
        def put_item(self, **kwargs):
            calls.append(kwargs)
            if len(calls) > 1:
                raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem")
    class Dynamo:
        def Table(self, name):
            assert name == "executions"
            return Table()
    monkeypatch.setenv("EXECUTIONS_TABLE", "executions")
    monkeypatch.setattr(backup.boto3, "resource", lambda name: Dynamo())
    assert backup._claim_email("2026-10-13") is True
    assert backup._claim_email("2026-10-13") is False
    assert calls[0]["ConditionExpression"] == "attribute_not_exists(execution_id)"
    assert calls[0]["Item"]["execution_id"] == "backup-failure-notice:2026-10-13"


def test_backup_excludes_external_team_secrets_table(env, monkeypatch):
    # The shared secret-store configuration is inherited from Globals, but
    # BackupFunction may only scan Dapier-owned tables. A scan of this external
    # table would fail IAM authorization and mark every daily backup failed.
    monkeypatch.setenv("TEAM_SECRETS_TABLE", "dataops-v1-secrets")
    monkeypatch.setenv("FUTURE_TABLE", "dapier-FutureTable-x")
    scanned = []

    class RecordingDynamo(FakeDynamo):
        def paginate(self, TableName=None, **kwargs):
            scanned.append(TableName)
            return super().paginate(TableName=TableName, **kwargs)

    tables = {
        "dapier-ConnectionsTable-x": [],
        "dapier-CredentialsTable-x": [],
        "dapier-FutureTable-x": [],
    }
    fake_s3 = FakeS3()
    monkeypatch.setattr(backup, "_dynamodb", lambda: RecordingDynamo(tables))
    monkeypatch.setattr(backup, "_s3", lambda: fake_s3)

    manifest = backup.handler({}, {})

    assert scanned == sorted(tables)
    assert set(manifest["tables"]) == set(tables)
    assert len(fake_s3.objects) == len(tables) + 1
    assert all("dataops-v1-secrets" not in key for key in fake_s3.objects)


def test_backup_failure_email_routes_only_to_dev_inbox():
    """Infra (backup) alerts go only to dev@dtcdev.click (intake 19fefe6f):
    the template default and the explicit deploy override must both be dev@,
    since CloudFormation keeps the previous value when an override is
    omitted and the old default was a personal inbox.

    Run-failure and operator mail is different (intake 779495a8): the
    operator inboxes NotifyEmail + NotifyExtraEmails deliberately include a
    human's address, so the personal-address ban is scoped to the backup
    path instead of the whole template — backup.py must read only
    BACKUP_ALERT_EMAIL and never the notify lists."""
    template = _load_yaml("template.yaml")
    assert template["Parameters"]["BackupAlertEmail"]["Default"] == "dev@dtcdev.click"
    deploy = Path("scripts/deploy.sh").read_text()
    assert 'BackupAlertEmail: "${BACKUP_ALERT_EMAIL:-dev@dtcdev.click}"' in deploy
    backup_src = Path("src/dapier/backup.py").read_text()
    assert "DAPIER_NOTIFY_EMAIL" not in backup_src
    assert "DAPIER_NOTIFY_EXTRA_EMAILS" not in backup_src
    for personal in ("alexey.s.grigoriev@gmail.com", "orin@dtcdev.click"):
        assert personal not in Path("template.yaml").read_text()
        assert personal not in deploy
    # The backup recipient parameter itself must never default to a personal inbox.
    assert "alexey@datatalks.club" not in template["Parameters"]["BackupAlertEmail"]["Default"]
    assert "alexey@datatalks.club" not in Path("src/dapier/backup.py").read_text()
