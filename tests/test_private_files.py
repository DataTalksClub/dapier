"""Token privacy uses ACLs on Windows and file modes on POSIX."""
import os

import pytest

from src.dapier.private_files import protect, require_private, _windows_acl


def test_protected_token_is_private(tmp_path):
    path = tmp_path / "token with spaces.txt"
    path.write_text("secret")
    protect(path)
    # Reapplying protection must not request the Windows audit privilege.
    protect(path)
    require_private(path)
    assert path.read_text() == "secret"


def test_shared_token_is_rejected(tmp_path):
    path = tmp_path / "token.txt"
    path.write_text("secret")
    protect(path)
    if os.name == "nt":
        _windows_acl(path, """
            $acl = [System.IO.File]::GetAccessControl($env:DAPIER_PRIVATE_PATH, [System.Security.AccessControl.AccessControlSections]::Access)
            $sid = New-Object System.Security.Principal.SecurityIdentifier('S-1-1-0')
            $rule = New-Object System.Security.AccessControl.FileSystemAccessRule($sid, 'Read', 'Allow')
            $acl.AddAccessRule($rule)
            [System.IO.File]::SetAccessControl($env:DAPIER_PRIVATE_PATH, $acl)
        """)
    else:
        os.chmod(path, 0o644)
    with pytest.raises(ValueError, match="owner-only"):
        require_private(path)


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell module resolution")
def test_acl_ignores_incompatible_inherited_security_module(tmp_path, monkeypatch):
    """A parent shell's module directory must not break credential privacy."""
    modules = tmp_path / "parent-shell-modules"
    security = modules / "Microsoft.PowerShell.Security"
    security.mkdir(parents=True)
    (security / "Microsoft.PowerShell.Security.psd1").write_text(
        "@{ ModuleVersion = '99.0.0'; RootModule = 'incompatible.psm1'; "
        "GUID = 'ee0c3554-2ffd-4474-b7eb-ad6cef54dce8' }"
    )
    (security / "incompatible.psm1").write_text(
        "throw 'The parent shell Security module cannot run in this process'"
    )
    monkeypatch.setenv(
        "PSModulePath", str(modules) + os.pathsep + os.environ.get("PSModulePath", "")
    )
    path = tmp_path / "token with spaces and 'quotes'.txt"
    path.write_text("synthetic credential")

    protect(path)
    require_private(path)

    assert path.read_text() == "synthetic credential"
