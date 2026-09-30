"""Run one headless harness process per Dapier host job over HTTPS."""

import json
import os
import signal
import subprocess
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_ROOT = "~/dapier-ws"
DEFAULT_TOKEN_FILE = "~/.config/dapier/host-worker.token"
HEARTBEAT_SECONDS = 40
MAX_RUNTIME_SECONDS = 3600


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


def run_job(job, api, *, workspace_root=DEFAULT_ROOT, max_runtime=MAX_RUNTIME_SECONDS,
            popen=subprocess.Popen, clock=time.monotonic, sleep=time.sleep):
    task_id, lease_id = job["task_id"], job["lease_id"]
    status, code, summary = "failed", None, ""
    output_path = error_path = None
    try:
        workspace = workspace_for(workspace_root, job.get("workspace"))
        argv = harness_argv(job.get("engine"))
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
                process.stdin.write((job.get("prompt") or "").encode())
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
                        api.call("heartbeat", {"task_id": task_id, "lease_id": lease_id})
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
              "exit_code": code, "summary": summary}
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
    workspace_for(workspace_root, "")
    api = api or WorkerApi(api_url, token_file)
    while True:
        response = api.call("claim")
        job = response.get("job")
        if job:
            run_job(job, api, workspace_root=workspace_root, max_runtime=max_runtime)
        if once:
            return
