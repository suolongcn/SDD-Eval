from sdd_eval.models import ArtifactBundle, BenchmarkInstance, EvaluationOracle, Prediction, RequirementIR, TraceLink
from sdd_eval.quality import (assess_quality, check_alibaba_java, check_flowchart,
                              command_quality_metrics, parse_coverage_percent)


def _java_patch(body: str = "return true;") -> str:
    return f"""diff --git a/src/Service.java b/src/Service.java
index 1111111..2222222 100644
--- a/src/Service.java
+++ b/src/Service.java
@@ -1,2 +1,4 @@
 package demo;
+public class Service {{
+    {body}
+}}
"""


def test_flowchart_requires_success_and_failure_branches():
    complete = """```mermaid
flowchart TD
    Start[Request] --> Check{{Validate}}
    Check -->|success| Done[Success]
    Check -->|failure| Error[Error]
```"""
    assert check_flowchart(complete)["status"] == "covered"
    assert check_flowchart("Start -> Done")["status"] == "partial"
    assert check_flowchart("No process described")["status"] == "missing"


def test_alibaba_java_checker_reports_p3c_style_violations():
    report, findings = check_alibaba_java(_java_patch("System.out.println(\"debug\");"))
    assert report["applicable"]
    assert report["standard"].startswith("Alibaba Java")
    assert report["status"] == "failed"
    assert {item["rule"] for item in report["violations"]} >= {"ALI-SYSTEM-OUT"}
    assert any(item.check_id == "alibaba_java" for item in findings)


def test_configured_style_and_coverage_checks_are_scored():
    assert parse_coverage_percent("TOTAL 10 2 80%") == 80
    assert parse_coverage_percent('line-rate="0.875"') == 87.5

    metrics, findings = command_quality_metrics(
        style_returncode=1, style_output="lint failed",
        coverage_returncode=0, coverage_output="COVERAGE: 60%",
        coverage_threshold=80,
    )

    assert metrics["style"]["score"] == 0
    assert metrics["coverage"]["score"] == 75
    assert {finding.check_id for finding in findings} == {"code_style", "test_coverage"}


def test_strict_quality_requires_ha_concurrency_flow_and_consistent_code():
    design = """# Design
REQ-1: expose the service result.

## Availability
High availability uses failover and health checks. SLO 99.99%, RTO 5m, timeout and retry with backoff.

## Concurrency
High concurrency target is 500 QPS. Requests are idempotent, use a bounded queue and backpressure; p99 latency is measured.

## Failure handling and observability
Dependency failure returns an error, retries then fallback/rollback. Metrics, structured logs, traces and alerts are emitted.

## Verification
Acceptance, negative, boundary, load and recovery tests have observable oracles.

## Flowchart
```mermaid
flowchart TD
  Start[Request] --> Check{{Validate}}
  Check -->|success| Done[Success]
  Check -->|failure| Error[Error fallback]
```
The implementation is in `src/Service.java` and verified by `ServiceTest`.
"""
    instance = BenchmarkInstance(
        instance_id="demo__quality-1", repo="demo/repo", base_commit="abc123",
        problem_statement="Expose the service result.",
        requirements=[RequirementIR(id="REQ-1", description="Expose the service result.", acceptance_criteria=["ServiceTest passes."])],
    )
    oracle = EvaluationOracle(instance_id=instance.instance_id)
    prediction = Prediction(
        instance_id=instance.instance_id, model_name_or_path="model", model_patch=_java_patch(),
        artifacts=ArtifactBundle(
            documents={"design.md": design},
            trace_links=[
                TraceLink(source_type="requirement", source_id="REQ-1", target_type="code", target_id="src/Service.java", status="covered"),
                TraceLink(source_type="requirement", source_id="REQ-1", target_type="test", target_id="ServiceTest", status="covered"),
            ],
        ),
    )
    report = assess_quality(prediction, instance, oracle, patch_applied=True, build_passed=True, functional_score=100)
    assert report.documentation_score == 100
    assert report.code_score == 100
    assert report.quality_gate == "pass"
    assert report.documentation_metrics["availability"]["status"] == "covered"
    assert report.documentation_metrics["concurrency"]["status"] == "covered"
    assert report.documentation_metrics["flowchart"]["status"] == "covered"


def test_consistency_and_cross_cutting_findings_lower_strict_score():
    instance = BenchmarkInstance(
        instance_id="demo__quality-2", repo="demo/repo", base_commit="abc123",
        problem_statement="Change the service.",
        requirements=[RequirementIR(id="REQ-1", description="Change the service.")],
    )
    prediction = Prediction(
        instance_id=instance.instance_id, model_name_or_path="model", model_patch=_java_patch(),
        artifacts=ArtifactBundle(documents={"design.md": "REQ-1 changes `src/Other.java`."}),
    )
    report = assess_quality(prediction, instance, EvaluationOracle(instance_id=instance.instance_id), functional_score=100)
    assert report.documentation_score < 100
    assert report.quality_gate == "conditional"
    assert any(item.check_id in {"implementation_consistency", "availability", "concurrency"} for item in report.findings)
