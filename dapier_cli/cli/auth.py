"""The `dapier auth` noun: device/browser sign-in, status, logout."""

from .. import auth, config

GROUPS = ("auth",)


def register(sub):
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


def run(args, api_url, debug=None, child=None):
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
