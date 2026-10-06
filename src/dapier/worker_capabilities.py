"""Shared validation for host capabilities and agent task requirements."""
import re


def capabilities(value=None):
    if value is None or value == "":
        return []
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, (list, tuple)) or len(value) > 32:
        raise ValueError("Capabilities must be a list or comma-separated names (maximum 32)")
    result = []
    for name in value:
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name.strip()):
            raise ValueError("Capability names must use lowercase letters, numbers, underscores or hyphens")
        result.append(name.strip())
    return sorted(set(result))
