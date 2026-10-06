"""Owner-only token files across POSIX modes and Windows ACLs."""
import os
import subprocess


def _windows_acl(path, script):
    env = dict(os.environ, DAPIER_PRIVATE_PATH=os.fspath(path))
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
         "$ErrorActionPreference = 'Stop'; " + script],
        env=env, capture_output=True, text=True, check=True,
    ).stdout.strip()


def protect(path):
    if os.name != "nt":
        os.chmod(path, 0o600)
        return
    _windows_acl(path, """
        $sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
        $acl = New-Object System.Security.AccessControl.FileSecurity
        $acl.SetOwner($sid)
        $acl.SetAccessRuleProtection($true, $false)
        $rule = New-Object System.Security.AccessControl.FileSystemAccessRule($sid, 'FullControl', 'Allow')
        $acl.AddAccessRule($rule)
        Set-Acl -LiteralPath $env:DAPIER_PRIVATE_PATH -AclObject $acl
    """)


def require_private(path):
    if os.name != "nt":
        private = not (path.stat().st_mode & 0o077)
    else:
        private = _windows_acl(path, """
            $sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
            $acl = Get-Acl -LiteralPath $env:DAPIER_PRIVATE_PATH
            $allowed = @($sid, 'S-1-5-18', 'S-1-5-32-544')
            $unsafe = @($acl.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier]) |
                Where-Object { $_.AccessControlType -eq 'Allow' -and $_.IdentityReference.Value -notin $allowed })
            if ($unsafe.Count -eq 0) { 'private' } else { 'shared' }
        """) == "private"
    if not private:
        raise ValueError(f"Worker token file must be owner-only: {path}")
