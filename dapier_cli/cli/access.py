"""The `dapier credentials|grants|tokens|oauth-clients` nouns (operator access)."""

from .. import commands

GROUPS = ("credentials", "grants", "tokens", "oauth-clients",)


def register(sub):
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
    grants_list_p.add_argument("--limit", type=int, default=None,
                               help="Page size (default: the API's)")
    grants_list_p.add_argument("--next", dest="next_token",
                               help="Page token from the previous call's `next page:` footer")
    grants_save_p = grants_sub.add_parser("save", help="Create or update a grant from a JSON file")
    grants_save_p.add_argument("file", help="Path to the grant JSON, or - for stdin")
    grants_del_p = grants_sub.add_parser("delete", help="Revoke one grant")
    grants_del_p.add_argument("connection_id")
    grants_del_p.add_argument("grantee", help="The grant's grantee ID (subject#agent)")
    tokens_p = sub.add_parser("tokens", help="Operator-issued API tokens for headless consumers")
    tokens_sub = tokens_p.add_subparsers(dest="command", required=True)
    tokens_sub.add_parser("list", help="List API tokens (no secrets)")
    tokens_create_p = tokens_sub.add_parser("create", help="Issue an API token; the value prints once")
    tokens_create_p.add_argument("--name", required=True,
                                 help="Token ID; the machine subject becomes token:<name>")
    tokens_create_p.add_argument("--agent", required=True,
                                 help="The one agent name this token may act as")
    tokens_create_p.add_argument("--output", default=None,
                                 help="Write the one-time token to a new owner-only file")
    tokens_del_p = tokens_sub.add_parser("revoke", help="Revoke an API token")
    tokens_del_p.add_argument("name")
    tokens_rm_p = tokens_sub.add_parser("delete",
                                        help="Permanently remove a revoked API token and its grants")
    tokens_rm_p.add_argument("name")
    oac_p = sub.add_parser("oauth-clients", help="Shared OAuth clients per provider (same as the console's Credentials view)")
    oac_sub = oac_p.add_subparsers(dest="command", required=True)
    oac_sub.add_parser("list", help="Show the configured shared OAuth clients (no secrets)")
    oac_set_p = oac_sub.add_parser("set", help="Store the shared OAuth client for a provider")
    oac_set_p.add_argument("provider", help="dropbox, google, youtube, or zoom")
    oac_set_p.add_argument("--client-id", required=True)
    oac_set_p.add_argument("--client-secret-file", required=True,
                           help="File with the client secret, or - for stdin")


def run(args, api_url, debug, child=None):
    if args.group == "credentials":
            if args.command == "set":
                return commands.credentials_set(api_url, args.provider, args.file, debug)
            return 2
    if args.group == "grants":
            if args.command == "list":
                return commands.grants_list(api_url, args.connection, debug,
                                            limit=args.limit, next_token=args.next_token)
            if args.command == "save":
                return commands.grants_save(api_url, args.file, debug)
            if args.command == "delete":
                return commands.grants_delete(api_url, args.connection_id, args.grantee, debug)
            return 2
    if args.group == "tokens":
            if args.command == "list":
                return commands.tokens_list(api_url, debug)
            if args.command == "create":
                return commands.tokens_create(api_url, args.name, args.agent, debug,
                                              output=args.output)
            if args.command == "revoke":
                return commands.tokens_revoke(api_url, args.name, debug)
            if args.command == "delete":
                return commands.tokens_delete(api_url, args.name, debug)
            return 2
    if args.group == "oauth-clients":
            if args.command == "list":
                return commands.oauth_clients_list(api_url, debug)
            if args.command == "set":
                return commands.oauth_clients_set(api_url, args.provider, args.client_id,
                                                  args.client_secret_file, debug)
            return 2
    return 2
