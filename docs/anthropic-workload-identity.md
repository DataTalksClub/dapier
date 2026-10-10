# Anthropic workload identity for Dapier Lambda jobs

Dapier's sandbox Lambda workflow worker can use AWS-signed identity tokens
to authenticate to the direct Anthropic API. This prepares authentication
for future Anthropic jobs; it does not implement a new action or change the
existing host-based `agent` runner or OpenAI-compatible `ai_complete` action.

## Configuration inventory

Recorded 2026-10-10:

| Setting | Value |
| --- | --- |
| AWS account | `817685572750` (sandbox) |
| Application stack / region | `dapier` / `eu-west-1` |
| Lambda | `dapier-WorkerFunction-MQiKo69YkFKA` |
| Trusted IAM role | `arn:aws:iam::817685572750:role/dapier-WorkerFunctionRole-OBH37M0PnOup` |
| STS issuer | `https://a1dce6f2-3ca7-4659-bf0d-da6ae3d2ac73.tokens.sts.global.api.aws` |
| Audience | `https://api.anthropic.com` |
| STS token | RS256, at most 900 seconds |
| Anthropic scope / token lifetime | `workspace:developer` / 600 seconds |

AWS account outbound federation was enabled and verified. Anthropic resource
creation is pending confirmation; record the issuer, rule, service account,
organization and workspace IDs here after saving the wizard. These identifiers
are configuration, not secrets. JWTs and exchanged access tokens are secrets.

## Ownership and AWS setup

Shared account and deployment infrastructure lives in `~/git/aws-infra`,
including `sandbox/dapier/template.github-actions.yaml`. The application
Lambda roles are SAM-generated from this repository's `template.yaml`.
Keep runtime policy changes here so application deployments preserve them.

Outbound federation is an account-level, one-time IAM setting. Use an
authorized sandbox operator session with a recent Boto3 version:

```python
import boto3

assert boto3.client("sts", region_name="eu-west-1").get_caller_identity()["Account"] == "817685572750"
iam = boto3.client("iam")
iam.enable_outbound_web_identity_federation()  # Once; skip if already enabled.
info = iam.get_outbound_web_identity_federation_info()
assert info["JwtVendingEnabled"]
print(info["IssuerIdentifier"])
```

`WorkerAnthropicIdentityToken` in `template.yaml` grants only the workflow
worker `sts:GetWebIdentityToken`. Its conditions require the Anthropic
audience, RS256 signing and a maximum 900-second identity-token lifetime.
The non-null audience check prevents an empty/missing list satisfying
`ForAllValues`. No IAM role trust-policy change is needed: Lambda continues
assuming its normal execution role.

To find the actual role after a deployment (generated names can change):

```sh
FUNCTION=$(aws cloudformation describe-stack-resource --region eu-west-1 \
  --stack-name dapier --logical-resource-id WorkerFunction \
  --query StackResourceDetail.PhysicalResourceId --output text)
aws lambda get-function-configuration --region eu-west-1 \
  --function-name "$FUNCTION" --query Role --output text
```

## Claude Console setup

1. Open [Workload identity](https://platform.claude.com/settings/workload-identity-federation)
   in the intended Anthropic organization. Select **Connect workload**, then **AWS**.
2. Enter the verified account STS issuer URL above, confirm outbound federation
   is enabled, and register the issuer using JWKS discovery.
3. Name the rule `dapier-lambda-sandbox`. Match the exact IAM role ARN above
   as the subject, with no trailing wildcard, and the Anthropic audience.
4. Restrict the rule to the intended workspace, choose `workspace:developer`
   and a 600-second access-token lifetime.
5. Create/select a dedicated `dapier-lambda-sandbox` service account with
   membership in that workspace. Save the rule and record the resource IDs.
6. Verify an exchange using a token minted by the Lambda execution role.
   A token from an operator or deployment role must not match this rule.

Reuse the account issuer for another sandbox workload, but give it a separate
rule and service account. If a later job runs in a separate Lambda, provision
its own restricted token policy and a rule for its exact role; it does not
inherit this worker's trust. Update the rule if SAM replaces the worker role.

## Future Lambda integration

Package recent `boto3` and `anthropic` SDK versions that expose
`get_web_identity_token` and `WorkloadIdentityCredentials`, respectively.
The current application requirements do not install the Anthropic SDK.
Set these non-secret values on the future job Lambda through its SAM template:

```text
ANTHROPIC_FEDERATION_RULE_ID=fdrl_...
ANTHROPIC_ORGANIZATION_ID=<organization UUID>
ANTHROPIC_SERVICE_ACCOUNT_ID=svac_...
ANTHROPIC_WORKSPACE_ID=wrkspc_...
```

Use a token-provider callback so refreshes obtain fresh STS tokens from the
Lambda's ambient credentials. A static token file would need its own refresh
mechanism. Use the regional STS endpoint explicitly:

```python
import os

import boto3
from anthropic import Anthropic, WorkloadIdentityCredentials

sts = boto3.client("sts", region_name="eu-west-1")

def identity_token():
    return sts.get_web_identity_token(
        Audience=["https://api.anthropic.com"],
        SigningAlgorithm="RS256",
        DurationSeconds=900,
    )["WebIdentityToken"]

client = Anthropic(credentials=WorkloadIdentityCredentials(
    identity_token_provider=identity_token,
    federation_rule_id=os.environ["ANTHROPIC_FEDERATION_RULE_ID"],
    organization_id=os.environ["ANTHROPIC_ORGANIZATION_ID"],
    service_account_id=os.environ["ANTHROPIC_SERVICE_ACCOUNT_ID"],
    workspace_id=os.environ["ANTHROPIC_WORKSPACE_ID"],
))
# The future job calls client.messages.create(...) with its chosen model.
```

Keep credentials in memory and never log either token. Configuration alone
does not route existing actions through Anthropic. Implement any future
provider integration in `plugins/`, preserving Console/CLI/API parity.
Domain work remains in the owning service (for example, DataOps).

## Verification and troubleshooting

From the workload, mint an STS token and exchange it with
`POST https://api.anthropic.com/v1/oauth/token`. The JSON request contains
`grant_type: urn:ietf:params:oauth:grant-type:jwt-bearer`, `assertion` (the JWT),
and the four configuration IDs above without their environment prefixes.
Check for a successful response and the expected expiry without printing
the access token. A model-list request with the returned Bearer token can
verify API authentication without generating paid inference.

- Disabled outbound federation: check `JwtVendingEnabled` in IAM.
- STS access denied: check the execution role's token policy and requested
  audience, algorithm and duration.
- Missing SDK method: update the packaged SDK and select regional STS.
- Exchange denied: inspect the Console's **Authentication events**; compare
  issuer, exact subject, audience, workspace membership and resource IDs.
- Revocation: disable/archive the Dapier rule, then remove its worker token
  permission if no longer needed. Account-wide federation may serve other
  workloads, so disabling it is a separate account decision.

## References

- [Anthropic AWS federation guide](https://platform.claude.com/docs/en/manage-claude/wif-providers/aws)
- [Anthropic WIF concepts](https://platform.claude.com/docs/en/manage-claude/workload-identity-federation)
- [Anthropic token exchange reference](https://platform.claude.com/docs/en/manage-claude/wif-reference)
- [AWS outbound federation policy controls](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_providers_outbound_policies.html)
