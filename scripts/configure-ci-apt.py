"""Route CI downloads around the observed slow Azure Ubuntu mirror.

Only the ephemeral GitHub runner's APT configuration is changed. Repository
signing keys, suites, components and third-party sources stay intact.
"""

from pathlib import Path
import re


AZURE_ARCHIVE = re.compile(
    r"https?://azure\.archive\.ubuntu\.com/ubuntu(?=/|\s|$)"
)
NETWORK_OPTIONS = '''Acquire::Retries "2";
Acquire::http::Timeout "30";
Acquire::https::Timeout "30";
'''


def configure(apt_dir: Path) -> list[Path]:
    sources = [apt_dir / "apt-mirrors.txt", apt_dir / "sources.list"]
    sources += sorted((apt_dir / "sources.list.d").glob("*.list"))
    sources += sorted((apt_dir / "sources.list.d").glob("*.sources"))
    changed = []
    for path in sources:
        if not path.is_file():
            continue
        original = path.read_text(encoding="utf-8")
        updated = AZURE_ARCHIVE.sub("https://archive.ubuntu.com/ubuntu", original)
        if updated != original:
            path.write_text(updated, encoding="utf-8")
            changed.append(path)
    # APT applies these to connection/inactivity failures, not a minimum
    # transfer rate. The workflow also bounds each installation phase.
    (apt_dir / "apt.conf.d" / "99-dapier-ci-network").write_text(
        NETWORK_OPTIONS, encoding="utf-8"
    )
    return changed


if __name__ == "__main__":
    for source in configure(Path("/etc/apt")):
        print(f"Using official Ubuntu archive in {source}")
    print("APT connection/data timeout: 30 seconds; retries: 2")
