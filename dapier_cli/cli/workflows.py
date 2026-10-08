"""The `dapier workflows` noun."""

from .. import commands

GROUPS = ("workflows",)


def register(sub):
    wf_p = sub.add_parser("workflows", help="Workflow YAML committed by the console designer")
    wf_sub = wf_p.add_subparsers(dest="command", required=True)
    wf_list_p = wf_sub.add_parser("list", help="List workflows and their On/Off state")
    wf_list_p.add_argument("--search", default=None,
                           help="Only workflows whose id, name, description, trigger, "
                                "or action types contain this text")
    wf_list_p.add_argument("--tag", default=None,
                           help="Only workflows carrying this tag (case-insensitive)")
    wf_list_p.add_argument("--folder", default=None,
                           help="Only workflows in this folder (case-insensitive)")
    wf_show_p = wf_sub.add_parser("show", help="Show one workflow (JSON)")
    wf_show_p.add_argument("file", help="Workflow id, file (my-flow.yaml), or exact name")
    wf_export_p = wf_sub.add_parser(
        "export", help="Print (or write) a workflow's canonical YAML; --all bundles every workflow")
    wf_export_p.add_argument("file", nargs="?", default=None,
                             help="Workflow id, file, or exact name (omit with --all)")
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
    wf_save_p = wf_sub.add_parser("save", help="Save a workflow YAML as a draft "
                                  "(nothing goes live; publish it with workflows publish)")
    wf_save_p.add_argument("file", help="Path to the workflow YAML, or - for stdin")
    wf_save_p.add_argument("--rename-from", default=None,
                           help="Previous file name when the workflow was renamed")
    def add_bulk_toggle_flags(parser):
        parser.add_argument("file", nargs="*", default=None,
                            help="Workflow id(s), file(s), or exact name(s)")
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
    wf_dup_p.add_argument("file", help="Workflow id, file (my-flow.yaml), or exact name")
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
    wf_test_code_p = wf_sub.add_parser("test-code",
                                       help="Run a code step's tests (the action's tests list)")
    wf_test_code_p.add_argument("file", help="Path to the workflow YAML, or - for stdin")
    wf_test_code_p.add_argument("--action", required=True,
                                help="The code step's action id (e.g. triage)")
    wf_versions_p = wf_sub.add_parser("versions",
                                      help="List a workflow's published versions (newest first)")
    wf_versions_p.add_argument("file", help="Workflow id, file (my-flow.yaml), or exact name")
    wf_diff_p = wf_sub.add_parser("diff",
                                  help="Unified diff between two published versions")
    wf_diff_p.add_argument("file", help="Workflow id, file (my-flow.yaml), or exact name")
    wf_diff_p.add_argument("--from", dest="from_revision", type=int, required=True,
                           help="Version to diff from, as shown by workflows versions")
    wf_diff_p.add_argument("--to", dest="to_revision", type=int, required=True,
                           help="Version to diff to, as shown by workflows versions")
    wf_rollback_p = wf_sub.add_parser("rollback",
                                      help="Restore an old version of a workflow (live immediately)")
    wf_rollback_p.add_argument("file", help="Workflow id, file (my-flow.yaml), or exact name")
    wf_rollback_p.add_argument("revision",
                               help="Version number to restore, as shown by workflows versions")
    wf_publish_p = wf_sub.add_parser("publish",
                                     help="Promote a workflow's saved draft live "
                                          "(v1 for a draft-only workflow; 409 when stale)")
    wf_publish_p.add_argument("file", help="Workflow id, file (my-flow.yaml), or exact name")
    wf_discard_p = wf_sub.add_parser("discard",
                                     help="Throw a workflow's saved draft away (live untouched)")
    wf_discard_p.add_argument("file", help="Workflow id, file (my-flow.yaml), or exact name")
    wf_discard_p.add_argument("--yes", action="store_true",
                              help="Discard without a confirmation prompt")
    wf_draft_diff_p = wf_sub.add_parser("draft-diff",
                                        help="Unified diff of a workflow's draft against live")
    wf_draft_diff_p.add_argument("file", help="Workflow id, file (my-flow.yaml), or exact name")
    wf_sample_p = wf_sub.add_parser(
        "sample", help="Pull a sample of what a flow receives when it starts")
    wf_sample_p.add_argument("connector", nargs="?", default=None,
                             help="e.g. custom, dataops, dropbox, email, mailchimp, poll, "
                                  "renderer, schedule, telegram, webhook, youtube, zoom")
    wf_sample_p.add_argument("--workflow", default=None,
                             help="Workflow id: print its newest run's recorded "
                                  "input (else the connector's sample) "
                                  "with the {trigger.*} fields it offers")
    wf_sample_p.add_argument("--event", default=None,
                             help="Event name override; for poll, the trigger's name")
    wf_sample_p.add_argument("--connection-id", default=None,
                             help="Prefer this connected account for live pulls")
    wf_sample_p.add_argument("--limit", type=int, default=None,
                             help="Max options when kind is options (1-25)")
    wf_sample_p.add_argument("--resource", default=None,
                             help="Options listing instead of a sample (e.g. slack.channels)")
    wf_delete_p = wf_sub.add_parser("delete",
                                    help="Delete a workflow: unpublish it live and remove its YAML from the repo")
    wf_delete_p.add_argument("file", help="Workflow id, file (my-flow.yaml), or exact name")
    wf_delete_p.add_argument("--yes", action="store_true",
                             help="Skip the confirmation prompt")
    wf_tags_p = wf_sub.add_parser("tags",
                                  help="Edit a workflow's tags (Zapier-style organization labels)")
    wf_tags_p.add_argument("file", help="Workflow id, file (my-flow.yaml), or exact name")
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
    wf_folder_p.add_argument("file", help="Workflow id, file (my-flow.yaml), or exact name")
    wf_folder_p.add_argument("--set", dest="set_value", default=None,
                             help="Folder name — sets or moves the workflow (at most one folder)")
    wf_folder_p.add_argument("--clear", action="store_true",
                             help="Remove the workflow from its folder")


# Commands whose `file` argument names a stored workflow (not a local YAML
# path): each also accepts the bare id or the exact workflow name.
_WORKFLOW_REF_COMMANDS = ("show", "export", "on", "off", "enable", "disable", "duplicate",
                          "versions", "diff", "rollback", "publish", "discard",
                          "draft-diff", "delete", "tags", "folder")


def _resolve_refs(args, api_url, debug):
    """Rewrites args.file from an id or name to the workflow file; returns an
    error message, or None."""
    if args.command not in _WORKFLOW_REF_COMMANDS or not getattr(args, "file", None):
        return None
    try:
        if isinstance(args.file, list):
            args.file = [commands.resolve_workflow_ref(api_url, ref, debug=debug)
                         for ref in args.file]
        else:
            args.file = commands.resolve_workflow_ref(api_url, args.file, debug=debug)
    except commands.WorkflowRefError as exc:
        return str(exc)
    return None


def run(args, api_url, debug, child=None):
    error = _resolve_refs(args, api_url, debug)
    if error:
        print(f"Error: {error}")
        return 4
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
        return commands.workflows_diff(api_url, args.file, args.from_revision,
                              args.to_revision, debug=debug)
    if args.command == "rollback":
        return commands.workflows_rollback(api_url, args.file, args.revision, debug=debug)
    if args.command == "publish":
        return commands.workflows_publish(api_url, args.file, debug=debug)
    if args.command == "discard":
        return commands.workflows_discard(api_url, args.file, assume_yes=args.yes, debug=debug)
    if args.command == "draft-diff":
        return commands.workflows_draft_diff(api_url, args.file, debug=debug)
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
    if args.command == "sample":
        if args.workflow:
            return commands.triggers_workflow_sample(api_url, args.workflow, debug=debug)
        if not args.connector:
            print("Error: give a connector (e.g. email) or --workflow <workflow_id>")
            return 2
        return commands.triggers_sample(api_url, args.connector, event=args.event,
                                        connection_id=args.connection_id,
                                        limit=args.limit, resource=args.resource, debug=debug)
    if args.command == "test-step":
        return commands.workflows_test_step(api_url, args.file, args.action, args.event,
                                            steps_spec=args.steps, execute=args.execute,
                                            debug=debug)
    if args.command == "test-code":
        return commands.workflows_test_code(api_url, args.file, args.action, debug=debug)
    return 2
