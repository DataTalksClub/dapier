"""Local configuration and session storage for the dapier CLI.

The session file holds DTC-issued tokens only (never a password, never a
provider secret) and is created with owner-only permissions.
"""

import json
import os
from pathlib import Path

DEFAULT_API_URL = "https://dapier.dtcdev.click"


def base_dir():
    root = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(root) / "dapier"


def config_file():
    return base_dir() / "config.json"


def session_file():
    return base_dir() / "session.json"


def api_url(args_url=None):
    if args_url:
        return args_url.rstrip("/")
    if os.environ.get("DAPIER_API_URL"):
        return os.environ["DAPIER_API_URL"].rstrip("/")
    try:
        stored = json.loads(config_file().read_text())
        if isinstance(stored, dict) and stored.get("api_url"):
            return str(stored["api_url"]).rstrip("/")
    except (OSError, ValueError):
        pass
    return DEFAULT_API_URL


def save_api_url(url):
    path = config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"api_url": url.rstrip("/")}) + "\n")
    _restrict(path)


def load_session():
    try:
        data = json.loads(session_file().read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def save_session(data):
    path = session_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data) + "\n")
    _restrict(path)


def clear_session():
    try:
        session_file().unlink()
        return True
    except OSError:
        return False


def _restrict(path):
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
