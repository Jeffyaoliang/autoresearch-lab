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
    # Quote native-process arguments without evaluating a command expression.
    $escaped = [regex]::Replace($Value, '(\\+)$', '$1$1')
    return '"' + $escaped + '"'
}

function Set-HubPrivateAcl {
    param([Parameter(Mandatory = $true)][string]$Path)
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw 'Private deployment ACL target must not be a reparse point.'
    }
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $trustedOwners = @($sid, 'S-1-5-18', 'S-1-5-32-544')
    $acl = Get-Acl -LiteralPath $Path -ErrorAction Stop
    try { $owner = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value }
    catch { throw 'Existing ACL owner requires operator review.' }
    if ($owner -notin $trustedOwners) {
        throw 'Existing ACL owner requires operator review.'
    }
    # OWNER RIGHTS refers to this object's owner, whose identity was verified
    # above. Preserve that legitimate ACE without trusting an arbitrary owner.
    $allowed = $trustedOwners + @('S-1-3-4')
    foreach ($rule in $acl.Access) {
        if (-not $rule.IsInherited -and $rule.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow) {
            try { $principal = $rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value }
            catch { throw 'Existing explicit ACL principal requires operator review.' }
            if ($principal -notin $allowed) {
                # Do not remove another principal's explicit access on an existing
                # operator-selected directory or file. Require a dedicated scope.
                throw 'Existing explicit ACL access requires operator review.'
            }
        }
    }
    $rights = 'F'
    if ($item.PSIsContainer) { $rights = '(OI)(CI)F' }
    & icacls.exe $Path '/inheritance:r' '/grant:r' ('*' + $sid + ':' + $rights) ('*S-1-5-18:' + $rights) | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Private deployment ACL setup failed.' }
}
