"""CI runner configuration tests use throwaway APT trees only."""

import importlib.util
from pathlib import Path


spec = importlib.util.spec_from_file_location(
    "configure_ci_apt", Path(__file__).resolve().parents[1] / "scripts/configure-ci-apt.py"
)
ci_apt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci_apt)


def test_runner_mirrorlist_and_both_source_formats_preserve_trust(tmp_path):
    (tmp_path / "apt.conf.d").mkdir()
    (tmp_path / "sources.list.d").mkdir()
    fixtures = {
        "apt-mirrors.txt": "http://azure.archive.ubuntu.com/ubuntu/\tpriority:1\nhttps://security.ubuntu.com/ubuntu/\n",
        "sources.list": "deb [signed-by=/usr/share/keyrings/ubuntu-archive-keyring.gpg] http://azure.archive.ubuntu.com/ubuntu noble main universe\n",
        "sources.list.d/ubuntu.sources": "Types: deb\nURIs: https://azure.archive.ubuntu.com/ubuntu/\nSuites: noble noble-updates\nComponents: main universe\nSigned-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n",
        "sources.list.d/vendor.list": "deb https://packages.microsoft.com/repos/code stable main\n",
    }
    for name, content in fixtures.items():
        (tmp_path / name).write_text(content, encoding="utf-8")

    changed = ci_apt.configure(tmp_path)

    assert len(changed) == 3
    for name, content in fixtures.items():
        expected = content.replace("http://azure.archive.ubuntu.com", "https://archive.ubuntu.com").replace(
            "https://azure.archive.ubuntu.com", "https://archive.ubuntu.com"
        )
        assert (tmp_path / name).read_text(encoding="utf-8") == expected
    options = (tmp_path / "apt.conf.d/99-dapier-ci-network").read_text()
    assert 'Acquire::Retries "2";' in options
    assert 'Acquire::http::Timeout "30";' in options
    assert 'Acquire::https::Timeout "30";' in options
    assert "AllowInsecure" not in options
    assert ci_apt.configure(tmp_path) == []


def test_no_azure_source_leaves_existing_sources_unchanged(tmp_path):
    (tmp_path / "apt.conf.d").mkdir()
    source = "deb https://archive.ubuntu.com/ubuntu noble main\n# https://azure.archive.ubuntu.com/ubuntu-extra\n"
    (tmp_path / "sources.list").write_text(source)

    assert ci_apt.configure(tmp_path) == []
    assert (tmp_path / "sources.list").read_text() == source
