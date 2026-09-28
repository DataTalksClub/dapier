"""Mailchimp webhook intake: form-encoded deliveries to workflow events.

Mailchimp POSTs ``application/x-www-form-urlencoded`` bodies — a ``type``
field plus a flat ``data[key]`` / ``data[a][b]`` field set — to the URL
registered in the Mailchimp account, and sends no signature or auth header,
so the unguessable hook URL is the credential. The registration ``ping``
Mailchimp sends when a webhook is saved is answered 200 without publishing;
the declared types (the Mailchimp chip's events) publish
``connector="mailchimp"`` with ``event`` named by ``type`` and ``data`` the
full parsed body plus the ``hook`` the URL named — the exact shape the
mailchimp trigger sample documents (connectors.mailchimp), so a pulled
sample is what a real delivery carries.
"""

from urllib.parse import parse_qsl

# The types Mailchimp POSTs and the chip declares (connectors.triggers);
# anything else is a 400 so a misconfigured URL fails loudly instead of
# silently publishing an event no author matched a workflow to.
EVENT_TYPES = ("subscribe", "unsubscribe", "profile", "upemail", "cleaned", "campaign")


def _assign(root, key, value):
    """One ``data[merges][FNAME]``-style key into a nested dict; a plain key
    (``type``) lands at the root."""
    parts = [part for part in key.replace("]", "").split("[") if part]
    node = root
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[parts[-1]] = value


def parse_form(body):
    """The parsed webhook body: ``{"type": ..., "data": {...}}`` from the
    flat ``type`` / ``data[...]`` form fields."""
    parsed = {}
    for key, value in parse_qsl(body.decode(errors="replace"), keep_blank_values=True):
        _assign(parsed, key.strip(), value)
    return parsed


def handle(body, *, hook=None, publish):
    """One Mailchimp delivery to ``/hooks/mailchimp/{hook}``: ``(status,
    response payload)``.

    Never raises for a bad body — Mailchimp retries on non-2xx, and a
    malformed POST should settle, not loop.
    """
    parsed = parse_form(body or b"")
    kind = str(parsed.get("type") or "").strip().lower()
    if not kind:
        return 400, {"error": "no mailchimp webhook type in the form body"}
    if kind == "ping":
        return 200, {"accepted": True, "ping": True}
    if kind not in EVENT_TYPES:
        return 400, {"error": (f"unknown mailchimp webhook type '{kind}'; "
                               f"declared: {', '.join(EVENT_TYPES)}")}
    data = parsed.get("data")
    publish("mailchimp", kind,
            {"hook": hook, "type": kind, "data": data if isinstance(data, dict) else {}},
            source=(data or {}).get("list_id") or "mailchimp")
    return 200, {"accepted": True}
