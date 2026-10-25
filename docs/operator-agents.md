# Operator directory

A directory row in `src/dapier/auth/actors.py` names a datatalks.club
account (email plus alias). A row with the `operator` role holds the same
rights as the operator allowlist: the full console and the full agent API.
A row with the `agent` role is least privilege — it passes only
`AGENT_ACTIONS` (or `workflow.*`) on the CLI bearer gate, stays
owner-scoped by the G17 write gate, and is refused on console routes
outside `console_allowed`. The first successful sign-in stores the DTC
subject beside the email in the device-sessions table
(`actor#<email>`, no expiry). Until that subject exists, `--owner <alias>`
matches the alias and the email only.

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
directory row keeps the stable alias so `--owner orin` and the console
"View as" control keep working.

## Seeing another person's work

A full operator stays signed in as themselves and narrows a list:

```bash
dapier runs list --owner orin
dapier agent-tasks list --owner orin
dapier runs list --as <subject-id>
dapier agent-tasks list --assignee <subject-id>
```

`--owner` must name someone in the directory. `--as` and `--assignee` take a subject id, or the same alias. The console has a View as control in the page header, next to Refresh. Choosing a person filters workflows, runs, and agent tasks and hides write controls. The filter does not switch the account a write runs as.
