"""CLI credential storage: owner-only modes, atomic replacement, no debris.

The write path is the piece DataOps kept when it removed user token
import (21317f4a): credential files are replaced atomically through a
0600 exclusive temp file, the directory is 0700, and a failed
replacement leaves the previous store intact with no temp files left.
"""

import json
import os
import stat

import pytest

from dapier_cli import config


@pytest.fixture
def isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("DAPIER_API_URL", raising=False)
    return tmp_path


def _modes(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def test_session_writes_are_owner_only_and_leave_no_temporary_files(isolated_home):
    directory = config.base_dir()
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o755)
    session = config.session_file()
    session.write_text('{"token": "synthetic-old"}\n')
    os.chmod(session, 0o644)

    config.save_session({"token": "synthetic-device-token", "kind": "device"})

    assert json.loads(session.read_text())["token"] == "synthetic-device-token"
    assert _modes(session) == 0o600
    assert _modes(directory) == 0o700
    assert [entry.name for entry in directory.iterdir()] == ["session.json"]
    # A separate read sees the saved session.
    assert config.load_session()["kind"] == "device"


def test_a_fresh_config_directory_is_created_owner_only(isolated_home):
    config.save_session({"token": "synthetic-first"})

    directory = config.base_dir()
    assert _modes(directory) == 0o700
    assert _modes(config.session_file()) == 0o600


def test_api_url_writes_preserve_the_session_file(isolated_home):
    config.save_session({"token": "synthetic-old"})
    config.save_api_url("https://custom.example.test/")

    assert json.loads(config.config_file().read_text()) == {
        "api_url": "https://custom.example.test"}
    assert config.load_session() == {"token": "synthetic-old"}
    assert _modes(config.config_file()) == 0o600
    assert _modes(config.base_dir()) == 0o700
    assert sorted(entry.name for entry in config.base_dir().iterdir()) == \
        ["config.json", "session.json"]


def test_a_failed_replacement_leaves_the_original_intact(isolated_home, monkeypatch):
    directory = config.base_dir()
    directory.mkdir(parents=True)
    session = config.session_file()
    original = '{"token": "synthetic-old"}\n'
    session.write_text(original)
    os.chmod(session, 0o600)

    real_replace = os.replace

    def broken_replace(source, target, **kwargs):
        if str(target) == str(session):
            raise OSError("Synthetic disk failure")
        return real_replace(source, target, **kwargs)

    monkeypatch.setattr(os, "replace", broken_replace)
    with pytest.raises(OSError, match="Synthetic disk failure"):
        config.save_session({"token": "synthetic-new"})

    assert session.read_text() == original
    assert [entry.name for entry in directory.iterdir()] == ["session.json"]
    assert _modes(session) == 0o600
