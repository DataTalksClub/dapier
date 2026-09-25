"""webhook action: POST the event, optionally HMAC-signed."""
import hashlib
import hmac
import json
import urllib.request

from . import base


def run_webhook(action, event):
    body = json.dumps(event, separators=(",", ":"), sort_keys=True).encode()
    headers = {"content-type": "application/json", "user-agent": "dapier/0.1"}
    if action.get("secret_id"):
        digest = hmac.new(base._signing_secret(action["secret_id"]).encode(), body, hashlib.sha256).hexdigest()
        headers["x-dapier-signature"] = f"sha256={digest}"
    request = urllib.request.Request(action["url"], data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=action.get("timeout_seconds", 10)) as response:
        if response.status >= 300:
            raise RuntimeError(f"webhook returned HTTP {response.status}")
        return {"status": response.status}
