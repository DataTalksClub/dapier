"""Connector registry: one place to add an integration.

Importing this package registers every built-in connector action, the
trigger connector catalog, the logic-step metadata, and the ingress
normalizers. See ``registry.py`` for the contract; ``GET /api/catalog``
serves the manifests, the engine dispatches through
``registry.run_action``, and trigger validation shares the same specs.
"""

from . import registry  # noqa: F401
from . import (  # noqa: F401  (import = registration)
    code,
    dataops,
    dropbox,
    email,
    ingress,
    logic,
    render,
    sheets,
    slack,
    telegram,
    triggers,
    webhook,
)
from .ingress import normalize_event  # noqa: F401
from .registry import (  # noqa: F401
    Action,
    ActionError,
    Connector,
    LogicStep,
    action_specs,
    catalog,
    connector,
    logic_step,
    register,
    run_action,
    validate_action_chain,
)
