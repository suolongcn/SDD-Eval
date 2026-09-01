# Benchmark V2 Security Boundary

## Trust zones

### Agent-visible zone

The agent may receive `BenchmarkInstance`, repository contents at `base_commit`, public build instructions, and its own generated artifacts. It must not receive oracle fields.

### Evaluator-private zone

`EvaluationOracle` contains gold/test patches, test selectors, forbidden paths, expected results, and quality review metadata. Only administrative import and the future evaluator may access it.

### Public API zone

The HTTP API exposes benchmark instances and predictions. It intentionally provides no route for listing or retrieving evaluation oracles. Public dataset export omits oracle fields unless an administrator explicitly invokes CLI export with `--include-oracle`.

## Data-flow rules

1. Never serialize `EvaluationOracle` into an agent prompt, run request, or public response.
2. Never mount gold patches in an agent-accessible workspace.
3. Apply hidden tests only after generation has completed.
4. Store the submitted model patch before grading.
5. Record patch and environment hashes for auditability.
6. Treat generated repositories and patches as untrusted input.

## Future container requirements

The Docker harness phase must use separate agent and grading steps, disable grading network access, apply CPU/memory/disk/time limits, prevent modification of harness or hidden tests, and remove containers after execution. Secrets must be passed only to the generation process and must not be persisted in logs or images.

## Implemented Docker controls

- Separate Prediction generation and grading processes
- Grading network disconnection after optional setup
- CPU, memory, PID and tmpfs limits plus per-command timeouts
- All Linux capabilities dropped
- `no-new-privileges` security option
- Read-only container root by default
- Only the prepared repository mounted into `/workspace`
- Forced container removal on success and failure
- Image ID and security settings recorded in the execution manifest

Image building is an administrative operation. `build_context` and `dockerfile` must not be supplied by an untrusted Agent. Docker does not by itself make arbitrary host mounts or a privileged daemon safe; deployments must keep Docker socket access away from the public API and workers processing untrusted prompts.

## Known LocalBackend limitation

Oracle records currently share the same SQLite database file but use a separate table and have no HTTP route. Stronger deployments should move oracles to a separate database or encrypted administrative store before exposing SDD Eval to untrusted tenants.

`LocalEvaluationBackend` directly executes commands declared by a benchmark instance. It is restricted to trusted repositories and local development. Untrusted or multi-tenant evaluation must wait for the Docker backend and must never be exposed as an unauthenticated HTTP operation.
