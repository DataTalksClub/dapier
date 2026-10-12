"""DataOps intake 409 is already-filed, not a failed run.

The intake is idempotent per (recipientRoute, messageId). Replaying a
rendered invoice that DataOps already accepted must complete, the same
way Dropbox skip_existing treats a path 409.
"""
import io
import urllib.error
from unittest.mock import patch

from src.dapier.engine.actions.dataops import run_dataops


def _http_error(code, reason="Conflict"):
    return urllib.error.HTTPError(
        "https://ops.example.test/api/v1/intake/email-documents",
        code, reason, hdrs=None, fp=io.BytesIO(b""),
    )


def _run(side_effect):
    action = {"type": "dataops", "auth_secret_id": "dapier/dataops",
              "connection_id": "dataops"}
    with patch("src.dapier.engine.actions.base.secrets_value",
               return_value='{"token": "tok"}'), \
         patch("src.dapier.engine.actions.dataops._target_url",
               return_value="https://ops.example.test/api/v1/intake/email-documents"), \
         patch("src.dapier.engine.actions.dataops._intake_body",
               return_value={"messageId": "already-filed"}), \
         patch("src.dapier.engine.actions.base._json_request",
               side_effect=side_effect):
        return run_dataops(action, {"id": "replay-1"})


def test_intake_409_is_already_ingested():
    result = _run(_http_error(409))
    assert result == {"already_exists": True, "status": 409}


def test_intake_500_still_fails():
    try:
        _run(_http_error(500, "Internal Server Error"))
    except urllib.error.HTTPError as exc:
        assert exc.code == 500
    else:
        raise AssertionError("expected HTTPError 500")
