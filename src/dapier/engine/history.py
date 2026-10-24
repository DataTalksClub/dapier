"""Copies safe for run telemetry; runtime step outputs stay untouched."""
import re


SECRET_NAME = re.compile(r"token|secret|password|authorization|api_key", re.I)
REDACTED = re.compile(r"<redacted len=\d+>\Z")


def is_redacted(value):
    return isinstance(value, str) and bool(REDACTED.fullmatch(value))


def mask(value):
    return value if is_redacted(value) else f"<redacted len={len(str(value))}>"


def redact(value, secrets=()):
    """Redact credential fields and storage key/value pairs before trimming.

    Recursive copies also cover nested branch summaries and HTTP inputs.
    Idempotence lets the read path protect older history with the same rules.
    """
    if isinstance(value, dict):
        secret_key = bool(SECRET_NAME.search(str(value.get("key") or "")))
        return {
            key: mask(item) if ((isinstance(item, str) and
                                 (SECRET_NAME.search(str(key)) or key == "auth_key_value"))
                                or (key == "value" and secret_key)) else redact(item, secrets)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item, secrets) for item in value]
    if isinstance(value, str) and not is_redacted(value):
        for secret in secrets:
            value = value.replace(secret, mask(secret))
    return value


def step_output(step, output, steps=None):
    """A storage step can explicitly hide a value with an ordinary key."""
    secrets = set()
    for entry in (steps or {}).values():
        previous = entry.get("output")
        if isinstance(previous, dict) and (entry.get("secret") is True or
                SECRET_NAME.search(str(previous.get("key") or ""))):
            value = previous.get("value")
            if isinstance(value, str) and value and not is_redacted(value):
                secrets.add(value)
    safe = redact(output, sorted(secrets, key=len, reverse=True))
    if (step.get("type") == "storage_get" and step.get("secret") is True
            and isinstance(safe, dict) and "value" in safe):
        safe["value"] = mask(safe["value"])
    return safe
