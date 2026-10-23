# SPDX-License-Identifier: AGPL-3.0-only
# Shared helpers only; dot-sourcing does not install or start anything.
function Resolve-HubDirectory {
    param([Parameter(Mandatory = $true)][string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path) -or $Path.IndexOfAny([char[]]"`r`n`0`"") -ge 0) {
        throw 'Invalid directory argument.'
    }
    return [IO.Path]::GetFullPath($Path)
}

function Get-HubPython {
    param([Parameter(Mandatory = $true)][string]$RuntimeDirectory)
    $python = Join-Path (Resolve-HubDirectory $RuntimeDirectory) '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        throw 'Package runtime is missing; run prepare-runtime.ps1 first.'
    }
    return $python
}

function ConvertTo-HubArgument {
    param([Parameter(Mandatory = $true)][string]$Value)
    if ($Value.IndexOfAny([char[]]"`r`n`0`"") -ge 0) {
        throw 'Invalid task argument.'
    }
    # These arguments are passed to PowerShell -File, never evaluated as commands.
    $escaped = [regex]::Replace($Value, '(\\+)$', '$1$1')
    return '"' + $escaped + '"'
}

function Set-HubPrivateAcl {
    param([Parameter(Mandatory = $true)][string]$Path)
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $rights = 'F'
    if (Test-Path -LiteralPath $Path -PathType Container) { $rights = '(OI)(CI)F' }
    & icacls.exe $Path '/inheritance:r' '/grant:r' ('*' + $sid + ':' + $rights) ('*S-1-5-18:' + $rights) | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Private deployment ACL setup failed.' }
}
