# Executor and acceptance components

These packages contain the portable code from the local Research Executor and Acceptance Channel prototypes. They do not include a live deployment, credentials, session history, TLS relays, cloud account configuration, or experiment results. Importing the modules does not read configuration, create state, contact a service, or start a model turn.

## Executor

`research_hub.executor.state.State` maintains a durable SQLite job queue, redacted events, and a hash-chained audit. Request identifiers are constrained and idempotent; a duplicate identifier with a different fixed task is rejected. The durable queue binds to its first explicit executor configuration; retargeting it to another session or deployment is rejected. A prior `starting` or `running` job is marked `recovery_required` when the actor restarts. It is never automatically replayed. A text-only retry requires explicit `retry_text` and evidence of no tool items and an idle terminal thread, with the original retry ceiling.

`research_hub.executor.core.Actor` binds one actor to one configured session. The only currently implemented model operation is the fixed `text_acceptance` request. It requires the configured peer identity, subscription authentication, an idle target, a read-only sandbox, and disabled tools in its requested feature configuration. Event observation cannot prove that the interface prevented tool use. A successful text response is a connectivity receipt, not proof of general experiment readiness.

The legacy `rax_click_mouse` enum remains recognizable so an existing queue record cannot silently become a different task. It is blocked before a connection or remote sandbox change: text acceptance, isolation and budget checks do not enable it. The final gate is always `scoped_runtime_not_yet_enabled`. No arbitrary prompt, shell RPC, account selection, GPU provisioning or remote command interface is exposed.

`core.serve(settings=None, root=None, advisory_callback=None)` is an explicit Linux-only local Unix-socket server. Its socket is mode `0600`, and the caller UID must equal `principal_uid`. It holds an exclusive owner lock. It does not discover, signal or take over another running process. The optional `advisory_callback(actor)` is a dependency injection point for an embedding application; no deployed guidance module is automatically imported. Adapters need their own scope review.

`client.request(data, root=None)` is the local caller. To inspect an already configured and separately started server:

```sh
python -m research_hub.executor.client status
```

To explicitly start a server after configuring a **new test deployment**:

```sh
python -m research_hub.executor.core
```

Neither command is part of installation or an offline demo. The executor may submit a fixed text model turn only after an explicit operator request; installation itself does not submit work.

## Acceptance channel

`research_hub.acceptance.channel` is an independent read-only preflight and denial interface. `observe(settings=None)` can send only `initialize`, `thread/read`, and `thread/loaded/list`. It checks a configured Unix peer; a snapshot of an idle thread does not guarantee exclusive ownership or an atomic start.

`prepare(store, observer=...)` stores a singleton request and preflight evidence. `trigger(store)` **always** returns `DISABLED_FAIL_CLOSED` before any connection, execution claim, prompt submission, or event subscription. Config files, forged approval records, CLI flags and free-text messages cannot enable it. `OnceClaim` is a retained reservation primitive for future reviewed implementations; the disabled CLI never calls it, and an uncertain claim is not replayed.

`EventCapture(turn_id, session_id)` and `collect_notifications(client, turn_id, deadline_monotonic, session_id)` reduce a supplied event stream. They do not open a transport or start a turn. They enforce session and turn binding, lifecycle order and reply size; unexpected replies and server errors are represented by hashes. A detected tool event is evidence of detection, not proof that it was prevented.

CLI entry points are explicit:

```sh
python -m research_hub.acceptance.channel status
python -m research_hub.acceptance.channel trigger
```

`prepare` additionally performs live **read-only** observation and must be invoked deliberately. With missing configuration, the CLI reports unavailable and exits `2`; it does not fall back to a previously deployed session.

## Configuration and state

The shared `research_hub.config.read_config()` reads component JSON files directly under `state_dir()`. No component has a shipped session ID, host, user account, AFS path, Windows path or login location.

- Queue and audit root: `state_dir()/executor`; override with `RESEARCH_HUB_EXECUTOR_DIR`.
- Acceptance root: `state_dir()/acceptance`; override with `RESEARCH_HUB_ACCEPTANCE_DIR`.
- Component settings remain `state_dir()/executor.json` and `state_dir()/acceptance.json`, even when a data-root override is used.

All configuration fields below are required; unknown fields are rejected. All path fields must be absolute. `principal_uid` is a nonnegative integer; session IDs must be nonempty strings. The following values are **examples only**, with no live target:

`executor.json`:

```json
{
  "session_id": "example-session-id",
  "project_dir": "/srv/example-research-project",
  "socket_path": "/run/example-codex/app-server.sock",
  "peer_manifest": "/run/example-codex/peer.json",
  "codex_home": "/srv/example-codex-config",
  "auth_home": "/srv/example-service-home",
  "principal_uid": 1000
}
```

`acceptance.json`:

```json
{
  "session_id": "example-session-id",
  "socket_path": "/run/example-codex/app-server.sock",
  "peer_manifest": "/run/example-codex/peer.json",
  "principal_uid": 1000
}
```

Both transports require Linux `SO_PEERCRED` and `/proc`. The peer manifest is a regular, non-symlink file owned by `principal_uid`, with no group or world permissions (normally mode `0600`). Its bounded JSON schema is:

```json
{"pid": 12345, "start_ticks": "987654321"}
```

The values must come from the operator's newly configured peer. The transport compares the peer PID and UID, then checks Linux process start ticks so a reused PID is rejected. There is no shipped PID fallback. These files describe identity and paths; no credential contents belong in them. The executor's login check invokes the configured peer executable's `login status` with the explicitly configured login directories. It does not read or export credential files.

The components preserve prototype limits rather than claim deployment readiness: the text executor requests no-tools settings but has no independently proven interface-level no-tools guarantee; acceptance execution remains disabled; Linux peer verification and authentication need a separately operated service. Automated tests are synthetic and do not constitute live model or scientific evidence.

## Offline validation

From the repository root:

```sh
PYTHONPATH=src python -m unittest discover -s tests -p 'test_executor*.py'
PYTHONPATH=src python -m unittest discover -s tests -p 'test_acceptance*.py'
```

The tests use temporary directories, fake session IDs and mocked clients. They do not connect to an App Server, authenticate, call paid APIs, allocate compute or run experiments. The TLS relay's deployment-only test is intentionally excluded because its relay implementation is not part of this package.
