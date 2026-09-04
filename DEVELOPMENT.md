# Development Guide

SDD Eval is a V2-only executable-oracle benchmark. There is no compatibility layer for the former Task/Run workflow. Opening a database without V2 `schema_metadata` intentionally drops its application tables and creates the current schema.

## Setup and quality gates

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .

python -m pytest -q
python -m compileall -q sdd_eval tests
git diff --check
```

Git and Python 3.11+ are required. Docker is required for untrusted workloads. Host language toolchains are only needed when developing with Local Backend.

## Architecture

| Module | Responsibility |
| --- | --- |
| `models.py` | Instance, Oracle, Prediction, Result, Validation, Job, and Attempt contracts |
| `storage.py` | V2-only SQLite schema, CRUD, atomic claims, leases, cancellation, and retry |
| `benchmark_io.py` | SWE-bench-compatible JSON/JSONL exchange |
| `harness.py` | Checkout, patch ordering, test execution, and outcome classification |
| `docker_backend.py` | Resource-limited and network-isolated container execution |
| `worker.py` | Durable queue consumption and heartbeat maintenance |
| `api.py` | Public V2 management API; private Oracle is never exposed |
| `cli.py` | Dataset, Prediction, direct grading, queue, Worker, and service operations |
| `dashboard.html` | V2 operational dashboard |

The canonical flow is:

```text
BenchmarkInstance + private EvaluationOracle
        |
        +-- Agent-visible input -> Prediction
                                  |
                                  v
                         BenchmarkJob / JobAttempt
                                  |
                                  v
                         Local or Docker Backend
                                  |
                                  v
                   EvaluationResult / InstanceValidationResult
```

## Data and privacy boundary

`BenchmarkInstance` is Agent-visible. It contains the repository, fixed base commit, issue, Requirement IR, constraints, environment commands, and Docker execution configuration.

`EvaluationOracle` is private. It contains the gold patch, hidden test patch, FAIL_TO_PASS/PASS_TO_PASS selectors, forbidden paths, and review metadata. Never place Oracle fields in:

- Instance API responses
- Prediction artifacts or logs
- Job payloads, errors, or Attempt records
- public JSONL exports
- container mounts

The API deliberately has no Oracle route. `export-dataset --include-oracle` is an administrator-only backup operation.

## V2 database lifecycle

`Store` uses an internal `schema_metadata.schema_version` (currently `3`). If metadata is absent or has another version, all existing application tables are dropped and the V2 schema is created. This includes databases from the former Task/Run implementation. The internal database revision is independent from the V2 public protocol version.

Current tables:

- `schema_metadata`
- `benchmark_instances`
- `evaluation_oracles`
- `predictions`
- `evaluation_results`
- `instance_validations`
- `benchmark_jobs`
- `job_attempts`

Foreign keys cascade from Instance to Oracle, Prediction, Result, and Validation. Deleting an Instance therefore deletes its complete benchmark history. Never add fallback deserializers for former schemas.

## Dataset ingestion

```powershell
sdd-eval import-dataset tasks.jsonl dataset-id --dataset-version v1 --split verified
sdd-eval export-dataset public.jsonl --dataset-id dataset-id
sdd-eval export-dataset backup.jsonl --dataset-id dataset-id --include-oracle
```

Required source fields are `instance_id`, `repo`, `base_commit`, and `problem_statement`. SWE-bench `patch`, `test_patch`, `FAIL_TO_PASS`, and `PASS_TO_PASS` become the private Oracle. Import writes Instance and Oracle in one transaction.

When adding a dataset adapter, produce the same two contracts. Do not add dataset-specific fields to the Harness or Result.

## Prediction contract

A `Prediction` stores the exact `model_patch`, its calculated SHA-256 hash, model/client/workflow identity, Token usage, documents, trace links, and logs. A supplied hash must match the patch.

Prediction generation is intentionally outside the benchmark Harness. Agents or orchestration systems submit finished Predictions through `POST /api/predictions` or:

```powershell
sdd-eval import-predictions predictions.jsonl
```

## Executable-oracle protocol

Evaluation order is a correctness and secrecy invariant:

1. Create a disposable checkout at exact `base_commit`.
2. Run setup commands.
3. Apply the model patch.
4. Inspect tracked and untracked changes for forbidden paths.
5. Apply the hidden test patch.
6. Run the build command.
7. Run every FAIL_TO_PASS selector independently.
8. Run every PASS_TO_PASS selector independently.
9. Persist the classified outcome and execution manifest.

The Agent patch is always applied before hidden tests. A Result is `resolved` only when the patch/build succeeds and both test groups pass completely. Target failures and regressions remain distinct outcomes.

Instance validation separately proves:

- target behavior fails on baseline;
- preservation behavior passes on baseline;
- the gold patch applies;
- both groups pass with the gold patch.

```powershell
sdd-eval validate-instance owner__repo-123 --backend docker
sdd-eval evaluate <prediction-id> --backend docker
```

## Backend extension

Backends implement:

```python
validate_instance(instance, oracle, workspace=None) -> InstanceValidationResult
evaluate(instance, oracle, prediction, workspace=None) -> EvaluationResult
```

Register a new backend in both `cli.backend_for()` and `worker.create_backend()`. Preserve checkout and patch ordering, outcome names, test counts, and the existing result shape.

Local Backend executes commands directly on the host and is limited to trusted repositories. Docker Backend is the default and must preserve:

- explicit image or administrator-controlled build context;
- CPU, memory, PID, and tmpfs limits;
- capability drop and `no-new-privileges`;
- optional read-only root and non-root user;
- setup network disconnection before grading;
- image identity, limits, platform, and network policy in the manifest.

Default tests mock the Docker boundary. Release validation should additionally run a real-container smoke test.

## Jobs, leases, and attempts

Production grading is asynchronous:

```powershell
sdd-eval enqueue validate_instance owner__repo-123 --backend docker
sdd-eval enqueue evaluate_prediction owner__repo-123 --prediction-id <id> --backend docker
sdd-eval worker --concurrency 4
```

State transitions:

```text
queued -> preparing -> evaluating -> completed
   ^          |             |
   +----------+-------------+-> queued (retry available)
                              -> failed (attempt limit)

queued/running -- cancellation requested --> cancelled
```

`claim_job()` uses `BEGIN IMMEDIATE`; concurrent same-host Workers cannot claim the same row. Every claim increments `attempt` and inserts a `JobAttempt`. Heartbeats extend `lease_expires_at`. A future claimant marks stale Attempts `expired` and requeues their Jobs when attempts remain.

Execution is at least once. Backend side effects and consumers must use stable Instance, Prediction, patch hash, Job, and Result identifiers. Running cancellation is cooperative at backend boundaries; queued cancellation is immediate. Explicit retry only accepts terminal failed/cancelled Jobs.

SQLite scheduling targets multiple Workers on one host. Multi-host deployment requires a transactional shared queue while retaining these contracts.

## HTTP API

### Pull-request source integration

`pr_sources.py` provides the forge boundary used by the dashboard and API. `PullRequestSourceService` performs repository search, merged-PR listing, changed-line filtering, and import. The API surface is:

- `GET /api/pr-sources/repositories`
- `GET /api/pr-sources/pulls`
- `POST /api/pr-sources/import`

Import is transactional: public instance metadata and the private executable Oracle are written together. The API returns neither gold patches nor hidden test selectors. Configure `GITHUB_TOKEN` for authenticated GitHub requests; unauthenticated requests remain supported with the provider's lower rate limits. Keep provider-specific parsing inside `pr_sources.py` so the worker and storage layers remain forge-agnostic.

### Dashboard assets

The dashboard shell is `sdd_eval/dashboard.html`; its active client bundle is served from `GET /dashboard.js` and implemented in `sdd_eval/dashboard.js`. Keep the bundle free of Oracle data and verify both the source-import and model-comparison flows when changing dashboard markup.

| Route | Purpose |
| --- | --- |
| `GET /api/summary` | Dashboard counters and resolve rate |
| `/api/instances` | Public Instance CRUD |
| `/api/predictions` | Prediction create/list/detail |
| `/api/jobs` | Job create/list/detail |
| `/api/jobs/{id}/attempts` | Attempt history |
| `/api/jobs/{id}/cancel` | Cooperative cancellation |
| `/api/jobs/{id}/retry` | Terminal Job retry |
| `/api/results` | Evaluation results |
| `/api/validations` | Instance validation history |

API handlers validate Instance existence, Oracle presence, and Prediction ownership before creating a Job. HTTP-created Jobs require Docker Backend; trusted Local execution is CLI-only. Instance HTTP creation also rejects administrator-only image build/pull settings. API handlers never execute repository code in the web process.

## Testing strategy

- `test_benchmark_v2.py`: destructive V2 schema initialization, contracts, persistence, import/export, and Oracle isolation
- `test_local_harness.py`: real temporary Git repositories, patch ordering, forbidden paths, and outcomes
- `test_docker_backend.py`: Docker commands, isolation policy, manifest, errors, and cleanup
- `test_benchmark_jobs.py`: atomic claims, lease recovery, cancellation, retry, Worker persistence, and API validation

Use real temporary Git repositories for protocol behavior and fakes only at external boundaries. The default suite must not require network access, credentials, or Docker.

## Troubleshooting

- Old data disappeared: expected on first V2 startup; the migration is intentionally destructive.
- Instance cannot enqueue: import its private Oracle through `import-dataset`.
- Job remains queued: start `sdd-eval worker` and verify `available_at`.
- Job becomes expired: inspect Attempt heartbeat and SQLite contention before increasing the Lease.
- `invalid_patch`: verify the patch applies to the exact base commit and avoids forbidden paths.
- `target_tests_failed`: one or more FAIL_TO_PASS selectors still fail.
- `regression`: target tests pass but at least one PASS_TO_PASS selector fails.
- cancellation is delayed: cancellation is cooperative and bounded by the active command timeout.

## Pull requests

Describe schema, protocol, security, and operational impact. Update docs for changes to contracts, routes, CLI, outcomes, or Worker semantics. Never commit SQLite databases, private Oracle exports, disposable workspaces, secrets, or repository execution logs.

See also [V2 architecture](docs/architecture/benchmark-v2.md), [evaluation protocol](docs/architecture/evaluation-protocol.md), [security boundary](docs/architecture/security-boundary.md), and [Job/Worker design](docs/architecture/job-worker.md).
