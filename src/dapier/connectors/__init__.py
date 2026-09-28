"""Connector registry: one place to add an integration.

Importing this package registers every built-in connector action, the
trigger connector catalog, the logic-step metadata, the connection-scoped
discovery resources and health checks, the trigger-connector sample/options
discovery, and the ingress normalizers. See ``registry.py`` for the
contract; ``GET /api/catalog`` serves the manifests, the engine dispatches
through ``registry.run_action``, and trigger validation shares the same
specs.
"""

from . import registry, trigger_discovery  # noqa: F401
from . import (  # noqa: F401  (import = registration)
    ai,
    calendar,
    code,
    csv,
    dataops,
    digest,
    dropbox,
    drive,
    email,
    ingress,
    logic,
    mailchimp,
    poll,
    render,
    rss,
    s3,
    schedule,
    sheets,
    slack,
    storage,
    subworkflow,
    telegram,
    triggers,
    webhook,
    youtube,
    zoom,
)
from .ingress import normalize_event  # noqa: F401
from .registry import (  # noqa: F401
    Action,
    ActionError,
    Connector,
    ConnectionTest,
    Discovery,
    LogicStep,
    action_specs,
    catalog,
    connector,
    connection_test_for,
    connection_tests,
    discoveries,
    discoveries_for_provider,
    logic_step,
    register,
    register_connection_test,
    register_discovery,
    run_action,
    validate_action_chain,
)
