"""Local configuration and session storage for the dapier CLI.

The session file holds DTC-issued tokens only (never a password, never a
provider secret). Credentials are secrets on a shared machine: the
directory and file are owner-only, the token is never accepted as a
command line argument (that lands in shell history and `ps` output),
and every write replaces the file atomically through an owner-only temp
file — a reader never sees a half-written store, a crash never
truncates it, and the secret never sits on disk at default
permissions. The write path matches DataOps's credential store.
"""

import json
import os
import uuid
from pathlib import Path

DEFAULT_API_URL = "https://dapier.dtcdev.click"


def base_dir():
    root = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(root) / "dapier"


def session_file():
    return base_dir() / "session.json"


def api_url(args_url=None):
    if args_url:
        return args_url.rstrip("/")
    if os.environ.get("DAPIER_API_URL"):
        return os.environ["DAPIER_API_URL"].rstrip("/")
    return DEFAULT_API_URL


def load_session():
    try:
        data = json.loads(session_file().read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def save_session(data):
    _write_private(session_file(), json.dumps(data) + "\n")


def clear_session():
    try:
        session_file().unlink()
        return True
    except OSError:
        return False


def _write_private(path, text):
    """Replace a credential file atomically and owner-only.

    Written once through a 0600 exclusive temp file beside the target and
    renamed into place; the temp file is always removed, so a failed
    replacement leaves the previous store intact and no debris behind.
    """
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass
    temporary = directory / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    _restrict(path)


def _restrict(path):
    if os.name == "nt":
        from src.dapier.private_files import protect

        protect(path)
        return
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
