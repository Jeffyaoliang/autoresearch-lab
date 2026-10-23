"""Runtime configuration lives outside the installed source tree."""

from __future__ import annotations

import json
import os
from pathlib import Path, PurePath

MAX_CONFIG_BYTES = 262_144


def state_dir() -> Path:
    """Return the configured state directory without creating it or reading secrets."""
    explicit = os.environ.get("RESEARCH_HUB_STATE_DIR")
    if explicit:
        return Path(explicit).expanduser().absolute()
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
        return base / "ResearchHub"
    base = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state")))
    return base / "research-hub"


def read_config(filename: str) -> dict:
    """Read a bounded JSON object by a fixed component filename; fail closed."""
    relative = PurePath(filename)
    if relative.is_absolute() or len(relative.parts) != 1 or filename in {".", ".."}:
        raise ValueError("Configuration must use a single relative filename")
    path = state_dir() / filename
    try:
        st = path.lstat()
        if path.is_symlink() or getattr(st, "st_file_attributes", 0) & 0x400:
            raise ValueError("Configuration links are not accepted")
        with path.open("rb") as stream:
            raw = stream.read(MAX_CONFIG_BYTES + 1)
        if len(raw) > MAX_CONFIG_BYTES:
            raise ValueError("Configuration is too large")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("Configuration must be a JSON object")
        return value
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        # Do not put private paths, credentials, or raw JSON in the exception message.
        raise ValueError("Configuration is missing or invalid") from None
