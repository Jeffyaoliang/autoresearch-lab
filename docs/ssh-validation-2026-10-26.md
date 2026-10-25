# Standard SSH validation — 2026-10-26

The new transport was validated on macOS with Python 3.12.13 and system OpenSSH 10.0p2. This is separate from the original Windows/SCO validation on 2026-10-24.

## What actually ran

The opt-in integration test starts a real OpenSSH daemon on a random **127.0.0.1-only** port with newly generated host/client keys and an explicit trusted known_hosts entry. It connects through the system `ssh` executable, uploads the built public-package wheel over stdin, creates an isolated remote venv, and installs that wheel without downloading dependencies. In this test the SSH target is another process on the same Mac, not a cloud GPU server.

Verified:

- Exact same deployment can be retried without changing the binding.
- Doctor can read the configured thread through its control socket and distinguishes a missing worker from readiness.
- Explicit worker startup reaches a fresh ready state; a repeated start detects the existing worker lock.
- Guidance submit → mailbox → worker → context injection acknowledgment → status receipt succeeds over SSH. Submitting the same ID/text again does not produce a second injection.
- Reviewed manifest reads preserve the source hash and pass the existing projection rules.
- The installed package's real MCP stdio interface initializes and returns the SSH task-list limitation, reviewed progress, and guidance receipt.
- A mismatched OS account is rejected; removing the trusted host key causes SSH to reject the connection.
- The simulated Codex peer received no `turn/start` or arbitrary execution method.

**The Codex control peer is a protocol fixture.** It implements the existing initialize/read/resume/inject response shape, not a running Codex model. The tests prove real SSH, packaging, module dispatch, persistence and projection under that protocol; they do not establish compatibility with every Codex version or cloud platform. No GPU experiment, autonomous research result, productivity measurement, Windows SSH live acceptance or cloud Tunnel connection was tested here. The earlier real SCO/Codex evidence remains separately documented.

## Reproduce

```sh
uv sync --locked --no-editable
uv build --wheel
RESEARCH_HUB_TEST_SSH=1 uv run --no-editable pytest -q
uv run --no-editable ruff check src tests
uv run --no-editable ruff format --check src tests
```

Result: **275 passed, 38 skipped, 42 subtests passed**. The 38 skips cover existing Windows-only tests on this Mac; the SSH integration test was explicitly enabled and passed. One existing Starlette/httpx deprecation warning remains. Ruff lint and formatting checks pass.

Additional offline tests cover alias/option injection, canonical remote paths, credentials/unknown configuration fields, output-size and timeout limits, redacted SSH errors, ambiguous receipts, CLI dispatch, conflicting local/remote deployment directories, SCO dispatch compatibility and explicit unsupported task listing. The normal CI suite skips the opt-in SSH daemon test.

## Operational boundaries

Installation does not configure SSH authentication/host trust, provision a machine, install Codex or a GPU driver, create a ChatGPT MCP/Tunnel connection, or install reboot persistence. Worker startup resumes the dedicated advisory thread read-only, with no network/tools. Readiness and delivery acknowledgment are not evidence that a model started executing.

Use [the SSH deployment guide](ssh.md) to repeat acceptance against a real target before treating that deployment as ready.
