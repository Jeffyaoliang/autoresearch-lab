"""Failure boundaries for the standard SSH route, independent of an SSH daemon."""

import base64
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

import pytest

from research_hub.deployment import ssh as setup
from research_hub.guidance import transport
from research_hub.transports import ssh


def config():
    return dict(
        transport="ssh",
        ssh_host="gpu-lab",
        expected_remote_user="researcher",
        session_alias="advisory",
        remote_python="/srv/hub/runtime/bin/python",
        remote_guidance_dir="/srv/hub/guidance",
        remote_state_dir="/srv/hub/state",
    )


@pytest.mark.parametrize("bad", ["-oProxyCommand=bad", "a;b", "user@host", "a\nb", "", "a b"])
def test_host_cannot_inject_options(bad):
    with pytest.raises(ValueError):
        ssh.validate_config({**config(), "ssh_host": bad})


@pytest.mark.parametrize("bad", ["relative", "/srv/../etc", "/a\nb", "/a//b", "/a/./b"])
def test_remote_paths_are_absolute_canonical(bad):
    with pytest.raises(ValueError):
        ssh.validate_config({**config(), "remote_python": bad})


def test_ssh_is_noninteractive_and_strict():
    args = ssh.argv(config(), "trusted-command")
    assert "BatchMode=yes" in args and "StrictHostKeyChecking=yes" in args
    assert "ForwardAgent=no" in args and "PermitLocalCommand=no" in args
    assert args[-2:] == ["gpu-lab", "trusted-command"]


def test_only_one_bounded_receipt():
    token = b"RAH_DATA_" + base64.b64encode(b'{"ok":true}') + b"_RAH_END"
    assert ssh.decode_receipt(b"banner\n" + token) == {"ok": True}
    for raw in [b"private output", token + token, b"RAH_DATA_e30_RAH_END"]:
        with pytest.raises(ValueError):
            ssh.decode_receipt(raw)


@pytest.mark.skipif(os.name == "nt", reason="executable fixture uses a POSIX shebang")
@pytest.mark.parametrize(
    "source,reason",
    [
        ("import time;time.sleep(5)", "ssh_timeout"),
        ("import sys;sys.stdout.write('x'*400000)", "ssh_output_too_large"),
        (
            "import sys;sys.stderr.write('Host key verification failed');sys.exit(255)",
            "ssh_host_key_verification_failed",
        ),
        (
            "import sys;sys.stderr.write('Permission denied private-detail');sys.exit(255)",
            "ssh_authentication_failed",
        ),
    ],
)
def test_real_subprocess_failure_is_bounded_and_redacted(tmp_path, source, reason):
    executable = tmp_path / "ssh-fixture"
    executable.write_text(f"#!{sys.executable}\n{source}\n")
    executable.chmod(0o700)
    with pytest.raises(ValueError, match="^" + reason + "$"):
        ssh.exchange(
            {**config(), "ssh_executable": str(executable)},
            "ignored",
            timeout=0.15 if reason == "ssh_timeout" else 3,
        )


def test_ssh_dispatch_does_not_import_winpty():
    with patch(
        "research_hub.transports.ssh.run_remote", return_value={"metadata": {"ok": True}}
    ) as call:
        assert transport.run_remote("fixed", config())["metadata"]["ok"]
        call.assert_called_once_with("fixed", config())


def test_unknown_transport_does_not_fall_back_to_sco():
    with pytest.raises(ValueError, match="unsupported_remote_transport"):
        transport.run_remote("fixed", {"transport": "unknown"})


def test_ssh_does_not_claim_platform_tasks(tmp_path, monkeypatch):
    from research_hub.mcp_server import read_status

    monkeypatch.setenv("RESEARCH_HUB_STATE_DIR", str(tmp_path))
    (tmp_path / "remote.json").write_text(json.dumps(config()))
    result = read_status()
    assert not result.available and not result.tasks
    assert result.reason == "platform_task_listing_not_supported"


def test_deployment_conflict_has_no_remote_side_effect(tmp_path, monkeypatch):
    import zipfile

    monkeypatch.setenv("RESEARCH_HUB_STATE_DIR", str(tmp_path))
    (tmp_path / "remote.json").write_text('{"transport":"sco"}')
    wheel = tmp_path / "autoresearch_hub-0.1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as z:
        z.writestr("research_hub/guidance/endpoint.py", "")
    data = {
        **config(),
        "remote_root": "/srv/hub",
        "binding": {
            "session_alias": "advisory",
            "session_id": "existing-thread",
            "cwd": "/srv/project",
            "codex_home": "/srv/codex",
        },
    }
    with (
        patch.object(setup, "invoke") as invoke,
        pytest.raises(ValueError, match="local_remote_config_conflict"),
    ):
        setup.deploy(data, wheel)
    invoke.assert_not_called()


def test_cli_routes_ssh_to_operator_setup():
    from research_hub.cli import main

    with patch("research_hub.deployment.ssh.main", return_value=7) as call:
        assert main(["ssh", "doctor", "--config", "/private/setup.json"]) == 7
    call.assert_called_once_with(["doctor", "--config", "/private/setup.json"])


def test_cli_failure_does_not_print_private_path(tmp_path, capsys):
    assert setup.main(["doctor", "--config", str(tmp_path / "private-credential-name")]) == 1
    error = capsys.readouterr().err
    assert "private-credential-name" not in error
    assert json.loads(error)["reason"] == "ssh_setup_or_check_failed"


@pytest.mark.skipif(os.name == "nt", reason="POSIX remote installer")
def test_remote_install_refuses_unowned_existing_directory(tmp_path):
    import pwd
    from research_hub.deployment.ssh_remote import deploy

    user = pwd.getpwuid(os.getuid()).pw_name
    with pytest.raises(ValueError, match="deployment_root_already_exists"):
        deploy(
            {
                "expected_remote_user": user,
                "remote_root": str(tmp_path.resolve()),
                "wheel": base64.b64encode(b"wheel").decode(),
                "wheel_sha256": __import__("hashlib").sha256(b"wheel").hexdigest(),
                "binding": {},
                "progress": None,
            }
        )
    assert list(tmp_path.iterdir()) == []


def test_ssh_config_rejects_credentials_and_missing_fields():
    for data in [
        {**config(), "password": "must-never-be-saved"},
        {**config(), "remote_python": None},
        {**config(), "session_alias": "wrong/alias"},
    ]:
        with pytest.raises(ValueError):
            ssh.validate_config(data)
