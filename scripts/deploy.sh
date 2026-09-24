#!/usr/bin/env bash
# Deploy via SAM, resolving the shared Cognito app client at deploy time
# instead of baking its ID into samconfig.toml. Cognito assigns a fresh
# client_id whenever the app client is recreated, so a hardcoded value goes
# stale silently (login starts failing with no code change on this side);
# reading it from the shared-auth stack's own outputs means it can't drift.
set -euo pipefail
cd "$(dirname "$0")/.."

AUTH_STACK="${AUTH_STACK:-dtcdev-shared-auth}"
auth_output() {
  aws cloudformation describe-stacks --region us-east-1 --stack-name "$AUTH_STACK" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}
AUTH_CLIENT_ID="${AUTH_CLIENT_ID:-$(auth_output DapierClientId)}"
AUTH_ISSUER="${AUTH_ISSUER:-$(auth_output IssuerUrl)}"
AUTH_JWKS_URL="${AUTH_JWKS_URL:-$(auth_output JwksUrl)}"
# Unset means every account that completes DTC sign-in may administer the
# console (the DTC IdP only issues datatalks.club accounts); set
# OPERATOR_EMAILS="a@datatalks.club,b@datatalks.club" to restrict it.
OPERATOR_EMAILS="${OPERATOR_EMAILS:-}"

# Overrides go through a JSON file: the shorthand key=value format rejects
# empty values, and CloudFormation keeps a parameter's previous value when an
# override is omitted — so OperatorEmails must be passed explicitly (possibly
# as '') to actually clear a previously deployed allowlist.
params_file="$(mktemp)"
trap 'rm -f "$params_file"' EXIT
cat > "$params_file" <<EOF
[
  {"ParameterKey": "DomainName", "ParameterValue": "dapier.dtcdev.click"},
  {"ParameterKey": "DomainCertificateArn", "ParameterValue": "arn:aws:acm:eu-west-1:817685572750:certificate/df237eb8-9d9a-4d48-8aa3-2fbe99018cd9"},
  {"ParameterKey": "HostedZoneId", "ParameterValue": "Z05963572WVWFHDQZH5NE"},
  {"ParameterKey": "HtmlRendererImageUri", "ParameterValue": "817685572750.dkr.ecr.eu-west-1.amazonaws.com/dapier-html-renderer:20260712-lambda"},
  {"ParameterKey": "InboundEmailTopicArn", "ParameterValue": "arn:aws:sns:us-east-1:817685572750:datamailer-sandbox-inbound-email-events"},
  {"ParameterKey": "DatamailerInboundBucketName", "ParameterValue": "datamailer-sandbox-817685572750-inbound-mail"},
  {"ParameterKey": "YouTubeChannelIds", "ParameterValue": "UCDvErgK0j5ur3aLgn6U-LqQ"},
  {"ParameterKey": "DataOpsIntakeUrl", "ParameterValue": "https://el4jt4z2k4lnxwvoqawrdcsedm0axjzc.lambda-url.eu-west-1.on.aws/api/v1/intake/email-documents"},
  {"ParameterKey": "AuthBaseUrl", "ParameterValue": "https://auth.dtcdev.click"},
  {"ParameterKey": "AuthClientId", "ParameterValue": "$AUTH_CLIENT_ID"},
  {"ParameterKey": "AuthIssuer", "ParameterValue": "$AUTH_ISSUER"},
  {"ParameterKey": "AuthJwksUrl", "ParameterValue": "$AUTH_JWKS_URL"},
  {"ParameterKey": "OperatorEmails", "ParameterValue": "$OPERATOR_EMAILS"},
  {"ParameterKey": "OperatorSubjects", "ParameterValue": "${OPERATOR_SUBJECTS:-}"},
  {"ParameterKey": "DropboxRootPath", "ParameterValue": "${DROPBOX_ROOT_PATH:-}"}
]
EOF

sam deploy --config-env sandbox --parameter-overrides "file://$params_file" "$@"
