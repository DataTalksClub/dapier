"""Authenticate queue delivery before normalizing inbound email.

SQS supplies SenderId as a system attribute (never a message attribute).
Direct messages must come from Dapier's intake, API replay or worker retry
roles. SNS messages must carry a valid AWS signature from a configured
inbound topic. Payload fields, TEST IDs and S3 pointers confer no trust.
"""
import base64
import os
import re
import urllib.request
from functools import lru_cache

INVALID_PROVENANCE_ERROR = "invalid provenance: untrusted inbound email publisher"


def is_inbound(payload):
    return isinstance(payload, dict) and (
        payload.get("contract") == "inbound-email" or
        (payload.get("connector") == "email" and
         payload.get("event", "message.received") == "message.received"))


@lru_cache(maxsize=8)
def _certificate(url):
    from cryptography import x509
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            raise ValueError("SNS certificate redirects are not allowed")

    opener = urllib.request.build_opener(NoRedirect)
    with opener.open(url, timeout=5) as response:
        return x509.load_pem_x509_certificate(response.read(65536)).public_key()


def _signed_notification(payload):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    topics = set(filter(None, os.environ.get("INBOUND_EMAIL_TOPICS", "").split(",")))
    topic = payload.get("TopicArn", "")
    if topic not in topics or payload.get("SignatureVersion") not in ("1", "2"):
        return False
    region = topic.split(":")[3]
    url = payload.get("SigningCertURL", "")
    # Only regional AWS SNS certificate URLs; no arbitrary fetches or redirects
    # to caller-controlled hosts (AWS SNS cert endpoints do not redirect).
    if not re.fullmatch(r"https://sns\." + re.escape(region) +
                        r"\.amazonaws\.com/SimpleNotificationService-[A-Za-z0-9_-]+\.pem", url):
        return False
    fields = ["Message", "MessageId"]
    if "Subject" in payload:
        fields.append("Subject")
    fields += ["Timestamp", "TopicArn", "Type"]
    try:
        canonical = "".join(f"{key}\n{payload[key]}\n" for key in fields)
        algorithm = hashes.SHA256() if payload["SignatureVersion"] == "2" else hashes.SHA1()
        _certificate(url).verify(base64.b64decode(payload["Signature"], validate=True),
                                 canonical.encode(), padding.PKCS1v15(), algorithm)
    except (KeyError, ValueError):
        return False
    except Exception as exc:
        from cryptography.exceptions import InvalidSignature
        if isinstance(exc, InvalidSignature):
            return False
        # Certificate/network failures retry rather than discard genuine mail.
        raise
    return True


def has_valid_provenance(payload, record):
    """Authenticate before any raw MIME reads, staging, matching or refusal."""
    if payload.get("Type") == "Notification":
        import json
        inner = json.loads(payload.get("Message", "{}"))
        if not is_inbound(inner):
            return True
        return _signed_notification(payload)
    if not is_inbound(payload):
        return True
    sender = str((record.get("attributes") or {}).get("SenderId", ""))
    role_id, separator, session = sender.partition(":")
    trusted = set(filter(None, os.environ.get("EMAIL_PUBLISHER_ROLE_IDS", "").split(",")))
    return bool(separator and session and role_id in trusted)
