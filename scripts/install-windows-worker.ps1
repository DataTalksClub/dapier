param(
    [string]$TaskName = 'Dapier Browser Worker',
    [string]$TokenFile = "$env:USERPROFILE/.config/dapier/host-worker.token",
    [string]$WorkspaceRoot = "$env:USERPROFILE/dapier-ws"
)

$ErrorActionPreference = 'Stop'
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) { throw "Task already exists: $TaskName. Stop and unregister it before reinstalling." }
$repoRoot = Split-Path -Parent $PSScriptRoot
$uvPath = (Get-Command uv.exe -ErrorAction Stop).Source
$tokenPath = (Resolve-Path -LiteralPath $TokenFile -ErrorAction Stop).Path
$configRoot = Join-Path $env:USERPROFILE '.config/dapier'
New-Item -ItemType Directory -Path $configRoot -Force | Out-Null
New-Item -ItemType Directory -Path $WorkspaceRoot -Force | Out-Null
$workspacePath = (Resolve-Path -LiteralPath $WorkspaceRoot).Path

# Validate the existing token's permissions without printing its value.
& $uvPath run --project $repoRoot python -c 'from pathlib import Path; import sys; from src.dapier.private_files import require_private; require_private(Path(sys.argv[1]))' $tokenPath
if ($LASTEXITCODE -ne 0) { throw 'Worker token must be an existing private file.' }

$runnerPath = Join-Path $configRoot 'run-browser-worker.ps1'
$configPath = Join-Path $configRoot 'browser-worker.json'
@{
    uv = $uvPath
    repo = $repoRoot
    arguments = @('run', '--project', $repoRoot, 'dapier', 'worker',
        '--engine', 'codex', '--capability', 'browser', '--capability', 'chrome',
        '--token-file', $tokenPath, '--workspace-root', $workspacePath)
} | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath $configPath -Encoding UTF8

@'
$ErrorActionPreference = 'Stop'
# Put this runner and all future children in a kill-on-close Windows job.
# Task Scheduler otherwise stops only PowerShell, leaving uv/Python alive.
Add-Type -TypeDefinition @"
using System;
using System.ComponentModel;
using System.Diagnostics;
using System.Runtime.InteropServices;
public static class DapierWorkerJob {
    [StructLayout(LayoutKind.Sequential)] struct BasicLimits {
        public long ProcessTime, JobTime;
        public uint Flags;
        public UIntPtr MinWorkingSet, MaxWorkingSet;
        public uint ActiveProcesses;
        public UIntPtr Affinity;
        public uint Priority, Scheduling;
    }
    [StructLayout(LayoutKind.Sequential)] struct IoCounters {
        public ulong ReadOps, WriteOps, OtherOps, ReadBytes, WriteBytes, OtherBytes;
    }
    [StructLayout(LayoutKind.Sequential)] struct Limits {
        public BasicLimits Basic;
        public IoCounters Io;
        public UIntPtr ProcessMemory, JobMemory, PeakProcessMemory, PeakJobMemory;
    }
    [DllImport("kernel32.dll", SetLastError=true)] static extern IntPtr CreateJobObject(IntPtr security, string name);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool SetInformationJobObject(IntPtr job, int kind, ref Limits limits, uint length);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
    static IntPtr job;
    public static void Attach() {
        job = CreateJobObject(IntPtr.Zero, null);
        if (job == IntPtr.Zero) throw new Win32Exception();
        var limits = new Limits();
        limits.Basic.Flags = 0x2000; // JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if (!SetInformationJobObject(job, 9, ref limits, (uint)Marshal.SizeOf(typeof(Limits)))) throw new Win32Exception();
        if (!AssignProcessToJobObject(job, Process.GetCurrentProcess().Handle)) throw new Win32Exception();
    }
}
"@
[DapierWorkerJob]::Attach()
$env:PYTHONIOENCODING = 'utf-8'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding
$PSDefaultParameterValues['Out-File:Encoding'] = 'utf8'
$config = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'browser-worker.json') -Raw | ConvertFrom-Json
Set-Location -LiteralPath $config.repo
$workerArgs = @($config.arguments)
& $config.uv @workerArgs >> (Join-Path $PSScriptRoot 'browser-worker.log') 2>> (Join-Path $PSScriptRoot 'browser-worker.err.log')
exit $LASTEXITCODE
'@ | Set-Content -LiteralPath $runnerPath -Encoding UTF8

$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument (
    '-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $runnerPath + '"'
) -WorkingDirectory $repoRoot
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

Register-ScheduledTask -TaskName $TaskName -Action $action -Principal $principal `
    -Trigger $trigger -Settings $settings -Description 'Codex worker with browser and Chrome capabilities' | Out-Null
Start-ScheduledTask -TaskName $TaskName
Write-Output "Started scheduled task: $TaskName (user: $user)"
Write-Output "Logs: $configRoot/browser-worker.log and $configRoot/browser-worker.err.log"
