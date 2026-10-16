"""Distinguish exact duplicate previews from conflicting source content."""
import io
import urllib.error
from unittest.mock import patch
import pytest

from src.dapier.engine.actions.dataops import run_dataops


def _http_error(code, reason="Conflict"):
    return urllib.error.HTTPError(
        "https://ops.example.test/api/v1/intake/email-documents",
        code, reason, hdrs=None, fp=io.BytesIO(b""),
    )


def _run(outcome):
    action = {"type": "dataops", "auth_secret_id": "dapier/dataops",
              "connection_id": "dataops"}
    with patch("src.dapier.engine.actions.base.secrets_value",
               return_value='{"token": "tok"}'), \
         patch("src.dapier.engine.actions.dataops._target_url",
               return_value="https://ops.example.test/api/v1/intake/email-documents"), \
         patch("src.dapier.engine.actions.dataops._intake_body",
               return_value={"messageId": "already-filed"}), \
         patch("src.dapier.engine.actions.base._json_request",
               side_effect=outcome if isinstance(outcome, Exception) else None,
               return_value=outcome):
        return run_dataops(action, {"id": "replay-1"})


def test_changed_source_409_is_a_failure():
    with pytest.raises(ValueError, match="DataOps source conflict"):
        _run(_http_error(409))


def test_exact_duplicate_retains_company_preview():
    response = {"status": "duplicate", "invoices": [{"counterparty": "Example Ltd"}]}
    assert _run(response) == response


def test_intake_500_still_fails():
    try:
        _run(_http_error(500, "Internal Server Error"))
    except urllib.error.HTTPError as exc:
        assert exc.code == 500
    else:
        raise AssertionError("expected HTTPError 500")
