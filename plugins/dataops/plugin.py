"""DataOps service connections. Invoice processing remains in DataOps."""
import json
import re

from src.dapier.connections.importing import register_token_provider
from src.dapier.connectors.registry import ConnectionTest, register_connection_test

IDENTITY_URL = "https://ops.dtcdev.click/api/me"


def _default_transport(method, url, *, headers=None, body=None, timeout=15):
    from src.dapier.engine.actions import base

    return base._default_transport(method, url, headers=headers, body=body, timeout=timeout)


def verify_account(token, *, transport=None):
    if not re.fullmatch(r"dops_svc_[a-f0-9]{64}", str(token or "")):
        raise ValueError("Enter a valid DataOps service credential")
    try:
        status, raw = (transport or _default_transport)("GET", IDENTITY_URL,
            headers={"authorization": f"Bearer {token}"}, body=None, timeout=15)
        data = json.loads(raw) if status == 200 else {}
        service = data.get("service") or {}
        if service.get("id") != "invoice-reader" or service.get("scopes") != ["invoices:read"]:
            raise ValueError()
    except Exception:
        raise ValueError("DataOps rejected the service credential or is unavailable") from None
    return "invoice-reader", "DataOps invoice reader"


def test_connection(connection, *, transport=None):
    from src.dapier.connections.discovery import test_connection as test
    return test(connection, transport=transport)


register_token_provider("dataops", verify_account, scopes=("invoices:read",))
register_connection_test(ConnectionTest(connector="dataops", run=test_connection))
