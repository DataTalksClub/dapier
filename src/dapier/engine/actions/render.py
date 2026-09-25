"""render_html_to_pdf action: queue an html-renderer job."""
import json
import os


def run_render_job(action, event, workflow_id):
    import boto3

    data = event.get("data", {})
    job_id = f"{event['id']}:{workflow_id}:{action.get('id', 'render')}"
    input_value = data.get(action.get("input_field", "html"))
    if not isinstance(input_value, dict):
        raise ValueError("render input must be an email body object")
    s3 = boto3.client("s3")
    input_bucket = os.environ["RENDER_ARTIFACTS_BUCKET"]
    input_key = f"inputs/{job_id.replace('/', '_')}.html"
    if "value" in input_value:
        source = input_value["value"].encode()
    elif isinstance(input_value.get("s3"), dict):
        source_ref = input_value["s3"]
        source = s3.get_object(Bucket=source_ref["bucket"], Key=source_ref["key"])["Body"].read()
    else:
        raise ValueError("render input must contain value or s3 reference")
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
