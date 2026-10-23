"""CLI routing checks; no collector or tunnel client is started."""

import os

import pytest

from research_hub import cli
from research_hub.deployment import supervisor, tunnel


def test_supervisor_task_argument_selects_child_state(tmp_path, monkeypatch):
    selected = []
    monkeypatch.setenv("RESEARCH_HUB_STATE_DIR", str(tmp_path / "other"))

    def check_state():
        selected.append(os.environ["RESEARCH_HUB_STATE_DIR"])
        return 7

    monkeypatch.setattr(supervisor, "main", check_state)
    state = tmp_path / "private state"
    assert cli.main(["supervise", "--state-dir", str(state)]) == 7
    assert selected == [str(state)]
    assert not state.exists()


@pytest.mark.parametrize("mode", [None, "--preflight", "--prepare", "--connect"])
def test_tunnel_mode_and_exit_code_reach_operator_entry(monkeypatch, mode):
    calls = []

    def entry(options):
        calls.append(options)
        return 130

    monkeypatch.setattr(tunnel, "main", entry)
    assert cli.main(["tunnel", *([mode] if mode else [])]) == 130
    assert calls == [([mode] if mode else [])]
