"""Real SCO/WinPTY argv check; help only, no login or remote operation."""

import os
import queue
import threading
from pathlib import Path

import pytest

from research_hub.guidance.transport import spawn_sco_winpty

SCO = Path(os.environ.get("RESEARCH_HUB_TEST_SCO", str(Path.home() / ".sco/bin/sco.exe")))


@pytest.mark.skipif(os.name != "nt" or not SCO.is_file(), reason="requires Windows SCO CLI")
def test_winpty_preserves_sco_acp_command_prefix():
    pytest.importorskip("winpty")
    process = spawn_sco_winpty([str(SCO), "acp", "jobs", "exec", "--help"])
    result = queue.Queue()

    def read():
        output = ""
        try:
            while True:
                output += process.read(4096)
        except Exception:
            pass
        result.put(output)

    threading.Thread(target=read, daemon=True).start()
    try:
        output = result.get(timeout=20)
        assert "unknown command" not in output.lower()
        assert "--worker-name" in output
        assert "--workspace-name" in output
    finally:
        process.close(force=True)
