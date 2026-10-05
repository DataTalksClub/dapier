"""Implementations of the `dapier hooks` and `dapier webhooks` commands."""

import json

from .. import api
from .shared import read_json_file

__all__ = ["hooks_delete", "hooks_list", "hooks_save", "hooks_show", "print_hook"]


def print_hook(item):
    for key in ("hook_id", "kind", "url", "connection_id", "list_id", "description",
                "dedupe_path", "enabled", "created_by", "created_at",
                "updated_at"):
        if item.get(key) not in (None, ""):
            print(f"{key}: {item[key]}")
    if item.get("events"):
        print(f"events: {', '.join(item['events'])}")
    if item.get("response"):
        print(f"response: {json.dumps(item['response'], sort_keys=True)}")
    if item.get("kind") == "telegram":
        print("secret header: x-telegram-bot-api-secret-token (managed by Telegram)")
    elif item.get("kind") == "mailchimp":
        print("auth: none — Mailchimp calls the unguessable URL directly")
    else:
        print(f"auth header: {item.get('header', 'authorization')}: Bearer {item.get('token', '')}")


def hooks_list(api_url, kind=None, debug=False):
    query = f"?kind={kind}" if kind else ""
    data = api.call(api_url, "GET", f"/api/agent/hook-triggers{query}", debug=debug)
    items = data.get("hooks", [])
    if not items:
        print("No hook triggers yet. Create one with `dapier hooks save`.")
    for item in items:
        enabled = "yes" if item.get("enabled", True) else "no"
        print(f"{item.get('hook_id', ''):20} {item.get('kind', ''):9} "
              f"enabled={enabled:3} {item.get('url', '')}")
    return 0


def hooks_show(api_url, name, debug=False):
    data = api.call(api_url, "GET", "/api/agent/hook-triggers", debug=debug)
    item = next((h for h in data.get("hooks", []) if h.get("hook_id") == name), None)
    if item is None:
        print(f"No hook trigger named '{name}'.")
        return 4
    print_hook(item)
    return 0


def hooks_save(api_url, path, debug=False, sync_response=False):
    body, error = read_json_file(path)
    if error:
        print(error)
        return 2
    if sync_response:
        # Force response.mode sync (a template in the file's own response
        # object is kept); the API's shared validator still bounds the shape,
        # so a telegram kind or an unknown mode is rejected server-side.
        response = body.get("response") if isinstance(body.get("response"), dict) else {}
        body["response"] = {**response, "mode": "sync"}
    data = api.call(api_url, "PUT", "/api/agent/hook-triggers", body, debug=debug)
    verb = "Created" if data.get("created") else "Updated"
    print(f"{verb} {data.get('kind', 'webhook')} hook '{data.get('hook_id')}'. "
          "It is live immediately; no deploy needed.")
    for warning in data.get("warnings") or []:
        print(f"  warning: {warning}")
    if data.get("kind") == "telegram":
        print(f"  Telegram delivery URL: {data.get('url')}")
        if data.get("connection_id"):
            print(f"  Bot connection: {data.get('connection_id')}")
    elif data.get("kind") == "mailchimp":
        # Mailchimp sends no auth headers — the unguessable URL is the
        # credential — and the webhook registration was just (re)drawn with
        # the stored Mailchimp API key, so no curl hint with a bearer token.
        print(f"  Mailchimp delivery URL: {data.get('url')}")
        if data.get("list_id"):
            print(f"  Audience: {data.get('list_id')} — webhook registered on the list")
    else:
        print(f"  URL: {data.get('url')}")
        if data.get("signed"):
            # Signature-locked: the bearer token alone is rejected, so the
            # hint signs the body the way the ingress verifies it.
            header = data.get("signature_header", "x-dapier-signature")
            print(f"  Callers send: {header}: sha256=<hex HMAC-SHA256(secret, raw body)>")
        else:
            print(f"  Callers send: {data.get('header', 'authorization')}: Bearer {data.get('token', '')}")
            url, token = data.get("url", ""), data.get("token", "")
            print("  Try it: curl -X POST '" + url + "' "
                  "-H 'authorization: Bearer " + token + "' "
                  "-H 'content-type: application/json' -d '{\"hello\":\"world\"}'")
    return 0


def hooks_delete(api_url, name, kind=None, debug=False):
    query = f"name={name}" + (f"&kind={kind}" if kind else "")
    data = api.call(api_url, "DELETE", f"/api/agent/hook-triggers?{query}", debug=debug)
    print(f"Deleted {data.get('kind') or 'hook'} trigger '{data.get('hook_id') or name}'.")
    for warning in data.get("warnings") or []:
        print(f"  warning: {warning}")
    return 0


