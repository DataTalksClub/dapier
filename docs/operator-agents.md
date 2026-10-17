# Operator agents

An operator agent is a datatalks.club account that can run its own
workflows and read the runs and agent tasks that belong to them. It is
not a full operator.

## Orin

| | |
| --- | --- |
| Email | `orin@datatalks.club` |
| Alias | `orin` |
| Role | `agent` |
| API token | none. Sign in with the console or `dapier auth login` when the account is ready. |

The role allows:

- workflows the account owns (list, save, publish, test, toggle, and the other workflow actions)
- runs of those workflows (list, show, export, replay, cancel, resolve)
- agent tasks of those workflows (list and show)
- a read of connections, workers, and the trigger sample, so a workflow can be built and a queued task can be seen

It does not allow credentials, grants, API tokens, OAuth clients, quota, audit, sender-policy changes, or storage. The directory entry lives in `src/dapier/auth/actors.py`. The first successful sign-in stores the DTC subject beside the email in the device-sessions table (`actor#orin@datatalks.club`, no expiry). Until that subject exists, `--owner orin` matches the alias and the email only.

## Seeing another person's work

A full operator stays signed in as themselves and narrows a list:

```bash
dapier runs list --owner orin
dapier agent-tasks list --owner orin
dapier runs list --as <subject-id>
dapier agent-tasks list --assignee <subject-id>
```

`--owner` must name someone in the directory. `--as` and `--assignee` take a subject id, or the same alias. The console has a View as control in the page header, next to Refresh. Choosing a person filters workflows, runs, and agent tasks and hides write controls. The filter does not switch the account a write runs as.
