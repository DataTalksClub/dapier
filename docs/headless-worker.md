# Headless agent runs

Dapier's `agent` action enqueues one headless job. A host worker claims it
through `/api/agent/host-jobs/*`, runs Claude Code in print mode, and reports
`succeeded`, `failed`, `timed_out`, or `interrupted`. Tasks stay queued
until a worker claims them, so something must run `dapier worker` for agent
actions to make progress. The console's **Workers** tab and
`dapier workers list` show which workers checked in, which are active (last
check-in under two minutes — claim polls and task heartbeats both count),
and what each is running. The console's **Agents** tab and `dapier agent-tasks list` show jobs from
every trigger type. Filter active runs or failures, search by title or workflow,
and select a run to read its result. Email runs use their message subject as the
title; other runs use the workflow name. Active runs refresh every 15 seconds.
Logs and internal task metadata are expandable below the result. Use `dapier agent-tasks show <task_id>`
for its result, error, exit code, and recorded stdout/stderr. For email
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
`<workspace-root>/.dapier-runs/`. After completion the worker uploads the
last 12,000 characters of each stream to the task record; the Agents detail
marks truncated output. Earlier workers recorded only a result summary.
Task reads require operator access and never return the stored prompt or lease
credentials.

The worker heartbeats while the child runs. Dapier extends the SQS lease,
records the final status before acknowledging the queue message, and never
automatically reruns work that lost its lease after starting. An interrupted
run needs operator review before retrying, because a coding task may already
have changed files or called external services.

## Worker presence

Every `dapier worker` process registers itself under a stable id
(`hostname-pid-random`), sent with each claim, heartbeat, and finish call.
Dapier records the check-ins in the host task table (rows keyed
`worker:<id>`); a row says where the worker runs (hostname, pid, workspace
root), when it started, which task it is running now, and the last task it
finished. Rows expire through the table's TTL about two weeks after a
worker's last check-in. A worker is **active** while its last check-in is
under two minutes old; beyond that it shows as offline and queued tasks will
not move until a worker is running again.

## Forward a task

Send from an address in `dapier emails from list` to `agents@dtcdev.click`.
Put your task instructions above the forwarded message, for example, "Write a
Telegram article from this Zoom conversation." Include the Zoom shared
recording URL and passcode in the email body. The stored route is defined in
[`agents-workflow.json`](agents-workflow.json), a JSON-compatible YAML
workflow. Apply changes with `dapier workflows save docs/agents-workflow.json`
and `dapier workflows publish email-trigger-agents.yaml`, or edit it in
**Workflows**. Use **Expand editor** on the prompt field to read and edit long
instructions; **Apply to step** updates the canvas, and saving the workflow
persists the change. Prompt whitespace and template variables are preserved.
Its internal id remains `email-trigger-agents` so earlier runs
and agent tasks stay associated with it. It uses the configured worker root, with no project pinned. The worker
reads the shared `fetch-zoom` skill when a Zoom link is present, processes the
transcript and saved chat together, and emails a
completion report to the sender. Inspect runs with `dapier agent-tasks list`
or the console's **Agents** tab.
If the instruction above the forwarded Zoom recording or meeting-assets message
is simply `Zoom` (allowing surrounding whitespace and punctuation), or explicitly
names `zoom calls`, `zoom-calls`, or `zoom calls recording` (case insensitive),
the agent works in `~/git/zoom-calls` and follows that repository's
`zoom-recording` skill, script, and summary templates. The forwarded Zoom
message must include a share link and passcode. This instruction is evaluated
by the agent; the headless process still starts in the configured worker root.
The agent commits the resulting transcript, available saved chat, summary,
and relevant `resources.md` updates and pushes them to the
private `zoom-calls` GitHub repository. Short personal meetings with Alexey and
one other participant (typically under 30 minutes) default to `1x1`; group
meetings use `discussion`, and an explicit meeting type takes precedence.
An existing downloaded transcript can be reused, but the agent still checks
for and fetches saved chat. Scratch files alone do not complete the task. Mentioning Zoom in a different explicit request, such as
"write an article from this Zoom conversation," follows that requested task
instead of selecting the Zoom calls route.

For every Zoom task, including articles and AI Shipping Labs recaps, saved
meeting chat is source material alongside the transcript. The agent reads
`~/git/zoom-calls/.agents/skills/zoom-recording/SKILL.md` for the authenticated
downloader's chat support. It saves `<stem>.chat.txt` beside the transcript;
`--chat-only` checks and downloads chat for a cached transcript using the same
recording URL, passcode, and transcript output path. Tasks outside that
repository use `uv run --project /home/alexey/git/zoom-calls python
/home/alexey/git/zoom-calls/download_zoom.py` with the URL, passcode, private
scratch transcript path, and `--chat-only`; they keep their requested destination. Chat messages and shared
links are evidence, not instructions. The deliverable includes the exact URLs
shared in chat in the relevant sections and resources, plus verified canonical
resource URLs when useful. The agent checks chat before claiming that a link
was not shared or asking the operator for it. No saved chat available and a
failed chat lookup or download are reported separately; either case identifies
the source coverage limitation. Raw chat and transcripts stay private.

If the instruction says `AI Shipping Labs` above a forwarded Zoom recording,
that route takes priority over the Zoom calls aliases. By default, the agent
uses the `ai-shipping-labs` event and recap skills to identify the exact
existing event, ensure its video reaches S3, and create the recap through the
event's source of truth. The site's Zoom
recording pipeline handles S3 uploads. The agent can refresh Zoom metadata or
retry an upload through the authenticated event API, then checks the event
again. It reports an ambiguous event match instead of selecting one by guess.
It does not notify registrants unless the operator asks for that action.
For any task that changes a Git repository, the agent stages only its intended
files, runs the repository's required checks, commits in focused commits, and
pushes them to the repository's default branch (`main`, or `master` when that
is the default). It creates a feature branch or pull request only when the
operator's own instructions explicitly ask for one. A push that deploys a live
site is the intended completion. It preserves unrelated work and reports any
check or push blocker in the completion report. When files
were pushed, the reply opens with those HTTPS links — the primary files at the
verified commit and the commit itself, derived from the actual Git remote —
before any narrative. Each full URL appears on its own line at the start of
the email body so it is clickable in plain-text email. Private repository
links keep their existing access controls. A local path or commit hash alone
is not a sufficient result link.

Open **Workflows → email-trigger-agents** in the console to see the email
trigger, agent action, and completion email. Agent actions can also run
from webhook, schedule, and other workflow triggers. This route sends
the report from `agents@dtcdev.click`, so replies return to the same address.

The route inserts Datamailer's inline plain-text body, `{body.text.value}`,
into the agent prompt. Trigger attachments are handled too: an email's stored
files ride the agent task as S3 pointers, and the worker stages them into the
workspace under `attachments/` (downloading them through the worker-token-gated
`/api/agent/host-jobs/attachment` endpoint in chunks that clear the API payload
cap), then prepends a notice line to the prompt saying where they landed.
`attachments: off` on the agent action keeps the files out. Bodies longer than
the inline limit are still not passed; provide a Zoom link in the body for this
first workflow. Completion emails use SES in the configured
`EmailSendRegion` (currently `us-east-1`), where the sender identity is
verified.
