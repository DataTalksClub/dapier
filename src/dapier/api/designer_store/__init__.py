"""Designer storage: validate, publish, version, and test managed workflows.

The console and the CLI both drive the api_* operations in ``store``; YAML
rules live in ``validation`` and the optional git sync in ``github``. The
layers rebind the names their own code calls to _LateBinding proxies, so
tests patching designer_store.<name> reach every caller while the facade
below keeps the real implementations for direct use.
"""

from . import github as _github_layer, store as _store_layer
from .validation import *
from .github import *
from .store import *

# The real implementations, after the layer modules rebind their
# internally-called names to proxies.
from .github import commit_workflow, commit_delete, fetch_workflow
from .github import _real_get_token as get_token, _real__github as _github
from .store import _real__sync_youtube as _sync_youtube
from .store import _summary  # re-exported: tests reach these privates
from .validation import _validate_folder
