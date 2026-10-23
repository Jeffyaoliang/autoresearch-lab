import json
from pathlib import Path

import pytest

from research_hub.config import read_config, state_dir


def test_missing_config_does_not_create_state(tmp_path, monkeypatch):
    root = tmp_path / "not-created"
    monkeypatch.setenv("RESEARCH_HUB_STATE_DIR", str(root))
    assert state_dir() == root
    with pytest.raises(ValueError, match="missing or invalid"):
        read_config("bridge.json")
    assert not root.exists()


@pytest.mark.parametrize("name", ["../private.json", "/private.json", "a/b.json", ".", ".."])
def test_config_name_cannot_escape_state(name):
    with pytest.raises(ValueError):
        read_config(name)


@pytest.mark.parametrize("raw", [b"[]", b"null", b"{broken", b"x" * 262145])
def test_invalid_or_oversized_config_rejected(tmp_path, monkeypatch, raw):
    monkeypatch.setenv("RESEARCH_HUB_STATE_DIR", str(tmp_path))
    (tmp_path / "bridge.json").write_bytes(raw)
    with pytest.raises(ValueError):
        read_config("bridge.json")


def test_config_symlink_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCH_HUB_STATE_DIR", str(tmp_path))
    (tmp_path / "target.json").write_text('{"sco_enabled":false}')
    (tmp_path / "bridge.json").symlink_to(tmp_path / "target.json")
    with pytest.raises(ValueError, match="links"):
        read_config("bridge.json")


def test_config_never_reports_private_contents(tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCH_HUB_STATE_DIR", str(tmp_path))
    (tmp_path / "bridge.json").write_text('{"token":"synthetic-sensitive-value", broken')
    with pytest.raises(ValueError) as error:
        read_config("bridge.json")
    assert "synthetic-sensitive-value" not in str(error.value)
    assert str(tmp_path) not in str(error.value)


def test_config_object_is_returned(tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCH_HUB_STATE_DIR", str(tmp_path))
    (tmp_path / "bridge.json").write_text(json.dumps({"sco_enabled": False}))
    assert read_config("bridge.json") == {"sco_enabled": False}
