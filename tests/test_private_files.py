"""Token privacy uses ACLs on Windows and file modes on POSIX."""
import os

import pytest

from src.dapier.private_files import protect, require_private, _windows_acl


def test_protected_token_is_private(tmp_path):
    path = tmp_path / "token with spaces.txt"
    path.write_text("secret")
    protect(path)
    require_private(path)
    assert path.read_text() == "secret"


def test_shared_token_is_rejected(tmp_path):
    path = tmp_path / "token.txt"
    path.write_text("secret")
    protect(path)
    if os.name == "nt":
        _windows_acl(path, """
            $acl = Get-Acl -LiteralPath $env:DAPIER_PRIVATE_PATH
            $sid = New-Object System.Security.Principal.SecurityIdentifier('S-1-1-0')
            $rule = New-Object System.Security.AccessControl.FileSystemAccessRule($sid, 'Read', 'Allow')
            $acl.AddAccessRule($rule)
            Set-Acl -LiteralPath $env:DAPIER_PRIVATE_PATH -AclObject $acl
        """)
    else:
        os.chmod(path, 0o644)
    with pytest.raises(ValueError, match="owner-only"):
        require_private(path)
