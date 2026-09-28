"""`dapier` command line interface."""

import argparse
import sys

from . import api, auth, commands, config
from .api import ApiError


def build_parser():
    parser = argparse.ArgumentParser(prog="dapier", description="Dapier OAuth token factory CLI")
    parser.add_argument("--api-url", default=None, help="Dapier API base URL")
    parser.add_argument("--debug", action="store_true", help="Print requests (never credentials)")
    sub = parser.add_subparsers(dest="group", required=True)

    auth_p = sub.add_parser("auth", help="DTC shared-auth session")
    auth_sub = auth_p.add_subparsers(dest="command", required=True)
    login_p = auth_sub.add_parser(
        "login", help="Sign in by pairing this device (or --browser for the localhost flow)")
    login_p.add_argument("--timeout", type=int, default=900,
                         help="Seconds to wait for approval (default: the code's 15-minute life)")
    login_p.add_argument("--browser", action="store_true",
                         help="Use the browser loopback flow instead of device pairing")
    auth_sub.add_parser("status", help="Show the stored session (no secrets)")
    auth_sub.add_parser("logout", help="Forget the stored session (revokes device sessions)")

    conn_p = sub.add_parser("connections", help="Named provider connections")
    conn_sub = conn_p.add_subparsers(dest="command", required=True)
    conn_sub.add_parser("list", help="List connections granted to this identity")
    show_p = conn_sub.add_parser("show", help="Show one connection (no secrets)")
    show_p.add_argument("connection_id")
    show_p.add_argument("--agent", default=None)
    connect_p = conn_sub.add_parser("connect", help="Run provider consent for a connection")
    connect_p.add_argument("connection_id")
    connect_p.add_argument("--agent", required=True)
    connect_p.add_argument("--timeout", type=int, default=300)
    create_p = conn_sub.add_parser("create", help="Provision a new OAuth connection before consent")
    create_p.add_argument("connection_id")
    create_p.add_argument("--provider", required=True,
                          choices=("google", "youtube", "dropbox", "zoom"))
    create_p.add_argument("--display-name", default=None)
    create_p.add_argument("--scopes", nargs="+", required=True, metavar="SCOPE")
    create_p.add_argument("--root-path", default=None, help="Dropbox only: listing root")
    edit_p = conn_sub.add_parser("edit", help="Edit connection display name, scopes, or Dropbox path")
    edit_p.add_argument("connection_id")
    edit_p.add_argument("--display-name", default=None)
    edit_p.add_argument("--scopes", nargs="+", default=None, metavar="SCOPE")
    root_group = edit_p.add_mutually_exclusive_group()
    root_group.add_argument("--root-path", default=None, help="Dropbox only: listing root")
    root_group.add_argument("--clear-root-path", action="store_true", help="Dropbox only: list from the Dropbox root")
    scopes_p = conn_sub.add_parser("scopes", help="Replace a connection's requested OAuth scopes")
    scopes_p.add_argument("connection_id")
    scopes_p.add_argument("--scopes", nargs="+", required=True, metavar="SCOPE")
    import_p = conn_sub.add_parser("import", help="One-time operator import of an existing credential")
    import_p.add_argument("connection_id")
    import_p.add_argument("--provider", required=True)
    import_p.add_argument("--client-id", default=None,
                          help="Optional; defaults to the shared deploy-time OAuth client")
    import_p.add_argument("--client-secret-file", default=None,
                          help="Optional; needed only for refresh tokens issued by another client")
    import_p.add_argument("--authorized-user-file", default=None,
                          help="Refresh-token JSON for OAuth providers")
    import_p.add_argument("--token-file", default=None,
                          help="Provider token or Zoom webhook Secret Token (read from file)")
    import_p.add_argument("--signing-secret-file", default=None,
                          help="Slack only: Events API signing secret (read from file)")
    import_p.add_argument("--display-name", default=None)
    import_p.add_argument("--expected-account", default=None)
    import_p.add_argument("--scopes", nargs="*", default=[])
    import_p.add_argument("--root-path", default=None,
                          help="Dropbox only: folder webhook resolution lists (empty lists everything)")
    revoke_p = conn_sub.add_parser("revoke", help="Revoke a connection's stored tokens")
    revoke_p.add_argument("connection_id")
    discover_p = conn_sub.add_parser(
        "discover", help="List a connection's discovery resources, or one resource's items")
    discover_p.add_argument("connection_id")
    discover_p.add_argument("resource", nargs="?", default=None,
                            help="Resource to list, e.g. spreadsheets or channels (omit to list resources)")
    discover_p.add_argument("--param", action="append", nargs="*", default=[],
                            metavar="KEY=VALUE",
                            help="Discovery param, repeatable (e.g. --param folder=/invoices)")
    test_p = conn_sub.add_parser("test", help="Test a connection's stored tokens against its provider")
    test_p.add_argument("connection_id")

    cred_p = sub.add_parser("credentials", help="Provider credentials used by workflows")
    cred_sub = cred_p.add_subparsers(dest="command", required=True)
    cred_set_p = cred_sub.add_parser("set", help="Store a credential (Slack bot token, Mailchimp API key, or AWS keys as JSON)")
    cred_set_p.add_argument("provider")
    cred_set_p.add_argument("--file", required=True,
                            help="File with the secret value, or - for stdin")

    grants_p = sub.add_parser("grants", help="Agent access grants per connection")
    grants_sub = grants_p.add_subparsers(dest="command", required=True)
    grants_list_p = grants_sub.add_parser("list", help="List grants")
    grants_list_p.add_argument("--connection", default=None)
    grants_save_p = grants_sub.add_parser("save", help="Create or update a grant from a JSON file")
    grants_save_p.add_argument("file", help="Path to the grant JSON, or - for stdin")
    grants_del_p = grants_sub.add_parser("delete", help="Revoke one grant")
    grants_del_p.add_argument("connection_id")
    grants_del_p.add_argument("grantee", help="The grant's grantee ID (subject#agent)")

    users_p = sub.add_parser("users", help="Console users and their roles (admin)")
    users_sub = users_p.add_subparsers(dest="command", required=True)
    users_sub.add_parser("list", help="List users: subject, role, status")
    users_set_p = users_sub.add_parser("set-role", help="Assign a role to a user")
    users_set_p.add_argument("subject", help="The user's DTC subject or email")
    users_set_p.add_argument("role", choices=["admin", "operator", "editor", "viewer"],
                             help="admin manages users; operator is the full console; "
                                  "editor edits workflows; viewer is read-only")
    users_set_p.add_argument("--display-name", default=None,
                             help="Optional human-readable name shown in the console")
    users_del_p = users_sub.add_parser("remove", help="Remove a user's stored role")
    users_del_p.add_argument("subject", help="The user's DTC subject or email")
    users_del_p.add_argument("--yes", action="store_true",
                             help="Skip the confirmation prompt")

    tokens_p = sub.add_parser("tokens", help="Operator-issued API tokens for headless consumers")
    tokens_sub = tokens_p.add_subparsers(dest="command", required=True)
    tokens_sub.add_parser("list", help="List API tokens (no secrets)")
    tokens_create_p = tokens_sub.add_parser("create", help="Issue an API token; the value prints once")
    tokens_create_p.add_argument("--name", required=True,
                                 help="Token ID; the machine subject becomes token:<name>")
    tokens_create_p.add_argument("--agent", required=True,
                                 help="The one agent name this token may act as")
    tokens_del_p = tokens_sub.add_parser("revoke", help="Revoke an API token")
    tokens_del_p.add_argument("name")

    sub.add_parser("overview", help="Operator overview: workflows, connections, credentials")

    runs_p = sub.add_parser("runs", help="Workflow run history (one run per trigger event)")
    runs_sub = runs_p.add_subparsers(dest="command", required=True)
    runs_list_p = runs_sub.add_parser("list", help="Recent runs, newest first")
    runs_list_p.add_argument("--limit", type=int, default=25)
    runs_list_p.add_argument("--workflow", help="Only runs of this workflow id")
    runs_list_p.add_argument("--status",
                             help="success | failed | error | problems (failed or error), "
                                  "or an exact status (completed, processing, filtered)")
    runs_list_p.add_argument("--since",
                             help="Only runs started at or after this ISO date/datetime")
    runs_list_p.add_argument("--before",
                             help="Only runs started before this ISO date/datetime (exclusive)")
    runs_list_p.add_argument("--search",
                             help="Substring search over each run's recorded step data "
                                  "(inputs, outputs, errors) and ids")
    runs_list_p.add_argument("--next", dest="next_token",
                             help="Page token from the previous call's `next page:` footer")
    runs_show_p = runs_sub.add_parser("show", help="Show one run's step-by-step flow")
    runs_show_p.add_argument("run_id", help="Run ID from `dapier runs list` (or the console)")
    runs_replay_p = runs_sub.add_parser("replay", help="Re-run a past run by re-injecting its original trigger event")
    runs_replay_p.add_argument("run_id", help="Run ID from `dapier runs list` (or the console)")
    runs_replay_p.add_argument("--from-step", dest="from_step",
                               help="Replay from this top-level step id onward: earlier "
                                    "steps do not run again; their recorded outputs are reused")
    runs_replay_failed_p = runs_sub.add_parser(
        "replay-failed", help="Re-run the latest failed runs of one workflow")
    runs_replay_failed_p.add_argument("workflow_id", help="Workflow whose failed runs to replay")
    runs_cancel_p = runs_sub.add_parser(
        "cancel", help="Cancel a suspended run: it closes out cancelled and will not resume")
    runs_cancel_p.add_argument("run_id", help="Run ID from `dapier runs list` (or the console)")
    runs_export_p = runs_sub.add_parser("export", help="Export run history as CSV")
    runs_export_p.add_argument("--workflow", help="Only runs of this workflow id")
    runs_export_p.add_argument("--status",
                               help="success | failed | error | problems (failed or error), "
                                    "or an exact status (completed, processing, filtered)")
    runs_export_p.add_argument("--since",
                               help="Only runs started at or after this ISO date/datetime")
    runs_export_p.add_argument("--before",
                               help="Only runs started before this ISO date/datetime (exclusive)")
    runs_export_p.add_argument("--search",
                               help="Substring search over each run's recorded step data "
                                    "(inputs, outputs, errors) and ids")
    runs_export_p.add_argument("--max-rows", type=int,
                               help="Cap the export (default 1000, max 5000)")
    runs_export_p.add_argument("--out",
                               help="Write the CSV here (default: the suggested filename)")

    def add_audit_filters(parser):
        parser.add_argument("--connection", help="Only events for this connection id")
        parser.add_argument("--action",
                            help="Exact action, or an action family like 'workflow.'")
        parser.add_argument("--actor", help="Only events recorded for this actor subject")
        parser.add_argument("--agent", help="Only events recorded for this agent")
        parser.add_argument("--outcome",
                            help="Only events with this outcome (ok, error, denied-no-grant, ...)")
        parser.add_argument("--since", help="Only events at or after this ISO date/datetime")
        parser.add_argument("--before",
                            help="Only events before this ISO date/datetime (exclusive)")
        parser.add_argument("--query",
                            help="Substring search over action, connection, actor, agent, and error text")

    audit_p = sub.add_parser("audit",
                             help="Operator audit trail: who changed what, and when")
    audit_sub = audit_p.add_subparsers(dest="command", required=True)
    audit_list_p = audit_sub.add_parser("list", help="Recent audit events, newest first")
    audit_list_p.add_argument("--limit", type=int, default=50)
    add_audit_filters(audit_list_p)
    audit_list_p.add_argument("--next", dest="next_token",
                              help="Page token from the previous call's `next page:` footer")
    audit_export_p = audit_sub.add_parser("export", help="Export the audit trail as CSV")
    add_audit_filters(audit_export_p)
    audit_export_p.add_argument("--max-rows", type=int,
                                help="Cap on exported rows (default 1000, max 5000)")
    audit_export_p.add_argument("--out", help="Write the CSV here (default: the suggested filename)")

    usage_p = sub.add_parser("usage", help="Task usage rollup: tasks per workflow per month")
    usage_p.add_argument("--months", type=int, default=12,
                         help="How many months of rollup to show (default 12, max 24)")

    errors_p = sub.add_parser("errors", help="Failed runs by workflow over the recent window")
    errors_p.add_argument("--days", type=int, default=7,
                          help="Window size in days (default 7, max 90)")
    errors_sub = errors_p.add_subparsers(dest="command")
    errors_sub.add_parser("send-digest",
                          help="Render and email the error digest now (operator)")

    inbox_p = sub.add_parser("inbox", help="Trigger inbox: every inbound trigger event, matched or not")
    inbox_sub = inbox_p.add_subparsers(dest="command", required=True)
    inbox_list_p = inbox_sub.add_parser("list", help="Recent inbox events, newest first")
    inbox_list_p.add_argument("--connector", help="Only events from this connector (e.g. webhook, telegram)")
    inbox_list_p.add_argument("--limit", type=int, default=25)
    inbox_show_p = inbox_sub.add_parser("show", help="Show one inbox event's stored envelope")
    inbox_show_p.add_argument("inbox_id", help="Inbox event ID from `dapier inbox list`")
    inbox_replay_p = inbox_sub.add_parser("replay", help="Send an inbox event through the engine again (fresh event id)")
    inbox_replay_p.add_argument("inbox_id", help="Inbox event ID from `dapier inbox list`")

    storage_p = sub.add_parser("storage",
                               help="Workflow storage: per-workflow key-value state (what the storage_* actions read and write)")
    storage_sub = storage_p.add_subparsers(dest="command", required=True)
    storage_get_p = storage_sub.add_parser("get", help="Read one stored value")
    storage_get_p.add_argument("workflow", help="Workflow ID whose storage to read")
    storage_get_p.add_argument("key", help="Stored key")
    storage_set_p = storage_sub.add_parser("set", help="Store a value for the workflow's runs")
    storage_set_p.add_argument("workflow", help="Workflow ID whose storage to write")
    storage_set_p.add_argument("key", help="Stored key")
    storage_set_p.add_argument("value", help="Value to store")
    storage_set_p.add_argument("--ttl-seconds", type=int, default=None,
                               help="Expire the value after this many seconds")
    storage_find_p = storage_sub.add_parser("find", help="List stored keys under a prefix")
    storage_find_p.add_argument("workflow", help="Workflow ID whose storage to list")
    storage_find_p.add_argument("prefix", nargs="?", default="",
                                help="Key prefix (default: every key)")
    storage_find_p.add_argument("--limit", type=int, default=None,
                                help="Max keys to list (default 20, max 50)")
    storage_delete_p = storage_sub.add_parser("delete", help="Remove one stored value")
    storage_delete_p.add_argument("workflow", help="Workflow ID whose storage to change")
    storage_delete_p.add_argument("key", help="Stored key")

    oac_p = sub.add_parser("oauth-clients", help="Shared OAuth clients per provider (same as the console's Credentials view)")
    oac_sub = oac_p.add_subparsers(dest="command", required=True)
    oac_sub.add_parser("list", help="Show the configured shared OAuth clients (no secrets)")
    oac_set_p = oac_sub.add_parser("set", help="Store the shared OAuth client for a provider")
    oac_set_p.add_argument("provider", help="dropbox, google, youtube, or zoom")
    oac_set_p.add_argument("--client-id", required=True)
    oac_set_p.add_argument("--client-secret-file", required=True,
                           help="File with the client secret, or - for stdin")

    token_p = sub.add_parser("token", help="Short-lived provider access tokens")
    token_sub = token_p.add_subparsers(dest="command", required=True)
    exec_p = token_sub.add_parser("exec", help="Run a command with a fresh token in its environment")
    exec_p.add_argument("connection_id")
    exec_p.add_argument("--agent", required=True)
    exec_p.add_argument("argv", nargs="*")
    write_p = token_sub.add_parser("write", help="Write a fresh token to a private file")
    write_p.add_argument("connection_id")
    write_p.add_argument("--agent", required=True)
    write_p.add_argument("--output", required=True)
    write_p.add_argument("--force", action="store_true")

    trig_p = sub.add_parser("triggers", help="Email triggers and trigger sample discovery")
    trig_sub = trig_p.add_subparsers(dest="command", required=True)
    trig_sub.add_parser("list", help="List email triggers and YAML-claimed routes")
    trig_sample_p = trig_sub.add_parser(
        "sample", help="Pull a sample event for a trigger connector, or a workflow's last trigger input")
    trig_sample_p.add_argument("connector", nargs="?", default=None,
                               help="e.g. custom, dataops, dropbox, email, mailchimp, poll, "
                                    "renderer, schedule, telegram, webhook, youtube, zoom")
    trig_sample_p.add_argument("--workflow", default=None,
                               help="Workflow id: print its newest run's recorded "
                                    "trigger input (else the connector's sample) "
                                    "with the {trigger.*} fields it offers")
    trig_sample_p.add_argument("--event", default=None,
                               help="Event name override; for poll, the trigger's name")
    trig_sample_p.add_argument("--connection-id", default=None,
                               help="Prefer this connected account for live pulls")
    trig_sample_p.add_argument("--limit", type=int, default=None,
                               help="Max options when kind is options (1-25)")
    trig_sample_p.add_argument("--resource", default=None,
                               help="Options listing instead of a sample (e.g. slack.channels)")
    trig_show_p = trig_sub.add_parser("show", help="Show one email trigger")
    trig_show_p.add_argument("name")
    trig_save_p = trig_sub.add_parser("save", help="Create or update a trigger from a JSON file")
    trig_save_p.add_argument("file", help="Path to the trigger JSON, or - for stdin")
    trig_del_p = trig_sub.add_parser("delete", help="Delete an email trigger")
    trig_del_p.add_argument("name")

    wf_p = sub.add_parser("workflows", help="Workflow YAML committed by the console designer")
    wf_sub = wf_p.add_subparsers(dest="command", required=True)
    wf_list_p = wf_sub.add_parser("list", help="List workflows and their On/Off state")
    wf_list_p.add_argument("--search", default=None,
                           help="Only workflows whose id, description, trigger, "
                                "or action types contain this text")
    wf_list_p.add_argument("--tag", default=None,
                           help="Only workflows carrying this tag (case-insensitive)")
    wf_list_p.add_argument("--folder", default=None,
                           help="Only workflows in this folder (case-insensitive)")
    wf_show_p = wf_sub.add_parser("show", help="Show one workflow (JSON)")
    wf_show_p.add_argument("file", help="Workflow file name, e.g. my-flow.yaml")
    wf_export_p = wf_sub.add_parser(
        "export", help="Print (or write) a workflow's canonical YAML; --all bundles every workflow")
    wf_export_p.add_argument("file", nargs="?", default=None,
                             help="Workflow file name, e.g. my-flow.yaml (omit with --all)")
    wf_export_p.add_argument("--all", action="store_true",
                             help="Export every workflow's YAML as one zip")
    wf_export_p.add_argument("-o", "--output", default=None,
                             help="Write the YAML (or with --all the zip) to this "
                                  "file instead of stdout / workflows-export.zip")
    wf_export_all_p = wf_sub.add_parser(
        "export-all", help="Every workflow's canonical YAML as one server-built zip")
    wf_export_all_p.add_argument("-o", "--out", dest="out", default=None,
                                 help="Write the zip here (default: the server-suggested "
                                      "dapier-workflows-YYYYMMDD.zip)")
    wf_save_p = wf_sub.add_parser("save", help="Publish a workflow YAML live (and sync to Git when configured)")
    wf_save_p.add_argument("file", help="Path to the workflow YAML, or - for stdin")
    wf_save_p.add_argument("--rename-from", default=None,
                           help="Previous file name when the workflow was renamed")
    wf_draft_p = wf_sub.add_parser("draft",
                                   help="Generate a draft workflow YAML from a natural-language prompt")
    wf_draft_p.add_argument("prompt",
                            help='What the workflow should do, e.g. "when someone emails todo@, push it to slack"')
    wf_draft_p.add_argument("--save", action="store_true",
                            help="Also save the draft through the `workflows save` path (only when it validates)")
    def add_bulk_toggle_flags(parser):
        parser.add_argument("file", nargs="*", default=None,
                            help="Workflow file name(s), e.g. my-flow.yaml")
        parser.add_argument("--tag", default=None,
                            help="Bulk: every workflow carrying this tag")
        parser.add_argument("--all", action="store_true",
                            help="Bulk: every workflow")

    wf_on_p = wf_sub.add_parser("on", help="Turn workflows on (live immediately)")
    add_bulk_toggle_flags(wf_on_p)
    wf_off_p = wf_sub.add_parser("off", help="Turn workflows off (live immediately)")
    add_bulk_toggle_flags(wf_off_p)
    wf_enable_p = wf_sub.add_parser("enable", help="Alias for workflows on")
    add_bulk_toggle_flags(wf_enable_p)
    wf_disable_p = wf_sub.add_parser("disable", help="Alias for workflows off")
    add_bulk_toggle_flags(wf_disable_p)
    wf_dup_p = wf_sub.add_parser("duplicate", help="Copy a workflow under a new id and publish the copy live")
    wf_dup_p.add_argument("file", help="Workflow file name, e.g. my-flow.yaml")
    wf_dup_p.add_argument("--name", default=None,
                          help="New workflow name (defaults to <id>-copy)")
    wf_test_p = wf_sub.add_parser("test", help="Test-run a workflow YAML against a sample event (dry-run)")
    wf_test_p.add_argument("file", help="Path to the workflow YAML, or - for stdin")
    wf_test_p.add_argument("--event", required=True,
                           help="Sample event JSON, inline or @file (e.g. --event @event.json)")
    wf_test_p.add_argument("--execute", action="store_true",
                           help="Actually run the actions (real side effects); default is a dry-run")
    wf_test_p.add_argument("--strict", action="store_true",
                           help="Fail dry-run steps whose rendered inputs trip the field "
                                "rules (required-but-empty, wrong type)")
    wf_test_step_p = wf_sub.add_parser("test-step",
                                       help="Test one step of a workflow against a sample event")
    wf_test_step_p.add_argument("file", help="Path to the workflow YAML, or - for stdin")
    wf_test_step_p.add_argument("--action", required=True,
                                help="The step's action id (run-history id, e.g. send or route.a.0)")
    wf_test_step_p.add_argument("--event", required=True,
                                help="Sample event JSON, inline or @file (e.g. --event @event.json)")
    wf_test_step_p.add_argument("--steps", default=None,
                                help="Prior steps' outputs as JSON or @file, shaped like run "
                                     "history ({id: {status, output}}); feeds {steps.*} templates")
    wf_test_step_p.add_argument("--execute", action="store_true",
                                help="Actually run this one step (real side effects); default "
                                     "renders and evaluates only")
    wf_versions_p = wf_sub.add_parser("versions",
                                      help="List a workflow's published versions (newest first)")
    wf_versions_p.add_argument("file", help="Workflow file name, e.g. my-flow.yaml")
    wf_diff_p = wf_sub.add_parser("diff",
                                  help="Unified diff between two published versions")
    wf_diff_p.add_argument("file", help="Workflow file name, e.g. my-flow.yaml")
    wf_diff_p.add_argument("--from", dest="from_revision", type=int, required=True,
                           help="Version to diff from, as shown by workflows versions")
    wf_diff_p.add_argument("--to", dest="to_revision", type=int, required=True,
                           help="Version to diff to, as shown by workflows versions")
    wf_rollback_p = wf_sub.add_parser("rollback",
                                      help="Restore an old version of a workflow (live immediately)")
    wf_rollback_p.add_argument("file", help="Workflow file name, e.g. my-flow.yaml")
    wf_rollback_p.add_argument("revision",
                               help="Version number to restore, as shown by workflows versions")
    wf_delete_p = wf_sub.add_parser("delete",
                                    help="Delete a workflow: unpublish it live and remove its YAML from the repo")
    wf_delete_p.add_argument("file", help="Workflow file name, e.g. my-flow.yaml")
    wf_delete_p.add_argument("--yes", action="store_true",
                             help="Skip the confirmation prompt")
    wf_tags_p = wf_sub.add_parser("tags",
                                  help="Edit a workflow's tags (Zapier-style organization labels)")
    wf_tags_p.add_argument("file", help="Workflow file name, e.g. my-flow.yaml")
    wf_tags_p.add_argument("--tags", default=None,
                           help="Comma-separated tags, e.g. billing,ops — replaces the whole set")
    wf_tags_p.add_argument("--add", default=None,
                           help="Comma-separated tags to add, e.g. billing,ops")
    wf_tags_p.add_argument("--remove", default=None,
                           help="Comma-separated tags to remove (case-insensitive)")
    wf_tags_p.add_argument("--clear", action="store_true",
                           help="Remove all tags")
    wf_folder_p = wf_sub.add_parser("folder",
                                    help="Put a workflow in a folder (Zapier-style, flat)")
    wf_folder_p.add_argument("file", help="Workflow file name, e.g. my-flow.yaml")
    wf_folder_p.add_argument("--set", dest="set_value", default=None,
                             help="Folder name — sets or moves the workflow (at most one folder)")
    wf_folder_p.add_argument("--clear", action="store_true",
                             help="Remove the workflow from its folder")
    tpl_p = sub.add_parser("templates", help="Workflow templates: browse the gallery, fork one, publish yours")
    tpl_sub = tpl_p.add_subparsers(dest="command", required=True)
    tpl_list_p = tpl_sub.add_parser("list", help="List the template gallery")
    tpl_list_p.add_argument("--json", action="store_true", dest="json",
                            help="Print the raw JSON instead of a table")
    tpl_apply_p = tpl_sub.add_parser("apply", help="Fork a template into a new workflow and publish it live")
    tpl_apply_p.add_argument("file", help="Template file name, e.g. template-webhook-to-slack.yaml")
    tpl_apply_p.add_argument("--name", default=None,
                             help="New workflow name (defaults to <template-id>-copy)")
    tpl_publish_p = tpl_sub.add_parser("publish", help="Offer a saved workflow in the template gallery")
    tpl_publish_p.add_argument("file", help="Workflow file name, e.g. my-flow.yaml")
    tpl_unpublish_p = tpl_sub.add_parser("unpublish", help="Remove a workflow from the template gallery")
    tpl_unpublish_p.add_argument("file", help="Workflow file name, e.g. my-flow.yaml")
    hook_p = sub.add_parser("hooks", help="Webhook and Telegram triggers")
    hook_sub = hook_p.add_subparsers(dest="command", required=True)
    hook_list_p = hook_sub.add_parser("list", help="List hook triggers")
    hook_list_p.add_argument("--kind", default=None,
                             choices=["webhook", "telegram", "mailchimp", "youtube"])
    hook_show_p = hook_sub.add_parser("show", help="Show one hook trigger, including its token")
    hook_show_p.add_argument("name")
    hook_save_p = hook_sub.add_parser("save", help="Create or update a hook trigger from a JSON file")
    hook_save_p.add_argument("file", help="Path to the hook JSON, or - for stdin")
    hook_save_p.add_argument("--sync-response", action="store_true",
                             help="Webhook only: answer each delivery by running the flow "
                                  "inline and replying with the outcome (response.mode sync) "
                                  "instead of the 202 ack; must finish inside ~30s")
    hook_del_p = hook_sub.add_parser("delete", help="Delete a hook trigger")
    hook_del_p.add_argument("name")
    hook_del_p.add_argument("--kind", default=None,
                            choices=["webhook", "telegram", "mailchimp", "youtube"])
    sched_p = sub.add_parser("schedules", help="Cron and rate schedule triggers (EventBridge rules)")
    sched_sub = sched_p.add_subparsers(dest="command", required=True)
    sched_sub.add_parser("list", help="List schedule triggers")
    sched_save_p = sched_sub.add_parser("save", help="Create or update a schedule trigger from a JSON file")
    sched_save_p.add_argument("file", help="Path to the schedule JSON, or - for stdin")
    sched_del_p = sched_sub.add_parser("delete", help="Delete a schedule trigger and its rule")
    sched_del_p.add_argument("name")
    poll_p = sub.add_parser("polls", help="API poll triggers: fetch on a schedule, one event per new item")
    poll_sub = poll_p.add_subparsers(dest="command", required=True)
    poll_sub.add_parser("list", help="List poll triggers")
    poll_save_p = poll_sub.add_parser("save", help="Create or update a poll trigger from a JSON file")
    poll_save_p.add_argument("file", help="Path to the poll trigger JSON, or - for stdin")
    poll_del_p = poll_sub.add_parser("delete", help="Delete a poll trigger and its rule")
    poll_del_p.add_argument("name")
    catalog_p = sub.add_parser("catalog", help="Show the action and trigger catalog (GET /api/catalog)")
    catalog_p.add_argument("--json", action="store_true", help="Print the raw catalog JSON")
    return parser


def main(argv=None):
    raw = list(sys.argv[1:] if argv is None else argv)
    # Split the child command on the first `--` before argparse sees it, so
    # child flags never confuse option parsing (nargs=REMAINDER would swallow
    # our own options).
    child = None
    if "--" in raw:
        index = raw.index("--")
        raw, child = raw[:index], raw[index + 1:]
    args = build_parser().parse_args(raw)
    api_url = config.api_url(args.api_url)
    debug = args.debug
    try:
        if args.group == "auth":
            return cmd_auth(args, api_url)
        if args.group == "connections":
            return cmd_connections(args, api_url, debug)
        if args.group == "token":
            return cmd_token(args, api_url, debug, child)
        if args.group == "triggers":
            return cmd_triggers(args, api_url, debug)
        if args.group == "workflows":
            return cmd_workflows(args, api_url, debug)
        if args.group == "templates":
            return cmd_templates(args, api_url, debug)
        if args.group == "hooks":
            return cmd_hooks(args, api_url, debug)
        if args.group == "schedules":
            return cmd_schedules(args, api_url, debug)
        if args.group == "polls":
            return cmd_polls(args, api_url, debug)
        if args.group == "usage":
            return commands.usage(api_url, debug, months=args.months)
        if args.group == "errors":
            if getattr(args, "command", None) == "send-digest":
                return commands.errors_send_digest(api_url, debug)
            return commands.errors_summary(api_url, debug, days=args.days)
        if args.group == "catalog":
            return commands.catalog_show(api_url, debug, as_json=args.json)
        if args.group == "credentials":
            return cmd_credentials(args, api_url, debug)
        if args.group == "grants":
            return cmd_grants(args, api_url, debug)
        if args.group == "users":
            return cmd_users(args, api_url, debug)
        if args.group == "tokens":
            return cmd_tokens(args, api_url, debug)
        if args.group == "overview":
            return commands.overview(api_url, debug)
        if args.group == "runs":
            return cmd_runs(args, api_url, debug)
        if args.group == "audit":
            return cmd_audit(args, api_url, debug)
        if args.group == "inbox":
            return cmd_inbox(args, api_url, debug)
        if args.group == "storage":
            return cmd_storage(args, api_url, debug)
        if args.group == "oauth-clients":
            return cmd_oauth_clients(args, api_url, debug)
    except ApiError as exc:
        print(f"Error: {exc}")
        if exc.status == 401:
            return 3
        if exc.status in (403, 404):
            return 4
        return 5
    except auth.LoginError as exc:
        print(f"Error: {exc}")
        return 3
    return 2


def cmd_auth(args, api_url):
    if args.command == "login":
        if args.browser:
            session = auth.login(api_url, timeout=args.timeout)
        else:
            try:
                session = auth.login_device(api_url, timeout=args.timeout)
            except auth.DeviceFlowUnavailable:
                print("This API has no device pairing; falling back to the browser flow.")
                session = auth.login(api_url, timeout=args.timeout)
        print(f"Signed in as {session.get('email') or session.get('subject')}.")
        return 0
    if args.command == "status":
        info = auth.describe(config.load_session())
        if not info["signed_in"]:
            print("Not signed in. Run `dapier auth login`.")
            return 3
        state = "expired; run `dapier auth login`" if info["expired"] else "valid"
        print(f"Signed in as {info.get('email')} ({info.get('subject')}); session {state}.")
        return 0 if not info["expired"] else 3
    if args.command == "logout":
        stored = config.load_session()
        auth.revoke_session(api_url, stored)
        config.clear_session()
        print("Signed out.")
        return 0
    return 2


def cmd_connections(args, api_url, debug):
    if args.command == "list":
        return commands.connections_list(api_url, debug)
    if args.command == "show":
        return commands.connections_show(api_url, args.connection_id, args.agent, debug)
    if args.command == "connect":
        return commands.connections_connect(api_url, args.connection_id, args.agent, args.timeout, debug)
    if args.command == "create":
        return commands.connections_create(
            api_url, args.connection_id, args.provider, args.scopes,
            display_name=args.display_name, root_path=args.root_path, debug=debug,
        )
    if args.command == "edit":
        root_path = "" if args.clear_root_path else args.root_path
        return commands.connections_edit(
            api_url, args.connection_id, display_name=args.display_name,
            scopes=args.scopes, root_path=root_path, debug=debug,
        )
    if args.command == "scopes":
        return commands.connections_scopes(api_url, args.connection_id, args.scopes, debug)
    if args.command == "import":
        return commands.connections_import(
            api_url, args.connection_id, args.provider, args.client_id,
            args.client_secret_file, args.authorized_user_file,
            expected_account_id=args.expected_account, scopes=args.scopes, debug=debug,
            token_path=args.token_file, root_path=args.root_path,
            display_name=args.display_name,
            signing_secret_path=args.signing_secret_file,
        )
    if args.command == "revoke":
        return commands.connections_revoke(api_url, args.connection_id, debug)
    if args.command == "discover":
        return commands.connections_discover(
            api_url, args.connection_id, args.resource,
            params=[pair for group in args.param for pair in group], debug=debug)
    if args.command == "test":
        return commands.connections_test(api_url, args.connection_id, debug)
    return 2


def cmd_token(args, api_url, debug, child=None):
    if args.command == "exec":
        argv = list(child) if child is not None else list(args.argv)
        return commands.token_exec(api_url, args.connection_id, args.agent, argv, debug)
    if args.command == "write":
        return commands.token_write(api_url, args.connection_id, args.agent,
                                    args.output, args.force, debug)
    return 2


def cmd_triggers(args, api_url, debug):
    if args.command == "list":
        return commands.triggers_list(api_url, debug)
    if args.command == "show":
        return commands.triggers_show(api_url, args.name, debug)
    if args.command == "save":
        return commands.triggers_save(api_url, args.file, debug)
    if args.command == "delete":
        return commands.triggers_delete(api_url, args.name, debug)
    if args.command == "sample":
        if args.workflow:
            return commands.triggers_workflow_sample(api_url, args.workflow, debug=debug)
        if not args.connector:
            print("Error: give a connector (e.g. email) or --workflow <workflow_id>")
            return 2
        return commands.triggers_sample(api_url, args.connector, event=args.event,
                                        connection_id=args.connection_id,
                                        limit=args.limit, resource=args.resource, debug=debug)
    return 2


def cmd_workflows(args, api_url, debug):
    if args.command == "list":
        return commands.workflows_list(api_url, debug, search=args.search, tag=args.tag,
                                       folder=args.folder)
    if args.command == "show":
        return commands.workflows_show(api_url, args.file, debug)
    if args.command == "export":
        if getattr(args, "all", False):
            return commands.workflows_export_all(api_url, output=args.output, debug=debug)
        if not args.file:
            print("Error: name a workflow file (e.g. my-flow.yaml) or pass --all.")
            return 2
        return commands.workflows_export(api_url, args.file, output=args.output, debug=debug)
    if args.command == "export-all":
        return commands.workflows_export_all_bundle(api_url, out=args.out, debug=debug)
    if args.command == "save":
        return commands.workflows_save(api_url, args.file, args.rename_from, debug)
    if args.command == "draft":
        return commands.workflows_draft(api_url, args.prompt, save=args.save, debug=debug)
    if args.command in ("on", "off", "enable", "disable"):
        enabled = args.command in ("on", "enable")
        if getattr(args, "tag", None) or getattr(args, "all", False):
            return commands.workflows_bulk_enabled(api_url, enabled, tag=args.tag,
                                                   all_workflows=args.all, debug=debug)
        if not args.file:
            print("Error: name workflow file(s) or pass --tag <tag> / --all.")
            return 2
        return commands.workflows_set_enabled(api_url, args.file, enabled, debug)
    if args.command == "duplicate":
        return commands.workflows_duplicate(api_url, args.file, name=args.name, debug=debug)
    if args.command == "versions":
        return commands.workflows_versions(api_url, args.file, debug=debug)
    if args.command == "diff":
        return workflows_diff(api_url, args.file, args.from_revision,
                              args.to_revision, debug=debug)
    if args.command == "rollback":
        return commands.workflows_rollback(api_url, args.file, args.revision, debug=debug)
    if args.command == "delete":
        return commands.workflows_delete(api_url, args.file, assume_yes=args.yes, debug=debug)
    if args.command == "tags":
        return commands.workflows_tags(api_url, args.file, tags_spec=args.tags,
                                       clear=args.clear, add=args.add, remove=args.remove,
                                       debug=debug)
    if args.command == "folder":
        return commands.workflows_folder(api_url, args.file, set_value=args.set_value,
                                         clear=args.clear, debug=debug)
    if args.command == "test":
        return commands.workflows_test(api_url, args.file, args.event, args.execute,
                                       strict=args.strict, debug=debug)
    if args.command == "test-step":
        return commands.workflows_test_step(api_url, args.file, args.action, args.event,
                                            steps_spec=args.steps, execute=args.execute,
                                            debug=debug)
    return 2


def workflows_diff(api_url, file, from_revision, to_revision, debug=False):
    """Unified diff between two published versions (the server builds it;
    the raw text goes to stdout so it pipes into `less` or `patch`)."""
    data = api.call(api_url, "GET",
                    f"/api/agent/designer/workflows/{file}/versions/diff"
                    f"?from={from_revision}&to={to_revision}", debug=debug)
    diff = data.get("diff") or ""
    if diff:
        sys.stdout.write(diff if diff.endswith("\n") else diff + "\n")
    if data.get("same"):
        print(f"v{from_revision} and v{to_revision} are identical.")
    if data.get("truncated"):
        print("Warning: the diff was truncated at the server's size cap; "
              "narrow the revision range to see more.", file=sys.stderr)
    return 0


def cmd_templates(args, api_url, debug):
    if args.command == "list":
        return commands.templates_list(api_url, as_json=args.json, debug=debug)
    if args.command == "apply":
        return commands.templates_apply(api_url, args.file, name=args.name, debug=debug)
    if args.command == "publish":
        return commands.templates_publish(api_url, args.file, True, debug=debug)
    if args.command == "unpublish":
        return commands.templates_publish(api_url, args.file, False, debug=debug)
    return 2


def cmd_hooks(args, api_url, debug):
    if args.command == "list":
        return commands.hooks_list(api_url, kind=args.kind, debug=debug)
    if args.command == "show":
        return commands.hooks_show(api_url, args.name, debug)
    if args.command == "save":
        return commands.hooks_save(api_url, args.file, debug=debug,
                                   sync_response=getattr(args, "sync_response", False))
    if args.command == "delete":
        return commands.hooks_delete(api_url, args.name, kind=args.kind, debug=debug)
    return 2


def cmd_schedules(args, api_url, debug):
    if args.command == "list":
        return commands.schedules_list(api_url, debug)
    if args.command == "save":
        return commands.schedules_save(api_url, args.file, debug)
    if args.command == "delete":
        return commands.schedules_delete(api_url, args.name, debug)
    return 2


def cmd_polls(args, api_url, debug):
    if args.command == "list":
        return commands.polls_list(api_url, debug)
    if args.command == "save":
        return commands.polls_save(api_url, args.file, debug)
    if args.command == "delete":
        return commands.polls_delete(api_url, args.name, debug)
    return 2


def cmd_credentials(args, api_url, debug):
    if args.command == "set":
        return commands.credentials_set(api_url, args.provider, args.file, debug)
    return 2


def cmd_grants(args, api_url, debug):
    if args.command == "list":
        return commands.grants_list(api_url, args.connection, debug)
    if args.command == "save":
        return commands.grants_save(api_url, args.file, debug)
    if args.command == "delete":
        return commands.grants_delete(api_url, args.connection_id, args.grantee, debug)
    return 2


def cmd_users(args, api_url, debug):
    if args.command == "list":
        return commands.users_list(api_url, debug)
    if args.command == "set-role":
        return commands.users_set_role(api_url, args.subject, args.role,
                                       display_name=args.display_name, debug=debug)
    if args.command == "remove":
        return commands.users_remove(api_url, args.subject, assume_yes=args.yes,
                                     debug=debug)
    return 2


def cmd_tokens(args, api_url, debug):
    if args.command == "list":
        return commands.tokens_list(api_url, debug)
    if args.command == "create":
        return commands.tokens_create(api_url, args.name, args.agent, debug)
    if args.command == "revoke":
        return commands.tokens_revoke(api_url, args.name, debug)
    return 2


def cmd_oauth_clients(args, api_url, debug):
    if args.command == "list":
        return commands.oauth_clients_list(api_url, debug)
    if args.command == "set":
        return commands.oauth_clients_set(api_url, args.provider, args.client_id,
                                          args.client_secret_file, debug)
    return 2


def cmd_runs(args, api_url, debug):
    if args.command == "list":
        return commands.runs_list(api_url, args.limit, workflow=args.workflow,
                                  status=args.status, since=args.since,
                                  before=args.before, query=args.search,
                                  next_token=args.next_token, debug=debug)
    if args.command == "show":
        return commands.runs_show(api_url, args.run_id, debug)
    if args.command == "replay":
        return commands.runs_replay(api_url, args.run_id, from_step=args.from_step, debug=debug)
    if args.command == "replay-failed":
        return commands.runs_replay_failed(api_url, args.workflow_id, debug)
    if args.command == "cancel":
        return commands.runs_cancel(api_url, args.run_id, debug)
    if args.command == "export":
        return commands.runs_export(api_url, out=args.out, max_rows=args.max_rows,
                                    workflow=args.workflow, status=args.status,
                                    since=args.since, before=args.before,
                                    query=args.search, debug=debug)
    return 2


def cmd_inbox(args, api_url, debug):
    if args.command == "list":
        return commands.inbox_list(api_url, connector=args.connector,
                                   limit=args.limit, debug=debug)
    if args.command == "show":
        return commands.inbox_show(api_url, args.inbox_id, debug)
    if args.command == "replay":
        return commands.inbox_replay(api_url, args.inbox_id, debug)
    return 2


def cmd_audit(args, api_url, debug):
    if args.command == "list":
        return commands.audit_list(api_url, args.limit, connection=args.connection,
                                   action=args.action, actor=args.actor,
                                   agent=args.agent, outcome=args.outcome,
                                   since=args.since, before=args.before,
                                   query=args.query, next_token=args.next_token,
                                   debug=debug)
    if args.command == "export":
        return commands.audit_export(api_url, out=args.out, max_rows=args.max_rows,
                                     connection=args.connection, action=args.action,
                                     actor=args.actor, agent=args.agent,
                                     outcome=args.outcome, since=args.since,
                                     before=args.before, query=args.query, debug=debug)
    return 2


def cmd_storage(args, api_url, debug):
    if args.command == "get":
        return commands.storage_get(api_url, args.workflow, args.key, debug)
    if args.command == "set":
        return commands.storage_set(api_url, args.workflow, args.key, args.value,
                                    ttl_seconds=args.ttl_seconds, debug=debug)
    if args.command == "find":
        return commands.storage_find(api_url, args.workflow, prefix=args.prefix,
                                     limit=args.limit, debug=debug)
    if args.command == "delete":
        return commands.storage_delete(api_url, args.workflow, args.key, debug)
    return 2


if __name__ == "__main__":
    sys.exit(main())
