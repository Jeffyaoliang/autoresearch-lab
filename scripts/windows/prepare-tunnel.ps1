# SPDX-License-Identifier: AGPL-3.0-only
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$RuntimeDirectory = (Join-Path $env:LOCALAPPDATA 'ResearchHub\runtime'),
    [string]$Version = 'latest',
    [ValidateSet('amd64', 'arm64')][string]$Architecture = 'amd64',
    [ValidatePattern('^[a-fA-F0-9]{64}$')][string]$ExpectedSha256
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
. (Join-Path $PSScriptRoot 'Common.ps1')
$runtime = Resolve-HubDirectory $RuntimeDirectory
$destination = Join-Path $runtime 'tunnel-client'
if (Test-Path -LiteralPath $destination) {
    throw 'Tunnel installation already exists; reuse or inspect it before replacing it.'
}
if ($Version -eq 'latest') { $endpoint = 'https://api.github.com/repos/openai/tunnel-client/releases/latest' }
elseif ($Version -match '^v?[0-9][A-Za-z0-9._-]*$') {
    $endpoint = 'https://api.github.com/repos/openai/tunnel-client/releases/tags/' + $Version
}
else { throw 'Invalid release version.' }
if (-not $PSCmdlet.ShouldProcess($destination, 'Download and verify the official OpenAI tunnel-client')) { return }
$release = Invoke-RestMethod $endpoint -Headers @{'User-Agent' = 'ResearchHub-setup'} -TimeoutSec 30
$pattern = '^tunnel-client-v[^/]+-windows-' + $Architecture + '\.zip$'
$assets = @($release.assets | Where-Object { $_.name -match $pattern })
if ($assets.Count -ne 1) { throw 'Official Windows asset could not be uniquely resolved.' }
$asset = $assets[0]
if ($asset.browser_download_url -notmatch '^https://github\.com/openai/tunnel-client/releases/download/') {
    throw 'Unexpected official release download URL.'
}
$digest = [string]$asset.digest
$expected = $digest -replace '^sha256:', ''
if ($ExpectedSha256) {
    if ($expected -match '^[a-fA-F0-9]{64}$' -and $expected -ne $ExpectedSha256) {
        throw 'Release digest and supplied digest disagree.'
    }
    $expected = $ExpectedSha256
}
if ($expected -notmatch '^[a-fA-F0-9]{64}$') { throw 'A verified SHA-256 digest is required.' }
New-Item -ItemType Directory -Path $runtime -Force | Out-Null
$temporary = Join-Path $runtime ('tunnel-download-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $temporary | Out-Null
try {
    $zip = Join-Path $temporary 'client.zip'
    Invoke-WebRequest $asset.browser_download_url -OutFile $zip -UseBasicParsing -TimeoutSec 90
    if ((Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash -ne $expected) {
        throw 'Tunnel archive SHA-256 verification failed.'
    }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [IO.Compression.ZipFile]::OpenRead($zip)
    try {
        foreach ($entry in $archive.Entries) {
            $name = $entry.FullName.Replace('\', '/')
            if ($name.StartsWith('/') -or $name -match '(^|/)\.\.(/|$)' -or $name.Contains(':')) {
                throw 'Archive contains an unsafe path.'
            }
        }
    }
    finally { $archive.Dispose() }
    $unpacked = Join-Path $temporary 'unpacked'
    Expand-Archive -LiteralPath $zip -DestinationPath $unpacked
    $executables = @(Get-ChildItem -LiteralPath $unpacked -Recurse -File -Filter 'tunnel-client.exe')
    if ($executables.Count -ne 1) { throw 'Expected one tunnel-client executable.' }
    $relativeExe = $executables[0].FullName.Substring($unpacked.Length).TrimStart('\', '/')
    Move-Item -LiteralPath $unpacked -Destination $destination
    [pscustomobject]@{
        release = $release.tag_name
        sha256 = $expected.ToLowerInvariant()
        executable = (Join-Path $destination $relativeExe)
        started = $false
    } | ConvertTo-Json
}
finally { Remove-Item -LiteralPath $temporary -Recurse -Force }
