"""Execute deployment scripts with offline PowerShell fixtures, never live setup.

Set RESEARCH_HUB_POWERSHELL to a portable interpreter when pwsh is not on PATH.
Every script receives temporary state/runtime paths; remote and service commands
are mocked. No test installs dependencies, registers a task or runs a Tunnel.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts" / "windows"
POWERSHELL = (
    os.environ.get("RESEARCH_HUB_POWERSHELL") or shutil.which("pwsh") or shutil.which("powershell")
)
pytestmark = pytest.mark.skipif(not POWERSHELL, reason="PowerShell interpreter unavailable")


def ps_string(value):
    return "'" + str(value).replace("'", "''") + "'"


def run_ps(tmp_path, code, **extra_env):
    harness = tmp_path / "offline-test.ps1"
    harness.write_text(
        "$ErrorActionPreference = 'Stop'\n"
        "function Write-TestResult($Value) {\n"
        "  Write-Output ('HUB_TEST_RESULT=' + (ConvertTo-Json -InputObject $Value -Depth 12 -Compress))\n"
        "}\n" + code,
        encoding="utf-8-sig",
    )
    env = dict(
        os.environ,
        POWERSHELL_TELEMETRY_OPTOUT="1",
        POWERSHELL_UPDATECHECK="Off",
        DOTNET_CLI_TELEMETRY_OPTOUT="1",
        **{k: str(v) for k, v in extra_env.items()},
    )
    result = subprocess.run(
        [str(POWERSHELL), "-NoProfile", "-NonInteractive", "-File", str(harness)],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    markers = [
        line.partition("HUB_TEST_RESULT=")[2]
        for line in result.stdout.splitlines()
        if line.startswith("HUB_TEST_RESULT=")
    ]
    assert markers, result.stdout + result.stderr
    return json.loads(markers[-1])


def snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


@pytest.mark.parametrize("name", sorted(p.name for p in SCRIPTS.glob("*.ps1")))
def test_scripts_parse_with_real_powershell(tmp_path, name):
    result = run_ps(
        tmp_path,
        f"""
$tokens = $null; $errors = $null
[Management.Automation.Language.Parser]::ParseFile({ps_string(SCRIPTS / name)}, [ref]$tokens, [ref]$errors) | Out-Null
Write-TestResult @{{ errors = @($errors | ForEach-Object {{ $_.Message }}) }}
""",
    )
    assert result["errors"] == []


@pytest.mark.parametrize(
    "name", ["prepare-runtime.ps1", "prepare-tunnel.ps1", "install-service.ps1"]
)
def test_whatif_never_downloads_installs_or_writes(tmp_path, name):
    runtime = tmp_path / "runtime with spaces"
    python = runtime / ".venv" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"offline fixture; never execute")
    state = tmp_path / "uncreated state"
    before = snapshot(tmp_path)
    arguments = f"-RuntimeDirectory {ps_string(runtime)} -WhatIf"
    if name == "prepare-runtime.ps1":
        arguments += f" -RepositoryDirectory {ps_string(ROOT)}"
    elif name == "install-service.ps1":
        arguments += f" -StateDirectory {ps_string(state)}"
    result = run_ps(
        tmp_path,
        f"""
function Get-Command {{ [pscustomobject]@{{ Source = 'Forbidden-Uv' }} }}
function Get-ScheduledTask {{ $null }}
function Forbidden-Uv {{ throw 'UNEXPECTED_INSTALL' }}
function Invoke-RestMethod {{ throw 'UNEXPECTED_NETWORK' }}
function Invoke-WebRequest {{ throw 'UNEXPECTED_NETWORK' }}
function New-Item {{ throw 'UNEXPECTED_WRITE' }}
function Copy-Item {{ throw 'UNEXPECTED_WRITE' }}
function Move-Item {{ throw 'UNEXPECTED_WRITE' }}
function Register-ScheduledTask {{ throw 'UNEXPECTED_REGISTRATION' }}
function Start-ScheduledTask {{ throw 'UNEXPECTED_START' }}
& {ps_string(SCRIPTS / name)} {arguments}
Write-TestResult @{{ completed = $true }}
""",
    )
    assert result["completed"]
    after = snapshot(tmp_path)
    after.pop("offline-test.ps1")
    assert after == before
    assert not state.exists()


@pytest.mark.parametrize("exit_code", [0, 17])
def test_prepare_runtime_pins_install_and_restores_environment(tmp_path, exit_code):
    runtime = tmp_path / "runtime with spaces"
    python = runtime / ".venv" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.touch()
    result = run_ps(
        tmp_path,
        f"""
$env:UV_PROJECT_ENVIRONMENT = 'previous-environment'
$env:UV_CACHE_DIR = 'previous-cache'
$global:captured = @(); $global:environment = @{{}}; $failed = $false
function Get-Command {{ [pscustomobject]@{{ Source = 'Invoke-FixtureUv' }} }}
function New-Item {{ }}
function Invoke-FixtureUv {{
  $global:captured = @($args)
  $global:environment = @{{ environment = $env:UV_PROJECT_ENVIRONMENT; cache = $env:UV_CACHE_DIR }}
  $global:LASTEXITCODE = {exit_code}
}}
try {{ & {ps_string(SCRIPTS / "prepare-runtime.ps1")} -RuntimeDirectory {ps_string(runtime)} -RepositoryDirectory {ps_string(ROOT)} -PythonVersion '3.11' }}
catch {{ $failed = $true }}
Write-TestResult @{{ failed = $failed; arguments = @($global:captured); during = $global:environment; environment = $env:UV_PROJECT_ENVIRONMENT; cache = $env:UV_CACHE_DIR }}
""",
    )
    assert result["failed"] is (exit_code != 0)
    assert result["arguments"] == [
        "sync",
        "--project",
        str(ROOT),
        "--locked",
        "--no-editable",
        "--extra",
        "windows",
        "--python",
        "3.11",
    ]
    assert Path(result["during"]["environment"]) == runtime / ".venv"
    assert Path(result["during"]["cache"]) == runtime / "cache"
    assert result["environment"] == "previous-environment"
    assert result["cache"] == "previous-cache"


def wrapper_fixture(tmp_path, name):
    wrapper = tmp_path / name
    shutil.copyfile(SCRIPTS / name, wrapper)
    # Injection seam: the production wrapper is unchanged; helper lookup returns
    # a PS function so tests can capture argv without executing a binary.
    (tmp_path / "Common.ps1").write_text(
        "function Resolve-HubDirectory { param($Path) [IO.Path]::GetFullPath($Path) }\n"
        "function Get-HubPython { param($RuntimeDirectory) 'Invoke-FixturePython' }\n",
        encoding="utf-8-sig",
    )
    return wrapper


@pytest.mark.parametrize(
    "name,flags,expected",
    [
        ("Start-Collector.ps1", "", ["-m", "research_hub", "supervise"]),
        ("Start-ChatGPTBridge.ps1", "", ["-m", "research_hub", "tunnel", "--preflight"]),
        (
            "Start-ChatGPTBridge.ps1",
            "-PrepareProfile",
            ["-m", "research_hub", "tunnel", "--prepare"],
        ),
        (
            "Start-ChatGPTBridge.ps1",
            "-AuthorizeTunnel",
            ["-m", "research_hub", "tunnel", "--connect"],
        ),
    ],
)
@pytest.mark.parametrize("exit_code", [0, 19])
def test_wrappers_forward_only_explicit_modes_and_restore_state(
    tmp_path, name, flags, expected, exit_code
):
    wrapper = wrapper_fixture(tmp_path, name)
    state = tmp_path / "state with spaces and $characters"
    arguments = f"-StateDirectory {ps_string(state)} {flags}"
    if name == "Start-Collector.ps1":
        arguments += " -PythonExecutable Invoke-FixturePython"
    else:
        arguments += f" -RuntimeDirectory {ps_string(tmp_path / 'runtime')}"
    result = run_ps(
        tmp_path,
        f"""
$env:RESEARCH_HUB_STATE_DIR = 'previous-state'
$global:captured = @(); $global:during = ''; $failed = $false
function Test-Path {{ $true }}
function Invoke-FixturePython {{
  $global:captured = @($args); $global:during = $env:RESEARCH_HUB_STATE_DIR
  $global:LASTEXITCODE = {exit_code}
}}
try {{ & {ps_string(wrapper)} {arguments} }} catch {{ $failed = $true }}
Write-TestResult @{{ failed = $failed; arguments = @($global:captured); during = $global:during; restored = $env:RESEARCH_HUB_STATE_DIR }}
""",
    )
    assert result["arguments"] == expected
    assert result["failed"] is (exit_code != 0)
    assert result["during"] == str(state)
    assert result["restored"] == "previous-state"
    assert not state.exists()


def test_conflicting_tunnel_modes_fail_before_python_lookup(tmp_path):
    wrapper = wrapper_fixture(tmp_path, "Start-ChatGPTBridge.ps1")
    result = run_ps(
        tmp_path,
        f"""
$global:called = $false; $failed = $false
function Invoke-FixturePython {{ $global:called = $true }}
try {{ & {ps_string(wrapper)} -RuntimeDirectory {ps_string(tmp_path)} -StateDirectory {ps_string(tmp_path / "state")} -AuthorizeTunnel -PrepareProfile }}
catch {{ $failed = $true }}
Write-TestResult @{{ failed = $failed; called = $global:called }}
""",
    )
    assert result == {"failed": True, "called": False}


def test_missing_collector_python_never_starts(tmp_path):
    result = run_ps(
        tmp_path,
        f"""
$failed = $false
try {{ & {ps_string(SCRIPTS / "Start-Collector.ps1")} -PythonExecutable {ps_string(tmp_path / "missing-python.exe")} -StateDirectory {ps_string(tmp_path / "state")} }}
catch {{ $failed = $true }}
Write-TestResult @{{ failed = $failed }}
""",
    )
    assert result["failed"]
    assert not (tmp_path / "state").exists()


def test_existing_service_never_overwrites_or_starts(tmp_path):
    runtime = tmp_path / "runtime"
    python = runtime / ".venv" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.touch()
    result = run_ps(
        tmp_path,
        f"""
$failed = $false; $global:writes = 0
function Get-ScheduledTask {{ [pscustomobject]@{{ TaskName = 'already-exists' }} }}
function New-Item {{ $global:writes++; throw 'UNEXPECTED_WRITE' }}
function Register-ScheduledTask {{ $global:writes++; throw 'UNEXPECTED_REGISTER' }}
function Start-ScheduledTask {{ $global:writes++; throw 'UNEXPECTED_START' }}
try {{ & {ps_string(SCRIPTS / "install-service.ps1")} -RuntimeDirectory {ps_string(runtime)} -StateDirectory {ps_string(tmp_path / "state")} -Start }}
catch {{ $failed = $true }}
Write-TestResult @{{ failed = $failed; writes = $global:writes }}
""",
    )
    assert result == {"failed": True, "writes": 0}


@pytest.mark.skipif(os.name != "nt", reason="WindowsIdentity is Windows-only")
@pytest.mark.parametrize("start,registration_fails", [(False, False), (True, False), (True, True)])
def test_service_registers_python_directly_and_starts_only_after_success(
    tmp_path, start, registration_fails
):
    fixture_repo = tmp_path / "fixture-repository"
    script_dir = fixture_repo / "scripts" / "windows"
    script_dir.mkdir(parents=True)
    script = script_dir / "install-service.ps1"
    shutil.copyfile(SCRIPTS / script.name, script)
    # Keep the real argument/path helpers, but never change any Windows ACL.
    (script_dir / "Common.ps1").write_text(
        f". {ps_string(SCRIPTS / 'Common.ps1')}\nfunction Set-HubPrivateAcl {{ param($Path) }}\n",
        encoding="utf-8-sig",
    )
    examples = fixture_repo / "examples"
    examples.mkdir()
    (examples / "bridge.example.json").write_text('{"sco_enabled":false}')
    runtime = tmp_path / "runtime with spaces"
    python = runtime / ".venv" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.touch()
    state = tmp_path / "state with spaces"
    result = run_ps(
        tmp_path,
        f"""
$global:registration = $null; $global:starts = 0; $failed = $false
function Get-ScheduledTask {{ $null }}
function New-ScheduledTaskAction {{
  param($Execute, $Argument, $WorkingDirectory)
  @{{ executable = $Execute; arguments = $Argument; cwd = $WorkingDirectory }}
}}
function New-ScheduledTaskTrigger {{ param([switch]$AtStartup, [switch]$AtLogOn, $User) @{{ startup = [bool]$AtStartup; logon = [bool]$AtLogOn }} }}
function New-ScheduledTaskSettingsSet {{
  param([switch]$StartWhenAvailable, [switch]$AllowStartIfOnBatteries, [switch]$DontStopIfGoingOnBatteries,
        $ExecutionTimeLimit, $RestartCount, $RestartInterval, $MultipleInstances)
  @{{ multiple = $MultipleInstances }}
}}
function New-ScheduledTaskPrincipal {{ param($UserId, $LogonType, $RunLevel) @{{ level = $RunLevel }} }}
function Register-ScheduledTask {{
  param($TaskName, $Action, $Trigger, $Settings, $Principal, $Description)
  $global:registration = @{{ task = $TaskName; action = $Action; settings = $Settings; principal = $Principal }}
  {"throw 'FIXTURE_REGISTRATION_FAILED'" if registration_fails else ""}
}}
function Start-ScheduledTask {{ param($TaskName) $global:starts++ }}
function Start-Process {{ throw 'UNEXPECTED_TUNNEL' }}
function Invoke-WebRequest {{ throw 'UNEXPECTED_NETWORK' }}
try {{ & {ps_string(script)} -RuntimeDirectory {ps_string(runtime)} -StateDirectory {ps_string(state)} -TaskName 'Fixture-Collector' {"-Start" if start else ""} }}
catch {{ $failed = $true }}
Write-TestResult @{{ failed = $failed; registration = $global:registration; starts = $global:starts }}
""",
    )
    assert result["failed"] is registration_fails
    registration = result["registration"]
    assert registration["task"] == "Fixture-Collector"
    assert registration["action"] == {
        "executable": str(python),
        "arguments": '-m research_hub supervise --state-dir "' + str(state) + '"',
        "cwd": str(runtime),
    }
    assert registration["settings"]["multiple"] == "IgnoreNew"
    assert registration["principal"]["level"] == "Limited"
    assert result["starts"] == (1 if start and not registration_fails else 0)


@pytest.mark.parametrize(
    "case",
    ["valid", "wrong_digest", "unsafe_zip", "unofficial", "missing_digest", "digest_conflict"],
)
def test_tunnel_download_is_official_verified_and_never_executed(tmp_path, case):
    runtime = tmp_path / "runtime"
    archive = tmp_path / "fixture.zip"
    with zipfile.ZipFile(archive, "w") as stream:
        stream.writestr(
            "../escape.exe" if case == "unsafe_zip" else "client/tunnel-client.exe",
            b"synthetic fixture, not an executable",
        )
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    release_digest = "" if case == "missing_digest" else "sha256:" + digest
    if case == "wrong_digest":
        release_digest = "sha256:" + "0" * 64
    url = (
        "https://untrusted.invalid/client.zip"
        if case == "unofficial"
        else "https://github.com/openai/tunnel-client/releases/download/v1.2.3/client.zip"
    )
    supplied = " -ExpectedSha256 " + ps_string("f" * 64) if case == "digest_conflict" else ""
    result = run_ps(
        tmp_path,
        f"""
$global:downloads = 0; $global:started = $false; $failed = $false
function Invoke-RestMethod {{
  [pscustomobject]@{{ tag_name = 'v1.2.3'; assets = @([pscustomobject]@{{ name = 'tunnel-client-v1.2.3-windows-amd64.zip'; browser_download_url = {ps_string(url)}; digest = {ps_string(release_digest)} }}) }}
}}
function Invoke-WebRequest {{
  param($Uri, $OutFile, [switch]$UseBasicParsing, $TimeoutSec)
  $global:downloads++
  [IO.File]::Copy({ps_string(archive)}, $OutFile)
}}
function Start-Process {{ $global:started = $true; throw 'UNEXPECTED_START' }}
try {{ & {ps_string(SCRIPTS / "prepare-tunnel.ps1")} -RuntimeDirectory {ps_string(runtime)} -Version 'v1.2.3'{supplied} }}
catch {{ $failed = $true }}
Write-TestResult @{{ failed = $failed; downloads = $global:downloads; started = $global:started }}
""",
    )
    assert result["failed"] is (case != "valid")
    assert result["downloads"] == (
        0 if case in {"unofficial", "missing_digest", "digest_conflict"} else 1
    )
    assert not result["started"]
    if case == "valid":
        assert (
            runtime / "tunnel-client" / "client" / "tunnel-client.exe"
        ).read_bytes() == b"synthetic fixture, not an executable"
    else:
        assert not (runtime / "tunnel-client").exists()
    assert not list(runtime.glob("tunnel-download-*"))
    assert not (runtime / "escape.exe").exists()


def test_task_argument_quoting_does_not_change_drive_root(tmp_path):
    result = run_ps(
        tmp_path,
        f"""
. {ps_string(SCRIPTS / "Common.ps1")}
Write-TestResult @{{ argument = (ConvertTo-HubArgument {ps_string("C:" + chr(92))}) }}
""",
    )
    assert result["argument"] == '"C:' + chr(92) * 2 + '"'


@pytest.mark.skipif(os.name != "nt", reason="Windows filesystem ACLs required")
@pytest.mark.parametrize("kind", ["directory", "file"])
def test_private_acl_rejects_existing_everyone_read_without_changing_acl(tmp_path, kind):
    target = tmp_path / "empty-acl-fixture"
    if kind == "directory":
        target.mkdir()
    else:
        target.touch()
    result = run_ps(
        tmp_path,
        f"""
. {ps_string(SCRIPTS / "Common.ps1")}
$path = {ps_string(target)}
$acl = Get-Acl -LiteralPath $path
$everyone = [Security.Principal.SecurityIdentifier]::new('S-1-1-0')
$rule = [Security.AccessControl.FileSystemAccessRule]::new($everyone, 'Read', {ps_string("ContainerInherit,ObjectInherit" if kind == "directory" else "None")}, 'None', 'Allow')
$acl.AddAccessRule($rule)
Set-Acl -LiteralPath $path -AclObject $acl
$before = (Get-Acl -LiteralPath $path).Sddl
$failed = $false
try {{ Set-HubPrivateAcl $path }} catch {{ $failed = $true }}
Write-TestResult @{{ rejected = $failed; unchanged = ((Get-Acl -LiteralPath $path).Sddl -eq $before) }}
""",
    )
    assert result == {"rejected": True, "unchanged": True}


@pytest.mark.skipif(os.name != "nt", reason="Windows filesystem ACLs required")
def test_new_private_directory_has_only_expected_allow_sids(tmp_path):
    directory = tmp_path / "dedicated-private-fixture"
    directory.mkdir()
    result = run_ps(
        tmp_path,
        f"""
. {ps_string(SCRIPTS / "Common.ps1")}
$path = {ps_string(directory)}
Set-HubPrivateAcl $path
$own = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$sids = @((Get-Acl -LiteralPath $path).Access | Where-Object {{
  $_.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow
}} | ForEach-Object {{ $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value }})
Write-TestResult @{{ own = $own; sids = @($sids) }}
""",
    )
    assert {result["own"], "S-1-5-18"} <= set(result["sids"])
    assert set(result["sids"]) <= {result["own"], "S-1-5-18", "S-1-5-32-544"}


@pytest.mark.skipif(os.name != "nt", reason="Windows filesystem reparse points required")
def test_private_acl_rejects_junction_without_changing_target(tmp_path):
    target = tmp_path / "junction-target-fixture"
    target.mkdir()
    link = tmp_path / "junction-fixture"
    result = run_ps(
        tmp_path,
        f"""
. {ps_string(SCRIPTS / "Common.ps1")}
$target = {ps_string(target)}; $link = {ps_string(link)}
New-Item -ItemType Junction -Path $link -Target $target | Out-Null
$before = (Get-Acl -LiteralPath $target).Sddl
$failed = $false
try {{ Set-HubPrivateAcl $link }} catch {{ $failed = $true }}
Write-TestResult @{{ rejected = $failed; target_unchanged = ((Get-Acl -LiteralPath $target).Sddl -eq $before) }}
""",
    )
    assert result == {"rejected": True, "target_unchanged": True}
