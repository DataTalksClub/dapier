"""Run one headless harness process per Dapier host job over HTTPS."""

import base64
import json
import os
import signal
import socket
import subprocess
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_ROOT = "~/dapier-ws"
DEFAULT_TOKEN_FILE = "~/.config/dapier/host-worker.token"
HEARTBEAT_SECONDS = 40
MAX_RUNTIME_SECONDS = 3600
SKILL_DIRS_ENV = "DAPIER_SKILL_DIRS"
# Matches host_jobs.MAX_ATTACHMENT_CHUNK: one download call moves at most
# this many raw bytes so each response clears the API payload cap.
CHUNK_BYTES = 4_000_000


def _safe_name(name):
    name = str(name or "").replace("\\", "/").split("/")[-1].strip()
    return (name or "attachment")[:255]


def fetch_attachments(api, job, workspace, *, chunk=CHUNK_BYTES):
    """Stage the job's trigger attachments into ``workspace/attachments``.

    Returns the paths relative to the workspace. A failed or truncated
    download raises, which fails the job like any other worker error.
    """
    descriptors = job.get("attachments") or []
    if not descriptors:
        return []
    target = workspace / "attachments"
    target.mkdir(mode=0o700, exist_ok=True)
    staged = []
    for index, descriptor in enumerate(descriptors):
        name = _safe_name(descriptor.get("filename") if isinstance(descriptor, dict) else None)
        path = target / name
        stem, suffix = path.stem, path.suffix
        serial = 0
        while path.exists():
            serial += 1
            path = target / f"{stem}-{serial}{suffix}"
        with path.open("wb") as sink:
            offset = 0
            while True:
                response = api.call("attachment", {
                    "task_id": job["task_id"], "lease_id": job["lease_id"],
                    "index": index, "offset": offset, "length": chunk,
                })
                data = base64.b64decode(response.get("b64") or "")
                if not data and not response.get("done"):
                    raise RuntimeError(f"attachment {name}: empty chunk at offset {offset}")
                sink.write(data)
                offset += len(data)
                if response.get("done"):
                    break
        staged.append(str(path.relative_to(workspace)))
    return staged


def _prompt_with_attachments(prompt, staged):
    """One notice line so the agent sees files its template may not name."""
    listed = ", ".join(staged)
    return f"[Dapier] Trigger attachments saved to: {listed}\n\n{prompt}"


class WorkerApi:
    def __init__(self, api_url, token_file):
        self.api_url = api_url.rstrip("/")
        path = Path(token_file).expanduser()
        if path.stat().st_mode & 0o077:
            raise ValueError(f"Worker token file must be owner-only: {path}")
        self.token = path.read_text().strip()
        if not self.token.startswith("dap_"):
            raise ValueError("Worker token file does not contain a Dapier API token")

    def call(self, operation, body=None):
        request = Request(
            f"{self.api_url}/api/agent/host-jobs/{operation}",
            data=json.dumps(body or {}).encode(), method="POST",
            headers={"authorization": f"Bearer {self.token}",
                     "content-type": "application/json"},
        )
        try:
            with urlopen(request, timeout=25) as response:
                return json.loads(response.read().decode())
        except HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode()).get("error")
            except (ValueError, AttributeError):
                detail = None
            raise RuntimeError(f"Host API {operation}: HTTP {exc.code}: {detail or 'request failed'}") from exc
        except URLError as exc:
            raise RuntimeError(f"Host API {operation}: {exc.reason}") from exc


def workspace_for(root, requested):
    root = Path(root).expanduser().resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    requested = str(requested or "").strip()
    path = (Path(requested).expanduser() if Path(requested).is_absolute()
            else root / requested).resolve() if requested else root
    if not path.is_relative_to(root) or not path.is_dir():
        raise ValueError(f"Workspace must be an existing directory beneath {root}")
    return path


def env_skill_dirs(environ=None):
    """Colon-separated extra skill directories for worker agent sessions."""
    environ = os.environ if environ is None else environ
    return [os.path.expanduser(part.strip())
            for part in (environ.get(SKILL_DIRS_ENV) or "").split(":") if part.strip()]


def ensure_workspace_skills(workspace, extra_dirs=(), pool=None):
    """Make extra skills discoverable to agent sessions in this workspace only.

    Claude discovers skills from ~/.claude/skills — machine-global — and from
    the session start directory's .claude/skills. Jobs start in the workspace,
    so that copy is the only place to add skills without publishing them to
    every agent on the machine. With extras configured, the pool symlink is
    replaced by a real directory linking the pool plus each extra entry (the
    pool wins name clashes); links to skills that disappeared are pruned on
    the next job. Without extras the workspace is left exactly as set up.
    """
    if not extra_dirs:
        return
    sources = [source for source in [pool or os.path.expanduser("~/.claude/skills"),
                                     *extra_dirs] if os.path.isdir(source)]
    if not sources:
        return
    skills_dir = os.path.join(os.fspath(workspace), ".claude", "skills")
    if os.path.islink(skills_dir):
        os.unlink(skills_dir)
    os.makedirs(skills_dir, exist_ok=True)
    for source in sources:
        for name in sorted(os.listdir(source)):
            if name.startswith("."):
                continue
            entry = os.path.join(skills_dir, name)
            if os.path.lexists(entry):
                if os.path.islink(entry) and not os.path.exists(entry):
                    os.unlink(entry)
                else:
                    continue
            try:
                os.symlink(os.path.join(source, name), entry)
            except FileExistsError:
                pass  # a concurrent job wired the same name
    for name in os.listdir(skills_dir):
        entry = os.path.join(skills_dir, name)
        if os.path.islink(entry) and not os.path.exists(entry):
            try:
                os.unlink(entry)
            except FileNotFoundError:
                pass


def _summary(output_path, status, returncode):
    raw = output_path.read_bytes()[-100_000:]
    text = raw.decode("utf-8", "replace")
    try:
        data = json.loads(text)
        answer = str(data.get("result") or data.get("error") or "")
    except ValueError:
        answer = text
    return (answer.strip() or f"Claude exited with code {returncode} ({status}).")[:2000]


def harness_argv(engine):
    """Fixed command catalog: email content cannot select a binary or flags."""
    if engine in (None, "", "claude"):
        return ["claude", "--print", "--output-format", "json",
                "--no-session-persistence", "--dangerously-skip-permissions"]
    raise ValueError(f"Unsupported headless harness: {engine}")


def _stop(process):
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def identity(workspace_root):
    """One running `dapier worker` process: registry id plus check-in meta."""
    hostname = socket.gethostname()
    worker_id = f"{hostname}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    return worker_id, {"worker_id": worker_id, "hostname": hostname,
                       "pid": os.getpid(), "workspace_root": str(workspace_root)}


def run_job(job, api, *, workspace_root=DEFAULT_ROOT, max_runtime=MAX_RUNTIME_SECONDS,
            worker_id=None, popen=subprocess.Popen, clock=time.monotonic, sleep=time.sleep):
    task_id, lease_id = job["task_id"], job["lease_id"]
    presence = {"worker_id": worker_id} if worker_id else None
    status, code, summary = "failed", None, ""
    output_path = error_path = None
    try:
        workspace = workspace_for(workspace_root, job.get("workspace"))
        ensure_workspace_skills(workspace, env_skill_dirs())
        argv = harness_argv(job.get("engine"))
        prompt = job.get("prompt") or ""
        staged = fetch_attachments(api, job, workspace)
        if staged:
            prompt = _prompt_with_attachments(prompt, staged)
        log_dir = Path(workspace_root).expanduser().resolve() / ".dapier-runs"
        log_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        safe_name = __import__("hashlib").sha256(task_id.encode()).hexdigest()[:24]
        output_path = log_dir / f"{safe_name}.json"
        error_path = log_dir / f"{safe_name}.err"
        child_env = {key: value for key, value in os.environ.items()
                     if not key.startswith("DAPIER_WORKER_TOKEN")}
        with output_path.open("wb") as output, error_path.open("wb") as errors:
            os.chmod(output_path, 0o600)
            os.chmod(error_path, 0o600)
            process = popen(
                argv,
                cwd=workspace, env=child_env, stdin=subprocess.PIPE,
                stdout=output, stderr=errors, start_new_session=True,
            )
            try:
                process.stdin.write(prompt.encode())
                process.stdin.close()
                started = clock()
                next_heartbeat = started + HEARTBEAT_SECONDS
                while process.poll() is None:
                    current = clock()
                    if current - started >= max_runtime:
                        status = "timed_out"
                        _stop(process)
                        break
                    if current >= next_heartbeat:
                        api.call("heartbeat", {"task_id": task_id, "lease_id": lease_id,
                                               "worker": presence})
                        next_heartbeat = current + HEARTBEAT_SECONDS
                    sleep(1)
            except BaseException:
                _stop(process)
                raise
            code = process.wait()
        if status != "timed_out":
            status = "succeeded" if code == 0 else "failed"
        summary = _summary(output_path, status, code)
        if status != "succeeded" and not summary.strip():
            summary = error_path.read_text(errors="replace")[-2000:]
    except Exception as exc:
        summary = str(exc)[:2000]
    result = {"task_id": task_id, "lease_id": lease_id, "status": status,
              "exit_code": code, "summary": summary, "worker": presence}
    from .host_jobs import MAX_LOG_CHARS

    logs = {"stdout": "", "stderr": "", "truncated": False}
    for key, path in (("stdout", output_path), ("stderr", error_path)):
        if path is not None and path.exists():
            with path.open("rb") as handle:
                size = path.stat().st_size
                handle.seek(max(0, size - MAX_LOG_CHARS * 4))
                value = handle.read().decode("utf-8", errors="replace")
            logs[key] = value[-MAX_LOG_CHARS:]
            logs["truncated"] |= size > MAX_LOG_CHARS * 4 or len(value) > MAX_LOG_CHARS
    result["logs"] = logs
    api.call("finish", result)
    return result


def serve(*, api_url="https://dapier.dtcdev.click", token_file=DEFAULT_TOKEN_FILE,
          workspace_root=DEFAULT_ROOT, max_runtime=MAX_RUNTIME_SECONDS, once=False,
          api=None):
    if max_runtime < 1:
        raise ValueError("Maximum runtime must be positive")
    root = workspace_for(workspace_root, "")
    api = api or WorkerApi(api_url, token_file)
    worker_id, meta = identity(root)
    print(f"dapier worker {worker_id} on {api_url} — polling for agent tasks",
          flush=True)
    while True:
        response = api.call("claim", {"worker": meta})
        job = response.get("job")
        if job:
            run_job(job, api, workspace_root=workspace_root, max_runtime=max_runtime,
                    worker_id=worker_id)
        if once:
            return
