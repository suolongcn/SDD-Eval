# Quality Evaluation Contract

Benchmark V2 scores executable behavior and engineering quality separately. A
quality finding never turns a passing test into a failure, and quality points
never turn a failing implementation into a resolved result. The quality report
is persisted with every `EvaluationResult` so a score can be audited later.

## Design-document checks

When an Instance has requirements, or its private `quality_review` policy opts
into strict checks, the design artifacts are checked for the following
evidence:

| Check | Required evidence |
| --- | --- |
| Requirement traceability | Every requirement ID is present in the specification/design, has a design decision, and links to implementation and verification evidence. `partial`, `missing`, and `contradicted` links remain visible. |
| High availability | A failover, replication, health-check, degradation, or equivalent strategy, plus an SLO/SLA or recovery/timeout target. An explicit N/A rationale is accepted for a genuinely non-distributed component. |
| High concurrency | Capacity or latency bounds, synchronization/idempotency, and a limit, queue, or backpressure strategy. An explicit N/A rationale is accepted when concurrency cannot affect the component. |
| Failure paths | Dependency errors, timeouts, retry/backoff, rollback/compensation, and the externally visible error contract. |
| Observability | Metrics, structured logs, traces, alerts, and the signal used to decide rollout or recovery. |
| Testability | Acceptance, negative, boundary, concurrency/load, and recovery tests with an observable oracle. |
| Flowchart completeness | Mermaid or equivalent flow with an entry, at least one decision/branch, a success path, and a failure/error path. |
| Implementation consistency | Changed production paths and trace-link targets are named by the design; stale or unreferenced paths are findings. |

Each check is `covered`, `partial`, `missing`, or `not_applicable`. N/A is only
valid when the document states the reason. The documentation score is a
weighted score over applicable checks. The default weights are traceability
20%, availability 15%, concurrency 15%, flowchart 15%, failure handling 10%,
observability 10%, testability 10%, and implementation consistency 5%.
Partial evidence earns half credit. Missing or contradicted must-have
requirements earn no credit and raise the quality gate to `fail` or
`conditional` according to severity.

## Alibaba Java check

For every changed `.java` file, the evaluator labels the standard as
`Alibaba Java Coding Guidelines (P3C)`. A configured command can be supplied in
`EvaluationOracle.quality_review.alibaba_command` or the
`SDD_EVAL_ALIBABA_COMMAND` environment variable. The deterministic fallback
also rejects common P3C violations such as wildcard imports, direct
`System.out`/`System.err`, printed stack traces, empty catches, unsafe string
identity comparison, blocking `Thread.sleep`, precision-unsafe
`BigDecimal(double)`, and overlong lines. The report records the rule ID,
path/line evidence, tool status, and score. Non-Java changes are explicitly
marked `not_applicable`, rather than silently claiming a Java pass.

## Repository style and coverage commands

An Oracle can configure language- and repository-specific checks through
`quality_review.style_command` (alias `lint_command`) and
`quality_review.coverage_command`. Commands should preferably be JSON argument
arrays and run in the Instance working directory after the build and executable
tests. `quality_timeout_seconds` defaults to 300 seconds.

The style command receives full credit only when it exits successfully. The
coverage command must also exit successfully and print a recognizable line
coverage summary such as `COVERAGE: 82.5%`, `TOTAL ... 82%`, or a Cobertura
`line-rate` value. `coverage_threshold` defaults to 80%. Coverage below the
threshold receives proportional credit; failed or unparseable output receives
zero for that component. Unconfigured checks are recorded as `not_configured`
and excluded from the code-quality average, so dataset authors must configure
both commands when they are required by a benchmark.

The code-quality score is the average of the built-in patch/P3C score and every
configured command component. Command output, return code, measured percentage,
threshold, and findings are persisted for auditability. A style or coverage
finding affects the quality score and gate but does not change the executable
functional outcome.

## Code/document consistency

Consistency is evaluated against the actual model patch, not only prose. The
checker extracts changed source paths from the unified diff, compares them with
design references and code trace links, and reports stale targets or changed
files that no design artifact identifies. This is a review signal and does not
replace executable tests; both are displayed in `sdd_metrics` and in the
top-level quality fields of `EvaluationResult`.

## Gate and score

The result retains the existing 50% functional, 25% code-quality, and 25%
documentation-quality weights. The quality gate is `pass` when no finding is
present, `conditional` when review findings remain, and `fail` for an
execution error, a blocker, or a contradictory/missing must-have contract.
The functional outcome continues to be determined only by patch application,
the FAIL_TO_PASS family, the PASS_TO_PASS family, and forbidden-path checks.
