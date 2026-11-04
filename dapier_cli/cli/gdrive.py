"""The `dapier gdrive` noun: search/list, meta, download/export, upload,
update, copy, move, share, trash, mkdir over /api/agent/gdrive/*."""

from .. import commands

GROUPS = ("gdrive",)

EPILOG = """examples:
  dapier gdrive accounts                          # list Google accounts serving Drive
  dapier gdrive list --account me@gmail.com --max 20
  dapier gdrive search "name contains 'invoice'" --account me@gmail.com
  dapier gdrive download FILE_ID --out ./report.pdf --account me@gmail.com
  dapier gdrive upload ./notes.txt --name notes.txt --folder FOLDER_ID --account me@gmail.com
  dapier gdrive share FILE_ID --role reader --with user --email peer@example.com --account me@gmail.com
  dapier gdrive trash FILE_ID --account me@gmail.com  # recoverable; --permanent destroys
--account is required on every operation except accounts; use the verified
email or connection id. Ambiguous fragments are rejected.
Google-native docs export on download (doc/slide → PDF, sheet → CSV,
drawing → PNG; override with --export-mime)."""

COMMANDS = ("accounts", "list", "search", "meta", "download", "upload",
            "update", "copy", "move", "share", "trash", "restore", "mkdir")


def _preview_flag(p):
    p.add_argument("--preview", action="store_true",
                   help="Print the exact request body and exit — nothing is sent")


def register(sub):
    import argparse

    p = sub.add_parser(
        "gdrive", help="Google Drive over a stored connection: search, "
        "download/export, upload, share, trash (server-side token)",
        epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = p.add_subparsers(dest="command", required=True)

    acc = sp.add_parser("accounts", help="List Google accounts serving Drive")
    acc.add_argument("--json", action="store_true")

    def account_arg(x):
        x.add_argument("--account", required=True)

    listing = sp.add_parser("list", help="List files, newest first")
    listing.add_argument("--query", default=None, help="Extra Drive query, e.g. name contains 'x'")
    listing.add_argument("--folder", default=None, help="Restrict to a folder id")
    account_arg(listing)
    listing.add_argument("--max", type=int, default=25)
    listing.add_argument("--page-token", default=None)
    listing.add_argument("--json", action="store_true")

    search = sp.add_parser("search", help="Search files with a Drive query")
    search.add_argument("query")
    search.add_argument("--folder", default=None)
    account_arg(search)
    search.add_argument("--max", type=int, default=25)
    search.add_argument("--page-token", default=None)
    search.add_argument("--json", action="store_true")

    meta = sp.add_parser("meta", help="Show one file's metadata")
    meta.add_argument("file_id")
    account_arg(meta)
    meta.add_argument("--json", action="store_true")

    download = sp.add_parser("download", help="Download/export a file to disk")
    download.add_argument("file_id")
    account_arg(download)
    download.add_argument("--out", default=None, help="Output path (default: the file's name)")
    download.add_argument("--export-mime", default=None,
                          help="Export target mime for Google-native files")

    upload = sp.add_parser("upload", help="Upload a file (path, or '-' for stdin)")
    upload.add_argument("path", help="Local file to upload; '-' reads stdin")
    upload.add_argument("--name", required=True, help="Target filename in Drive")
    account_arg(upload)
    upload.add_argument("--folder", default=None, help="Parent folder id")
    upload.add_argument("--mime", default=None, help="Content type (guessed from the name)")
    upload.add_argument("--json", action="store_true")
    _preview_flag(upload)

    update = sp.add_parser("update", help="Rename and/or replace a file's content")
    update.add_argument("file_id")
    account_arg(update)
    update.add_argument("--name", default=None)
    update.add_argument("--file", default=None, help="Replacement content (path or '-' = stdin)")
    update.add_argument("--mime", default=None)
    update.add_argument("--json", action="store_true")
    _preview_flag(update)

    copy = sp.add_parser("copy", help="Copy a file")
    copy.add_argument("file_id")
    account_arg(copy)
    copy.add_argument("--name", default=None, help="Name for the copy")
    copy.add_argument("--json", action="store_true")
    _preview_flag(copy)

    move = sp.add_parser("move", help="Move a file between folders")
    move.add_argument("file_id")
    account_arg(move)
    move.add_argument("--add-folder", default=None, help="Folder id to file it into")
    move.add_argument("--remove-folder", default=None, help="Folder id to take it out of")
    move.add_argument("--json", action="store_true")
    _preview_flag(move)

    share = sp.add_parser("share", help="Share a file (create a permission)")
    share.add_argument("file_id")
    account_arg(share)
    share.add_argument("--role", default="reader",
                       choices=["reader", "commenter", "writer"])
    share.add_argument("--with", dest="share_type", default="user",
                       choices=["user", "group", "domain", "anyone"])
    share.add_argument("--email", default=None, help="Grantee (required for user/group)")
    share.add_argument("--json", action="store_true")
    _preview_flag(share)

    trash = sp.add_parser("trash", help="Trash a file (recoverable; --permanent destroys)")
    trash.add_argument("file_id")
    account_arg(trash)
    trash.add_argument("--permanent", action="store_true",
                       help="Destroy outright — no trash stop, no way back")
    trash.add_argument("--json", action="store_true")
    _preview_flag(trash)

    restore = sp.add_parser("restore", help="Restore a file from the trash")
    restore.add_argument("file_id")
    account_arg(restore)
    restore.add_argument("--json", action="store_true")

    mkdir = sp.add_parser("mkdir", help="Create a folder")
    mkdir.add_argument("name")
    account_arg(mkdir)
    mkdir.add_argument("--parent", default=None, help="Parent folder id (default: root)")
    mkdir.add_argument("--json", action="store_true")
    _preview_flag(mkdir)


def run(args, api_url, debug, child=None):
    c = commands
    account = getattr(args, "account", None)
    as_json = getattr(args, "json", False)
    if args.command == "accounts":
        return c.gdrive_accounts(api_url, as_json=args.json, debug=debug)
    if args.command == "list":
        return c.gdrive_list(api_url, query=args.query, folder=args.folder, account=account,
                             max_results=args.max, page_token=args.page_token,
                             as_json=as_json, debug=debug)
    if args.command == "search":
        return c.gdrive_list(api_url, query=args.query, folder=args.folder, account=account,
                             max_results=args.max, page_token=args.page_token,
                             as_json=as_json, debug=debug)
    if args.command == "meta":
        return c.gdrive_meta(api_url, args.file_id, account=account, as_json=as_json, debug=debug)
    if args.command == "download":
        return c.gdrive_download(api_url, args.file_id, account=account, out=args.out,
                                 export_mime=args.export_mime, debug=debug)
    if args.command == "upload":
        return c.gdrive_upload(api_url, args.path, name=args.name, account=account,
                               folder=args.folder, mime=args.mime, as_json=as_json,
                               preview=args.preview, debug=debug)
    if args.command == "update":
        return c.gdrive_update(api_url, args.file_id, account=account, name=args.name,
                               file=args.file, mime=args.mime, as_json=as_json,
                               preview=args.preview, debug=debug)
    if args.command == "copy":
        return c.gdrive_copy(api_url, args.file_id, account=account, name=args.name,
                             as_json=as_json, preview=args.preview, debug=debug)
    if args.command == "move":
        if not (args.add_folder or args.remove_folder):
            print("move needs --add-folder and/or --remove-folder")
            return 2
        return c.gdrive_move(api_url, args.file_id, account=account,
                             add_folder=args.add_folder, remove_folder=args.remove_folder,
                             as_json=as_json, preview=args.preview, debug=debug)
    if args.command == "share":
        if args.share_type in ("user", "group") and not args.email:
            print(f"share --with {args.share_type} needs --email")
            return 2
        return c.gdrive_share(api_url, args.file_id, account=account, role=args.role,
                              share_type=args.share_type, email=args.email,
                              as_json=as_json, preview=args.preview, debug=debug)
    if args.command == "trash":
        return c.gdrive_trash(api_url, args.file_id, account=account,
                              permanent=args.permanent, as_json=as_json,
                              preview=args.preview, debug=debug)
    if args.command == "restore":
        return c.gdrive_restore(api_url, args.file_id, account=account,
                                as_json=as_json, debug=debug)
    if args.command == "mkdir":
        return c.gdrive_mkdir(api_url, args.name, account=account, parent=args.parent,
                              as_json=as_json, debug=debug)
    return 2
