# Course Management Platform connection

Dapier stores CMP API tokens as write-only connection secrets. Import with
`uv run dapier connections import cmp-production --provider cmp --token-file /private/raw-token`.
The file contains only the token; never pass a whole `.env` file or put a token in argv.
`uv run dapier connections test cmp-production` verifies authenticated production course-list access.
The console Connections view offers the same CMP token setup and maintenance.

CMP currently exposes no authenticated user identity endpoint. The verified
account is **production platform access**, not a claim about the token owner's
identity or permissions. Token grants restrict which Dapier agents can use the
connection; they do not narrow the underlying CMP token's privileges.

After granting the connection to an agent, use
`uv run dapier token exec cmp-production --agent course-telegram-management -- python your_cmp_script.py`.
The child reads `DAPIER_ACCESS_TOKEN` and sends `Authorization: Token <value>`
to CMP. Do not log it. Course changes remain the responsibility of CMP and
require operator authorization; connection setup makes no course changes.
