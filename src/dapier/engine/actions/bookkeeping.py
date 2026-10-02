"""bookkeeping_stage: file a parsed invoice into the review queue.

The pending write of the invoice pipeline. It reads the ``invoice_parse``
step's output (``entry_from`` names the step, default ``parse``) and stages
one pending bookkeeping entry — parsed fields, the source flag, the message
the invoice arrived with, and the s3 pointer to the PDF for the reviewer's
``file to Dropbox`` follow-up. A redelivered invoice dedupes into the
existing entry instead of staging a second one.
"""

from ... import bookkeeping_store


def run_bookkeeping_stage(action, event, workflow_id, steps=None):
    from_step = str(action.get("entry_from") or "parse")
    output = ((steps or {}).get(from_step) or {}).get("output") or {}
    entry = output.get("entry")
    if not isinstance(entry, dict) or not entry:
        raise ValueError(
            f"bookkeeping_stage: step '{from_step}' has no parsed entry to "
            "stage (nothing matched, or the parse step has not run)")
    data = event.get("data") or {}
    sender = data.get("sender") or {}
    addresses = sender.get("addresses") if isinstance(sender, dict) else None
    message = {
        "message_id": data.get("message_id"),
        "route": data.get("route"),
        "subject": data.get("subject"),
        "sender": (addresses or [sender.get("header")] if isinstance(sender, dict)
                   else [None])[0],
        "date": data.get("date"),
    }
    result = bookkeeping_store.create_pending(
        entry=entry,
        source=output.get("source"),
        workflow_id=workflow_id,
        message=message,
        pdf=output.get("pdf"),
    )
    return {**result, "provider": entry.get("provider"),
            "invoice_number": entry.get("invoice_number")}
