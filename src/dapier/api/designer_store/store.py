"""The designer operations: list, get, save, publish, rollback, tags,
folders, bulk toggles, versions and diffs, duplicates, deletion, and
test runs — the one surface both the console and the CLI drive.

One module per domain: listing and export (reads), drafts (save/publish),
lifecycle (toggle/auto-pause/delete), organize (tags/folders/bulk),
history (versions/diff/rollback), duprun (duplicate/test runs). The
git-sync seams each domain calls are _LateBinding proxies resolved
through the package at call time — tests patching designer_store.<name>
reach every domain; the facade re-binds the real implementations after
the star imports below.
"""

from .drafts import *  # noqa: F401
from .duprun import *  # noqa: F401
from .export import *  # noqa: F401
from .history import *  # noqa: F401
from .lifecycle import *  # noqa: F401
from .listing import *  # noqa: F401
from .organize import *  # noqa: F401

# The privates the facade (and tests) reach through this module.
from .drafts import _draft_view  # noqa: F401
from .lifecycle import _real__sync_youtube  # noqa: F401
from .listing import _read_denied, _summary  # noqa: F401

__all__ = ["MAX_BULK_IDS", "MAX_DIFF_CHARS", "MAX_EXPORT_WORKFLOWS", "RUN_STATE_KEYS", "STALE_DRAFT", "WORKFLOW_KEY_ORDER", "api_auto_pause", "api_bulk", "api_delete", "api_diff", "api_discard", "api_draft", "api_draft_diff", "api_duplicate", "api_export", "api_export_all", "api_folder", "api_get", "api_list", "api_publish", "api_rollback", "api_save", "api_tags", "api_test_run", "api_test_step", "api_toggle", "api_versions", "save_gate_ids", "workflow_yaml_text"]
