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
# Empty allowlist denies everyone (fail closed), so default to the maintainer;
# override with OPERATOR_EMAILS="a@x,b@y" for additional operators.
OPERATOR_EMAILS="${OPERATOR_EMAILS:-alexey.s.grigoriev@gmail.com}"

sam deploy --config-env sandbox --parameter-overrides \
  DomainName=dapier.dtcdev.click \
  DomainCertificateArn=arn:aws:acm:eu-west-1:817685572750:certificate/df237eb8-9d9a-4d48-8aa3-2fbe99018cd9 \
  HostedZoneId=Z05963572WVWFHDQZH5NE \
  HtmlRendererImageUri=817685572750.dkr.ecr.eu-west-1.amazonaws.com/dapier-html-renderer:20260712-lambda \
  InboundEmailTopicArn=arn:aws:sns:us-east-1:817685572750:datamailer-sandbox-inbound-email-events \
  DatamailerInboundBucketName=datamailer-sandbox-817685572750-inbound-mail \
  YouTubeChannelIds=UCDvErgK0j5ur3aLgn6U-LqQ \
  DataOpsIntakeUrl=https://el4jt4z2k4lnxwvoqawrdcsedm0axjzc.lambda-url.eu-west-1.on.aws/api/v1/intake/email-documents \
  AuthBaseUrl=https://auth.dtcdev.click \
  AuthClientId="$AUTH_CLIENT_ID" \
  AuthIssuer="$AUTH_ISSUER" \
  AuthJwksUrl="$AUTH_JWKS_URL" \
  OperatorEmails="$OPERATOR_EMAILS" \
  "$@"
