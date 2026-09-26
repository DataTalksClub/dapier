"""Shared test fixtures/helpers."""
from contextlib import contextmanager
from dataclasses import replace

from src.dapier.connectors import registry as connector_registry


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
