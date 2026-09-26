"""Renderer connector: render HTML to PDF (jobs report back as renderer events)."""
from ..engine.actions.render import run_render_job
from .registry import Action, register

register(Action(
    type="render_html_to_pdf",
    label="Render PDF",
    icon="file-text",
    run=lambda action, event, workflow_id, steps=None: run_render_job(action, event, workflow_id),
    required=frozenset({"input_field", "output_bucket_env", "output_key"}),
    optional=frozenset({"pdf", "id", "output_bucket"}),
    fields=(
        {"key": "input_field", "label": "Input field", "placeholder": "html"},
        {"key": "output_key", "label": "Output key", "placeholder": "rendered/{event_id}.pdf"},
        {"key": "output_bucket", "label": "Output bucket"},
        {"key": "output_bucket_env", "label": "Output bucket env", "placeholder": "RENDER_ARTIFACTS_BUCKET"},
        {"key": "page_format", "label": "PDF page format", "group": "pdf", "default": "A4"},
        {"key": "print_background", "label": "Print background", "group": "pdf", "type": "boolean", "default": "true"},
    ),
))

