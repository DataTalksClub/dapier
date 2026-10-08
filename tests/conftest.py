"""Shared test fixtures/helpers."""
from contextlib import contextmanager
from dataclasses import replace
from unittest.mock import MagicMock, patch

import pytest

from src.dapier.connections.providers import slack_tokens
from src.dapier.connectors import registry as connector_registry


@pytest.fixture(autouse=True)
def _no_live_slack(monkeypatch):
    """Slack lookups never reach slack.com from tests: the default transport
    fails, so best-effort identity lookups fall back to the token prefix.
    Tests that exercise Slack calls inject their own ``transport``."""
    def offline(*_args, **_kwargs):
        raise OSError("network disabled in tests")

    monkeypatch.setattr(slack_tokens, "_default_transport", offline)


@contextmanager
def fake_logic_time(clock=None):
    """One fake clock across the three logic_pkg modules that read it.

    The engine's clock reads moved with the logic split — ``core._elapsed``,
    ``controls``' delay sleeps, ``execution``'s step timing — so tests fake
    all three with the same mock and the ``monotonic`` call order matches
    the pre-split single-module seam. Pass ``clock`` to stand in a
    module-like clock object (e.g. test_logic's ``_Clock``); the default is
    a plain MagicMock.
    """
    fake = clock if clock is not None else MagicMock()
    with patch("src.dapier.engine.logic_pkg.controls.time", new=fake):
        with patch("src.dapier.engine.logic_pkg.core.time", new=fake):
            with patch("src.dapier.engine.logic_pkg.execution.time", new=fake):
                yield fake


@contextmanager
def stubbed_action(action_type, runner):
    """Swap one registered connector's runner for a fake inside the block.

    The engine dispatches through the connector registry, so tests stub there.
    ``runner`` keeps the historical test signature ``(action, event)``.
    """
    original = connector_registry.ACTIONS[action_type]
    entry = replace(original, run=lambda action, event, workflow_id, steps=None: runner(action, event))
    connector_registry.ACTIONS[action_type] = entry
    try:
        yield
    finally:
        connector_registry.ACTIONS[action_type] = original
