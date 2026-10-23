"""Explicit identity pins, separate from mutable per-cycle approval policies."""

from dataclasses import dataclass
import re
from typing import Mapping

from research_hub.config import read_config


@dataclass(frozen=True)
class Target:
    """Operator-supplied task identity and naming scope.

    Hashes use the guards' canonical JSON SHA-256, not a raw-string hash.
    Construction performs no reads, remote commands, or state writes.
    """

    alias: str
    workspace: str
    owner_prefix: str
    uid_sha256: str
    owner_user_id_sha256: str
    profile_sha256: str
    name_prefix: str = "research-renew"
    display_name_prefix: str = "research-renew"

    def __post_init__(self):
        if not isinstance(self.alias, str) or not re.fullmatch(r"task_[a-f0-9]{12}", self.alias):
            raise ValueError("invalid target alias")
        for field in ("workspace", "owner_prefix", "name_prefix", "display_name_prefix"):
            value = getattr(self, field)
            if not isinstance(value, str) or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value
            ):
                raise ValueError(f"invalid target {field}")
        for field in ("uid_sha256", "owner_user_id_sha256", "profile_sha256"):
            value = getattr(self, field)
            if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
                raise ValueError(f"invalid target {field}")

    @classmethod
    def from_mapping(cls, value: Mapping):
        """Load explicit pins; absent or invalid configuration fails closed."""
        if not isinstance(value, Mapping):
            raise ValueError("target configuration must be an object")
        try:
            return cls(**{field: value[field] for field in cls.__dataclass_fields__})
        except (KeyError, TypeError):
            raise ValueError("target configuration fields are missing or invalid") from None

    @classmethod
    def from_config(cls, filename="renewal-target.json"):
        """Read the shared config location without creating a deployment."""
        return cls.from_mapping(read_config(filename))
