# Headless agent runs

Dapier's `agent` action enqueues one headless job. A host worker claims it
through `/api/agent/host-jobs/*`, runs Claude Code in print mode, and reports
`succeeded`, `failed`, `timed_out`, or `interrupted`. The console's **Headless
runs** section and `dapier agent-tasks list` show the result. For email
triggers, Dapier also emails the sender a short completion report. An action
can override that address with `notify_to` or set it to an empty string to
disable the mail.

The host authenticates with a dedicated Dapier API token. Dapier stores only
its hash; the plaintext is issued once into a local owner-only file. The host
does not need AWS credentials or Aplexer.

## Configure the host

Install Claude Code and sign it in on the host. Then create the token from an
operator CLI session:

```sh
uv run dapier tokens create --name host-worker --agent host-worker \
  --output ~/.config/dapier/host-worker.token
mkdir -p ~/dapier-ws
mkdir -p ~/dapier-ws/.claude
ln -s ~/git/.agents/skills ~/dapier-ws/.claude/skills
uv run dapier worker --workspace-root ~/dapier-ws
```

`dapier worker` reads `~/.config/dapier/host-worker.token` by default. Use
`--token-file` for another location, `--max-runtime` to change the one-hour
per-job limit, and `--once` to poll once. The root defaults to `~/dapier-ws`;
`--workspace-root` changes it. A job with no `workspace` runs in that root.
An explicit relative workspace must exist beneath the root. An absolute
workspace must also resolve beneath the root; symlink escapes are rejected.
The symlink makes the shared skills under `~/git/.agents/skills` available
to Claude Code in the new workspace. Keep any project-specific skills in
the relevant project directory when adding workspaces beneath the root.

The worker starts `claude --print --output-format json` as a foreground child,
with no persisted Claude session. It supplies the prompt on stdin and does
not pass the Dapier token to the child. It runs noninteractively with Claude's
permission checks bypassed, so the host account and sender allow-list must
be trusted. Each run's stdout and stderr are owner-only files under
`<workspace-root>/.dapier-runs/`.

The worker heartbeats while the child runs. Dapier extends the SQS lease,
records the final status before acknowledging the queue message, and never
automatically reruns work that lost its lease after starting. An interrupted
run needs operator review before retrying, because a coding task may already
have changed files or called external services.

## Configure a future email route

Create a reserved address with `dapier emails save` or the console's Emails
view. The action needs only a prompt; omitting `workspace` uses the host root.
For a forwarded Zoom mail, include `{body.text.value}` in the prompt. This is
the inline plain-text body from Datamailer's inbound-email contract. If that
field is empty or the body exceeded the inline limit, inspect the trigger
input before enabling the workflow; large bodies are stored as private S3
objects rather than inserted into the prompt.

The shared `fetch-zoom` skill at
`/home/alexey/git/.agents/skills/fetch-zoom/SKILL.md` can fetch Zoom captions
or transcribe the recording if captions are absent. The prompt can point the
agent to that file and ask it to carry out the instructions at the top of the
forwarded message. No address or project-specific action is created by this
worker setup.
