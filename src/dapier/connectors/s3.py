"""Amazon S3 connector: upload files to a bucket with stored AWS keys."""
from ..engine.actions.s3 import run_s3_upload
from .registry import Action, register

register(Action(
    type="s3_upload",
    label="Amazon S3",
    icon="s3",
    description="Upload a file to an S3 bucket (Upload File)",
    run=lambda action, event, workflow_id, steps=None: run_s3_upload(action, event, steps=steps),
    required=frozenset({"bucket", "key"}),
    optional=frozenset({
        "credential_id", "connection_id",
        "source_url", "source_connection_id", "source_s3", "content_type",
    }),
    fields=(
        {"key": "credential_id", "label": "Credential ID", "placeholder": "aws (default)"},
        {"key": "bucket", "label": "Bucket", "placeholder": "datatalks-mailchimp-backup", "required": True},
        {"key": "key", "label": "Object key", "placeholder": "mailchimp/{name}", "required": True},
        {"key": "source_url", "label": "Source URL", "placeholder": "https://www.googleapis.com/drive/v3/files/{id}?alt=media"},
        {"key": "source_connection_id", "label": "Source connection ID", "placeholder": "google — authorizes the source URL"},
        {"key": "content_type", "label": "Content type", "placeholder": "defaults to the trigger's mimeType"},
    ),
))
