"""Independent fixed acceptance scope. There is deliberately no enable switch."""

from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path

from research_hub.config import read_config, state_dir

REQUEST_ID = "minimal_text_acceptance_v1"
PROMPT = "只回复：Research Hub 连接测试成功。不要调用工具或执行命令。"
EXPECTED_REPLY = "Research Hub 连接测试成功"
EXECUTION_IMPLEMENTED = False
NO_TOOLS_ENFORCEMENT_VERIFIED = False
READ_RPC = frozenset({"initialize", "thread/read", "thread/loaded/list"})


def data_dir():
    return Path(
        os.environ.get("RESEARCH_HUB_ACCEPTANCE_DIR", state_dir() / "acceptance")
    ).expanduser()


@dataclass(frozen=True)
class Settings:
    session_id: str
    socket_path: Path
    peer_manifest: Path
    principal_uid: int

    def __post_init__(self):
        if (
            not isinstance(self.session_id, str)
            or not self.session_id.strip()
            or len(self.session_id) > 200
        ):
            raise ValueError("invalid_acceptance_session_id")
        if type(self.principal_uid) is not int or self.principal_uid < 0:
            raise ValueError("invalid_acceptance_principal_uid")
        for key in ("socket_path", "peer_manifest"):
            value = Path(getattr(self, key)).expanduser()
            if not value.is_absolute():
                raise ValueError("acceptance_paths_must_be_absolute")
            object.__setattr__(self, key, value)


def load_settings():
    data = read_config("acceptance.json")
    required = {field for field in Settings.__dataclass_fields__}
    if not isinstance(data, dict) or set(data) != required:
        raise ValueError("acceptance_configuration_required")
    try:
        return Settings(**data)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid_acceptance_configuration") from exc


def spec(session_id):
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("acceptance_session_required")
    data = {
        "schema_version": 1,
        "request_id": REQUEST_ID,
        "thread_id": session_id,
        "prompt": PROMPT,
        "expected_reply": EXPECTED_REPLY,
        "execution_limit": 1,
        "tool_calls_allowed": 0,
        "requires_separate_human_authorization": True,
    }
    data["spec_sha256"] = sha256(
        json.dumps(data, sort_keys=True, ensure_ascii=True).encode()
    ).hexdigest()
    return data


def barriers():
    return [
        "no_interface_level_no_tools_guarantee_verified",
        "no_atomic_exclusive_turn_start_verified",
        "separate_trigger_authorization_not_granted",
        "trigger_transport_not_implemented",
    ]


def execution_gate():
    # No prompt-only fallback, policy override, RPC passthrough or force flag.
    return {"enabled": False, "decision": "DISABLED_FAIL_CLOSED", "barriers": barriers()}
