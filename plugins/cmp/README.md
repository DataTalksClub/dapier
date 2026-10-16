# Course Management Platform connection

Dapier stores CMP API tokens as write-only connection secrets. Import with
`uv run dapier connections import cmp-production --provider cmp --token-file -`.
Supply only the raw token through stdin; never pass a whole `.env` file or put a token in argv.
`uv run dapier connections test cmp-production` verifies authenticated production course-list access.
The console Connections view offers the same CMP token setup and maintenance.

CMP currently exposes no authenticated user identity endpoint. The verified
account is **production platform access**, not a claim about the token owner's
identity or permissions. Token grants restrict which Dapier agents can use the
connection; they do not narrow the underlying CMP token's privileges.

After granting the connection to an agent, fetch the token in-script (see
the `google-data-pull` skill's `dtoken.py` helper) and send
`Authorization: Token <value>` to CMP from a local variable. Do not log it.
Course changes remain the responsibility of CMP and require operator
authorization; connection setup makes no course changes.


## Read-only workflow action

`cmp_read_course` takes `connection_id`, `course_slug`, and an optional timeout
of 1-30 seconds. It makes only a GET to the fixed production course endpoint,
using the stored connection's Token header. It refuses redirects, incorrect
providers or account bindings, unexpected payloads, and duplicate entity slugs.
Output contains allowlisted course facts and homework/project maps indexed by
slug, so native filter steps can stop an announcement when its expected dates
or required state differ. A read failure halts the action chain by default.
The API action registry, CLI catalog/test routes and console designer expose
this same action; no secret is a workflow field or result.
