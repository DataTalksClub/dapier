"""Materialize email body references for content-consuming actions.

Inbound-email v1 can carry UTF-8 body parts in S3 instead of inline. Passing
their metadata to an LLM silently turns missing content into an empty result.
Keep the stored event small, but resolve a private copy before rendering.
"""
from copy import deepcopy

# Fail rather than silently drop a closing ad or other content beyond a cap.
MAX_BODY_BYTES = 2_000_000


def _read(pointer):
    import boto3

    try:
        response = boto3.client("s3").get_object(
            Bucket=pointer["bucket"], Key=pointer["key"])
        stream = response["Body"]
        try:
            raw = stream.read(MAX_BODY_BYTES + 1)
        finally:
            stream.close()
    except Exception:
        # Provider exceptions can contain private bucket/key names.
        raise ValueError("email body could not be loaded from storage") from None
    if len(raw) > MAX_BODY_BYTES:
        raise ValueError("email body exceeds the content loading limit")
    return raw


def materialize_email(event):
    """Resolve body parts without mutating the trigger input or step history.

    Unreadable referenced content fails the action before any AI request.
    Legacy inline emails and non-email events pass through unchanged.
    """
    if event.get("connector") != "email":
        return event
    result = deepcopy(event)
    data = result.get("data") or {}
    body = data.get("body")
    cache = {}

    def resolve(part):
        if not isinstance(part, dict) or not part.get("s3"):
            return part
        pointer = part["s3"]
        try:
            key = (pointer["bucket"], pointer["key"])
            if key not in cache:
                cache[key] = _read(pointer).decode("utf-8")
        except (KeyError, TypeError, UnicodeDecodeError):
            raise ValueError("email body has an invalid storage reference or encoding") from None
        return {**part, "value": cache[key]}

    if isinstance(body, dict):
        for kind in ("text", "html"):
            if kind in body:
                body[kind] = resolve(body[kind])
    if isinstance(data.get("html"), dict):
        data["html"] = resolve(data["html"])
    # Slim Relay events can reach here after best-effort ingress hydration
    # failed. Do not classify a subject-only email as successfully checked.
    if not body and not data.get("text") and not data.get("html") and data.get("raw_mime"):
        from email import policy
        from email.parser import BytesParser

        message = BytesParser(policy=policy.default).parsebytes(_read(data["raw_mime"]))
        for kind, subtype in (("text", "plain"), ("html", "html")):
            part = message.get_body(preferencelist=(subtype,))
            data[kind] = part.get_content() if part else ""
    return result
