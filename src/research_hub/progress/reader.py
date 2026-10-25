"""Fixed-target SSH/SCO adapter and bounded remote manifest probe."""

import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shlex
import sys

from research_hub.guidance.transport import load_remote_config, remote_directory, run_remote
from .projection import COUNTS, projection_config


def read_manifest_probe(config=None):
    """Read one configured local manifest; never return arbitrary source text."""
    config = projection_config(config)
    path = Path(config["manifest_path"])
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 262144:
        raise ValueError("manifest_unavailable")
    raw = path.read_bytes()
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("invalid_manifest")
    fields = [
        {"field": "/" + key, "value": data.get(key)}
        for key in ("project", "current_stage", "status", "updated_at_utc")
    ]
    checks = data.get("checks", {})
    for key in config["check_names"]:
        fields.append(
            {"field": "/checks/" + key + "/status", "value": checks.get(key, {}).get("status")}
        )
    counts = {
        "/execution/" + key: data.get("execution", {}).get(key) for key in (*COUNTS, "task_success")
    }
    # Concurrent edits require a new probe rather than combining source versions.
    if path.read_bytes() != raw:
        raise ValueError("source_changed_during_read")
    return {
        "metadata": {
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "sources": [
                {
                    "path": config["manifest_path"],
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "fields": fields,
                    "execution_counts": counts,
                }
            ],
        }
    }


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv == ["--probe"]:
        try:
            result = read_manifest_probe()
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            result = {"available": False, "reason": "source_unavailable_or_requires_review"}
        print("RAH_DATA_" + base64.b64encode(json.dumps(result).encode()).decode() + "_RAH_END")
        return
    if argv:
        raise ValueError("invalid_reader_arguments")
    config = load_remote_config()
    root = remote_directory(config, "remote_state_dir")
    command = (
        "RESEARCH_HUB_STATE_DIR="
        + shlex.quote(root)
        + " "
        + shlex.quote(config["remote_python"])
        + " -I -B -m research_hub.progress.reader --probe"
    )
    # The probe already has a metadata envelope; do not wrap it twice.
    print(json.dumps(run_remote(command, config)["metadata"]))


if __name__ == "__main__":
    main()
