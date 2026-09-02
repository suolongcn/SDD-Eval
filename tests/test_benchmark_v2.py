import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sdd_eval import api
from sdd_eval.benchmark_io import export_predictions, export_swebench, import_swebench
from sdd_eval.models import (
    BenchmarkInstance,
    EvaluationOracle,
    EvaluationResult,
    InstanceValidationResult,
    Prediction,
    RequirementIR,
)
from sdd_eval.storage import Store


def sample_instance() -> BenchmarkInstance:
    return BenchmarkInstance(
        instance_id="demo__repo-1",
        dataset_id="demo-verified",
        dataset_version="2026-09",
        split="verified",
        repo="demo/repo",
        base_commit="abc123",
        problem_statement="Fix the broken behavior.",
        requirements=[RequirementIR(id="REQ-1", description="The behavior must work.")],
    )


def test_v2_store_drops_a_pre_v2_schema_instead_of_migrating_history(tmp_path):
    path = tmp_path / "v2.db"
    with sqlite3.connect(path) as connection:
        connection.execute("create table tasks (id text primary key, data text)")
        connection.execute("insert into tasks values ('legacy', '{}')")
    store = Store(str(path))
    instance = sample_instance()
    store.put_benchmark_instance(instance)

    assert store.get_benchmark_instance(instance.instance_id) == instance
    assert store.list_benchmark_instances(dataset_id="demo-verified", split="verified") == [instance]
    with store.conn() as connection:
        tables = {row[0] for row in connection.execute("select name from sqlite_master where type='table'")}
    assert "tasks" not in tables
    assert "evaluation_results" in tables


def test_oracle_prediction_and_v2_result_are_persisted_separately(tmp_path):
    store = Store(str(tmp_path / "v2.db"))
    instance = sample_instance()
    oracle = EvaluationOracle(
        instance_id=instance.instance_id,
        gold_patch="gold",
        test_patch="tests",
        fail_to_pass=["test_bug"],
        pass_to_pass=["test_existing"],
    )
    prediction = Prediction(
        prediction_id="prediction-1",
        instance_id=instance.instance_id,
        model_name_or_path="demo-model",
        model_patch="diff --git a/a.py b/a.py\n",
    )
    result = EvaluationResult(
        evaluation_id="evaluation-1",
        prediction_id=prediction.prediction_id,
        instance_id=instance.instance_id,
        outcome="unresolved",
        prediction_hash=prediction.patch_hash,
    )
    store.put_benchmark_instance(instance, oracle)
    store.put_prediction(prediction)
    store.put_evaluation_result(result)
    validation = InstanceValidationResult(instance_id=instance.instance_id, valid=True)
    store.put_instance_validation(validation)

    assert store.get_evaluation_oracle(instance.instance_id) == oracle
    assert store.get_prediction(prediction.prediction_id).model_patch == prediction.model_patch
    assert store.get_evaluation_result(result.evaluation_id) == result
    assert store.get_instance_validation(instance.instance_id) == validation
    assert store.list_evaluation_results(instance_id=instance.instance_id) == [result]
    assert prediction.patch_hash == hashlib.sha256(prediction.model_patch.encode()).hexdigest()


def test_official_pr_line_count_is_derived_from_imported_patch(tmp_path):
    source = tmp_path / "source.jsonl"
    source.write_text(json.dumps({
        "instance_id": "demo__repo-1",
        "repo": "demo/repo",
        "base_commit": "abc123",
        "problem_statement": "Fix the broken behavior.",
        "patch": "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,3 @@\n-old()\n+new()\n+extra()\n",
    }) + "\n", encoding="utf-8")

    [(instance, _)] = import_swebench(source, dataset_id="demo", split="verified")

    assert instance.reference_code_lines == 3
    assert instance.reference_code_estimated is False


def test_official_pr_line_count_accepts_reference_stats_and_url_aliases(tmp_path):
    source = tmp_path / "source.jsonl"
    source.write_text(json.dumps({
        "instance_id": "demo__repo-2",
        "repo": "demo/repo",
        "base_commit": "abc123",
        "problem_statement": "Fix the broken behavior.",
        "reference_pr_url": "https://github.com/demo/repo/pull/2",
        "reference_additions": 4,
        "reference_deletions": 3,
    }) + "\n", encoding="utf-8")

    [(instance, _)] = import_swebench(source, dataset_id="demo", split="verified")

    assert instance.reference_code_lines == 7
    assert instance.source_pr_url == "https://github.com/demo/repo/pull/2"


def test_store_backfills_official_pr_line_count_from_private_oracle(tmp_path):
    store = Store(str(tmp_path / "line-count.db"))
    instance = sample_instance()
    oracle = EvaluationOracle(
        instance_id=instance.instance_id,
        gold_patch="diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1,2 @@\n-old()\n+new()\n",
    )
    store.put_benchmark_instance(instance)
    store.put_evaluation_oracle(oracle)

    assert store.get_benchmark_instance(instance.instance_id).reference_code_lines == 2


def test_private_oracle_replaces_an_estimated_line_count(tmp_path):
    store = Store(str(tmp_path / "line-count-estimate.db"))
    instance = sample_instance()
    instance.reference_code_lines = 99
    instance.reference_code_estimated = True
    oracle = EvaluationOracle(
        instance_id=instance.instance_id,
        gold_patch="diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1,2 @@\n-old()\n+new()\n",
    )

    store.put_benchmark_instance(instance, oracle)

    restored = store.get_benchmark_instance(instance.instance_id)
    assert restored.reference_code_lines == 2
    assert restored.reference_code_estimated is False


def test_legacy_resolved_result_gets_a_derived_effectiveness_score(tmp_path):
    store = Store(str(tmp_path / "score.db")); instance = sample_instance()
    prediction = Prediction(instance_id=instance.instance_id, model_name_or_path="model", model_patch="patch")
    store.put_benchmark_instance(instance); store.put_prediction(prediction)
    result = EvaluationResult(prediction_id=prediction.prediction_id, instance_id=instance.instance_id,
                              outcome="resolved", resolved=True, fail_to_pass_total=1,
                              fail_to_pass_passed=1, pass_to_pass_total=1, pass_to_pass_passed=1)
    payload = result.model_dump(mode="json"); payload.pop("score")
    with store.conn() as connection:
        connection.execute("insert into evaluation_results values (?, ?, ?, ?, ?, ?)",
                           (result.evaluation_id, prediction.prediction_id, instance.instance_id,
                            result.outcome, json.dumps(payload), result.created_at.isoformat()))

    restored = store.get_evaluation_result(result.evaluation_id)
    assert restored.score == 100
    assert restored.functional_score == 100
    assert restored.code_quality_score == 100
    assert restored.documentation_score == 100


def test_updating_an_instance_does_not_delete_predictions_or_results(tmp_path):
    store = Store(str(tmp_path / "v2.db")); instance = sample_instance()
    oracle = EvaluationOracle(instance_id=instance.instance_id)
    prediction = Prediction(prediction_id="prediction-stable", instance_id=instance.instance_id,
                            model_name_or_path="model", model_patch="patch")
    result = EvaluationResult(prediction_id=prediction.prediction_id, instance_id=instance.instance_id, outcome="unresolved")
    store.put_benchmark_instance(instance, oracle); store.put_prediction(prediction); store.put_evaluation_result(result)
    instance.problem_statement = "Updated public description"
    store.put_benchmark_instance(instance)
    assert store.get_prediction(prediction.prediction_id) == prediction
    assert store.get_evaluation_result(result.evaluation_id) == result
    assert store.get_evaluation_oracle(instance.instance_id) == oracle


def test_prediction_rejects_a_mismatched_patch_hash():
    with pytest.raises(ValueError, match="patch_hash"):
        Prediction(
            instance_id="demo__repo-1",
            model_name_or_path="demo-model",
            model_patch="patch",
            patch_hash="wrong",
        )


def test_v2_result_rejects_inconsistent_resolution_and_test_counts():
    with pytest.raises(ValueError, match="resolved must match"):
        EvaluationResult(
            prediction_id="prediction-1",
            instance_id="demo__repo-1",
            outcome="resolved",
            resolved=False,
        )
    with pytest.raises(ValueError, match="cannot exceed"):
        EvaluationResult(
            prediction_id="prediction-1",
            instance_id="demo__repo-1",
            outcome="unresolved",
            fail_to_pass_total=1,
            fail_to_pass_passed=2,
        )


def test_swebench_jsonl_round_trip_keeps_oracle_private_by_default(tmp_path):
    source = tmp_path / "source.jsonl"
    source.write_text(json.dumps({
        "instance_id": "demo__repo-1",
        "repo": "demo/repo",
        "base_commit": "abc123",
        "problem_statement": "Fix the broken behavior.",
        "patch": "gold patch",
        "test_patch": "hidden tests",
        "FAIL_TO_PASS": '["test_bug"]',
        "PASS_TO_PASS": '["test_existing"]',
        "issue_url": "https://github.com/demo/repo/issues/1",
    }) + "\n", encoding="utf-8")
    [(instance, oracle)] = import_swebench(source, dataset_id="demo", split="verified")

    public_output = tmp_path / "public.jsonl"
    export_swebench(public_output, [instance])
    public_record = json.loads(public_output.read_text(encoding="utf-8"))
    assert "patch" not in public_record and "test_patch" not in public_record

    private_output = tmp_path / "private.jsonl"
    export_swebench(private_output, [instance], oracles={instance.instance_id: oracle}, include_oracle=True)
    private_record = json.loads(private_output.read_text(encoding="utf-8"))
    assert private_record["patch"] == "gold patch"
    assert json.loads(private_record["FAIL_TO_PASS"]) == ["test_bug"]


def test_prediction_export_uses_swebench_shape(tmp_path):
    prediction = Prediction(
        instance_id="demo__repo-1",
        model_name_or_path="demo-model",
        model_patch="diff --git a/a.py b/a.py\n",
    )
    output = tmp_path / "predictions.jsonl"
    export_predictions(output, [prediction])
    exported = json.loads(output.read_text(encoding="utf-8"))
    assert exported["instance_id"] == prediction.instance_id
    assert exported["model_name_or_path"] == prediction.model_name_or_path
    assert exported["model_patch"] == prediction.model_patch
    assert exported["patch_hash"] == prediction.patch_hash
    assert exported["artifacts"] == {"documents": {}, "trace_links": [], "logs": {}}


def test_v2_dataset_extensions_round_trip(tmp_path):
    instance = sample_instance(); instance.constraints = ["No network"]
    instance.environment.test_command = ["pytest", "{tests}"]
    instance.docker.image = "demo:latest"
    oracle = EvaluationOracle(instance_id=instance.instance_id, gold_patch="gold", forbidden_paths=["tests/**"],
                              expected_results={"target": "pass"})
    output = tmp_path / "v2.jsonl"
    export_swebench(output, [instance], oracles={instance.instance_id: oracle}, include_oracle=True)
    [(restored_instance, restored_oracle)] = import_swebench(output, dataset_id=instance.dataset_id,
                                                              dataset_version=instance.dataset_version, split=instance.split)
    assert restored_instance.environment == instance.environment
    assert restored_instance.docker == instance.docker
    assert restored_instance.requirements == instance.requirements
    assert restored_instance.constraints == instance.constraints
    assert restored_oracle.forbidden_paths == oracle.forbidden_paths
    assert restored_oracle.expected_results == oracle.expected_results


def test_public_api_never_exposes_oracle_routes(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "api.db"))
    instance = sample_instance()
    store.put_benchmark_instance(instance)
    store.put_evaluation_oracle(EvaluationOracle(instance_id=instance.instance_id, gold_patch="secret"))
    monkeypatch.setattr(api, "store", store)
    client = TestClient(api.app)

    response = client.get(f"/api/instances/{instance.instance_id}")
    assert response.status_code == 200
    assert "gold_patch" not in response.text
    assert not any("oracle" in path for path in api.app.openapi()["paths"])
    assert client.get("/api/tasks").status_code == 404
    assert client.get("/api/runs").status_code == 404


def test_dashboard_only_contains_v2_navigation(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "store", Store(str(tmp_path / "dashboard.db")))
    response = TestClient(api.app).get("/")
    assert response.status_code == 200
    for label in ("Instances", "Predictions", "Jobs", "Results", "Validations"):
        assert label in response.text
    assert "Single Task" not in response.text
    assert "Runs History" not in response.text


def test_dashboard_results_render_composite_score_dimensions():
    html = Path(__file__).parents[1].joinpath("sdd_eval", "dashboard.html").read_text(encoding="utf-8")
    for label in ("Functional 50%", "Code 25%", "Docs 25%", "Composite"):
        assert label in html


def test_instances_tab_renders_official_pr_line_count():
    html = Path(__file__).parents[1].joinpath("sdd_eval", "dashboard.html").read_text(encoding="utf-8")
    assert "Official PR Lines" in html
    assert "reference_code_lines" in html
    assert "Issue / PR" in html
    assert "source_issue_url" in html
    assert "source_pr_url" in html
    assert 'rel="noopener noreferrer"' in html
