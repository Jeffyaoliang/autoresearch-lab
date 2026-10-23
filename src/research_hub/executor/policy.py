"""Explicit executor binding and fixed task policy; importing never connects."""

from dataclasses import dataclass
import os
from pathlib import Path

from research_hub.config import read_config, state_dir

MAX_GPU_HOURS = 4.0
MAX_EPISODES = 1
MAX_TURNS = 8
MAX_RETRIES = 2
TURN_TIMEOUT_SECONDS = 900
TEXT_TIMEOUT_SECONDS = 120
TEXT_PROMPT = "只回复：Research Hub 连接测试成功。不要调用工具或执行命令。"
TEXT_REPLY = "Research Hub 连接测试成功"


def data_dir():
    return Path(os.environ.get("RESEARCH_HUB_EXECUTOR_DIR", state_dir() / "executor")).expanduser()


@dataclass(frozen=True)
class Settings:
    session_id: str
    project_dir: Path
    socket_path: Path
    peer_manifest: Path
    codex_home: Path
    auth_home: Path
    principal_uid: int

    def __post_init__(self):
        if (
            not isinstance(self.session_id, str)
            or not self.session_id.strip()
            or len(self.session_id) > 200
        ):
            raise ValueError("invalid_executor_session_id")
        if type(self.principal_uid) is not int or self.principal_uid < 0:
            raise ValueError("invalid_executor_principal_uid")
        for key in ("project_dir", "socket_path", "peer_manifest", "codex_home", "auth_home"):
            value = Path(getattr(self, key)).expanduser()
            if not value.is_absolute():
                raise ValueError("executor_paths_must_be_absolute")
            object.__setattr__(self, key, value)


def load_settings():
    data = read_config("executor.json")
    required = {field for field in Settings.__dataclass_fields__}
    if not isinstance(data, dict) or set(data) != required:
        raise ValueError("executor_configuration_required")
    try:
        return Settings(**data)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid_executor_configuration") from exc


def feature_config(settings, text=False):
    keys = [
        "apps",
        "hooks",
        "multi_agent",
        "plugins",
        "remote_plugin",
        "code_mode",
        "code_mode_host",
        "web_search_request",
        "image_generation",
        "browser_use",
        "computer_use",
        "remote_control",
    ]
    cfg = {"features." + key: False for key in keys}
    cfg.update(
        {
            "features.shell_tool": not text,
            "features.unified_exec": not text,
            "web_search": "disabled",
            "shell_environment_policy.inherit": "none",
            "shell_environment_policy.include_only": [],
            "shell_environment_policy.set": {
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "HOME": str(settings.project_dir / "runtime" / "executor-home"),
                "TMPDIR": str(settings.project_dir / "runtime" / "executor-tmp"),
            },
            "background_terminal_max_timeout": 300000,
        }
    )
    return cfg
