# Benchmark V2 Architecture

## Objective

Benchmark V2 is the only supported protocol. It combines SWE-bench-compatible instances and predictions with executable resolution, SDD traceability, and efficiency dimensions.

## Schema boundary

The Store requires V2 schema metadata. A database without the current metadata is cleared and rebuilt; historical Task/Run records are not migrated or interpreted.

## Components

```text
Dataset importer
  -> BenchmarkInstance (public)
  -> EvaluationOracle (private)

Agent runtime
  BenchmarkInstance -> ArtifactBundle + model patch -> Prediction

Persistent job queue -> independent workers

Local/Docker evaluation harness
  Prediction + EvaluationOracle + environment -> EvaluationResult
```

## Core contracts

### BenchmarkInstance

Contains only information an agent may see: repository, base commit, problem statement, Requirement IR, constraints, dataset identity, language, and public source links.

When available, `reference_code_lines` records the added and removed lines in the official reference PR. The count is derived while importing the private reference patch and is safe to expose with the public Instance metadata; `reference_code_estimated` marks externally supplied estimates.

### EvaluationOracle

Contains gold patch, hidden test patch, FAIL_TO_PASS/PASS_TO_PASS selectors, forbidden paths, and review metadata. It is stored separately and is not exposed by the HTTP API.

### Prediction

Captures the exact model patch, its SHA-256 hash, model/client/workflow identity, artifacts, trace links, and token usage.

### EvaluationResult

Uses explicit executable outcomes and keeps `functional_metrics`, `sdd_metrics`, and `efficiency_metrics` independent. Results expose `functional_score`, `code_quality_score`, `documentation_score`, and the weighted `score` composite. The weights are fixed at 50% functional, 25% code quality, and 25% documentation; FAIL_TO_PASS and PASS_TO_PASS contribute equally to the functional component. Strict SDD review also persists `code_quality_metrics`, `documentation_quality_metrics`, `quality_findings`, and `quality_gate`. Design checks cover high availability, high concurrency, complete flowcharts, failure/observability/testability evidence, and implementation consistency. Changed Java files are checked against the Alibaba Java Coding Guidelines (P3C); see [quality-evaluation.md](quality-evaluation.md).

## Persistence

V2 uses these application tables:

- `benchmark_instances`
- `evaluation_oracles`
- `predictions`
- `evaluation_results`
- `instance_validations`
- `benchmark_jobs`
- `job_attempts`

Foreign-key cascades remove all dependent benchmark records when an Instance is deleted.

## Import and export

The JSONL importer maps SWE-bench fields to a public Instance and private Oracle while preserving V2 environment, Docker, Requirement IR, constraint, and Oracle extensions. Public export excludes Oracle fields by default. Prediction export includes the standard `instance_id`, `model_name_or_path`, and `model_patch` fields plus the complete V2 identity, hash, artifacts, trace links, and usage metadata.

## Implemented executable-oracle foundation

`LocalEvaluationBackend` checks out the exact base commit, runs trusted setup/build commands, applies the model patch before the hidden test patch, rejects forbidden-path edits, executes every FAIL_TO_PASS and PASS_TO_PASS selector independently, and stores an explicit V2 outcome. It also validates baseline and gold behavior before an instance is accepted.

The local backend is a protocol implementation for trusted development only; it is not a sandbox.

## Docker backend

`DockerEvaluationBackend` grades an already-created Prediction in a dedicated container. It supports cached images, registry pulls, or an administrator-provided build context. The checked-out repository is mounted at `/workspace`; the Oracle itself is never mounted. Setup commands may use the configured setup network, after which the backend disconnects that network before build and grading.

Every result records the image ID, backend version, platform, network policy, read-only setting, and resource limits in `execution_manifest`. The environment digest covers both that manifest and the command contract.

## Job execution

Benchmark evaluation and instance validation can be queued as durable `BenchmarkJob` records. Independent workers atomically claim jobs, maintain a lease, record attempts, recover expired work, apply bounded retries, and persist result identifiers. See [job-worker.md](job-worker.md).

## Dashboard

The V2 dashboard directly represents Instances, Predictions, Jobs, Results, and Validations. Results show each score component and the weighted composite score.
