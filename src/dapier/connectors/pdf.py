"""Document connector: one PDF out of a code step's attachment plan."""
from ..engine.actions.pdf import run_merge_pdfs
from .registry import Action, register

register(Action(
    type="merge_pdfs",
    label="Merge PDFs",
    icon="copy",
    description=("Turn a code step's document plan into exactly one PDF: a single "
                 "attachment passes through untouched (the output references the "
                 "stored object), several merge page-wise in plan order under "
                 "output_key. The plan is the plan_step result's document_plan: "
                 "{mode, attachments: [{filename, s3: {bucket, key}, ...}]}."),
    run=lambda action, event, workflow_id, steps=None:
        run_merge_pdfs(action, event, workflow_id, steps=steps),
    required=frozenset({"plan_step", "output_key"}),
    optional=frozenset({"output_bucket", "output_bucket_env"}),
    fields=(
        {"key": "plan_step", "label": "Plan step", "placeholder": "triage", "required": True,
         "help": "Code step whose result.document_plan names the attachments "
                 "({mode, attachments: [{filename, s3}]})."},
        {"key": "output_key", "label": "Output key", "placeholder": "merged/{event_id}.pdf"},
        {"key": "output_bucket", "label": "Output bucket",
         "discover": {"resource": "s3.buckets"}},
        {"key": "output_bucket_env", "label": "Output bucket env",
         "placeholder": "RENDER_ARTIFACTS_BUCKET"},
    ),
))
