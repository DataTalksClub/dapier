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
# The headless machine client behind the agent API; an auth stack that
# predates the SchedulerMachineClientId output yields "None", which leaves
# CLI token issuance disabled and agent requests failing closed (401/503).
AUTH_CLI_CLIENT_ID="${AUTH_CLI_CLIENT_ID:-$(auth_output SchedulerMachineClientId)}"
AUTH_CLI_CLIENT_ID="$(printf '%s' "$AUTH_CLI_CLIENT_ID" | head -n 1)"
if [ "$AUTH_CLI_CLIENT_ID" = "None" ]; then AUTH_CLI_CLIENT_ID=""; fi
# Unset means every account that completes DTC sign-in may administer the
# console (the DTC IdP only issues datatalks.club accounts); set
# OPERATOR_EMAILS="a@datatalks.club,b@datatalks.club" to restrict it.
OPERATOR_EMAILS="${OPERATOR_EMAILS:-}"

# Shared OAuth clients (Google for Calendar/YouTube, Dropbox), created once in
# each provider console with the redirect URI https://dapier.dtcdev.click/oauth/
# callback. Export GOOGLE_OAUTH_CLIENT_ID/SECRET and DROPBOX_OAUTH_CLIENT_ID/
# SECRET to (re)set them; a variable left unset is omitted from the overrides
# file so CloudFormation keeps the previously deployed value instead of
# clearing it — do not "fix" that by passing empty strings.
oauth_override_lines=""
for pair in \
  "GoogleOAuthClientId:GOOGLE_OAUTH_CLIENT_ID" \
  "GoogleOAuthClientSecret:GOOGLE_OAUTH_CLIENT_SECRET" \
  "DropboxOAuthClientId:DROPBOX_OAUTH_CLIENT_ID" \
  "DropboxOAuthClientSecret:DROPBOX_OAUTH_CLIENT_SECRET"; do
  param="${pair%%:*}" env_var="${pair##*:}"
  value="${!env_var:-}"
  if [ -n "$value" ]; then
    oauth_override_lines+="${param}: \"${value}\""$'\n'
  fi
done

# Optional: Secrets Manager secret name holding the GitHub token the console
# designer uses to commit workflows/*.yaml (Contents: read/write on this
# repo). Like the OAuth clients, omitted when unset so CloudFormation keeps
# the previously deployed value.
github_token_lines=""
if [ -n "${GITHUB_WORKFLOWS_TOKEN_SECRET:-}" ]; then
  github_token_lines="GithubWorkflowsTokenSecret: \"${GITHUB_WORKFLOWS_TOKEN_SECRET}\""$'\n'
fi

# Overrides go through a YAML file: SAM rejects empty values in the shorthand
# key=value format and CloudFormation keeps a parameter's previous value when
# an override is omitted — so this file passes every parameter explicitly
# (an empty value clears it). SAM only accepts .yaml/.yml/.toml here, not JSON.
params_file="$(mktemp --suffix .yaml)"
trap 'rm -f "$params_file"' EXIT
cat > "$params_file" <<EOF
DomainName: "dapier.dtcdev.click"
DomainCertificateArn: "arn:aws:acm:eu-west-1:817685572750:certificate/df237eb8-9d9a-4d48-8aa3-2fbe99018cd9"
HostedZoneId: "Z05963572WVWFHDQZH5NE"
HtmlRendererImageUri: "817685572750.dkr.ecr.eu-west-1.amazonaws.com/dapier-html-renderer:20260712-lambda"
InboundEmailTopicArn: "arn:aws:sns:us-east-1:817685572750:datamailer-sandbox-inbound-email-events"
DatamailerInboundBucketName: "datamailer-sandbox-817685572750-inbound-mail"
DataOpsIntakeUrl: "https://el4jt4z2k4lnxwvoqawrdcsedm0axjzc.lambda-url.eu-west-1.on.aws/api/v1/intake/email-documents"
AuthBaseUrl: "https://auth.dtcdev.click"
AuthClientId: "$AUTH_CLIENT_ID"
AuthIssuer: "$AUTH_ISSUER"
AuthJwksUrl: "$AUTH_JWKS_URL"
AuthCliClientId: "$AUTH_CLI_CLIENT_ID"
OperatorEmails: "$OPERATOR_EMAILS"
OperatorSubjects: "${OPERATOR_SUBJECTS:-}"
${oauth_override_lines}${github_token_lines}
EOF

sam deploy --config-env sandbox --parameter-overrides "file://$params_file" "$@"
