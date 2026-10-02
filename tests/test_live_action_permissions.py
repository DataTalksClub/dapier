"""Live execution uses the same provider capabilities in API and worker."""
from pathlib import Path

import yaml


def test_api_and_worker_can_send_attachment_email_and_api_can_stage_intake():
    class Loader(yaml.SafeLoader):
        pass

    Loader.add_multi_constructor(
        "!", lambda loader, tag, node: loader.construct_scalar(node)
        if isinstance(node, yaml.ScalarNode)
        else loader.construct_sequence(node, deep=True)
        if isinstance(node, yaml.SequenceNode)
        else loader.construct_mapping(node, deep=True),
    )
    resources = yaml.load(Path("template.yaml").read_text(), Loader=Loader)["Resources"]
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
