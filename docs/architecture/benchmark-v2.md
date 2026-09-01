# Benchmark V2 Architecture

## Objective

Benchmark V2 adds SWE-bench-compatible task and prediction contracts without changing the meaning of legacy `TaskSpec` or `RunResult` records. The primary functional outcome in V2 will be executable resolution; SDD process quality and efficiency remain independent result dimensions.

## Compatibility boundary

| Protocol | Task model | Result model | Meaning |
| --- | --- | --- | --- |
| Legacy | `TaskSpec` | `RunResult` | Existing weighted document/code/test/reference/efficiency evaluation |
| V2 | `BenchmarkInstance` + private `EvaluationOracle` | `EvaluationResultV2` | Executable-oracle outcome with separate SDD and efficiency metrics |

Legacy rows are never rewritten into V2 outcomes. New V2 records use separate SQLite tables. A future UI must label and aggregate the protocols separately.

## Components

```text
Dataset importer
  -> BenchmarkInstance (public)
  -> EvaluationOracle (private)

Agent runtime
  BenchmarkInstance -> ArtifactBundle + model patch -> Prediction

Persistent job queue -> independent workers

Local/Docker evaluation harness
  Prediction + EvaluationOracle + environment -> EvaluationResultV2
```

## Core contracts

### BenchmarkInstance

Contains only information an agent may see: repository, base commit, problem statement, Requirement IR, constraints, dataset identity, language, and public source links.

### EvaluationOracle

Contains gold patch, hidden test patch, FAIL_TO_PASS/PASS_TO_PASS selectors, forbidden paths, and review metadata. It is stored separately and is not exposed by the HTTP API.

### Prediction

Captures the exact model patch, its SHA-256 hash, model/client/workflow identity, artifacts, trace links, token usage, and optional originating run.

### EvaluationResultV2

Reserves explicit executable outcomes and keeps `functional_metrics`, `sdd_metrics`, and `efficiency_metrics` independent. Phase 1 defines and stores the contract; a later harness phase will produce results.

## Persistence

V2 adds the following append-only-compatible tables through `create table if not exists` migrations:

- `benchmark_instances`
- `evaluation_oracles`
- `predictions`
- `evaluation_results_v2`
- `benchmark_jobs`
- `job_attempts`

The existing `tasks`, `runs`, `run_artifacts`, `collections`, and `comparisons` tables are unchanged.

## Import and export

The JSONL importer maps SWE-bench fields to a public instance and private oracle. Public export excludes oracle fields by default. Prediction export follows the standard `instance_id`, `model_name_or_path`, `model_patch` shape.

## Implemented executable-oracle foundation

`LocalEvaluationBackend` checks out the exact base commit, runs trusted setup/build commands, applies the model patch before the hidden test patch, rejects forbidden-path edits, executes every FAIL_TO_PASS and PASS_TO_PASS selector independently, and stores an explicit V2 outcome. It also validates baseline and gold behavior before an instance is accepted.

The local backend is a protocol implementation for trusted development only; it is not a sandbox.

## Docker backend

`DockerEvaluationBackend` grades an already-created Prediction in a dedicated container. It supports cached images, registry pulls, or an administrator-provided build context. The checked-out repository is mounted at `/workspace`; the Oracle itself is never mounted. Setup commands may use the configured setup network, after which the backend disconnects that network before build and grading.

Every result records the image ID, backend version, platform, network policy, read-only setting, and resource limits in `execution_manifest`. The environment digest covers both that manifest and the command contract.

## Job execution

Benchmark evaluation and instance validation can be queued as durable `BenchmarkJob` records. Independent workers atomically claim jobs, maintain a lease, record attempts, recover expired work, apply bounded retries, and persist result identifiers. See [job-worker.md](job-worker.md).

## Deferred work

- Automatic Requirement IR extraction
- V2 dashboard and benchmark reports
