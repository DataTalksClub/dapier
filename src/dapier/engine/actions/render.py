"""render_html_to_pdf action: queue an html-renderer job."""
import json
import os
import re
from html import escape
from html.parser import HTMLParser


def _font_fallback(css):
    def fallback(match):
        if match.group(1) is None:  # @font-face declares a font, not a stack.
            return match.group(0)
        fonts = match.group(1).strip()
        if re.search(r"(?:^|,)\s*(?:serif|sans-serif|monospace|system-ui|cursive|fantasy)\s*(?:,|$)", fonts, re.I):
            return match.group(0)
        important = re.search(r"\s*!important\s*$", fonts, re.I)
        if important:
            fonts = fonts[:important.start()]
        return 'font-family:' + fonts + ',Arial,Helvetica,sans-serif' + (' !important' if important else '')
    return re.sub(r"@font-face\s*\{[^}]*\}|font-family\s*:\s*([^;}]+)",
                  fallback, css, flags=re.I)


class _OfflineImages(HTMLParser):
    """The renderer cannot fetch remote/CID images; keep their labels instead.

    Preserve the rest of the document verbatim, including email inline styles.
    In particular, don't leave a broken image with a width but no height: its
    browser placeholder can overlap the receipt below it.
    """

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.parts = []
        self.in_style = False

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "img":
            if not str(values.get("src") or "").lower().startswith("data:image/"):
                label = values.get("alt") or ""
                self.parts.append(escape(label))
                return
        raw = self.get_starttag_text()
        style = values.get("style")
        if style and _font_fallback(style) != style:
            raw = re.sub(r'''\bstyle\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)''',
                         lambda _: 'style="' + escape(_font_fallback(style), quote=True) + '"',
                         raw, count=1, flags=re.I)
        self.parts.append(raw)
        if tag == "style":
            self.in_style = True

    handle_startendtag = handle_starttag

    def handle_endtag(self, tag):
        if tag == "style":
            self.in_style = False
        self.parts.append(f"</{tag}>")

    def handle_data(self, data):
        self.parts.append(_font_fallback(data) if self.in_style else data)

    def handle_entityref(self, name):
        self.parts.append(f"&{name};")

    def handle_charref(self, name):
        self.parts.append(f"&#{name};")

    def handle_comment(self, data):
        self.parts.append(f"<!--{data}-->")

    def handle_decl(self, decl):
        self.parts.append(f"<!{decl}>")


def _offline_document(source):
    """Supply readable defaults without overriding the sender's own CSS."""
    parser = _OfflineImages()
    parser.feed(source.decode("utf-8"))
    parser.close()
    html = "".join(parser.parts)
    defaults = ('<meta charset="utf-8"><style>'
                'body{font-family:Arial,Helvetica,sans-serif;font-size:14px;'
                'line-height:1.5;color:#222}img{max-width:100%}'
                'pre{white-space:pre-wrap;overflow-wrap:anywhere}'
                '</style>')
    # Insert before the sender's styles so those win at equal specificity.
    head = re.search(r"<head\b[^>]*>", html, re.I)
    if head:
        html = html[:head.end()] + defaults + html[head.end():]
    else:
        root = re.search(r"<html\b[^>]*>", html, re.I)
        position = root.end() if root else 0
        html = html[:position] + '<head>' + defaults + '</head>' + html[position:]
    return html.encode("utf-8")


def run_render_job(action, event, workflow_id, steps=None):
    import boto3

    data = event.get("data", {})
    job_id = f"{event['id']}:{workflow_id}:{action.get('id', 'render')}"
    source = _offline_document(_render_source(action, event, data, steps))
    s3 = boto3.client("s3")
    input_bucket = os.environ["RENDER_ARTIFACTS_BUCKET"]
    input_key = f"inputs/{job_id.replace('/', '_')}.html"
    s3.put_object(Bucket=input_bucket, Key=input_key, Body=source, ContentType="text/html", ServerSideEncryption="AES256")
    input_ref = {"bucket": input_bucket, "key": input_key}
    key = action.get("output_key", "rendered/{event_id}.pdf").format(event_id=event["id"].replace("/", "_"))
    output_bucket = action.get("output_bucket") or os.environ[action.get("output_bucket_env", "RENDER_ARTIFACTS_BUCKET")]
    job = {
        "schema": "html-renderer.job.v1",
        "job_id": job_id,
        "input": input_ref,
        "output": {"bucket": output_bucket, "key": key},
        "format": "pdf",
        "pdf": action.get("pdf", {"page_format": "A4", "print_background": True}),
        "context": {"source_event": event},
    }
    boto3.client("sqs").send_message(QueueUrl=os.environ["RENDER_QUEUE_URL"], MessageBody=json.dumps(job))
    return {"job_id": job_id, "output": {"bucket": output_bucket, "key": key}}


def _render_source(action, event, data, steps):
    """The HTML bytes to render: ``input_value`` (a template rendered
    against the event and the steps run so far — how a code step hands the
    renderer its transformed body) wins over ``input_field`` (an event data
    field holding an email body object: ``{"value": ...}`` inline or an
    ``{"s3": {...}}`` pointer)."""
    template = action.get("input_value")
    if isinstance(template, str) and template.strip():
        from .templating import render

        html = render(template, event, steps)
        if not html.strip():
            raise ValueError("render input_value rendered empty")
        return html.encode()
    body = data.get(action.get("input_field", "html"))
    if not isinstance(body, dict):
        raise ValueError("render input must be an email body object")
    if "value" in body:
        return body["value"].encode()
    if isinstance(body.get("s3"), dict):
        import boto3

        s3 = boto3.client("s3")
        return s3.get_object(Bucket=body["s3"]["bucket"], Key=body["s3"]["key"])["Body"].read()
    raise ValueError("render input must contain value or s3 reference")
