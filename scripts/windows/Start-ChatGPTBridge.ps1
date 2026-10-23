# SPDX-License-Identifier: AGPL-3.0-only
[CmdletBinding()]
param(
    [string]$RuntimeDirectory = (Join-Path $env:LOCALAPPDATA 'ResearchHub\runtime'),
    [string]$StateDirectory = (Join-Path $env:LOCALAPPDATA 'ResearchHub'),
    [switch]$AuthorizeTunnel,
    [switch]$PrepareProfile
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Common.ps1')
if ($AuthorizeTunnel -and $PrepareProfile) { throw 'Choose profile preparation or tunnel connection.' }
$python = Get-HubPython $RuntimeDirectory
$state = Resolve-HubDirectory $StateDirectory
$mode = '--preflight'
if ($PrepareProfile) { $mode = '--prepare' }
if ($AuthorizeTunnel) { $mode = '--connect' }
$previous = $env:RESEARCH_HUB_STATE_DIR
try {
    $env:RESEARCH_HUB_STATE_DIR = $state
    & $python -m research_hub tunnel $mode
    if ($LASTEXITCODE -ne 0) { throw 'Tunnel command failed; inspect its structured diagnostics.' }
}
finally { $env:RESEARCH_HUB_STATE_DIR = $previous }
