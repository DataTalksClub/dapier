"""CSV connector: parse staged or inline CSV into rows, and format rows
back into CSV text (see ``engine/actions/csv.py`` for the runners)."""
from ..engine.actions.csv import run_csv_format, run_csv_parse
from .registry import Action, register

register(Action(
    type="csv_parse",
    label="CSV: parse",
    icon="table",
    description=("Parse CSV text into rows for the steps that follow. "
                 "Content comes from exactly one of content (inline CSV "
                 "text) or source_s3 {bucket, key} (a staged object — an "
                 "email attachment, a render output, s3_read_object). "
                 "Output: {headers, rows, count} — rows are dicts keyed by "
                 "the header row (header_row on, the default) or plain "
                 "lists; values stay strings."),
    run=lambda action, event, workflow_id, steps=None: run_csv_parse(
        action, event, workflow_id, steps=steps),
    required=frozenset(),
    optional=frozenset({"content", "source_s3", "delimiter", "header_row"}),
    fields=(
        {"key": "content", "label": "CSV content", "type": "textarea",
         "placeholder": "name,amount\n{subject},{amount}",
         "help": "Inline CSV text; takes templates. Set this or source_s3, "
                 "never both"},
        {"key": "source_s3", "label": "Staged file (S3)",
         "placeholder": "{bucket: …, key: …}",
         "help": "A staged {bucket, key} object to parse — templates, so an "
                 "earlier step's output feeds the parse. Set this or "
                 "content, never both"},
        {"key": "delimiter", "label": "Delimiter", "placeholder": ", (default)",
         "help": "One character between columns — e.g. ; or \\t for tabs"},
        {"key": "header_row", "label": "First row is headers", "type": "boolean",
         "default": "true",
         "help": "On: rows come back as dicts keyed by the header row. "
                 "Off: rows are plain lists and headers is empty"},
    ),
))

register(Action(
    type="csv_format",
    label="CSV: format",
    icon="table",
    description=("Serialize rows into CSV text (Save this output with an "
                 "s3_upload's content, an email body, …). Rows is a "
                 "template-rendered JSON array of dicts or of arrays; "
                 "headers sets the column order (default: the first dict "
                 "row's keys, insertion order). Output: {csv, count}."),
    run=lambda action, event, workflow_id, steps=None: run_csv_format(
        action, event, workflow_id, steps=steps),
    required=frozenset({"rows"}),
    optional=frozenset({"headers", "delimiter"}),
    fields=(
        {"key": "rows", "label": "Rows", "type": "textarea", "required": True,
         "placeholder": '[{"name": "{subject}", "amount": "{amount}"}]',
         "help": "A JSON array of objects (columns follow headers or the "
                 "first row's keys) or of arrays. Cells take templates"},
        {"key": "headers", "label": "Headers", "type": "textarea",
         "placeholder": '["name", "amount"]',
         "help": "Explicit column order, written as the first row. "
                 "Defaults to the first dict row's keys; leave empty for "
                 "headerless array rows"},
        {"key": "delimiter", "label": "Delimiter", "placeholder": ", (default)",
         "help": "One character between columns — e.g. ; or \\t for tabs"},
    ),
))
