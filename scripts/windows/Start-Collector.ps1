# SPDX-License-Identifier: AGPL-3.0-only
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$PythonExecutable,
    [Parameter(Mandatory = $true)][string]$StateDirectory
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Common.ps1')
$state = Resolve-HubDirectory $StateDirectory
if (-not (Test-Path -LiteralPath $PythonExecutable -PathType Leaf)) { throw 'Python runtime is missing.' }
$previous = $env:RESEARCH_HUB_STATE_DIR
try {
    $env:RESEARCH_HUB_STATE_DIR = $state
    & $PythonExecutable -m research_hub supervise
    if ($LASTEXITCODE -ne 0) { throw 'Collector supervisor exited unsuccessfully.' }
}
finally { $env:RESEARCH_HUB_STATE_DIR = $previous }
