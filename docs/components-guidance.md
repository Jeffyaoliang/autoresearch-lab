# Guidance and reviewed progress components

These modules were extracted from the working Research Hub integration. Importing them does not query a server, create state directories, import WinPTY, or start a worker. The examples below contain placeholders; they do not configure a real deployment.

## How this relates to daily operation

The primary daily route is Mac Codex → SSH → Windows → SCO → the research container. Container access can execute Shell/Python directly, or invoke a separate deployment controller that submits a task prompt with Codex App Server `turn/start`. Direct remote commands are not evidence that the server-side Codex agent performed the work.

The Research Hub MCP adapter is an additional, tested interface, not the main daily entry point. Its advisory worker only injects context with `thread/inject_items`. The deployment controller's ability to start a model turn must not be attributed to that advisory interface. Existing Codex, SSH and SCO provide the underlying capabilities; this project adds environment-specific integration. No measured usage split or research-productivity gain is claimed.

## Advisory delivery

The four cloud-facing tools (`list_research_tasks`, `get_research_progress`, `submit_research_guidance`, and `get_research_guidance_status`) are exposed by `research-hub stdio`. The `research-hub http` command serves a different, read-only Bridge and does not expose advisory submission. The existing deployment uses the official OpenAI tunnel-client to forward requests to the stdio adapter over an outbound connection. Generic versions of its installation, collector supervision, scheduled task and operator launcher are included; see [deployment](deployment.md). The remote package, worker, private configuration, existing Tunnel and ChatGPT connection still need to be configured for each deployment. Experimental TLS relays are separate from this MCP Tunnel path and remain excluded. An independently installed public package passed live Windows/remote-server stdio query, advisory injection and receipt checks on 2026-10-24. The existing deployment was not replaced; the initial cloud Tunnel probe returned HTTP 429. A later recovery passed a live cloud query against the existing deployment, which still uses its original adapter. A 24-hour installed-package soak is in progress, not yet accepted. See [validation scope](live-validation-2026-10-24.md).

`guidance.queue.GuidanceQueue(root, binding_alias)` creates immutable, idempotent mailbox requests. Its receipt explicitly says the model has not received the request. `guidance.delivery.Delivery(root, proxy, binding)` claims a request durably before calling the official `thread/inject_items` RPC. A claim with no valid receipt means **uncertain delivery: review, do not resend**. A transport timeout cannot determine whether the server applied the request. Claims do not expire automatically.

Successful injection means text was added to the bound thread context. It does not mean the model read the message, started a turn, or executed an experiment. Neither the endpoint nor the RPC allowlist exposes `turn/start`, arbitrary Shell commands, or caller-selected threads.

The operator creates `inbox/`, `claims/`, and `receipts/` under a dedicated guidance directory. Its default is `state_dir()/guidance`; `RESEARCH_HUB_GUIDANCE_DIR` may select another absolute local directory. The directory and records must not be symlinks. Store the following local-only file there as `binding.private.json`:

```json
{
  "session_alias": "example_session",
  "session_id": "example-thread-id",
  "cwd": "/srv/example/project",
  "codex_home": "/srv/example/codex"
}
```

An optional `socket_path` overrides `codex_home/app-server-control/app-server-control.sock`. These paths and thread identifiers are trusted operator configuration, never cloud request fields. `python -m research_hub.guidance.worker` runs the Unix resident worker; merely installing the package does not start it. The worker verifies read-only/no-network resume permissions before accepting mailbox work. The endpoint supports only data operations `submit` and `status` and is invoked by the adapter, not exposed as a Shell service.

The delivery component persists a target fingerprint in `delivery-binding.json`. Reusing the same mailbox after changing its thread, project directory, Codex home or socket is rejected. Provision a separately reviewed mailbox for a different target; deleting this marker is not a migration procedure.

Cloud-facing code calls `guidance.runtime.query_guidance({"operation": "status", "request_id": "example01"})`. Submit also requires `message`. Request IDs are 8–64 ASCII letters/digits/underscores/hyphens, beginning with a letter or digit. Reusing an ID with different text is rejected. Messages are capped at 2,000 characters and 8,000 UTF-8 bytes; likely secrets and control characters are rejected. Mailbox bodies, claims, and receipts remain private state, outside the installed source tree.

## Optional Windows SCO transport

Both the guidance transport and progress reader use local `state_dir()/remote.json`. Required fields are explicit; no account, task, workspace, executable, or remote path defaults to an original deployment:

```json
{
  "session_alias": "example_session",
  "sco_executable": "C:/Tools/sco.exe",
  "workspace_name": "example-workspace",
  "task_name": "example-task",
  "worker_name": "example-worker",
  "owner_user_id": "example-owner-id",
  "owner_user_name": "example-owner",
  "remote_python": "/srv/example/venv/bin/python",
  "remote_guidance_dir": "/srv/example/state/guidance",
  "remote_state_dir": "/srv/example/state"
}
```

The `session_alias` must match the remote binding. The adapter checks the current SCO task identity, owner, workspace and `RUNNING` state before opening a fresh interactive connection. Credential and security confirmation prompts stop the operation. Only a bounded, marked JSON reply is exported; terminal output is discarded. WinPTY is imported only when this Windows adapter is invoked. No authentication material is included in configuration examples or passed as command arguments.

The package must already be installed for `remote_python`; the adapter invokes reviewed package modules with `-I -B -m`, rather than uploading opaque code or installing anything. It fixes the remote guidance/state directory through explicit environment variables. This transport passed an independent live SCO connection and advisory receipt check on 2026-10-24 after correcting the WinPTY executable token. This does not establish cloud Tunnel availability or scientific execution.

## Reviewed progress

`progress.projection.project_progress(probe, config=None)` accepts only one matching manifest and exports reviewed status, check states, numeric execution counts and a source hash. It rejects substituted/duplicate manifests, duplicate field names, changed project/stage/status, invalid times/hashes, boolean or negative counts, and unreviewed task-success claims. It does not return unrelated manifest text.

The operator keeps an approved schema in `state_dir()/progress.json` on both the local reader host and remote manifest host:

```json
{
  "project": "Example Research",
  "manifest_path": "/srv/example/project/experiment_manifest.json",
  "evidence_ref": "example_manifest",
  "approved_stage": "dependency_check",
  "approved_status": "Blocked",
  "check_names": ["sandbox", "autonomous_episode"],
  "max_source_age_seconds": 3600,
  "allow_historical_source": false
}
```

The manifest contains `project`, `current_stage`, `status`, timezone-aware `updated_at_utc`, `checks.<name>.status`, and `execution` counts (`episodes_started`, `simulators_started`, `generated_programs_executed`). `task_success` must be absent or null; scientific results require a separately reviewed projection. Status/check values are restricted to `Blocked`, `Verified`, and `Unknown`.

`progress.reader.read_manifest_probe(config=None)` is a bounded local probe used by `python -m research_hub.progress.reader --probe`. It reads exactly the configured manifest, caps it at 256 KiB, excludes unrelated content and rejects concurrent source changes. The plain reader entry point uses the optional Windows transport to invoke this remote probe.

`progress.runtime.query_progress()` requires reads no older than 120 seconds and caches verified replies for at most 60 seconds. Missing configuration, stale/future observations, source errors or inconsistent data return `available: false`; an old cache is not a fallback. Changing the approved schema invalidates the cache. Source freshness is also checked against `max_source_age_seconds`. Historical sources may be allowed explicitly, but remain labelled `historical_project_record`, with `live_research_progress_verified: false` and `acp_task_mapping_verified: false`.

## Offline verification

From the repository root:

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_guidance*.py' -v
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_progress*.py' -v
```

Tests use temporary mailboxes, synthetic manifests, fake RPC/socket responses and mocked subprocess adapters. They do not query a live service, start a model turn or modify a running deployment.
