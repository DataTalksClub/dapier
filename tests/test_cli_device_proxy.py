"""Device login under the hostile proxy environment.

The assistant environments that run `dapier` set NODE_USE_ENV_PROXY=1,
HTTPS_PROXY, and NO_PROXY. urllib honors them natively; these tests prove
the whole flow -- start, poll, approval, credential save -- really travels
through an env proxy, and that NO_PROXY bypasses a dead one, using a real
local proxy and a real local API over sockets. Synthetic tokens only.

urllib builds its opener (and reads the proxy env) once per process, so
each test resets the opener the way a fresh `dapier` process would see
the environment.
"""

import json
import os
import stat
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import pytest

from dapier_cli import auth, config

SYNTHETIC_TOKEN = "dap_dev_synthetic_not_a_secret"


@pytest.fixture
def fresh_urllib_opener(monkeypatch):
    monkeypatch.setattr(urllib.request, "_opener", None, raising=False)
    yield


@pytest.fixture
def device_api():
    polls = {"count": 0}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("content-length") or 0)
            self.rfile.read(length)
            path = urlparse(self.path).path
            if path.endswith("/device/start"):
                self._reply(200, {
                    "device_code": "dapd_synthetic",
                    "user_code": "ABCD-EFGH",
                    "interval": 1,
                    "expires_in": 600,
                    "verification_uri": "/device",
                    "verification_uri_complete": "/device?code=ABCD-EFGH",
                })
            elif path.endswith("/device/token"):
                polls["count"] += 1
                if polls["count"] == 1:
                    self._reply(400, {"error": "authorization_pending"})
                else:
                    self._reply(200, {
                        "token": SYNTHETIC_TOKEN,
                        "kind": "device",
                        "subject": "synthetic-subject",
                        "email": "synthetic@example.test",
                        "expires_at": 2_000_000_000,
                    })
            else:
                self._reply(404, {"error": "not found"})

        def _reply(self, code, payload):
            data = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


@pytest.fixture
def recording_proxy():
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("content-length") or 0)
            body = self.rfile.read(length)
            target = urlparse(self.path)
            seen.append(f"{target.hostname}:{target.port}{target.path}")
            # Forward directly: this handler must not inherit the hostile
            # proxy env the test set for the code under test.
            request = urllib.request.Request(
                self.path, data=body,
                headers={"content-type": self.headers.get("content-type", "")},
                method="POST")
            direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            try:
                with direct.open(request, timeout=10) as response:
                    self._pass(response.status, response.read())
            except urllib.error.HTTPError as exc:
                self._pass(exc.code, exc.read())

        def _pass(self, code, payload):
            self.send_response(code)
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_port, seen
    server.shutdown()
    server.server_close()


def test_device_login_travels_through_the_env_proxy_and_saves_credentials(
        tmp_path, monkeypatch, fresh_urllib_opener, device_api, recording_proxy):
    proxy_port, seen = recording_proxy
    monkeypatch.setenv("NODE_USE_ENV_PROXY", "1")
    monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{proxy_port}")
    monkeypatch.setenv("HTTPS_PROXY", f"http://127.0.0.1:{proxy_port}")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    session = auth.login_device(
        device_api, poster=auth._post_json, sleeper=lambda seconds: None)

    assert session["token"] == SYNTHETIC_TOKEN
    assert any(path.endswith("/device/start") for path in seen), seen
    assert any(path.endswith("/device/token") for path in seen), seen
    saved = config.load_session()
    assert saved["token"] == SYNTHETIC_TOKEN
    assert stat.S_IMODE(os.stat(config.session_file()).st_mode) == 0o600


def test_no_proxy_bypasses_a_dead_env_proxy(
        tmp_path, monkeypatch, fresh_urllib_opener, device_api):
    monkeypatch.setenv("NODE_USE_ENV_PROXY", "1")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    session = auth.login_device(
        device_api, poster=auth._post_json, sleeper=lambda seconds: None)

    assert session["token"] == SYNTHETIC_TOKEN
    assert config.load_session()["kind"] == "device"
