# Operator directory

A directory row in `src/dapier/auth/actors.py` names a datatalks.club
account (email plus alias). A row with the `operator` role holds the same
rights as the operator allowlist: the full console and the full agent API.
A row with the `agent` role is least privilege — it passes only
`AGENT_ACTIONS` (or `workflow.*`) on the CLI bearer gate, stays
owner-scoped by the G17 write gate, and is refused on console routes
outside `console_allowed`. The first successful sign-in stores the DTC
subject beside the email in the device-sessions table
(`actor#<email>`, no expiry).

## Orin

| | |
| --- | --- |
| Email | `orin@datatalks.club` |
| Alias | `orin` |
| Role | `operator` |
| API token | none. Sign in with the console or `dapier auth login`. |

Orin holds the same role and rights as Alexey's dapier account:
workflows, runs, inbox, polls, schedules, errors, credential view, and
everything else the operator allowlist gates — reads and writes. The
directory row keeps the stable alias.

## Seeing another person's work

There are no per-identity views. Every signed-in user, Orin and Alexey
included, sees the same Home, workflows, runs, failures and connections.
Ownership still gates writes: an agent-role identity may change only
workflows it owns.
