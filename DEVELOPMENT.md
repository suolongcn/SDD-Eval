# Development Guide

This guide describes both the legacy SDD evaluation flow and the SWE-bench-inspired Benchmark V2 execution path. The two protocols intentionally use separate models, tables, and result semantics.

## Environment

- Python 3.11 or newer
- Git
- Docker when running untrusted Benchmark V2 instances
- Optional Codex CLI or OpenCode CLI for legacy generation runs
- Language/build tooling required by repositories evaluated through the trusted Local backend

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .
```

Run the web service:

```powershell
sdd-eval serve --host 127.0.0.1 --port 8000
```

The dashboard is at `/`, API routes are under `/api`, and interactive FastAPI documentation is at `/docs`.

## Quality gates

```powershell
python -m pytest -q
python -m compileall -q sdd_eval tests
git diff --check
```

These are the same primary gates used by GitHub Actions. Keep runtime dependencies small, preserve unrelated working-tree changes, and add focused regression tests for changed behavior.

## Architecture map

| Module | Responsibility |
| --- | --- |
| `models.py` | Legacy and V2 API/storage contracts |
| `storage.py` | SQLite persistence, atomic job claims, leases, retries, and attempts |
| `benchmark_io.py` | SWE-bench-compatible JSON/JSONL import and export |
| `harness.py` | Trusted local executable-oracle protocol |
| `docker_backend.py` | Isolated container implementation of the same protocol |
| `worker.py` | Durable Benchmark Job consumer and heartbeat loop |
| `api.py` | Dashboard and HTTP management API; never exposes private Oracle data |
| `cli.py` | Dataset, direct evaluation, queue, worker, and service commands |
| `adapters.py` | Legacy OpenSpec/Superpowers workflows |
| `providers.py` | Legacy model calls and retries |
| `evaluator.py` | Legacy checkout, generation, validation, scoring, and archival |

Keep provider-specific behavior in `providers.py`. Benchmark execution behavior belongs behind the backend interface rather than in API handlers or workers.

## Protocol boundary

Legacy evaluation remains compatible with existing Test Cases and Runs:

```text
TaskSpec -> adapter/provider -> RunResult
```

Benchmark V2 adds a separate executable-oracle path:

```text
BenchmarkInstance (public) + Prediction
                    |
                    v
             Local/Docker backend <--- EvaluationOracle (private)
                    |
                    v
             EvaluationResultV2
```

Do not reinterpret or migrate legacy `RunResult` scores into `EvaluationResultV2`. V2 first determines whether executable behavior is resolved, then stores SDD quality and efficiency as independent dimensions.

## Benchmark V2 contracts

### Public instance

`BenchmarkInstance` contains information visible to an Agent: repository, fixed `base_commit`, problem statement, environment contract, Requirement IR, constraints, dataset identity, language, and public source references.

`EnvironmentSpec` commands are argument arrays rather than shell strings. `{tests}` in `test_command` is replaced by one FAIL_TO_PASS or PASS_TO_PASS selector for each isolated test execution.

### Private Oracle

`EvaluationOracle` contains the gold patch, hidden test patch, FAIL_TO_PASS/PASS_TO_PASS selectors, forbidden paths, and review metadata. It is persisted in `evaluation_oracles`, but there is deliberately no Oracle HTTP endpoint.

Never add Oracle fields to public instance, prediction, job, logs, API errors, or exports. Public dataset export excludes Oracle data unless an administrator explicitly passes `--include-oracle`.

### Prediction and result

`Prediction` archives the exact model patch and calculates its SHA-256 hash. It also records model/client/workflow identity, SDD artifacts, trace links, and token usage.

`EvaluationResultV2` uses explicit outcomes such as `resolved`, `invalid_patch`, `build_failed`, `target_tests_failed`, `regression`, `agent_timeout`, and environment/harness errors. `resolved` must agree with the outcome, and passed test counts cannot exceed totals.

## Importing and inspecting datasets

Import SWE-bench JSON or JSONL into the separate V2 tables:

```powershell
sdd-eval import-swebench data.jsonl demo-verified --dataset-version 2026-09 --split verified
```

Export public instances or stored predictions:

```powershell
sdd-eval export-swebench public.jsonl --dataset-id demo-verified
sdd-eval export-predictions predictions.jsonl
```

Only trusted administrative workflows should export Oracle data:

```powershell
sdd-eval export-swebench private.jsonl --include-oracle
```

The persistence tables are additive: `benchmark_instances`, `evaluation_oracles`, `predictions`, `evaluation_results_v2`, `instance_validations`, `benchmark_jobs`, and `job_attempts`. Existing legacy tables are unchanged.

## Executable-oracle lifecycle

For each evaluation, the backend:

1. Creates a disposable checkout at the exact `base_commit`.
2. Runs trusted setup commands.
3. Applies the model patch.
4. Rejects edits to forbidden paths, including untracked files.
5. Applies the hidden test patch.
6. Runs the declared build command.
7. Executes every FAIL_TO_PASS selector independently.
8. Executes every PASS_TO_PASS selector independently.
9. Classifies and persists an explicit outcome and execution manifest.

The ordering is intentional: the Agent patch must not see or overwrite hidden tests. A resolved result requires all target tests and preservation tests to pass.

Before publishing an instance, validate that the target behavior fails at baseline, preservation tests pass at baseline, and both groups pass after applying the gold patch:

```powershell
sdd-eval validate-benchmark owner__repo-123 --backend docker
```

Direct evaluation is useful for development and debugging:

```powershell
sdd-eval evaluate-prediction <prediction-id> --backend local
```

The Local backend executes repository commands directly on the host and is only safe for trusted repositories. Use Docker for untrusted or shared benchmark workloads.

## Docker backend

An instance can reference a cached image, request an explicit registry pull, or use an administrator-controlled build context. Build context and Dockerfile selection must not come from Agent output.

The Docker backend applies CPU, memory, PID, and temporary-filesystem limits; drops Linux capabilities; enables `no-new-privileges`; supports a read-only root filesystem; and disconnects setup networking before grading when configured. The result manifest records the image ID, platform, resource limits, network policy, backend version, and environment digest.

Docker is optional for unit development. Contract tests mock the Docker CLI, but a release environment should also run a real-container smoke test.

## Persistent Jobs and Workers

Production Benchmark V2 work should be queued rather than executed inside the web process:

```powershell
sdd-eval enqueue-benchmark evaluate_prediction owner__repo-123 `
  --prediction-id <prediction-id> --backend docker --max-attempts 3

sdd-eval benchmark-worker --db sdd_eval.db --concurrency 4
```

Validation can use the same queue:

```powershell
sdd-eval enqueue-benchmark validate_instance owner__repo-123 --backend docker
```

The state flow is:

```text
queued -> preparing -> evaluating -> completed
   ^          |             |
   +----------+-------------+-> queued (retry remains)
                              -> failed (attempt limit)

queued/running -- cancellation requested --> cancelled
```

`claim_job()` uses a SQLite `BEGIN IMMEDIATE` transaction, so concurrent workers cannot claim the same row. Each claim increments the attempt number and creates a `JobAttempt`. The Worker periodically refreshes `heartbeat_at` and `lease_expires_at`.

Before claiming new work, a Worker recovers expired `preparing` or `evaluating` jobs. The interrupted attempt becomes `expired`; the job is requeued when attempts remain or becomes terminal after the limit. This is at-least-once execution, so new backends and result consumers must be idempotent around stable job, prediction, patch-hash, and result identifiers.

Queued cancellation is immediate. Running cancellation is cooperative and is observed at backend boundaries; its latency is therefore bounded by the active command timeout. Explicit retry is accepted only for terminal `failed` or `cancelled` jobs.

Useful management endpoints:

| Method and route | Purpose |
| --- | --- |
| `POST /api/benchmark-jobs` | Create validation or evaluation job |
| `GET /api/benchmark-jobs` | List jobs, optionally filtered by status |
| `GET /api/benchmark-jobs/{id}` | Inspect state, lease, result ID, and error |
| `GET /api/benchmark-jobs/{id}/attempts` | Inspect execution history |
| `POST /api/benchmark-jobs/{id}/cancel` | Request cancellation |
| `POST /api/benchmark-jobs/{id}/retry` | Requeue a terminal job |

SQLite scheduling is designed for multiple workers on one host. A multi-host deployment should replace the claim layer with a transactional shared queue while preserving the models and Worker contract.

## Extending the Benchmark

When adding a language or repository family:

1. Express setup, build, and test invocation in `EnvironmentSpec` without shell interpolation.
2. Configure a reproducible Docker image or administrator-owned build context.
3. Add log parsing only inside the backend; keep result classification consistent.
4. Construct baseline, gold, regression, invalid-patch, and timeout fixtures.
5. Verify FAIL_TO_PASS and PASS_TO_PASS selectors independently.
6. Confirm public API and export payloads contain no Oracle fields.

When adding a new execution backend, implement `validate_instance(instance, oracle, workspace=None)` and `evaluate(instance, oracle, prediction, workspace=None)`. Register backend selection in both `worker.create_backend()` and the direct CLI helper. Preserve the patch/test ordering and return existing V2 result contracts rather than backend-specific response shapes.

## Testing strategy

Tests are grouped by responsibility:

- `test_benchmark_v2.py`: schema, persistence, import/export, and Oracle API isolation
- `test_local_harness.py`: real Git patch application and outcome classification
- `test_docker_backend.py`: Docker command, isolation, manifest, and cleanup contracts
- `test_benchmark_jobs.py`: atomic claims, lease recovery, cancellation, retries, API, and Worker persistence

Prefer real temporary Git repositories for harness behavior and fakes only at external boundaries such as Docker. Never require network access or credentials in the default test suite.

## Troubleshooting

- A job remains `queued`: confirm a Worker is running, `available_at` has passed, and its backend is supported.
- A job repeatedly becomes `expired`: increase the lease only after confirming heartbeats are not blocked; inspect Attempt history and SQLite lock contention.
- `environment_error`: verify Git/Docker availability, repository access, image configuration, and setup networking.
- `invalid_patch`: verify the patch applies to the declared base commit and does not touch forbidden paths.
- `target_tests_failed` versus `regression`: inspect FAIL_TO_PASS and PASS_TO_PASS results separately.
- Cancellation appears delayed: the current implementation is cooperative; reduce backend command timeouts if faster interruption is required.

## Task authoring and pull requests

Legacy task files should include a stable ID, repository/revision, concrete requirements, acceptance scenarios, build/test commands, and an issue reference. Record merged PR and commit metadata when available.

Pull requests must describe compatibility, data migration, security, and operational impact. Do not commit API keys, SQLite databases, disposable workspaces, generated caches, or private benchmark exports. Update public documentation whenever contracts, commands, routes, outcomes, or worker semantics change.

Additional design details are available in:

- [Benchmark V2 architecture](docs/architecture/benchmark-v2.md)
- [Executable evaluation protocol](docs/architecture/evaluation-protocol.md)
- [Security boundary](docs/architecture/security-boundary.md)
- [Job and Worker design](docs/architecture/job-worker.md)
