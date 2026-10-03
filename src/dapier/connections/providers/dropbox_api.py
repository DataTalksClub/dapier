"""Dropbox provider glue used by core: the connection lookup and the
content download behind the DataOps forwarding path (dataops stages a
dropbox attachment into S3 before handing the email to DataOps).

This is core, not plugin code: engine.actions.dataops calls both on the
email-intake path, and core must not import plugin code (the slack_tokens
precedent). The dropbox plugin's runner imports the same pair from here,
so its tests keep patching the names on the runner module.
"""
import json

DROPBOX_DOWNLOAD_URL = "https://content.dropboxapi.com/2/files/download"


def dropbox_connection(connection_id):
    """The stored connection record for ``connection_id``."""
    from src.dapier.engine.actions import base

    return base._connected_connection(connection_id)


def dropbox_download(access_token, path, *, transport=None):
    """The file content at ``path`` (files/download), or RuntimeError."""
    transport = transport or _default_transport()
    headers = {
        "authorization": f"Bearer {access_token}",
        "dropbox-api-arg": json.dumps({"path": path}, separators=(",", ":")),
    }
    try:
        status, raw = transport(
            "POST", DROPBOX_DOWNLOAD_URL, headers=headers, body=b"", timeout=15,
        )
    except Exception as exc:
        raise RuntimeError(f"dropbox download unreachable: {type(exc).__name__}")
    if status >= 300:
        raise RuntimeError(f"dropbox download returned HTTP {status}")
    return raw


def _default_transport():
    # Resolved at call time like the runners' ``transport or
    # base._default_transport`` read, so tests patching the engine's
    # default transport keep working.
    from src.dapier.engine.actions import base

    return base._default_transport
