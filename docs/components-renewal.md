# Container renewal state machines

For Linux/Docker, use the separate [Docker renewal CLI and backend](docker-renewal.md), which restarts one pinned existing failed container without SCO. The remainder of this page documents the original **SCO/ACP-specific guards**.

This package preserves two related implementations from the local Container Renewal prototype. They solve different operations and are not competing versions of one executable:

| Module | Operation | Durable record | Acceptance evidence |
| --- | --- | --- | --- |
| `research_hub.renewal.renewal_guard` | Restart the same verified task instance | `ledger.json`, indexed by lifecycle | A later start time and an active state, independently described |
| `research_hub.renewal.copy_guard` | Copy a verified template into a deterministically named replacement generation | `copy-ledger.json`, an ordered generation chain | The exact named child, identity and derived profile, independently listed and described |

Use one operation appropriate to the backend. The copy guard checks all tasks belonging to the configured owner and blocks overlapping active work. These modules do not coordinate with each other through a shared lock; concurrent deployment of both requires an external coordinator.

## Scope of the migration

The migrated library retains the lifecycle checks, explicit expiry attestations, durable intent before dispatch, exclusive local locks, no-resend handling after uncertain transport results, complete paginated task listing, template/profile checks, a generation limit, and sanitized error summaries. `run()` is a read-only check: it makes only `describe` and, for copying, `list` calls, and does not create a lock or ledger. Applying is an explicit library call, `run(apply=True)`, and still passes all policy checks.

For these legacy SCO guards there is no installed renewal CLI, background scheduler, service definition or deployment wrapper. Both `Guard` constructors require an explicitly supplied runner; neither defaults to `subprocess.run`. Importing the package runs no remote command. No operational policy, ledger, task describe cache, authorization report or status receipt is included.

## Explicit configuration

Create a `Target` with the fields below, or load the same object using `Target.from_config("renewal-target.json")`. The loader uses the shared `research_hub.config.read_config` location. All fields must be present when loading a mapping or file:

| Target field | Meaning |
| --- | --- |
| `alias` | `task_` followed by the first 12 lowercase hex characters of SHA-256 of the verified task name |
| `workspace` | Backend workspace name |
| `owner_prefix` | Expected prefix of the backend owner name |
| `uid_sha256` | Canonical JSON digest of the verified template/original UID |
| `owner_user_id_sha256` | Canonical JSON digest of the backend owner user ID |
| `profile_sha256` | Canonical JSON digest of the selected complete profile fields |
| `name_prefix` | Prefix for replacement names, followed by `-g0001`, `-g0002`, etc. |
| `display_name_prefix` | Prefix for replacement display names with the same generation suffix |

Use the modules' `digest()` and `profile()` functions for canonical JSON hashes. A raw-string SHA-256 is not interchangeable with `digest()`. `Target` is immutable and provides identity pins separately from the mutable policy file; changing a cache or policy cannot silently rebind the configured identity.

Construct a guard with explicit `policy_path`, `cache_path`, backend executable identifier `sco`, and a callable `runner(args, **kwargs)` that returns an object with `returncode`, `stdout` and `stderr`. The identifier is passed as an argument to that runner; the library does not resolve a host-specific executable. A runner used for offline work should implement fake describe/list/start/copy responses. Tests demonstrate this contract entirely with fixtures.

The optional `state_root` selects the ledger, lock and `stop.flag` directory. Its default is `research_hub.config.state_dir() / "renewal"`. Neither loading a target nor constructing a guard creates that directory. Policy and cached describe paths are explicit and can be outside the source checkout.

Before any apply operation, an operator must provision the directory and reconcile an initial ledger. The restart ledger has `{"schema": 1, "records": {}}`; the copy ledger additionally pins `template_name` and `template_uid_sha256` and uses an empty `records` list. A missing ledger blocks applying; it is never silently reset after losing historical dispatch evidence.

## Policies and state transitions

The restart policy must enable the configured alias, profile, UID and owner hashes and contain an exact `expiry_attestations` pair of `start_time` and `suspend_time`. A suspended state alone is insufficient, and a lifecycle shorter than 32 hours is rejected. Once the operation records `DISPATCHING`, it either reconciles a newer active lifecycle as `ACCEPTED` or persists `UNCERTAIN`. The same cycle is never dispatched again, including after a timeout or nonzero exit. There is no aggregate restart-cost budget in this prototype.

The copy policy must enable the same identity pins and explicitly specify:

- `replacement_policy: "scheduled_32h_replacement_policy"`
- `window_start_hours: 32` and `window_end_hours: 34`
- `copy_mount_policy: "afs_only"`
- `deterministic_name_prefix` equal to `Target.name_prefix`
- `max_generations`, an integer from 1 through 10000, or explicit `null` for no generation limit
- `initial_expiry_attestations`, containing the initial exact lifecycle pair

The copy guard keeps the prototype's specific 32–34 hour replacement window. It derives one AFS root mount from the verified template, excludes other mounts, and normalizes only the documented default restart rule and equivalent AFS root-subdirectory representation. This is backend-specific compatibility logic, not a general mount or profile converter.

A copy first persists `CLAIMED`, dispatches once, then records `UNCERTAIN` regardless of the immediate CLI response. A subsequent independent observation binds the verified child as `BOUND`. Missing or ambiguous named children require reconciliation, never another create request. Historic `SETUP_FAILED_RETIRED` records remain history and do not independently authorize another replacement. Generation limits are a resource-count bound, not a monetary budget.

Both modules recheck mutable policy and lifecycle before dispatch. A `stop.flag` blocks reads and dispatch and is checked around durable writes. Locking is local to a filesystem and operation; it does not provide distributed exactly-once delivery. The design prioritizes avoiding duplicate operations, so a crash between durable intent and actual dispatch may require manual reconciliation even when nothing was created.

## Offline verification

Run from the repository root:

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_renewal*.py'
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_copy_guard*.py'
```

The migrated tests use temporary directories, fixture identities and fake runners. They exercise single dispatch and reconciliation, timeout/disconnect/nonzero no-resend behavior, profile and identity changes, exact approvals, read-only checks, policy changes during preflight, missing/corrupt ledgers, locking, complete pagination, overlap rejection, derived AFS profiles, sanitization, and generation-budget exhaustion. They do not contact SCO, SSH, model APIs or GPUs. Backend compatibility and a real deployment have not been validated by this migration.
