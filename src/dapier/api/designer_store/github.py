"""Optional git sync: commit a readable YAML copy of every saved
workflow to the GitHub repo the console designer publishes to."""

import base64
import json
import os
import re
import urllib.error
import urllib.request
import yaml

from .validation import (_LateBinding, WorkflowError, FILE_PATTERN,
                         filename_for, parse_workflow)

__all__ = ["BRANCH_ENV", "DEFAULT_REPO_URL", "GITHUB_API", "REPO_URL_ENV", "SyncConfigError", "SyncError", "TOKEN_SECRET_ENV", "branch", "commit_delete", "commit_workflow", "fetch_workflow", "get_token", "repo_slug", "sync_status"]


DEFAULT_REPO_URL = "https://github.com/DataTalksClub/dapier"
REPO_URL_ENV = "WORKFLOWS_REPO_URL"
BRANCH_ENV = "WORKFLOWS_GITHUB_BRANCH"
TOKEN_SECRET_ENV = "GITHUB_WORKFLOWS_TOKEN_SECRET"
GITHUB_API = "https://api.github.com"
class SyncConfigError(Exception):
    """Git sync is not configured (no token secret wired to the stack)."""


class SyncError(Exception):
    """GitHub refused or failed the commit."""


def repo_slug():
    url = os.environ.get(REPO_URL_ENV, DEFAULT_REPO_URL).rstrip("/")
    match = re.search(r"github\.com[/:]([^/]+)/([^/]+?)(?:\.git)?$", url)
    if not match:
        raise SyncConfigError(f"cannot parse owner/repo from {REPO_URL_ENV}")
    return f"{match.group(1)}/{match.group(2)}"


def branch():
    return os.environ.get(BRANCH_ENV, "main").strip() or "main"


def sync_status():
    """What the console needs to show about the save target."""
    configured = bool(os.environ.get(TOKEN_SECRET_ENV))
    return {
        "git_sync": {
            "configured": configured,
            "repo": repo_slug(),
            "branch": branch(),
        },
    }


def get_token():
    secret_id = os.environ.get(TOKEN_SECRET_ENV, "").strip()
    if not secret_id:
        raise SyncConfigError(
            "git sync is not configured: store a GitHub token (Contents: read/write) "
            "in Secrets Manager and set GithubWorkflowsTokenSecret at deploy time"
        )
    import boto3

    value = boto3.client("secretsmanager").get_secret_value(SecretId=secret_id)["SecretString"]
    token = str(value).strip()
    if not token:
        raise SyncConfigError(f"the secret {secret_id} is empty")
    return token


def _github(method, path, token, payload=None):
    """One GitHub API call; non-2xx becomes SyncError with the API's message."""
    request = urllib.request.Request(
        f"{GITHUB_API}{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={
            "accept": "application/vnd.github+json",
            "authorization": f"Bearer {token}",
            "content-type": "application/json",
            "user-agent": "dapier-designer",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise SyncError(f"github refused {method} {path}: HTTP {exc.code} {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise SyncError(f"github request failed: {exc}") from exc
    return json.loads(body) if body else {}


def _tree_entry_write(filename, yaml_text):
    return {"path": f"workflows/{filename}", "mode": "100644", "type": "blob", "content": yaml_text}


def _tree_entry_delete(filename):
    # sha: null with base_tree removes the entry.
    return {"path": f"workflows/{filename}", "mode": "100644", "type": "blob", "sha": None}


def commit_workflow(yaml_text, *, message, rename_from=None, token=None):
    """Validate and commit the workflow (create/update, optionally renaming).

    Returns the commit sha, its html_url, and the final file name. The whole
    change — write plus optional delete of rename_from — is one atomic commit.
    """
    workflow = parse_workflow(yaml_text)
    filename = filename_for(workflow["id"])
    if rename_from is not None and not FILE_PATTERN.fullmatch(rename_from):
        raise WorkflowError(f"invalid source file name: {rename_from!r}")

    token = token if token is not None else get_token()
    slug = repo_slug()
    ref = _github("GET", f"/repos/{slug}/git/ref/heads/{branch()}", token)
    base_commit = ref["object"]["sha"]
    base = _github("GET", f"/repos/{slug}/git/commits/{base_commit}", token)

    tree = [_tree_entry_write(filename, yaml_text)]
    if rename_from is not None and rename_from != filename:
        tree.append(_tree_entry_delete(rename_from))
    new_tree = _github(
        "POST", f"/repos/{slug}/git/trees", token,
        payload={"base_tree": base["tree"]["sha"], "tree": tree},
    )
    commit = _github(
        "POST", f"/repos/{slug}/git/commits", token,
        payload={"message": message, "tree": new_tree["sha"], "parents": [base_commit]},
    )
    _github("PATCH", f"/repos/{slug}/git/refs/heads/{branch()}", token, payload={"sha": commit["sha"]})
    return {
        "file": filename,
        "removed": rename_from if rename_from is not None and rename_from != filename else None,
        "commit": commit["sha"],
        "html_url": commit.get("html_url"),
    }


def commit_delete(filename, *, message, token=None):
    """Remove workflows/<filename> from the repo in one atomic commit.

    The delete is expressed as a tree entry with ``sha: null`` against the
    current base tree (the same mechanism commit_workflow uses for the
    rename-away half of a save), so the file cannot partially vanish.
    """
    if not FILE_PATTERN.fullmatch(filename):
        raise WorkflowError(f"invalid workflow file name: {filename!r}")
    token = token if token is not None else get_token()
    slug = repo_slug()
    ref = _github("GET", f"/repos/{slug}/git/ref/heads/{branch()}", token)
    base_commit = ref["object"]["sha"]
    base = _github("GET", f"/repos/{slug}/git/commits/{base_commit}", token)
    new_tree = _github(
        "POST", f"/repos/{slug}/git/trees", token,
        payload={"base_tree": base["tree"]["sha"], "tree": [_tree_entry_delete(filename)]},
    )
    commit = _github(
        "POST", f"/repos/{slug}/git/commits", token,
        payload={"message": message, "tree": new_tree["sha"], "parents": [base_commit]},
    )
    _github("PATCH", f"/repos/{slug}/git/refs/heads/{branch()}", token, payload={"sha": commit["sha"]})
    return {
        "file": filename,
        "commit": commit["sha"],
        "html_url": commit.get("html_url"),
    }


def fetch_workflow(filename, *, token=None):
    """The committed YAML for one file, straight from GitHub (no deploy wait)."""
    if not FILE_PATTERN.fullmatch(filename):
        raise WorkflowError(f"invalid workflow file name: {filename!r}")
    token = token if token is not None else get_token()
    slug = repo_slug()
    try:
        payload = _github(
            "GET",
            f"/repos/{slug}/contents/workflows/{filename}?ref={branch()}",
            token,
        )
    except SyncError as exc:
        if "HTTP 404" in str(exc):
            raise KeyError(filename) from exc
        raise
    content = payload.get("content") or ""
    if payload.get("encoding") != "base64" or not content:
        raise SyncError(f"github returned an unexpected payload for {filename}")
    return yaml.safe_load(base64.b64decode(content).decode())

# The sync helpers below call get_token/_github through the package so
# tests patching designer_store.get_token / ._github reach them; the
# facade re-binds the real implementations for unpatched callers.
_real_get_token = get_token
_real__github = _github
get_token = _LateBinding("get_token")
_github = _LateBinding("_github")
