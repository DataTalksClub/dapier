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

## Forward a task

Send from an address in `dapier emails from list` to `agents@dtcdev.click`.
Put your task instructions above the forwarded message, for example, "Write a
Telegram article from this Zoom conversation." Include the Zoom shared
recording URL and passcode in the email body. The stored route is defined in
[`agents-email-trigger.json`](agents-email-trigger.json); apply changes with
`dapier emails save docs/agents-email-trigger.json` or the console's Emails
view. It uses the configured worker root, with no project pinned. The worker
reads the shared `fetch-zoom` skill when a Zoom link is present and emails a
completion report to the sender. Inspect runs with `dapier agent-tasks list`
or the console's **Headless runs** section.
If the instruction above the forwarded message says `zoom calls recording`,
the agent works in `~/git/zoom-calls` and follows that repository's
`zoom-recording` skill, script, and summary templates. The forwarded Zoom
message must include a share link and passcode. This instruction is evaluated
by the agent; the headless process still starts in the configured worker root.
Open **Emails** in the console and select **Flow** on the `agents@dtcdev.click`
row to see the route, headless action, and completion email. This route sends
the report from `agents@dtcdev.click`, so replies return to the same address.

The route inserts Datamailer's inline plain-text body, `{body.text.value}`,
into the agent prompt. Email attachments and messages whose body exceeds the
inline limit are not passed to the agent yet; provide a Zoom link in the body
for this first workflow. Completion emails use SES in the configured
`EmailSendRegion` (currently `us-east-1`), where the sender identity is
verified.
