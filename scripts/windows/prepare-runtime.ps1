# SPDX-License-Identifier: AGPL-3.0-only
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$RuntimeDirectory = (Join-Path $env:LOCALAPPDATA 'ResearchHub\runtime'),
    [string]$RepositoryDirectory = ([IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))),
    [string]$UvExecutable = 'uv',
    [string]$PythonVersion = '3.12'
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Common.ps1')
$runtime = Resolve-HubDirectory $RuntimeDirectory
$repository = Resolve-HubDirectory $RepositoryDirectory
foreach ($name in @('pyproject.toml', 'uv.lock')) {
    if (-not (Test-Path -LiteralPath (Join-Path $repository $name) -PathType Leaf)) {
        throw 'Repository must contain pyproject.toml and uv.lock.'
    }
}
$uv = (Get-Command $UvExecutable -CommandType Application -ErrorAction Stop).Source
if ($PSCmdlet.ShouldProcess($runtime, 'Install the locked Research Hub package and Windows transport')) {
    New-Item -ItemType Directory -Path $runtime -Force | Out-Null
    $oldEnvironment = $env:UV_PROJECT_ENVIRONMENT
    $oldCache = $env:UV_CACHE_DIR
    try {
        $env:UV_PROJECT_ENVIRONMENT = Join-Path $runtime '.venv'
        $env:UV_CACHE_DIR = Join-Path $runtime 'cache'
        & $uv sync --project $repository --locked --no-editable --extra windows --python $PythonVersion
        if ($LASTEXITCODE -ne 0) { throw 'Locked package installation failed.' }
    }
    finally {
        $env:UV_PROJECT_ENVIRONMENT = $oldEnvironment
        $env:UV_CACHE_DIR = $oldCache
    }
    [pscustomobject]@{
        runtime = $runtime
        python = (Get-HubPython $runtime)
        collector_started = $false
        tunnel_started = $false
    } | ConvertTo-Json
}
