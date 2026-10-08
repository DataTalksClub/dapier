"""Implementations of the `dapier hooks` and `dapier webhooks` commands."""

import json
from urllib.parse import quote, urlencode

from .. import api
from .shared import read_json_file

__all__ = ["hooks_deliveries", "hooks_delete", "hooks_delivery", "hooks_list", "hooks_save",
           "hooks_show", "hooks_test", "print_delivery", "print_hook"]


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
    elif item.get("signed"):
        print(f"verification: signed — {item.get('signature_header') or 'x-dapier-signature'}: "
              "sha256=<hex HMAC-SHA256(secret, raw body)>")
    else:
        print("verification: bearer token")
        print(f"auth header: {item.get('header', 'authorization')}: Bearer {item.get('token', '')}")
    if "workflows" in item:
        flows = item["workflows"] or []
        if not flows:
            print("starts: no workflow yet — deliveries are logged but nothing runs")
        for flow in flows:
            notes = [note for note, on in (("disabled", not flow.get("enabled", True)),
                                           ("when its filters pass", flow.get("conditional")),
                                           ("listens to every webhook", flow.get("any_hook")))
                     if on]
            print(f"starts: {flow.get('id')}" + (f" ({', '.join(notes)})" if notes else ""))


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




def _size(value):
    if value is None:
        return "-"
    return f"{value} B" if value < 1024 else f"{value / 1024:.1f} KB"


def hooks_deliveries(api_url, name=None, limit=25, next_token=None, debug=False):
    """Recent webhook deliveries with their outcome, newest first."""
    params = {"limit": int(limit)}
    if name:
        params["hook"] = name
    if next_token:
        params["next"] = next_token
    data = api.call(api_url, "GET",
                    f"/api/agent/hook-triggers/deliveries?{urlencode(params)}", debug=debug)
    rows = data.get("deliveries") or []
    if not rows:
        print("No deliveries yet" + (f" for '{name}'" if name else "") +
              ". Send one with `dapier hooks test <name>`.")
        return 0
    print(f"{'RECEIVED':19} {'HOOK':18} {'SIZE':8} {'OUTCOME':40} DELIVERY ID")
    for row in rows:
        received = (row.get("received_at") or "-")[:19].replace("T", " ")
        outcome = row.get("outcome_text") or row.get("outcome") or ""
        if row.get("test"):
            outcome = "[test] " + outcome
        print(f"{received:19} {str(row.get('hook') or '-'):18} {_size(row.get('size_bytes')):8} "
              f"{outcome[:40]:40} {row.get('delivery_id', '')}")
    next_page = (data.get("paging") or {}).get("next")
    if next_page:
        print(f"\nnext page: {next_page}  (pass it to --next)")
    return 0


def print_delivery(row):
    print(f"delivery: {row.get('delivery_id')}" + ("  [test request]" if row.get("test") else ""))
    for key in ("hook", "received_at", "processed_at", "response_status", "content_type"):
        if row.get(key) not in (None, ""):
            print(f"  {key}: {row[key]}")
    print(f"  size: {_size(row.get('size_bytes'))}")
    print(f"  outcome: {row.get('outcome_text') or row.get('outcome')}")
    for run in row.get("runs") or []:
        line = f"  run: {run.get('run_id')} {run.get('status')}"
        if run.get("error"):
            line += f" — {run['error']}"
        print(line)
    if row.get("headers"):
        print("  headers:")
        for key, value in sorted(row["headers"].items()):
            print(f"    {key}: {value}")
    if row.get("query"):
        print(f"  query: {json.dumps(row['query'], sort_keys=True)}")
    if "body" in row:
        print("  body:")
        for line in json.dumps(row.get("body"), indent=2, sort_keys=True, default=str).splitlines():
            print(f"    {line}")


def hooks_delivery(api_url, delivery_id, debug=False):
    """One delivery: request headers, payload, run outcome."""
    data = api.call(api_url, "GET",
                    f"/api/agent/hook-triggers/deliveries/{quote(delivery_id, safe='')}",
                    debug=debug)
    print_delivery(data.get("delivery") or {})
    print(f"Replay it with `dapier runs events replay {delivery_id}`.")
    return 0


def hooks_test(api_url, name, data_path=None, debug=False):
    """Send test request: POST a sample (or --data) payload at the hook
    through its real intake, then show what it answered."""
    body = {"name": name}
    if data_path:
        payload, error = read_json_file(data_path)
        if error:
            print(error)
            return 2
        body["data"] = payload
    data = api.call(api_url, "POST", "/api/agent/hook-triggers/test", body, debug=debug)
    response = data.get("response") or {}
    print(f"Sent a test request to {data.get('url')}")
    print(f"  response: {response.get('status')} {json.dumps(response.get('body'), sort_keys=True, default=str)}")
    if data.get("delivery_id"):
        print(f"  delivery: {data['delivery_id']}")
        print(f"  Follow it with `dapier hooks delivery {data['delivery_id']}`.")
    status = response.get("status") or 0
    return 0 if 200 <= int(status) < 300 else 1
