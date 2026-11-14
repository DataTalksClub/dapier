import pytest


@pytest.fixture(autouse=True)
def _no_hub_backoff(monkeypatch):
    """Default to a single attempt so replay tests see the raw failure.

    Retry/backoff tests pass ``delays`` and ``sleep`` explicitly.
    """
    monkeypatch.setattr("plugins.hub.session.RETRY_DELAYS", ())
