"""Telegram message -> Slack mrkdwn rendering (the au-tomator port).

The au-tomator-telegram-bot reposted announcement-channel posts to Slack by
translating Telegram formatting entities into Slack mrkdwn, wrapping prose as
quote sections while keeping code blocks real, splitting long posts across a
thread, and closing the thread with a link to the original post. This module
is that rendering, adapted to webhook-shaped updates: entities arrive as
plain dicts (``{"offset", "length", "type", "url"}``) rather than Bot API
objects.

Telegram entity offsets are UTF-16 code units (emoji count as two), so all
slicing goes through UTF-16; Slack section text caps at 3000 characters, so
sections group into messages of the same bound. The slack action
(``telegram_format: true``) posts the first group to the channel and
continues the rest in its thread.
"""

import re
import textwrap

# Slack allows up to 3000 characters of text per section block
SECTION_TEXT_LIMIT = 3000

# Anything longer than this is continued in the thread of the first message
MESSAGE_TEXT_LIMIT = 3000

CODE_BLOCK_RE = re.compile(r"(`{3,})\n.*?\n\1", re.S)


def utf16_len(text):
    # Telegram entity offsets are in UTF-16 code units, so emoji count as 2
    return len(text.encode("utf-16-le")) // 2


def utf16_slice(text, offset, length):
    raw = text.encode("utf-16-le")
    return raw[offset * 2:(offset + length) * 2].decode("utf-16-le")


def escape_mrkdwn(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def code_fragment(text, always_fenced=False):
    ticks = "```" if always_fenced else "`"
    while ticks in text:
        ticks += "`"

    if always_fenced or "\n" in text:
        return f"{ticks}\n{text}\n{ticks}"

    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{ticks}{pad}{text}{pad}{ticks}"


def render_range(text, entities, start, end):
    """Render a slice of the message as Slack mrkdwn, escaping everything
    that Telegram did not mark up and translating its entities."""
    parts = []
    pos = start

    for entity in entities:
        if entity["offset"] < pos or entity["offset"] + entity["length"] > end:
            continue

        parts.append(escape_mrkdwn(utf16_slice(text, pos, entity["offset"] - pos)))

        fragment = utf16_slice(text, entity["offset"], entity["length"])
        escaped = escape_mrkdwn(fragment)
        stop = entity["offset"] + entity["length"]

        if entity["type"] == "text_link":
            inner = [
                other for other in entities
                if other is not entity
                and other["offset"] >= entity["offset"]
                and other["offset"] + other["length"] <= stop
            ]
            label = render_range(text, inner, entity["offset"], stop)
            parts.append(f"<{entity['url']}|{label}>")
        elif entity["type"] == "bold":
            parts.append(f"*{escaped}*")
        elif entity["type"] == "italic":
            parts.append(f"_{escaped}_")
        elif entity["type"] in ("strikethrough", "underline"):
            parts.append(f"~{escaped}~")
        elif entity["type"] == "code":
            parts.append(code_fragment(escaped))
        elif entity["type"] == "pre":
            parts.append(code_fragment(escaped, always_fenced=True))
        elif entity["type"] == "url":
            parts.append(f"<{escaped}>")
        else:
            parts.append(escaped)

        pos = stop

    parts.append(escape_mrkdwn(utf16_slice(text, pos, end - pos)))

    return "".join(parts)


def to_mrkdwn(text, entities):
    if not text:
        return ""

    entities = sorted(entities or [], key=lambda entity: entity["offset"])

    return render_range(text, entities, 0, utf16_len(text))


def split_code_blocks(mrkdwn):
    parts = []
    pos = 0
    after_code = False

    for match in CODE_BLOCK_RE.finditer(mrkdwn):
        if match.start() > pos:
            prose = mrkdwn[pos:match.start()]
            parts.append((False, prose.lstrip("\n") if after_code else prose))

        parts.append((True, match.group()))
        pos = match.end()
        after_code = True

    if pos < len(mrkdwn):
        prose = mrkdwn[pos:]
        parts.append((False, prose.lstrip("\n") if after_code else prose))

    return [part for part in parts if part[1]]


def render_sections(text, entities=None, limit=SECTION_TEXT_LIMIT):
    """Render the message into Slack sections: prose as a quote block, code
    blocks kept as real code blocks."""
    sections = []
    current = []

    def flush():
        if current:
            sections.append("\n".join(current))
            current.clear()

    def add_line(line):
        quoted = f">{line}".rstrip()
        if current and len("\n".join(current)) + 1 + len(quoted) > limit:
            flush()
        current.append(quoted)

    for is_code, chunk in split_code_blocks(to_mrkdwn(text, entities)):
        if is_code:
            flush()
            if len(chunk) > limit:
                chunk = chunk[:limit - len("\n```")] + "\n```"
            sections.append(chunk)
            continue

        for line in chunk.splitlines():
            indent = line[:len(line) - len(line.lstrip())]
            width = limit - len("> ") - len(indent)
            pieces = textwrap.wrap(
                line.strip(),
                width=width,
                subsequent_indent=indent,
                break_on_hyphens=False,
            ) or [""]
            pieces[0] = indent + pieces[0]

            for piece in pieces:
                add_line(piece)

    flush()

    return sections


def group_sections(sections, limit=MESSAGE_TEXT_LIMIT):
    """Group sections into Slack messages. Only the first one goes to the
    channel, the rest continue in the thread of the first message."""
    groups = []
    current = []
    size = 0

    for section in sections:
        if current and size + 1 + len(section) > limit:
            groups.append(current)
            current = []
            size = 0

        current.append(section)
        size += 1 + len(section)

    if current:
        groups.append(current)

    return groups


def build_messages(text, entities=None, limit=MESSAGE_TEXT_LIMIT):
    """The message content as Slack block payloads, split into as many
    messages as it takes."""
    messages = [
        [
            {"type": "section", "text": {"type": "mrkdwn", "text": section}}
            for section in group
        ]
        for group in group_sections(render_sections(text, entities), limit)
    ]

    return messages or [[]]


def link_blocks(link):
    return [{"type": "section", "text": {"type": "mrkdwn", "text": link}}]
