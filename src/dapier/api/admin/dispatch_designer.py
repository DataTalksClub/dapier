"""The designer family of the admin dispatcher: the collection
endpoints, the per-workflow item routes, history, and the draft
lifecycle — resolved through the routes hub like every domain."""
import re
from urllib.parse import unquote

from ... import http
from . import routes  # noqa: F401 (resolved via the module ref)
from .dispatch import _read_scope

# A workflow ref: the managed file name, or a bare id — hook-backed
# workflows (webhook/telegram/... triggers) have no file and are addressed
# by id on the read/duplicate/test routes. The store gates every verb, so
# the id form still refuses writes with its own error.
_WORKFLOW_REF = r"([a-z0-9][a-z0-9._-]*\.yaml|[a-z0-9][a-z0-9_-]{0,62})"


def _route_designer_all(event, method, path, operator_payload,
                         operator_subject):
    """The four designer routers, in their original branch order."""
    for router in (_route_designer, _route_designer_item,
                   _route_designer_versions, _route_designer_draft):
        response = router(event, method, path, operator_payload,
                          operator_subject)
        if response is not None:
            return response
    return None


def _route_designer(event, method, path, operator_payload, operator_subject):
    """The designer collection endpoints: list, catalog, save, test,
bulk, export, and the copilot draft."""
    if method == "GET" and path == "/api/admin/designer/workflows":
        return routes.designer_list(event, visible=_read_scope(operator_payload))
    if method == "GET" and path == "/api/admin/designer/catalog":
        from ...connectors import registry

        return http._json_response(200, registry.catalog())
    if method == "PUT" and path == "/api/admin/designer/workflows":
        return routes.save_designer_workflow(event, operator_subject,
                                             visible=_read_scope(operator_payload))
    if method == "POST" and path == "/api/admin/designer/workflows/test":
        return routes.test_designer_workflow(event, operator_subject)
    if method == "POST" and path == "/api/admin/designer/workflows/test-step":
        return routes.test_designer_step(event, operator_subject)
    if method == "POST" and path == "/api/admin/designer/workflows/bulk":
        return routes.bulk_designer_workflow(event, operator_subject,
                                             visible=_read_scope(operator_payload))
    if method == "GET" and path == "/api/admin/designer/workflows/export-all":
        return routes.export_all_designer_workflows(event, operator_subject,
                                                    visible=_read_scope(operator_payload))
    if method == "GET" and path == "/api/admin/designer/export":
        return routes.export_designer_workflows(event, operator_subject,
                                                visible=_read_scope(operator_payload))
    if method == "POST" and path == "/api/admin/copilot/draft":
        return routes.copilot_draft(event, operator_subject)
    return None


def _route_designer_item(event, method, path, operator_payload, operator_subject):
    """Per-workflow designer endpoints: read, toggle, delete, tags,
folder, test, test-step, duplicate."""
    designer_match = re.fullmatch(r"/api/admin/designer/workflows/" + _WORKFLOW_REF, path)
    if method == "GET" and designer_match:
        return routes.designer_get(designer_match.group(1),
                                   visible=_read_scope(operator_payload))
    if method == "PUT" and designer_match:
        return routes.toggle_designer_workflow(event, operator_subject, designer_match.group(1),
                                               visible=_read_scope(operator_payload))
    if method == "DELETE" and designer_match:
        return routes.delete_designer_workflow(event, operator_subject, designer_match.group(1),
                                               visible=_read_scope(operator_payload))
    designer_tags_match = re.fullmatch(
        r"/api/admin/designer/workflows/([a-z0-9][a-z0-9._-]*\.yaml)/tags", path)
    if method == "PUT" and designer_tags_match:
        return routes.tags_designer_workflow(event, operator_subject, designer_tags_match.group(1),
                                             visible=_read_scope(operator_payload))
    designer_folder_match = re.fullmatch(
        r"/api/admin/designer/workflows/([a-z0-9][a-z0-9._-]*\.yaml)/folder", path)
    if method == "PUT" and designer_folder_match:
        return routes.folder_designer_workflow(event, operator_subject, designer_folder_match.group(1),
                                               visible=_read_scope(operator_payload))
    designer_test_match = re.fullmatch(
        r"/api/admin/designer/workflows/" + _WORKFLOW_REF + r"/test", path)
    if method == "POST" and designer_test_match:
        return routes.test_designer_workflow(event, operator_subject, designer_test_match.group(1),
                                             visible=_read_scope(operator_payload))
    return None


def _route_designer_versions(event, method, path, operator_payload, operator_subject):
    """Per-workflow history endpoints: versions, diff, rollback."""
    designer_test_step_match = re.fullmatch(
        r"/api/admin/designer/workflows/" + _WORKFLOW_REF + r"/test-step", path)
    if method == "POST" and designer_test_step_match:
        return routes.test_designer_step(event, operator_subject, designer_test_step_match.group(1),
                                         visible=_read_scope(operator_payload))
    designer_duplicate_match = re.fullmatch(
        r"/api/admin/designer/workflows/" + _WORKFLOW_REF + r"/duplicate", path)
    if method == "POST" and designer_duplicate_match:
        return routes.duplicate_designer_workflow(event, operator_subject, designer_duplicate_match.group(1))
    designer_versions_match = re.fullmatch(
        r"/api/admin/designer/workflows/([a-z0-9][a-z0-9._-]*\.yaml)/versions", path)
    if method == "GET" and designer_versions_match:
        return routes.versions_designer_workflow(designer_versions_match.group(1),
                                                 visible=_read_scope(operator_payload))
    designer_versions_diff_match = re.fullmatch(
        r"/api/admin/designer/workflows/([a-z0-9][a-z0-9._-]*\.yaml)/versions/diff", path)
    if method == "GET" and designer_versions_diff_match:
        return routes.diff_designer_workflow(event, designer_versions_diff_match.group(1),
                                             visible=_read_scope(operator_payload))
    designer_rollback_match = re.fullmatch(
        r"/api/admin/designer/workflows/([a-z0-9][a-z0-9._-]*\.yaml)/rollback", path)
    if method == "POST" and designer_rollback_match:
        return routes.rollback_designer_workflow(event, operator_subject, designer_rollback_match.group(1),
                                                 visible=_read_scope(operator_payload))
    return None


def _route_designer_draft(event, method, path, operator_payload, operator_subject):
    """The draft lifecycle endpoints (G15): publish, draft, discard,
and the draft diff."""
    # Draft vs live (G15): a save drafts; these promote or throw the draft.
    designer_publish_match = re.fullmatch(
        r"/api/admin/designer/workflows/([a-z0-9][a-z0-9._-]*\.yaml)/publish", path)
    if method == "POST" and designer_publish_match:
        return routes.publish_designer_workflow(event, operator_subject, designer_publish_match.group(1),
                                                visible=_read_scope(operator_payload))
    designer_draft_match = re.fullmatch(
        r"/api/admin/designer/workflows/([a-z0-9][a-z0-9._-]*\.yaml)/draft", path)
    if method == "GET" and designer_draft_match:
        return routes.draft_designer_workflow(designer_draft_match.group(1),
                                              visible=_read_scope(operator_payload))
    if method == "DELETE" and designer_draft_match:
        return routes.discard_designer_draft(event, operator_subject, designer_draft_match.group(1),
                                             visible=_read_scope(operator_payload))
    designer_draft_diff_match = re.fullmatch(
        r"/api/admin/designer/workflows/([a-z0-9][a-z0-9._-]*\.yaml)/draft/diff", path)
    if method == "GET" and designer_draft_diff_match:
        return routes.draft_diff_designer_workflow(designer_draft_diff_match.group(1),
                                                   visible=_read_scope(operator_payload))
    return None

