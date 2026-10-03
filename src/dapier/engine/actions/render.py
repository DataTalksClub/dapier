"""render_html_to_pdf action: queue an html-renderer job."""
import json
import os


def run_render_job(action, event, workflow_id, steps=None):
    import boto3

    data = event.get("data", {})
    job_id = f"{event['id']}:{workflow_id}:{action.get('id', 'render')}"
    source = _render_source(action, event, data, steps)
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
