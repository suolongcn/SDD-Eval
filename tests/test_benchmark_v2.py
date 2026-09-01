import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from sdd_eval import api
from sdd_eval.benchmark_io import export_predictions, export_swebench, import_swebench
from sdd_eval.models import (
    BenchmarkInstance,
    EvaluationOracle,
    EvaluationResultV2,
    InstanceValidationResult,
    Prediction,
    RequirementIR,
    TaskSpec,
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


def test_v2_tables_are_additive_and_legacy_tasks_still_work(tmp_path):
    store = Store(str(tmp_path / "v2.db"))
    store.put_task(TaskSpec(id="legacy", title="Legacy task", build_command=""))
    instance = sample_instance()
    store.put_benchmark_instance(instance)

    assert store.get_task("legacy").title == "Legacy task"
    assert store.get_benchmark_instance(instance.instance_id) == instance
    assert store.list_benchmark_instances(dataset_id="demo-verified", split="verified") == [instance]


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
    result = EvaluationResultV2(
        evaluation_id="evaluation-1",
        prediction_id=prediction.prediction_id,
        instance_id=instance.instance_id,
        outcome="unresolved",
        prediction_hash=prediction.patch_hash,
    )
    store.put_benchmark_instance(instance)
    store.put_evaluation_oracle(oracle)
    store.put_prediction(prediction)
    store.put_evaluation_result_v2(result)
    validation = InstanceValidationResult(instance_id=instance.instance_id, valid=True)
    store.put_instance_validation(validation)

    assert store.get_evaluation_oracle(instance.instance_id) == oracle
    assert store.get_prediction(prediction.prediction_id).model_patch == prediction.model_patch
    assert store.get_evaluation_result_v2(result.evaluation_id) == result
    assert store.get_instance_validation(instance.instance_id) == validation
    assert store.list_evaluation_results_v2(instance_id=instance.instance_id) == [result]
    assert prediction.patch_hash == hashlib.sha256(prediction.model_patch.encode()).hexdigest()


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
        EvaluationResultV2(
            prediction_id="prediction-1",
            instance_id="demo__repo-1",
            outcome="resolved",
            resolved=False,
        )
    with pytest.raises(ValueError, match="cannot exceed"):
        EvaluationResultV2(
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
    assert json.loads(output.read_text(encoding="utf-8")) == {
        "instance_id": prediction.instance_id,
        "model_name_or_path": prediction.model_name_or_path,
        "model_patch": prediction.model_patch,
    }


def test_public_api_never_exposes_oracle_routes(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "api.db"))
    instance = sample_instance()
    store.put_benchmark_instance(instance)
    store.put_evaluation_oracle(EvaluationOracle(instance_id=instance.instance_id, gold_patch="secret"))
    monkeypatch.setattr(api, "store", store)
    client = TestClient(api.app)

    response = client.get(f"/api/benchmark-instances/{instance.instance_id}")
    assert response.status_code == 200
    assert "gold_patch" not in response.text
    assert not any("oracle" in path for path in api.app.openapi()["paths"])
