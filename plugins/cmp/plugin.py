"""CMP credential orchestration; course management stays in CMP."""
import json
import re
import urllib.request
from src.dapier.connections.importing import register_token_provider
from src.dapier.connectors.registry import ConnectionTest, register_connection_test

API_URL = "https://courses.datatalks.club/api/courses/"
ACCOUNT_ID = "courses.datatalks.club"
ACCOUNT_TITLE = "Course Management Platform production"

def _transport(method, url, *, headers, body, timeout):
    request = urllib.request.Request(url, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, response.read()

def verify_account(token, *, transport=None):
    # CMP uses Django's Token authentication, not Bearer authentication.
    if not re.fullmatch(r"[a-fA-F0-9]{40}", str(token or "")):
        raise ValueError("Enter a valid CMP API token")
    try:
        status, raw = (transport or _transport)("GET", API_URL,
            headers={"authorization": f"Token {token}"}, body=None, timeout=15)
        data = json.loads(raw) if status == 200 else {}
        if not isinstance(data, dict) or not isinstance(data.get("courses"), list):
            raise ValueError()
    except Exception:
        raise ValueError("CMP rejected the API token or is unavailable") from None
    # CMP exposes no user identity route. This binds platform access only.
    return ACCOUNT_ID, ACCOUNT_TITLE

def test_connection(connection, *, transport=None):
    from src.dapier.connections.discovery import test_connection as test
    return test(connection, transport=transport)

register_token_provider("cmp", verify_account)
register_connection_test(ConnectionTest(connector="cmp", run=test_connection))
