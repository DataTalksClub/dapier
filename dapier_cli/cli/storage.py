"""The `dapier storage` noun."""

from .. import commands

GROUPS = ("storage",)


def register(sub):
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


def run(args, api_url, debug, child=None):
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
