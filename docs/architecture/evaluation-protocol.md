# Executable-Oracle Evaluation Protocol

## Result dimensions

Benchmark V2 reports three independent dimensions:

1. **Functional outcome**: whether the issue was resolved without regression.
2. **SDD process quality**: whether requirements remain traceable through specification, design, tasks, code, and tests.
3. **Efficiency**: tokens, duration, attempts, and resource consumption.

The persisted composite score is a 0-100 weighted score:

```text
functional = 50% * FAIL_TO_PASS rate + 50% * PASS_TO_PASS rate
composite = 50% * functional + 25% * code quality + 25% * documentation quality
```

FAIL_TO_PASS and PASS_TO_PASS are therefore equally weighted within the
functional half. Code quality checks patch validity, build success, forbidden
changes, and basic patch hygiene. Documentation quality checks non-empty SDD
documents, expected specification/design/plan naming, and requirement trace
links. A failed build, invalid patch, or environment error forces the functional
score to zero; quality scores cannot turn an unresolved patch into a resolved
outcome.

SDD quality and efficiency must never turn an unresolved patch into a resolved result.

## Intended functional decision

The harness uses the following strict rule:

```text
resolved = patch_applied
           AND all FAIL_TO_PASS tests pass
           AND all PASS_TO_PASS tests pass
           AND no forbidden change is detected
```

Functional outcomes are explicit: `resolved`, `unresolved`, `invalid_patch`, `build_failed`, `target_tests_failed`, `regression`, `agent_timeout`, `environment_error`, or `harness_error`.

## SDD traceability

`RequirementIR` provides stable requirement identifiers. `TraceLink` records evidence-bearing relationships across requirement, specification, design, task, code, and test artifacts. Missing and contradictory links remain visible rather than being hidden by an aggregate score.

The first structural relationships are:

- Requirement to specification
- Requirement to design
- Requirement to task
- Requirement to code
- Requirement to test
- Specification to design
- Design to task
- Task to code

LLM judging, if introduced later, is supplemental. Its model, prompt version, evidence, and confidence must be recorded, and judge failure cannot affect the functional outcome.

## Regrading and identity

A prediction is content-addressed by the SHA-256 hash of its exact UTF-8 model patch. Future evaluation cache keys must include at least:

```text
instance_id + patch_hash + environment_digest + harness_version + oracle_version
```

This prevents a changed patch or environment from incorrectly reusing an earlier result.

## Docker grading sequence

```text
host: clone and checkout base_commit
host: apply exact model patch
host: reject forbidden paths
host: apply private test patch
docker: create resource-limited container
docker: run trusted setup commands
docker: disconnect setup network
docker: build and run each test selector
host: persist result and execution manifest
docker: force-remove container
```

The Docker backend consumes a Prediction; it does not run the model. This keeps generation credentials and the private Oracle out of the same execution context.
