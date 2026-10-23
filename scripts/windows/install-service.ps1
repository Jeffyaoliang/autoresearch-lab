# SPDX-License-Identifier: AGPL-3.0-only
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$RuntimeDirectory = (Join-Path $env:LOCALAPPDATA 'ResearchHub\runtime'),
    [string]$StateDirectory = (Join-Path $env:LOCALAPPDATA 'ResearchHub'),
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')][string]$TaskName = 'ResearchHub-Collector',
    [switch]$Start
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Common.ps1')
$runtime = Resolve-HubDirectory $RuntimeDirectory
$state = Resolve-HubDirectory $StateDirectory
$python = Get-HubPython $runtime
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    throw 'Task already exists; inspect it instead of overwriting it.'
}
$template = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..\examples\bridge.example.json'))
if (-not (Test-Path -LiteralPath $template -PathType Leaf)) { throw 'Bridge configuration template is missing.' }
if (-not $PSCmdlet.ShouldProcess($TaskName, 'Create private collector state and register its scheduled task')) { return }
New-Item -ItemType Directory -Path $state -Force | Out-Null
Set-HubPrivateAcl $state
$configuration = Join-Path $state 'bridge.json'
if (-not (Test-Path -LiteralPath $configuration)) {
    Copy-Item -LiteralPath $template -Destination $configuration
}
$token = Join-Path $state 'http-token.txt'
if (-not (Test-Path -LiteralPath $token)) {
    $bytes = New-Object byte[] 32
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    [IO.File]::WriteAllText($token, [Convert]::ToBase64String($bytes), [Text.Encoding]::ASCII)
    $bytes = $null
}
Set-HubPrivateAcl $configuration
Set-HubPrivateAcl $token
$account = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$arguments = '-m research_hub supervise --state-dir ' + (ConvertTo-HubArgument $state)
# Invoke the installed Python directly, as the original deployment did. The task
# does not require a PowerShell execution-policy exception or a repository path.
$action = New-ScheduledTaskAction -Execute $python -Argument $arguments -WorkingDirectory $runtime
$triggers = @((New-ScheduledTaskTrigger -AtStartup), (New-ScheduledTaskTrigger -AtLogOn -User $account))
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $account -LogonType S4U -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers -Settings $settings `
    -Principal $principal -Description 'Research Hub local read-only collector; no remote jobs are managed.' | Out-Null
if ($Start) { Start-ScheduledTask -TaskName $TaskName }
[pscustomobject]@{ task = $TaskName; collector_started = [bool]$Start; tunnel_started = $false } | ConvertTo-Json
