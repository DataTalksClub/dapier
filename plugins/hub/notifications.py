"""Completion notification configuration and SES delivery."""
from src.dapier.engine.actions.templating import render
from src.dapier.triggers.email_from import bare_address, sender_addresses


def configuration(action, event, workflow_id, *, title, steps=None):
    recipient = (render(str(action["notify_to"]), event, steps).strip()
                 if "notify_to" in action else (sender_addresses(event)[:1] or [""])[0]
                 if event.get("connector") == "email" else "")
    sender = render(str(action.get("notify_from") or "agents@dtcdev.click"), event, steps).strip().lower()
    if recipient and bare_address(recipient) != recipient.lower():
        raise ValueError("hub_session notify_to must be one email address")
    if bare_address(sender) != sender:
        raise ValueError("hub_session notify_from must be one email address")
    return {"notify_to": recipient.lower(), "notify_from": sender, "title": title,
            "source_workflow_id": workflow_id, "source_event_id": event["id"], "step_id": action["id"]}


def send_notification(metadata, result):
    import boto3
    from botocore.config import Config

    title = metadata.get("title", "Hub session").replace("\r", " ").replace("\n", " ")
    report = result.get("finish_report") or {}
    text = (f"Hub session {result['hub_session_id']} reached state {result['state']}.\n"
            f"{result['hub_link']}\n\n" +
            (report.get("text") or "Hub supplied no finish report for this admission result."))
    response = boto3.client("ses", config=Config(
        connect_timeout=2, read_timeout=3, retries={"total_max_attempts": 1})).send_email(
        Source=metadata["notify_from"], Destination={"ToAddresses": [metadata["notify_to"]]},
        Message={"Subject": {"Data": f"Hub session {result['state']}: {title}", "Charset": "UTF-8"},
                 "Body": {"Text": {"Data": text, "Charset": "UTF-8"}}})
    return response.get("MessageId")
